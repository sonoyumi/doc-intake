"""Approved documents -> one Excel workbook: Documents, Lines, VAT summary.

Money is written as numbers (not text) so the accountant can sum it; dates as real Excel dates.
Text that starts with "=" is stored as text: a document cannot smuggle a formula into the
accountant's spreadsheet (formula injection).
"""

from __future__ import annotations

import io
from collections import defaultdict
from decimal import Decimal

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .model import DOC_TYPES, parse_date, to_decimal
from .store import Document

MONEY = "#,##0.00"
HEADER_FILL = PatternFill("solid", fgColor="1D5C86")


def _text(value: str) -> str:
    return ILLEGAL_CHARACTERS_RE.sub("", value or "")  # control characters make openpyxl crash


def _sheet(wb: Workbook, title: str, header: list[str], rows: list[list], money_cols: set[int], date_cols: set[int]):
    ws = wb.create_sheet(title)
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
    for row in rows:
        ws.append(row)
    for row_cells in ws.iter_rows(min_row=2):
        for cell in row_cells:
            if cell.data_type == "f":  # openpyxl turns any "=..." string into a formula: keep it as text
                cell.data_type = "s"
    for col in range(1, len(header) + 1):
        letter = get_column_letter(col)
        for cell in ws[letter][1:]:
            if col in money_cols:
                cell.number_format = MONEY
            elif col in date_cols:
                cell.number_format = "DD/MM/YYYY"
        width = max((len(str(c.value or "")) for c in ws[letter]), default=8)
        ws.column_dimensions[letter].width = min(max(width + 2, 9), 50)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def build(docs: list[Document], reviewers: dict[int, str]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    doc_rows, line_rows = [], []
    vat_by_rate: dict[Decimal, list[Decimal]] = defaultdict(lambda: [Decimal("0"), Decimal("0")])
    for doc in docs:
        d = doc.data
        doc_rows.append([
            doc.id, DOC_TYPES.get(d["doc_type"], d["doc_type"]), _text(d["number"]), parse_date(d["date"]),
            parse_date(d["due_date"]), _text(d["supplier_name"]), _text(d["supplier_vat"]), _text(d["customer_name"]),
            _text(d["customer_vat"]), d["currency"] or "EUR", to_decimal(d["net_total"]), to_decimal(d["vat_total"]),
            to_decimal(d["gross_total"]), doc.source, reviewers.get(doc.reviewed_by or 0, ""), _text(doc.filename),
        ])
        for line in d["lines"]:
            amount, rate = to_decimal(line["amount"]), to_decimal(line["vat_rate"])
            line_rows.append([doc.id, _text(d["number"]), _text(d["supplier_name"]), _text(line["description"]),
                              to_decimal(line["quantity"]), to_decimal(line["unit_price"]), rate, amount])
            if amount is not None and rate is not None:
                vat_by_rate[rate][0] += amount
                vat_by_rate[rate][1] += (amount * rate / 100).quantize(Decimal("0.01"))
    _sheet(wb, "Documents",
           ["ID", "Type", "Number", "Date", "Due date", "Supplier", "Supplier VAT", "Customer", "Customer VAT",
            "Currency", "Net", "VAT", "Total", "Read by", "Approved by", "File"],
           doc_rows, money_cols={11, 12, 13}, date_cols={4, 5})
    _sheet(wb, "Lines",
           ["Document ID", "Number", "Supplier", "Description", "Qty", "Unit price", "VAT %", "Net amount"],
           line_rows, money_cols={6, 8}, date_cols=set())
    summary = [[rate, net, vat] for rate, (net, vat) in sorted(vat_by_rate.items(), reverse=True)]
    if summary:
        zero = Decimal("0")
        summary.append(["TOTAL", sum((r[1] for r in summary), zero), sum((r[2] for r in summary), zero)])
    ws = _sheet(wb, "VAT summary", ["VAT %", "Net", "VAT"], summary, money_cols={2, 3}, date_cols=set())
    if summary:
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
