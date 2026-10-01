"""Uploaded files: type by content, limits, storage, PDF text layer, previews and images for the AI.

The type comes from the first bytes, never from the file name: a renamed .exe is rejected even if
it is called invoice.pdf. Files are stored by SHA-256 outside the web folder and are only served
to logged-in users through the app.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageOps
from pypdf import PdfReader
from pypdf.errors import PdfReadError

Image.MAX_IMAGE_PIXELS = 60_000_000  # a "decompression bomb" image is refused instead of eating the RAM

KINDS = {"pdf": "application/pdf", "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
AI_MAX_SIDE = 2576  # the model's high-resolution limit: larger images are downscaled anyway
TEXT_LAYER_MIN_CHARS = 80  # fewer characters = a scan inside a PDF


class FileRejected(ValueError):
    pass


@dataclass(frozen=True)
class Upload:
    sha256: str
    kind: str  # pdf | jpg | png | webp
    size: int
    pages: int
    text: str  # PDF text layer ("" for images and scans)


def sniff(head: bytes) -> str | None:
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def pdf_text(data: bytes, max_pages: int) -> tuple[int, str]:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise FileRejected("the PDF is password-protected")
        pages = len(reader.pages)
        if pages == 0:
            raise FileRejected("the PDF has no pages")
        if pages > max_pages:
            raise FileRejected(f"the PDF has {pages} pages, the limit is {max_pages}")
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, FileRejected):
            raise
        raise FileRejected("the PDF is damaged and cannot be read") from None
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return pages, text if len(text) >= TEXT_LAYER_MIN_CHARS else ""


def inspect(data: bytes, max_mb: int, max_pages: int) -> Upload:
    if not data:
        raise FileRejected("the file is empty")
    if len(data) > max_mb * 1024 * 1024:
        raise FileRejected(f"the file is larger than {max_mb} MB")
    kind = sniff(data[:16])
    if kind is None:
        raise FileRejected("only PDF, JPG, PNG and WEBP files are accepted")
    if kind == "pdf":
        pages, text = pdf_text(data, max_pages)
    else:
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.verify()
        except (OSError, Image.DecompressionBombError, SyntaxError):
            raise FileRejected("the image is damaged or too large") from None
        pages, text = 1, ""
    return Upload(hashlib.sha256(data).hexdigest(), kind, len(data), pages, text)


def path_for(storage: Path, sha256: str, kind: str) -> Path:
    return storage / sha256[:2] / f"{sha256}.{kind}"


def save(storage: Path, upload: Upload, data: bytes) -> Path:
    target = path_for(storage, upload.sha256, upload.kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(target)
    return target


def _page_image(path: Path, kind: str, page: int = 0, scale: float = 2.0) -> Image.Image:
    if kind == "pdf":
        pdf = pdfium.PdfDocument(str(path))
        try:
            return pdf[min(page, len(pdf) - 1)].render(scale=scale).to_pil().convert("RGB")
        finally:
            pdf.close()
    with Image.open(path) as img:
        return ImageOps.exif_transpose(img).convert("RGB")  # phone photos: honour the rotation flag


def preview_png(path: Path, kind: str, page: int = 0, width: int = 1100) -> bytes:
    img = _page_image(path, kind, page, scale=1.6)
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def images_for_ai(path: Path, kind: str, pages: int) -> list[bytes]:
    """JPEG pages no larger than the model's limit (smaller requests, same reading quality)."""
    result = []
    for page in range(pages if kind == "pdf" else 1):
        img = _page_image(path, kind, page, scale=2.5)
        if max(img.size) > AI_MAX_SIDE:
            ratio = AI_MAX_SIDE / max(img.size)
            img = img.resize((round(img.width * ratio), round(img.height * ratio)), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=88)
        result.append(out.getvalue())
    return result
