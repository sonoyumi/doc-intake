"""Shared fixtures: settings in a temp folder, a real SQLite store, a fake Claude client, sample files."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from doc_intake import files
from doc_intake.config import Settings
from doc_intake.store import Store

EXAMPLES = Path(__file__).parent.parent / "examples" / "documents"
NOW = 1_790_000_000.0  # 2026-09-21, a fixed "now" for every test

GOOD_INVOICE = {
    "doc_type": "invoice", "number": "118/2026", "date": "2026-09-15", "due_date": "2026-10-15",
    "supplier_name": "Tipografia Alpina Srl", "supplier_vat": "IT02849173055",
    "customer_name": "Ristorante Laurin Srl", "customer_vat": "IT03971560424", "currency": "EUR",
    "net_total": "290.00", "vat_total": "63.80", "gross_total": "353.80",
    "lines": [
        {"description": "Menu plastificati A4", "quantity": "40", "unit_price": "3.50", "vat_rate": "22",
         "amount": "140.00"},
        {"description": "Volantini A5", "quantity": "2000", "unit_price": "0.045", "vat_rate": "22", "amount": "90.00"},
        {"description": "Progetto grafico", "quantity": "1", "unit_price": "60.00", "vat_rate": "22",
         "amount": "60.00"},
    ],
    "unreadable_fields": [],
}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(secret_key="test-secret-" + "x" * 32, data_dir=tmp_path / "data", company_name="Studio Test")


@pytest.fixture
async def store():
    s = await Store.open(":memory:")
    yield s
    await s.close()


def example(name: str) -> bytes:
    return (EXAMPLES / name).read_bytes()


async def add_file(store: Store, settings: Settings, name: str, data: bytes | None = None) -> int:
    data = data if data is not None else example(name)
    upload = files.inspect(data, settings.max_file_mb, settings.max_pages)
    files.save(settings.storage_dir, upload, data)
    doc_id, _ = await store.add_document(upload, name, None, NOW)
    return doc_id


class FakeMessages:
    """Records requests; replies with prepared results (dict -> JSON text, Exception -> raised)."""

    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []

    async def create(self, **request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, NS):
            return reply
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(reply))])


class FakeClaude:
    def __init__(self, *replies) -> None:
        self.beta = NS(messages=FakeMessages(list(replies)))

    @property
    def requests(self) -> list[dict]:
        return self.beta.messages.requests
