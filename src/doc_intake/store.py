"""SQLite storage: documents and their life cycle, the edit log, users and the daily AI budget.

Life cycle:  queued ──claim──► reading ──► review ──► approved
                ▲                 │  └─(temporary API error)─► queued again with a pause
                └── re-read ──────┴──(lease expired: the worker died)──► taken again
             review ──► rejected;  reading ──(permanent problem)──► failed

A worker "leases" a document for LEASE seconds. If the process dies while reading, the lease
expires and another worker takes the document again: nothing is lost, nothing is read twice at once.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .files import Upload

STATUSES = ("queued", "reading", "review", "approved", "rejected", "failed")
LEASE = 180  # seconds a worker may spend on one document
MAX_ATTEMPTS = 4

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'reviewer')),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    pages INTEGER NOT NULL,
    text_layer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    source TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}',
    issues TEXT NOT NULL DEFAULT '[]',
    errors INTEGER NOT NULL DEFAULT 0,
    warnings INTEGER NOT NULL DEFAULT 0,
    duplicate_of INTEGER REFERENCES documents(id),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try_at REAL NOT NULL DEFAULT 0,
    lease_until REAL,
    last_error TEXT NOT NULL DEFAULT '',
    uploaded_by INTEGER REFERENCES users(id),
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at REAL,
    reject_reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_documents_status ON documents (status, next_try_at, id);
CREATE INDEX IF NOT EXISTS ix_documents_supplier_number ON documents (
    json_extract(data, '$.supplier_vat'), json_extract(data, '$.number'));
CREATE TABLE IF NOT EXISTS edits (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    user_id INTEGER REFERENCES users(id),
    field TEXT NOT NULL,
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_usage (
    day TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);
"""


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


@dataclass
class Document:
    id: int
    sha256: str
    filename: str
    kind: str
    size: int
    pages: int
    text_layer: str
    status: str
    source: str
    note: str
    data: dict[str, Any]
    issues: list[dict[str, str]]
    errors: int
    warnings: int
    duplicate_of: int | None
    attempts: int
    last_error: str
    reviewed_by: int | None
    reviewed_at: float | None
    reject_reason: str
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Document:
        d = dict(row)
        return cls(
            id=d["id"], sha256=d["sha256"], filename=d["filename"], kind=d["kind"], size=d["size"],
            pages=d["pages"], text_layer=d["text_layer"], status=d["status"], source=d["source"], note=d["note"],
            data=json.loads(d["data"]), issues=json.loads(d["issues"]), errors=d["errors"], warnings=d["warnings"],
            duplicate_of=d["duplicate_of"], attempts=d["attempts"], last_error=d["last_error"],
            reviewed_by=d["reviewed_by"], reviewed_at=d["reviewed_at"], reject_reason=d["reject_reason"],
            created_at=d["created_at"], updated_at=d["updated_at"],
        )


@dataclass
class User:
    id: int
    email: str
    name: str
    role: str
    password_hash: str


