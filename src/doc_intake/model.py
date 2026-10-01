"""What a document becomes: header fields + lines, all values as strings.

Strings on purpose: money stays exact (parsed with Decimal only when checked or exported), an empty
string means "not on the document", and the same shape travels through Claude's structured output,
the database (JSON), the review form and the Excel export without conversions.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

DOC_TYPES = {"invoice": "Invoice", "receipt": "Receipt", "delivery_note": "Delivery note", "other": "Other"}

HEADER_FIELDS: dict[str, str] = {
    "doc_type": "Type",
    "number": "Number",
    "date": "Date",
    "due_date": "Due date",
    "supplier_name": "Supplier",
    "supplier_vat": "Supplier VAT no.",
    "customer_name": "Customer",
    "customer_vat": "Customer VAT / tax code",
    "currency": "Currency",
    "net_total": "Net total",
    "vat_total": "VAT",
    "gross_total": "Total",
}
LINE_FIELDS: dict[str, str] = {
    "description": "Description",
    "quantity": "Qty",
    "unit_price": "Unit price",
    "vat_rate": "VAT %",
    "amount": "Net amount",
}
MONEY_FIELDS = {"net_total", "vat_total", "gross_total"}
MAX_LINES = 200

_str = {"type": "string"}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [*HEADER_FIELDS, "lines", "unreadable_fields"],
    "properties": {
        **{name: dict(_str) for name in HEADER_FIELDS},
        "doc_type": {"type": "string", "enum": list(DOC_TYPES)},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(LINE_FIELDS),
                "properties": {name: dict(_str) for name in LINE_FIELDS},
            },
        },
        "unreadable_fields": {"type": "array", "items": {"type": "string"}},
    },
}


def empty() -> dict[str, Any]:
    return {**{name: "" for name in HEADER_FIELDS}, "doc_type": "other", "lines": [], "unreadable_fields": []}


_KEEP = re.compile(r"[^0-9,.\-]")


def to_decimal(value: Any) -> Decimal | None:
    """'1.234,50', '1,234.50', '€ 1234.5', '-14,04' -> Decimal; '' or garbage -> None."""
    if value is None:
        return None
    text = _KEEP.sub("", str(value).replace("−", "-"))
    if not text or text in {"-", ".", ","}:
        return None
    if "," in text and "." in text:  # both: the last one is the decimal separator
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal_sep == "," else ","
        text = text.replace(thousands, "").replace(decimal_sep, ".")
    elif text.count(",") == 1:  # European documents: one comma is the decimal comma ("1,5" kg, "12,50" €)
        text = text.replace(",", ".")
    elif text.count(",") > 1:  # "1,234,567"
        text = text.replace(",", "")
    elif text.count(".") > 1:  # "1.234.567"
        text = text.replace(".", "")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_date(value: str) -> date | None:
    for pattern in (r"(\d{4})-(\d{2})-(\d{2})", r"(\d{2})[/.](\d{2})[/.](\d{4})"):
        m = re.fullmatch(pattern, value.strip())
        if m:
            parts = [int(x) for x in m.groups()]
            y, mo, d = parts if len(m.group(1)) == 4 else (parts[2], parts[1], parts[0])
            try:
                return date(y, mo, d)
            except ValueError:
                return None
    return None


def money(value: Decimal | None) -> str:
    return "" if value is None else f"{value.quantize(Decimal('0.01'))}"


def plain(value: Decimal | None) -> str:
    """Quantities and prices keep their precision: 0.045 stays 0.045, 3.50 becomes 3.5."""
    if value is None:
        return ""
    text = f"{value.normalize():f}"
    return text if text != "-0" else "0"


def _normal(text: str, fmt) -> str:
    number = to_decimal(text) if re.fullmatch(r"[\s€$£A-Za-z]*-?[\d.,\s]+[\s€$£A-Za-z%]*", text or "") else None
    return fmt(number) if number is not None else text


def clean(raw: Any) -> dict[str, Any]:
    """Whatever came from the model or a form -> the canonical shape (unknown keys dropped, strings trimmed)."""
    data = empty()
    if not isinstance(raw, dict):
        return data
    for name in HEADER_FIELDS:
        value = raw.get(name, "")
        data[name] = str(value).strip()[:300] if value is not None else ""
    if data["doc_type"] not in DOC_TYPES:
        data["doc_type"] = "other"
    data["currency"] = data["currency"].upper()[:3]
    for name in ("supplier_vat", "customer_vat"):
        data[name] = re.sub(r"[\s.\-]", "", data[name]).upper()
    for name in ("date", "due_date"):  # "22/09/2026" typed by a person -> "2026-09-22"; nonsense is kept for the checks
        parsed = parse_date(data[name]) if data[name] else None
        if parsed:
            data[name] = parsed.isoformat()
    for name in MONEY_FIELDS:  # "1.234,50" typed by a person -> "1234.50"; unparseable text is kept for the checks
        data[name] = _normal(data[name], money)
    lines = raw.get("lines") or []
    if isinstance(lines, list):
        for line in lines[:MAX_LINES]:
            if isinstance(line, dict):
                row = {name: str(line.get(name, "") or "").strip()[:300] for name in LINE_FIELDS}
                if any(row.values()):
                    row["amount"] = _normal(row["amount"], money)
                    for name in ("quantity", "unit_price", "vat_rate"):
                        row[name] = _normal(row[name], plain)
                    data["lines"].append(row)
    unsure = raw.get("unreadable_fields") or []
    if isinstance(unsure, list):
        data["unreadable_fields"] = [str(x) for x in unsure if str(x) in HEADER_FIELDS or str(x) == "lines"][:20]
    return data
