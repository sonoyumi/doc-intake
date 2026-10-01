"""Background worker: picks up the inbox folder, reads queued documents, runs the checks.

A document never gets stuck: a temporary API problem means "try again later" (30 s, 60 s, 120 s),
a permanent one or the daily AI limit means "read what we can without AI and let a person finish".
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from . import checks, files
from .config import Settings
from .extract import Reader, ReadError, Reading, read_document
from .store import MAX_ATTEMPTS, Document, Store

log = logging.getLogger(__name__)
RETRY_BASE = 30.0
INBOX_EVERY = 10.0


class Worker:
    def __init__(self, store: Store, settings: Settings, reader: Reader | None,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.settings = settings
        self.reader = reader
        self.clock = clock
        self._last_inbox = 0.0

    async def run(self, stop: asyncio.Event) -> None:
        log.info("worker started (AI: %s)", "on" if self.reader else "off")
        while not stop.is_set():
            try:
                busy = await self.tick()
            except Exception:  # one broken document must not stop the worker
                log.exception("worker error")
                busy = False
            if not busy:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except TimeoutError:
                    pass

    async def tick(self) -> bool:
        if self.clock() - self._last_inbox >= INBOX_EVERY:
            self._last_inbox = self.clock()
            await self.scan_inbox()
        doc = await self.store.claim(self.clock())
        if doc is None:
            return False
        await self.process(doc)
        return True

    async def scan_inbox(self) -> int:
        """Files dropped into data/inbox/ (scanner, shared folder, e-mail rule) join the queue."""
        inbox = self.settings.inbox_dir
        if not inbox.is_dir():
            return 0
        added = 0
        for path in sorted(p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")):
            if self.clock() - path.stat().st_mtime < 2:  # still being copied
                continue
            data = await asyncio.to_thread(path.read_bytes)
            try:
                upload = await asyncio.to_thread(files.inspect, data, self.settings.max_file_mb,
                                                 self.settings.max_pages)
            except files.FileRejected as exc:
                target = inbox / "rejected"
                target.mkdir(exist_ok=True)
                shutil.move(path, target / path.name)
                (target / f"{path.name}.txt").write_text(str(exc) + "\n")
                log.warning("inbox: %s rejected: %s", path.name, exc)
                continue
            await asyncio.to_thread(files.save, self.settings.storage_dir, upload, data)
            _, new = await self.store.add_document(upload, path.name, None, self.clock())
            done = inbox / "done"
            done.mkdir(exist_ok=True)
            shutil.move(path, done / path.name)
            added += new
        return added

    async def _read(self, doc: Document) -> Reading | None:
        """None = try again later (the document went back to the queue)."""
        reader = self.reader
        note = ""
        if reader is not None:
            day = datetime.fromtimestamp(self.clock(), UTC).strftime("%Y-%m-%d")
            if not await self.store.take_ai_call(day, self.settings.ai_daily_limit):
                reader, note = None, f"daily AI limit ({self.settings.ai_daily_limit}) reached"
        path = files.path_for(self.settings.storage_dir, doc.sha256, doc.kind)
        images = await asyncio.to_thread(files.images_for_ai, path, doc.kind, doc.pages) if reader else []
        try:
            reading = await read_document(reader, images, doc.text_layer)
        except ReadError as exc:
            if exc.temporary and doc.attempts < MAX_ATTEMPTS:
                delay = RETRY_BASE * 2 ** (doc.attempts - 1)
                log.warning("document %s: %s, retry in %.0f s", doc.id, exc, delay)
                await self.store.retry_later(doc.id, delay, str(exc), self.clock())
                return None
            log.error("document %s: %s; continuing without AI", doc.id, exc)
            reading = await read_document(None, [], doc.text_layer)
            note = f"AI reading failed ({exc})"
        if note:
            reading.note = f"{note}; {reading.note}" if reading.note else note
        return reading

    async def process(self, doc: Document) -> None:
        path = files.path_for(self.settings.storage_dir, doc.sha256, doc.kind)
        if not path.exists():
            await self.store.fail(doc.id, "the stored file is missing", self.clock())
            return
        reading = await self._read(doc)
        if reading is None:
            return
        today = datetime.fromtimestamp(self.clock(), UTC).date()
        issues = [i.as_dict() for i in checks.run(reading.data, today)]
        duplicate = await self.store.find_duplicate(doc.id, reading.data["supplier_vat"], reading.data["number"])
        if duplicate:
            issues.insert(0, {"level": "error", "field": "number",
                              "message": f"same supplier and number as document #{duplicate}: a duplicate?"})
        await self.store.finish_reading(doc.id, reading.data, issues, reading.source, reading.note, duplicate,
                                        self.clock())
        log.info("document %s read (%s): %d issue(s)", doc.id, reading.source, len(issues))


def ensure_dirs(settings: Settings) -> None:
    for path in (settings.data_dir, settings.storage_dir, settings.inbox_dir):
        Path(path).mkdir(parents=True, exist_ok=True)
