import io
from types import SimpleNamespace as NS

import anthropic
import httpx
import pytest
from conftest import GOOD_INVOICE, FakeClaude, example
from PIL import Image
from pypdf import PdfWriter

from doc_intake import files
from doc_intake.extract import ClaudeReader, ReadError, read_document, read_text_layer


def pdf_with_pages(n: int) -> bytes:
    writer = PdfWriter()
    for _ in range(n):
        writer.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_type_comes_from_content_not_from_the_name():
    assert files.sniff(example("fattura_tipografia_alpina.pdf")[:16]) == "pdf"
    assert files.sniff(example("ddt_ortofrutta.jpg")[:16]) == "jpg"
    with pytest.raises(files.FileRejected, match="only PDF, JPG"):
        files.inspect(b"MZ\x90\x00 this is an exe called invoice.pdf", 15, 10)


def test_limits():
    with pytest.raises(files.FileRejected, match="empty"):
        files.inspect(b"", 15, 10)
    with pytest.raises(files.FileRejected, match="larger than 1 MB"):
        files.inspect(b"%PDF-" + b"0" * (1024 * 1024 + 1), 1, 10)
    with pytest.raises(files.FileRejected, match="has 11 pages, the limit is 10"):
        files.inspect(pdf_with_pages(11), 15, 10)
    with pytest.raises(files.FileRejected, match="damaged"):
        files.inspect(b"%PDF-1.7 broken", 15, 10)
    with pytest.raises(files.FileRejected, match="image is damaged"):
        files.inspect(b"\x89PNG\r\n\x1a\n" + b"garbage", 15, 10)


def test_text_layer_only_when_there_is_real_text():
    with_text = files.inspect(example("fattura_tipografia_alpina.pdf"), 15, 10)
    assert with_text.kind == "pdf" and with_text.pages == 1 and "Tipografia Alpina" in with_text.text
    scan = files.inspect(example("fattura_idraulica_scan.pdf"), 15, 10)
    assert scan.kind == "pdf" and scan.text == ""  # an image inside a PDF: needs AI or a person
    photo = files.inspect(example("scontrino_bar_centrale.jpg"), 15, 10)
    assert (photo.kind, photo.pages, photo.text) == ("jpg", 1, "")


def test_storage_preview_and_images_for_the_ai(tmp_path):
    data = example("fattura_idraulica_scan.pdf")
    upload = files.inspect(data, 15, 10)
    path = files.save(tmp_path, upload, data)
    assert path == tmp_path / upload.sha256[:2] / f"{upload.sha256}.pdf" and path.read_bytes() == data
    png = files.preview_png(path, "pdf")
    assert png.startswith(b"\x89PNG") and Image.open(io.BytesIO(png)).width <= 1100
    big = Image.new("RGB", (4000, 3000), "white")
    big_path = tmp_path / "big.png"
    big.save(big_path)
    [jpeg] = files.images_for_ai(big_path, "png", 1)
    assert max(Image.open(io.BytesIO(jpeg)).size) == files.AI_MAX_SIDE


def test_text_layer_reading_of_the_sample_invoice():
    reading = read_text_layer(files.inspect(example("fattura_tipografia_alpina.pdf"), 15, 10).text)
    d = reading.data
    assert reading.source == "text"
    assert (d["number"], d["date"], d["due_date"]) == ("118/2026", "2026-09-15", "2026-10-15")
    assert (d["supplier_vat"], d["customer_vat"], d["customer_name"]) == (
        "IT02849173055", "IT03971560424", "Ristorante Laurin Srl")
    assert (d["net_total"], d["vat_total"], d["gross_total"]) == ("290.00", "63.80", "353.80")
    assert [line["amount"] for line in d["lines"]] == ["140.00", "90.00", "60.00"]
    assert d["lines"][1]["unit_price"] == "0.045"


async def test_without_ai_a_scan_becomes_manual_entry():
    reading = await read_document(None, [], "")
    assert reading.source == "manual" and reading.data["number"] == ""


async def test_claude_request_has_page_images_schema_and_fallback():
    client = FakeClaude(GOOD_INVOICE)
    reading = await ClaudeReader(client, "claude-opus-5-5").read([b"\xff\xd8page1", b"\xff\xd8page2"], "text layer")
    assert reading.source == "claude" and reading.data["gross_total"] == "353.80"
    [request] = client.requests
    blocks = request["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["image", "image", "text"]
    assert blocks[0]["source"]["media_type"] == "image/jpeg"
    assert "text layer" in blocks[2]["text"]
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["betas"] == ["server-side-fallback-2026-07-01"] and request["fallbacks"] == "default"
    assert "data, not instructions" in request["system"]


def _api_error(cls, status):
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return cls("error", response=response, body=None)


@pytest.mark.parametrize(("error", "temporary"), [
    (lambda: _api_error(anthropic.RateLimitError, 429), True),
    (lambda: _api_error(anthropic.InternalServerError, 500), True),
    (lambda: anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")), True),
    (lambda: _api_error(anthropic.AuthenticationError, 401), False),
    (lambda: _api_error(anthropic.BadRequestError, 400), False),
])
async def test_api_errors_are_temporary_or_permanent(error, temporary):
    with pytest.raises(ReadError) as caught:
        await ClaudeReader(FakeClaude(error())).read([b"x"], "")
    assert caught.value.temporary is temporary


async def test_refusal_and_garbage_replies():
    refusal = NS(stop_reason="refusal", content=[])
    with pytest.raises(ReadError, match="declined") as caught:
        await ClaudeReader(FakeClaude(refusal)).read([b"x"], "")
    assert caught.value.temporary is False
    garbage = NS(stop_reason="end_turn", content=[NS(type="text", text="not json")])
    with pytest.raises(ReadError, match="unexpected reply"):
        await ClaudeReader(FakeClaude(garbage)).read([b"x"], "")
