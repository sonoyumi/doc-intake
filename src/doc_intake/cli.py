"""Command line.

doc-intake add-user --email anna@studio.it --name Anna [--role admin]   create a user (asks for the password)
doc-intake passwd anna@studio.it        set a new password
doc-intake serve [--no-worker]          review panel + background reading (one process)
doc-intake worker                       background reading only (run next to `serve --no-worker`)
doc-intake import FILE_OR_FOLDER...     add documents to the queue
doc-intake read FILE                    read one file and print fields + checks (nothing is stored)
doc-intake export -o report.xlsx        approved documents to Excel
doc-intake stats                        documents by status, AI readings today
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import signal
import sys
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from . import __version__, checks, export, files, security
from .config import ConfigError, Settings, load_settings
from .extract import ReadError, build_reader, read_document
from .store import Conflict, Store
from .worker import Worker, ensure_dirs

log = logging.getLogger("doc_intake")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="doc-intake", description="Scans and PDFs -> checked data -> Excel")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    u = sub.add_parser("add-user", help="create a user of the panel")
    u.add_argument("--email", required=True)
    u.add_argument("--name", required=True)
    u.add_argument("--role", choices=["admin", "reviewer"], default="admin")
    pw = sub.add_parser("passwd", help="set a new password for a user")
    pw.add_argument("email")
    s = sub.add_parser("serve", help="run the review panel (and the worker)")
    s.add_argument("--no-worker", action="store_true", help="do not read documents in this process")
    sub.add_parser("worker", help="read queued documents")
    i = sub.add_parser("import", help="add files or folders to the queue")
    i.add_argument("paths", nargs="+", type=Path)
    r = sub.add_parser("read", help="read one file and print the result (stores nothing)")
    r.add_argument("path", type=Path)
    e = sub.add_parser("export", help="approved documents to Excel")
    e.add_argument("-o", "--output", type=Path, default=Path("documents.xlsx"))
    sub.add_parser("stats", help="documents by status")
    return p


async def _add_user(settings: Settings, args: argparse.Namespace) -> int:
    password = getpass.getpass("Password (10+ characters): ")
    if len(password) < 10:
        print("The password must have at least 10 characters.")
        return 1
    store = await Store.open(settings.database_path)
    try:
        await store.add_user(args.email, args.name, security.hash_password(password), args.role, time.time())
    except Conflict as exc:
        print(exc)
        return 1
    finally:
        await store.close()
    print(f"User {args.email} ({args.role}) created.")
    return 0


async def _passwd(settings: Settings, email: str) -> int:
    password = getpass.getpass("New password (10+ characters): ")
    if len(password) < 10:
        print("The password must have at least 10 characters.")
        return 1
    store = await Store.open(settings.database_path)
    try:
        found = await store.set_password(email, security.hash_password(password))
    finally:
        await store.close()
    print("Password changed." if found else f"No user {email}.")
    return 0 if found else 1


async def _serve(settings: Settings, with_worker: bool) -> int:
    import uvicorn

    from .web import create_app

    ensure_dirs(settings)
    store = await Store.open(settings.database_path)
    if not await store.users():
        print("No users yet: create one first with  doc-intake add-user --email ... --name ...")
        await store.close()
        return 1
    stop = asyncio.Event()
    server = uvicorn.Server(uvicorn.Config(create_app(store, settings), host=settings.host, port=settings.port,
                                           log_level="warning"))
    tasks = [server.serve()]
    if with_worker:
        reader = build_reader(settings.ai_mode, settings.anthropic_api_key, settings.claude_model)
        tasks.append(Worker(store, settings, reader).run(stop))
    print(f"Panel: http://{settings.host}:{settings.port}  (AI: {settings.ai_mode})")

    async def stop_worker_after_server():
        await tasks[0]
        stop.set()

    try:
        await asyncio.gather(stop_worker_after_server(), *tasks[1:])
    finally:
        await store.close()
    return 0


async def _worker(settings: Settings) -> int:
    ensure_dirs(settings)
    store = await Store.open(settings.database_path)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    reader = build_reader(settings.ai_mode, settings.anthropic_api_key, settings.claude_model)
    try:
        await Worker(store, settings, reader).run(stop)
    finally:
        await store.close()
    return 0


async def _import(settings: Settings, paths: list[Path]) -> int:
    ensure_dirs(settings)
    store = await Store.open(settings.database_path)
    added = dup = bad = 0
    try:
        for path in paths:
            items = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
            for item in items:
                data = item.read_bytes()
                try:
                    upload = files.inspect(data, settings.max_file_mb, settings.max_pages)
                except files.FileRejected as exc:
                    print(f"  skipped {item}: {exc}")
                    bad += 1
                    continue
                files.save(settings.storage_dir, upload, data)
                _, new = await store.add_document(upload, item.name, None, time.time())
                added += new
                dup += not new
    finally:
        await store.close()
    print(f"Queued: {added} · already known: {dup} · skipped: {bad}")
    return 0


async def _read(settings: Settings, path: Path) -> int:
    data = await asyncio.to_thread(path.read_bytes)
    try:
        upload = files.inspect(data, settings.max_file_mb, settings.max_pages)
    except files.FileRejected as exc:
        print(f"Not accepted: {exc}")
        return 1
    reader = build_reader(settings.ai_mode, settings.anthropic_api_key, settings.claude_model)
    images = await asyncio.to_thread(files.images_for_ai, path, upload.kind, upload.pages) if reader else []
    try:
        reading = await read_document(reader, images, upload.text)
    except ReadError as exc:
        print(f"AI reading failed: {exc}")
        return 1
    print(json.dumps(reading.data, ensure_ascii=False, indent=2))
    print(f"\nRead by: {reading.source}" + (f" — {reading.note}" if reading.note else ""))
    issues = checks.run(reading.data, date.today())
    for issue in issues:
        print(("ERROR  " if issue.level == "error" else "warning ") + issue.message)
    print("All checks passed." if not issues else f"{len(issues)} issue(s).")
    return 0


async def _export(settings: Settings, output: Path) -> int:
    store = await Store.open(settings.database_path)
    try:
        docs = await store.approved()
        reviewers = {u.id: u.name for u in await store.users()}
    finally:
        await store.close()
    await asyncio.to_thread(output.write_bytes, export.build(docs, reviewers))
    print(f"{len(docs)} approved document(s) -> {output}")
    return 0


async def _stats(settings: Settings) -> int:
    store = await Store.open(settings.database_path)
    try:
        counts = await store.counts()
        used = await store.ai_used(datetime.now(UTC).strftime("%Y-%m-%d"))
    finally:
        await store.close()
    print(" · ".join(f"{k}: {v}" for k, v in counts.items()))
    print(f"AI readings today: {used} / {settings.ai_daily_limit} (AI: {settings.ai_mode})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    commands = {
        "add-user": lambda: _add_user(settings, args),
        "passwd": lambda: _passwd(settings, args.email),
        "serve": lambda: _serve(settings, not args.no_worker),
        "worker": lambda: _worker(settings),
        "import": lambda: _import(settings, args.paths),
        "read": lambda: _read(settings, args.path),
        "export": lambda: _export(settings, args.output),
        "stats": lambda: _stats(settings),
    }
    try:
        return asyncio.run(commands[args.command]())
    except KeyboardInterrupt:
        return 130
