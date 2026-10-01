import os

import anthropic
import httpx
import pytest
from conftest import GOOD_INVOICE, NOW, FakeClaude, add_file, example

from doc_intake import files
from doc_intake.extract import ClaudeReader
from doc_intake.store import LEASE, Conflict
from doc_intake.worker import RETRY_BASE, Worker, ensure_dirs

SCAN_WITH_WRONG_VAT = {**GOOD_INVOICE, "number": "37", "supplier_name": "Idraulica Rossi Snc",
                       "supplier_vat": "IT02188744607", "net_total": "215.00", "vat_total": "30.00",
                       "gross_total": "245.00",
                       "lines": [{"description": "Riparazione caldaia", "quantity": "1", "unit_price": "180.00",
                                  "vat_rate": "22", "amount": "180.00"},
                                 {"description": "Valvola", "quantity": "1", "unit_price": "35.00", "vat_rate": "22",
                                  "amount": "35.00"}]}


def worker(store, settings, reader=None, now=NOW):
    clock = [now]
    w = Worker(store, settings, reader, clock=lambda: clock[0])
    w._last_inbox = now  # tests call the inbox scan explicitly
    return w, clock


async def test_same_file_twice_is_one_document(store, settings):
    first = await add_file(store, settings, "fattura_tipografia_alpina.pdf")
    upload = files.inspect(example("fattura_tipografia_alpina.pdf"), 15, 10)
    assert await store.add_document(upload, "copy.pdf", None, NOW) == (first, False)


async def test_lease_returns_the_document_if_the_worker_dies(store, settings):
    doc_id = await add_file(store, settings, "fattura_tipografia_alpina.pdf")
    taken = await store.claim(NOW)
    assert taken.id == doc_id and taken.status == "reading" and taken.attempts == 1
    assert await store.claim(NOW + 10) is None  # leased: nobody else takes it
    again = await store.claim(NOW + LEASE + 1)  # the first worker died
    assert again.id == doc_id and again.attempts == 2


async def test_pdf_with_text_layer_is_read_without_ai(store, settings):
    doc_id = await add_file(store, settings, "fattura_tipografia_alpina.pdf")
    w, _ = worker(store, settings)
    assert await w.tick() is True
    doc = await store.get(doc_id)
    assert (doc.status, doc.source, doc.errors, doc.warnings) == ("review", "text", 0, 0)
    assert doc.data["gross_total"] == "353.80"


async def test_scan_without_ai_waits_for_manual_entry(store, settings):
    doc_id = await add_file(store, settings, "fattura_idraulica_scan.pdf")
    w, _ = worker(store, settings)
    await w.tick()
    doc = await store.get(doc_id)
    assert doc.status == "review" and doc.source == "manual" and "enter the data by hand" in doc.note
    assert doc.errors == 2  # date and total are missing until a person types them


async def test_claude_reading_and_the_checks_catch_wrong_vat(store, settings):
    doc_id = await add_file(store, settings, "fattura_idraulica_scan.pdf")
    client = FakeClaude(SCAN_WITH_WRONG_VAT)
    w, _ = worker(store, settings, ClaudeReader(client))
    await w.tick()
    doc = await store.get(doc_id)
    assert doc.source == "claude" and doc.status == "review"
    assert [i["message"] for i in doc.issues] == ["VAT from the lines is 47.30, the document says 30.00"]
    blocks = client.requests[0]["messages"][0]["content"]
    assert blocks[0]["type"] == "image"  # the scanned page went to the AI as an image
    assert await store.ai_used("2026-09-21") == 1


