"""Rules that catch what reading (by a person or by AI) gets wrong.

Every rule returns a plain message; the reviewer sees errors (must be fixed or consciously approved)
and warnings (worth a look). The AI never decides alone: these checks run on its output too.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .model import HEADER_FIELDS, parse_date, to_decimal

__all__ = ["Issue", "check_vat_number", "italian_vat_ok", "parse_date", "run", "counts"]

TOLERANCE = Decimal("0.02")  # rounding on documents: up to 2 cents per total
VAT_RATES_IT = {Decimal(x) for x in ("0", "4", "5", "10", "22")}


@dataclass(frozen=True)
class Issue:
    level: str  # "error" | "warning"
    field: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def italian_vat_ok(digits: str) -> bool:
    """Partita IVA check digit (11 digits, Luhn-like algorithm of the Agenzia delle Entrate)."""
    if not re.fullmatch(r"\d{11}", digits) or digits == "0" * 11:
        return False
    total = 0
    for i, ch in enumerate(digits[:10]):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(digits[10])


def check_vat_number(value: str) -> str | None:
    """None if fine (or empty), else the problem."""
    if not value:
        return None
    if value.startswith("IT") or re.fullmatch(r"\d{11}", value):
        digits = value[2:] if value.startswith("IT") else value
        if not italian_vat_ok(digits):
            return f"{value}: not a valid Italian VAT number (check digit)"
        return None
    if re.fullmatch(r"[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]", value):  # codice fiscale of a person
        return None
    if not re.fullmatch(r"[A-Z]{2}[0-9A-Z]{2,13}", value):
        return f"{value}: does not look like a VAT number"
    return None


def run(data: dict[str, Any], today: date | None = None) -> list[Issue]:
    today = today or date.today()
    issues: list[Issue] = []

    def err(field: str, message: str) -> None:
        issues.append(Issue("error", field, message))

    def warn(field: str, message: str) -> None:
        issues.append(Issue("warning", field, message))

    delivery_note = data.get("doc_type") == "delivery_note"  # a DDT often has quantities only, no prices
    for name in ("number", "date", "supplier_name", "gross_total"):
        if name == "gross_total" and delivery_note:
            continue
        if not data.get(name):
            (err if name in ("date", "gross_total") else warn)(name, f"{HEADER_FIELDS[name]} is missing")

    for name in ("supplier_vat", "customer_vat"):
        problem = check_vat_number(data.get(name, ""))
        if problem:
            err(name, problem)

    doc_date = parse_date(data.get("date", "")) if data.get("date") else None
    if data.get("date") and doc_date is None:
        err("date", f"date {data['date']!r} is not a real date (expected YYYY-MM-DD)")
    if doc_date and doc_date > today + timedelta(days=1):
        err("date", f"date {doc_date:%d/%m/%Y} is in the future")
    if doc_date and doc_date.year < 2000:
        warn("date", f"date {doc_date:%d/%m/%Y} is suspiciously old")
    due = parse_date(data.get("due_date", "")) if data.get("due_date") else None
    if data.get("due_date") and due is None:
        err("due_date", f"due date {data['due_date']!r} is not a real date")
    if doc_date and due and due < doc_date:
        warn("due_date", "due date is before the document date")

    net, vat, gross = (to_decimal(data.get(n)) for n in ("net_total", "vat_total", "gross_total"))
    for name, value in (("net_total", net), ("vat_total", vat), ("gross_total", gross)):
        if data.get(name) and value is None:
            err(name, f"{HEADER_FIELDS[name]} {data[name]!r} is not a number")
    if net is not None and vat is not None and gross is not None and abs(net + vat - gross) > TOLERANCE:
        err("gross_total", f"net {net} + VAT {vat} = {net + vat}, but the total says {gross}")

    lines = data.get("lines") or []
    amounts = [to_decimal(line.get("amount")) for line in lines]
    for i, line in enumerate(lines, 1):
        qty, price, amount = (to_decimal(line.get(n)) for n in ("quantity", "unit_price", "amount"))
        if line.get("amount") and amount is None:
            err("lines", f"line {i}: amount {line['amount']!r} is not a number")
        if qty is not None and price is not None and amount is not None and abs(qty * price - amount) > TOLERANCE:
            warn("lines", f"line {i}: {qty} × {price} = {(qty * price).quantize(Decimal('0.01'))}, "
                          f"the line says {amount} (discount?)")
        rate = to_decimal(line.get("vat_rate"))
        if line.get("vat_rate") and rate is None:
            err("lines", f"line {i}: VAT rate {line['vat_rate']!r} is not a number")
        elif rate is not None and data.get("currency", "EUR") in ("", "EUR") and rate not in VAT_RATES_IT:
            warn("lines", f"line {i}: VAT {rate}% is not an Italian rate (0, 4, 5, 10, 22)")
    if lines and net is not None and amounts and all(a is not None for a in amounts):
        total_lines = sum(amounts, Decimal("0"))
        tolerance = TOLERANCE * max(1, len(lines))
        matches_net = abs(total_lines - net) <= tolerance
        # receipts print prices with VAT included: then the lines add up to the total instead
        matches_gross = gross is not None and abs(total_lines - gross) <= tolerance
        if not matches_net and not matches_gross:
            err("net_total", f"lines add up to {total_lines}, the net total says {net}")
        rates = [to_decimal(line.get("vat_rate")) for line in lines]
        if matches_net and vat is not None and all(r is not None for r in rates):
            expected = sum((a * r / 100 for a, r in zip(amounts, rates, strict=True)), Decimal("0"))
            if abs(expected - vat) > max(TOLERANCE * len(lines), Decimal("0.05")):
                err("vat_total", f"VAT from the lines is {expected.quantize(Decimal('0.01'))}, the document says {vat}")

    for name in data.get("unreadable_fields") or []:
        label = HEADER_FIELDS.get(name, "lines" if name == "lines" else name)
        warn(name, f"{label}: the AI could not read it with confidence — compare with the image")
    return issues


def counts(issues: list[Issue] | list[dict]) -> tuple[int, int]:
    levels = [i.level if isinstance(i, Issue) else i["level"] for i in issues]
    return levels.count("error"), levels.count("warning")
