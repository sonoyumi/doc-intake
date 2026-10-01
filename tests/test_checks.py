from datetime import date
from decimal import Decimal

from conftest import GOOD_INVOICE

from doc_intake.checks import check_vat_number, italian_vat_ok, parse_date, run
from doc_intake.model import SCHEMA, clean, to_decimal

TODAY = date(2026, 9, 30)


def messages(data):
    return [(i.level, i.field, i.message) for i in run(data, TODAY)]


def test_correct_invoice_passes_every_check():
    assert messages(GOOD_INVOICE) == []


def test_italian_vat_check_digit():
    assert italian_vat_ok("02849173055")
    assert italian_vat_ok("01234567897")
    assert not italian_vat_ok("02849173056")  # one digit off
    assert not italian_vat_ok("00000000000")
    assert check_vat_number("IT02849173055") is None
    assert "check digit" in check_vat_number("IT02849173056")
    assert check_vat_number("RSSMRA85T10A562S") is None  # codice fiscale of a person
    assert check_vat_number("DE123456789") is None
    assert "does not look" in check_vat_number("12-AB")


def test_wrong_vat_amount_is_an_error():
    data = clean({**GOOD_INVOICE, "vat_total": "30.00", "gross_total": "320.00"})
    found = messages(data)
    assert ("error", "vat_total", "VAT from the lines is 63.80, the document says 30.00") in found


def test_totals_that_do_not_add_up():
    data = clean({**GOOD_INVOICE, "gross_total": "350.00"})
    assert ("error", "gross_total", "net 290.00 + VAT 63.80 = 353.80, but the total says 350.00") in messages(data)
    data = clean({**GOOD_INVOICE, "net_total": "300.00", "gross_total": "363.80"})
    assert any(f == "net_total" and "lines add up to 290.00" in m for _, f, m in messages(data))


def test_line_quantity_times_price_is_only_a_warning():
    lines = [dict(GOOD_INVOICE["lines"][0], amount="126.00")] + GOOD_INVOICE["lines"][1:]
    data = clean({**GOOD_INVOICE, "lines": lines, "net_total": "276.00", "vat_total": "60.72",
                  "gross_total": "336.72"})
    assert messages(data) == [("warning", "lines", "line 1: 40 × 3.5 = 140.00, the line says 126.00 (discount?)")]


def test_receipt_lines_with_vat_included_match_the_total():
    receipt = clean({"doc_type": "receipt", "number": "0042-0117", "date": "2026-09-20", "supplier_name": "Bar",
                     "supplier_vat": "IT01633904576", "net_total": "6.73", "vat_total": "0.67", "gross_total": "7.40",
                     "lines": [{"description": "Caffè", "quantity": "2", "unit_price": "1.20", "vat_rate": "10",
                                "amount": "2.40"},
                               {"description": "Cornetto", "quantity": "1", "unit_price": "5.00", "vat_rate": "10",
                                "amount": "5.00"}]})
    assert messages(receipt) == []


def test_delivery_note_needs_no_total_but_a_date():
    ddt = clean({"doc_type": "delivery_note", "number": "2026/0457", "supplier_name": "Ortofrutta"})
    assert messages(ddt) == [("error", "date", "Date is missing")]


def test_dates():
    assert parse_date("2026-09-15") == date(2026, 9, 15)
    assert parse_date("15/09/2026") == date(2026, 9, 15)
    assert parse_date("2026-02-30") is None
    future = clean({**GOOD_INVOICE, "date": "2026-12-01", "due_date": "2026-12-31"})
    assert ("error", "date", "date 01/12/2026 is in the future") in messages(future)
    early_due = clean({**GOOD_INVOICE, "due_date": "2026-09-01"})
    assert ("warning", "due_date", "due date is before the document date") in messages(early_due)
    assert ("error", "date", "date '31/02/2026' is not a real date (expected YYYY-MM-DD)") in messages(
        clean({**GOOD_INVOICE, "date": "31/02/2026"}))


def test_fields_the_ai_was_unsure_about_are_flagged():
    data = clean({**GOOD_INVOICE, "unreadable_fields": ["gross_total", "not_a_field"]})
    assert messages(data) == [("warning", "gross_total",
                               "Total: the AI could not read it with confidence — compare with the image")]


def test_non_italian_vat_rate_warns():
    lines = [dict(line, vat_rate="21") for line in GOOD_INVOICE["lines"]]
    found = messages(clean({**GOOD_INVOICE, "lines": lines, "vat_total": "60.90", "gross_total": "350.90"}))
    assert ("warning", "lines", "line 1: VAT 21% is not an Italian rate (0, 4, 5, 10, 22)") in found


def test_decimal_parsing_of_european_and_english_formats():
    assert to_decimal("1.234,50") == Decimal("1234.50")
    assert to_decimal("1,234.50") == Decimal("1234.50")
    assert to_decimal("€ 12,50") == Decimal("12.50")
    assert to_decimal("1,5") == Decimal("1.5")
    assert to_decimal("-14,04") == Decimal("-14.04")
    assert to_decimal("1.234.567") == Decimal("1234567")
    assert to_decimal("") is None and to_decimal("abc") is None and to_decimal(None) is None


def test_clean_keeps_only_known_fields():
    data = clean({"number": " 7 ", "evil": "x", "doc_type": "spaceship", "supplier_vat": "it 0284 917 3055",
                  "lines": [{"description": "a", "amount": "1", "hack": 1}, {}, "junk"]})
    assert data["number"] == "7" and "evil" not in data and data["doc_type"] == "other"
    assert data["supplier_vat"] == "IT02849173055"
    assert data["lines"] == [{"description": "a", "quantity": "", "unit_price": "", "vat_rate": "", "amount": "1.00"}]


def test_schema_requires_every_field_for_structured_outputs():
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])
    assert SCHEMA["additionalProperties"] is False
    line = SCHEMA["properties"]["lines"]["items"]
    assert set(line["required"]) == set(line["properties"]) and line["additionalProperties"] is False