async def test_temporary_api_error_retries_later(store, settings):
    doc_id = await add_file(store, settings, "scontrino_bar_centrale.jpg")
    busy = anthropic.RateLimitError("slow down", body=None, response=httpx.Response(
        429, request=httpx.Request("POST", "https://api.anthropic.com")))
    w, clock = worker(store, settings, ClaudeReader(FakeClaude(busy, GOOD_INVOICE)))
    await w.tick()
    doc = await store.get(doc_id)
    assert doc.status == "queued" and doc.last_error == "Anthropic rate limit"
    assert await w.tick() is False  # not before the pause
    clock[0] += RETRY_BASE + 1
    assert await w.tick() is True
    assert (await store.get(doc_id)).source == "claude"


async def test_permanent_api_error_falls_back_to_text_layer(store, settings):
    doc_id = await add_file(store, settings, "fattura_tipografia_alpina.pdf")
    bad_key = anthropic.AuthenticationError("bad key", body=None, response=httpx.Response(
        401, request=httpx.Request("POST", "https://api.anthropic.com")))
    w, _ = worker(store, settings, ClaudeReader(FakeClaude(bad_key)))
    await w.tick()
    doc = await store.get(doc_id)
    assert doc.status == "review" and doc.source == "text"
    assert doc.note.startswith("AI reading failed (ANTHROPIC_API_KEY is invalid)")


async def test_daily_ai_limit_protects_the_bill(store, settings):
    from dataclasses import replace

    settings = replace(settings, ai_daily_limit=1)
    first = await add_file(store, settings, "scontrino_bar_centrale.jpg")
    second = await add_file(store, settings, "fattura_tipografia_alpina.pdf")
    client = FakeClaude(GOOD_INVOICE)
    w, _ = worker(store, settings, ClaudeReader(client))
    await w.tick()
    await w.tick()
    assert len(client.requests) == 1
    assert (await store.get(first)).source == "claude"
    late = await store.get(second)
    assert late.source == "text" and "daily AI limit (1) reached" in late.note


async def test_same_supplier_and_number_is_flagged_as_duplicate(store, settings):
    original = await add_file(store, settings, "fattura_tipografia_alpina.pdf")
    photo = await add_file(store, settings, "fattura_tipografia_foto.jpg")
    w, _ = worker(store, settings, ClaudeReader(FakeClaude(GOOD_INVOICE, GOOD_INVOICE)))
    await w.tick()
    await w.tick()
    doc = await store.get(photo)
    assert doc.duplicate_of == original
    assert doc.issues[0]["message"] == f"same supplier and number as document #{original}: a duplicate?"


async def test_inbox_folder(store, settings):
    ensure_dirs(settings)
    (settings.inbox_dir / "fattura.pdf").write_bytes(example("fattura_tipografia_alpina.pdf"))
    (settings.inbox_dir / "virus.pdf").write_bytes(b"MZ not a pdf")
    for path in settings.inbox_dir.iterdir():
        os.utime(path, (NOW - 60, NOW - 60))
    w, _ = worker(store, settings)
    assert await w.scan_inbox() == 1
    assert (settings.inbox_dir / "done" / "fattura.pdf").exists()
    assert "only PDF, JPG" in (settings.inbox_dir / "rejected" / "virus.pdf.txt").read_text()
    assert (await store.counts())["queued"] == 1


async def test_review_log_and_decisions(store, settings):
    doc_id = await add_file(store, settings, "fattura_tipografia_alpina.pdf")
    w, _ = worker(store, settings)
    await w.tick()
    user = await store.add_user("anna@studio.it", "Anna", "x", "reviewer", NOW)
    fixed = {**(await store.get(doc_id)).data, "number": "118/2026-B"}
    assert await store.save_review(doc_id, user, fixed, [], NOW) == 1
    [edit] = await store.edits(doc_id)
    assert (edit["field"], edit["old_value"], edit["new_value"], edit["user_name"]) == (
        "number", '"118/2026"', '"118/2026-B"', "Anna")
    await store.decide(doc_id, user, True, "", NOW)
    with pytest.raises(Conflict):
        await store.save_review(doc_id, user, fixed, [], NOW)  # approved documents are final
    with pytest.raises(Conflict):
        await store.requeue(doc_id, NOW)