class Store:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        self.lock = asyncio.Lock()  # one writer at a time inside this process

    @classmethod
    async def open(cls, path: Path | str) -> Store:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    # ------------------------------------------------------------ users
    async def add_user(self, email: str, name: str, password_hash: str, role: str, now: float) -> int:
        async with self.lock:
            try:
                cur = await self.conn.execute(
                    "INSERT INTO users (email, name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                    (email.strip(), name.strip(), password_hash, role, now))
            except sqlite3.IntegrityError:
                raise Conflict(f"user {email} already exists") from None
            await self.conn.commit()
            return int(cur.lastrowid)

    async def user_by_email(self, email: str) -> User | None:
        cur = await self.conn.execute("SELECT * FROM users WHERE email = ?", (email.strip(),))
        row = await cur.fetchone()
        return User(row["id"], row["email"], row["name"], row["role"], row["password_hash"]) if row else None

    async def user(self, user_id: int) -> User | None:
        cur = await self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
        return User(row["id"], row["email"], row["name"], row["role"], row["password_hash"]) if row else None

    async def users(self) -> list[User]:
        cur = await self.conn.execute("SELECT * FROM users ORDER BY id")
        return [User(r["id"], r["email"], r["name"], r["role"], r["password_hash"]) for r in await cur.fetchall()]

    # ------------------------------------------------------------ documents
    async def add_document(self, upload: Upload, filename: str, user_id: int | None, now: float) -> tuple[int, bool]:
        """(id, True) for a new file; (id of the existing one, False) if the same file was uploaded before."""
        async with self.lock:
            cur = await self.conn.execute("SELECT id FROM documents WHERE sha256 = ?", (upload.sha256,))
            row = await cur.fetchone()
            if row:
                return int(row["id"]), False
            cur = await self.conn.execute(
                "INSERT INTO documents (sha256, filename, kind, size, pages, text_layer, uploaded_by, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (upload.sha256, filename[:200], upload.kind, upload.size, upload.pages, upload.text, user_id, now, now))
            await self.conn.commit()
            return int(cur.lastrowid), True

    async def get(self, doc_id: int) -> Document:
        cur = await self.conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,))
        row = await cur.fetchone()
        if row is None:
            raise NotFound(f"document {doc_id}")
        return Document.from_row(row)

    async def claim(self, now: float) -> Document | None:
        """Takes the oldest due document (or one whose worker died) and leases it."""
        async with self.lock:
            cur = await self.conn.execute(
                "UPDATE documents SET status = 'reading', lease_until = ?, attempts = attempts + 1, updated_at = ? "
                "WHERE id = (SELECT id FROM documents WHERE (status = 'queued' AND next_try_at <= ?) "
                "OR (status = 'reading' AND lease_until < ?) ORDER BY id LIMIT 1) RETURNING *",
                (now + LEASE, now, now, now))
            row = await cur.fetchone()
            await self.conn.commit()
            return Document.from_row(row) if row else None

    async def find_duplicate(self, doc_id: int, supplier_vat: str, number: str) -> int | None:
        if not supplier_vat or not number:
            return None
        cur = await self.conn.execute(
            "SELECT id FROM documents WHERE id != ? AND status NOT IN ('rejected', 'failed') "
            "AND json_extract(data, '$.supplier_vat') = ? AND json_extract(data, '$.number') = ? ORDER BY id LIMIT 1",
            (doc_id, supplier_vat, number))
        row = await cur.fetchone()
        return int(row["id"]) if row else None

    async def finish_reading(self, doc_id: int, data: dict, issues: list[dict], source: str, note: str,
                             duplicate_of: int | None, now: float) -> None:
        errors = sum(1 for i in issues if i["level"] == "error")
        async with self.lock:
            await self.conn.execute(
                "UPDATE documents SET status = 'review', data = ?, issues = ?, errors = ?, warnings = ?, source = ?, "
                "note = ?, duplicate_of = ?, lease_until = NULL, last_error = '', updated_at = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), json.dumps(issues, ensure_ascii=False), errors,
                 len(issues) - errors, source, note, duplicate_of, now, doc_id))
            await self.conn.commit()

    async def retry_later(self, doc_id: int, delay: float, error: str, now: float) -> None:
        async with self.lock:
            await self.conn.execute(
                "UPDATE documents SET status = 'queued', next_try_at = ?, lease_until = NULL, last_error = ?, "
                "updated_at = ? WHERE id = ?", (now + delay, error[:300], now, doc_id))
            await self.conn.commit()

    async def fail(self, doc_id: int, error: str, now: float) -> None:
        async with self.lock:
            await self.conn.execute(
                "UPDATE documents SET status = 'failed', lease_until = NULL, last_error = ?, updated_at = ? "
                "WHERE id = ?", (error[:300], now, doc_id))
            await self.conn.commit()

    async def requeue(self, doc_id: int, now: float) -> None:
        """Read again (e.g. after the API key was fixed). Approved documents are final."""
        async with self.lock:
            cur = await self.conn.execute(
                "UPDATE documents SET status = 'queued', next_try_at = 0, attempts = 0, last_error = '', "
                "updated_at = ? WHERE id = ? AND status IN ('review', 'failed', 'rejected')", (now, doc_id))
            await self.conn.commit()
            if cur.rowcount == 0:
                raise Conflict("only documents in review, failed or rejected can be read again")

    async def save_review(self, doc_id: int, user_id: int, data: dict, issues: list[dict], now: float) -> int:
        """Stores the reviewer's corrections; every changed field goes to the edit log. Returns how many changed."""
        async with self.lock:
            cur = await self.conn.execute("SELECT status, data FROM documents WHERE id = ?", (doc_id,))
            row = await cur.fetchone()
            if row is None:
                raise NotFound(f"document {doc_id}")
            if row["status"] != "review":
                raise Conflict(f"the document is {row['status']}, it cannot be edited")
            old = json.loads(row["data"])
            changed = 0
            for name in sorted(set(old) | set(data)):
                if name == "unreadable_fields":
                    continue
                before, after = old.get(name, ""), data.get(name, "")
                if before != after:
                    changed += 1
                    await self.conn.execute(
                        "INSERT INTO edits (document_id, user_id, field, old_value, new_value, at) VALUES "
                        "(?, ?, ?, ?, ?, ?)",
                        (doc_id, user_id, name, json.dumps(before, ensure_ascii=False),
                         json.dumps(after, ensure_ascii=False), now))
            errors = sum(1 for i in issues if i["level"] == "error")
            await self.conn.execute(
                "UPDATE documents SET data = ?, issues = ?, errors = ?, warnings = ?, updated_at = ? WHERE id = ?",
                (json.dumps(data, ensure_ascii=False), json.dumps(issues, ensure_ascii=False), errors,
                 len(issues) - errors, now, doc_id))
            await self.conn.commit()
            return changed

    async def decide(self, doc_id: int, user_id: int, approve: bool, reason: str, now: float) -> None:
        async with self.lock:
            cur = await self.conn.execute(
                "UPDATE documents SET status = ?, reviewed_by = ?, reviewed_at = ?, reject_reason = ?, updated_at = ? "
                "WHERE id = ? AND status = 'review'",
                ("approved" if approve else "rejected", user_id, now, "" if approve else reason[:300], now, doc_id))
            await self.conn.commit()
            if cur.rowcount == 0:
                raise Conflict("the document is not waiting for review")

    async def edits(self, doc_id: int) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT e.*, u.name AS user_name FROM edits e LEFT JOIN users u ON u.id = e.user_id "
            "WHERE e.document_id = ? ORDER BY e.id DESC", (doc_id,))
        return [dict(r) for r in await cur.fetchall()]

    async def documents(self, status: str | None = None, query: str = "", limit: int = 200) -> list[Document]:
        sql, args = "SELECT * FROM documents WHERE 1 = 1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if query:
            sql += " AND (filename LIKE ? ESCAPE '\\' OR data LIKE ? ESCAPE '\\')"
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            args += [like, like]
        sql += " ORDER BY id DESC LIMIT ?"
        cur = await self.conn.execute(sql, (*args, limit))
        return [Document.from_row(r) for r in await cur.fetchall()]

    async def counts(self) -> dict[str, int]:
        cur = await self.conn.execute("SELECT status, COUNT(*) AS n FROM documents GROUP BY status")
        found = {r["status"]: r["n"] for r in await cur.fetchall()}
        return {s: found.get(s, 0) for s in STATUSES}

    async def approved(self) -> list[Document]:
        cur = await self.conn.execute("SELECT * FROM documents WHERE status = 'approved' ORDER BY id")
        return [Document.from_row(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------ AI budget
    async def take_ai_call(self, day: str, limit: int) -> bool:
        """Counts one AI reading for the day; False when the daily limit is reached (nothing counted)."""
        async with self.lock:
            cur = await self.conn.execute(
                "INSERT INTO ai_usage (day, count) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET count = count + 1 "
                "WHERE count < ? RETURNING count", (day, limit))
            row = await cur.fetchone()
            await self.conn.commit()
            return row is not None and limit > 0

    async def ai_used(self, day: str) -> int:
        cur = await self.conn.execute("SELECT count FROM ai_usage WHERE day = ?", (day,))
        row = await cur.fetchone()
        return int(row["count"]) if row else 0
