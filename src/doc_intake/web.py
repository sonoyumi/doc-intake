"""Review panel (FastAPI + Jinja2, no JavaScript framework).

Every page needs a signed session cookie, every form carries a CSRF token, files and previews are
served only to logged-in users. Templates escape everything, so text read from a document is shown
as text, never executed.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile

from . import checks, export, files, security
from .config import Settings
from .model import DOC_TYPES, HEADER_FIELDS, LINE_FIELDS, clean
from .store import STATUSES, Conflict, NotFound, Store, User

HERE = Path(__file__).parent
COOKIE = "di_session"
LOGIN_WINDOW, LOGIN_MAX_FAILS = 300, 5
MAX_FILES_PER_UPLOAD = 50
STATUS_LABELS = {"queued": "Queued", "reading": "Reading", "review": "To review", "approved": "Approved",
                 "rejected": "Rejected", "failed": "Failed"}
SOURCE_LABELS = {"claude": "AI (Claude)", "text": "PDF text layer", "manual": "Manual entry", "": "—"}


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


class LoginLimiter:
    """At most 5 failed logins per IP in 5 minutes (slows down password guessing)."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.fails: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, ip: str) -> bool:
        q = self.fails[ip]
        while q and q[0] < self.clock() - LOGIN_WINDOW:
            q.popleft()
        return len(q) >= LOGIN_MAX_FAILS

    def fail(self, ip: str) -> None:
        self.fails[ip].append(self.clock())

    def reset(self, ip: str) -> None:
        self.fails.pop(ip, None)


def local_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M") if ts else ""


def size_label(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def form_data(form) -> dict:
    """Review form -> canonical document dict (model.clean drops anything unexpected)."""
    raw = {name: form.get(name, "") for name in HEADER_FIELDS}
    lines = []
    try:
        count = min(int(form.get("line_count", "0")), 300)
    except ValueError:
        count = 0
    for i in range(count):
        lines.append({name: form.get(f"line-{i}-{name}", "") for name in LINE_FIELDS})
    raw["lines"] = lines
    return clean(raw)


def create_app(store: Store, settings: Settings, clock: Callable[[], float] = time.time) -> FastAPI:
    app = FastAPI(title="Doc intake", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(STATUS=STATUS_LABELS, SOURCE=SOURCE_LABELS, DOC_TYPES=DOC_TYPES,
                                 HEADER_FIELDS=HEADER_FIELDS, LINE_FIELDS=LINE_FIELDS,
                                 company=settings.company_name, ai_on=settings.ai_mode == "claude")
    templates.env.filters.update(t=local_time, size=size_label)
    limiter = LoginLimiter(clock)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; form-action 'self'")
        return response

    @app.exception_handler(LoginRequired)
    async def to_login(request: Request, exc: LoginRequired):
        return RedirectResponse("/login?next=" + quote(request.url.path), status_code=303)

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden):
        return HTMLResponse("Forbidden", status_code=403)

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound):
        return HTMLResponse("Not found", status_code=404)

    async def current_user(request: Request) -> User:
        user_id = security.read_session(settings.secret_key, request.cookies.get(COOKIE), clock())
        user = await store.user(user_id) if user_id else None
        if user is None:
            raise LoginRequired
        return user

    async def checked_form(request: Request):
        form = await request.form()
        if not security.check_csrf(settings.secret_key, request.cookies.get(COOKIE), form.get("csrf")):
            raise Forbidden
        return form

    def page(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
        cookie = request.cookies.get(COOKIE) or ""
        return templates.TemplateResponse(request, name, {
            "user": user, "csrf": security.csrf_token(settings.secret_key, cookie) if cookie else "", **ctx})

    # ------------------------------------------------------------ login
    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/documents"):
        return page(request, "login.html", None, next=next, error="")

    @app.post("/login")
    async def login(request: Request):
        form = await request.form()
        ip = request.client.host if request.client else "?"
        nxt = str(form.get("next") or "/documents")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/documents"  # no open redirect to another site
        if limiter.blocked(ip):
            return page(request, "login.html", None, next=nxt, error="Too many attempts, wait 5 minutes.")
        user = await store.user_by_email(str(form.get("email", "")))
        if user is None or not security.verify_password(str(form.get("password", "")), user.password_hash):
            limiter.fail(ip)
            return page(request, "login.html", None, next=nxt, error="Wrong e-mail or password.")
        limiter.reset(ip)
        response = RedirectResponse(nxt, status_code=303)
        response.set_cookie(COOKIE, security.make_session(settings.secret_key, user.id, clock()), httponly=True,
                            samesite="lax", secure=settings.cookie_secure, max_age=security.SESSION_TTL)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        await checked_form(request)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    # ------------------------------------------------------------ list and upload
    @app.get("/")
    async def root():
        return RedirectResponse("/documents", status_code=303)

    @app.get("/documents", response_class=HTMLResponse)
    async def documents(request: Request, status: str = "", q: str = ""):
        user = await current_user(request)
        status = status if status in STATUSES else ""
        docs = await store.documents(status or None, q.strip()[:100])
        day = datetime.fromtimestamp(clock(), UTC).strftime("%Y-%m-%d")
        return page(request, "documents.html", user, docs=docs, counts=await store.counts(), status=status, q=q,
                    msg=request.query_params, ai_used=await store.ai_used(day), ai_limit=settings.ai_daily_limit)

    @app.post("/upload")
    async def upload(request: Request):
        user = await current_user(request)
        form = await request.form()
        if not security.check_csrf(settings.secret_key, request.cookies.get(COOKIE), form.get("csrf")):
            raise Forbidden
        uploads = [f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename][:MAX_FILES_PER_UPLOAD]
        added = dup = 0
        rejected: list[str] = []
        limit = settings.max_file_mb * 1024 * 1024
        for item in uploads:
            data = await item.read(limit + 1)
            try:
                info = await asyncio.to_thread(files.inspect, data, settings.max_file_mb, settings.max_pages)
            except files.FileRejected as exc:
                rejected.append(f"{item.filename}: {exc}")
                continue
            await asyncio.to_thread(files.save, settings.storage_dir, info, data)
            _, new = await store.add_document(info, Path(item.filename).name, user.id, clock())
            added += new
            dup += not new
        query = {"added": added, "dup": dup}
        if rejected:
            query["rejected"] = " · ".join(rejected)[:600]
        return RedirectResponse("/documents?" + urlencode(query), status_code=303)

    # ------------------------------------------------------------ one document
    @app.get("/documents/{doc_id}", response_class=HTMLResponse)
    async def document(request: Request, doc_id: int, p: int = 1):
        user = await current_user(request)
        doc = await store.get(doc_id)
        lines = doc.data.get("lines", []) if doc.data else []
        spare = 3 if doc.status == "review" else 0
        return page(request, "document.html", user, doc=doc, d=doc.data or {}, lines=lines, spare=spare,
                    page_no=max(1, min(p, doc.pages)), edits=await store.edits(doc_id),
                    flash=request.query_params.get("flash", ""))

    @app.get("/documents/{doc_id}/page/{page_no}.png")
    async def page_png(request: Request, doc_id: int, page_no: int):
        await current_user(request)
        doc = await store.get(doc_id)
        if not 1 <= page_no <= doc.pages:
            raise NotFound("page")
        path = files.path_for(settings.storage_dir, doc.sha256, doc.kind)
        png = await asyncio.to_thread(files.preview_png, path, doc.kind, page_no - 1)
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/documents/{doc_id}/file")
    async def original(request: Request, doc_id: int):
        await current_user(request)
        doc = await store.get(doc_id)
        path = files.path_for(settings.storage_dir, doc.sha256, doc.kind)
        data = await asyncio.to_thread(path.read_bytes)
        name = quote(Path(doc.filename).stem + "." + doc.kind)
        return Response(data, media_type=files.KINDS[doc.kind],
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}",
                                 "Cache-Control": "private, no-store"})

    @app.post("/documents/{doc_id}/save")
    async def save(request: Request, doc_id: int):
        user = await current_user(request)
        form = await checked_form(request)
        doc = await store.get(doc_id)
        data = form_data(form)
        today = datetime.fromtimestamp(clock(), UTC).date()
        issues = [i.as_dict() for i in checks.run(data, today)]
        duplicate = await store.find_duplicate(doc_id, data["supplier_vat"], data["number"])
        if duplicate:
            issues.insert(0, {"level": "error", "field": "number",
                              "message": f"same supplier and number as document #{duplicate}: a duplicate?"})
        try:
            await store.save_review(doc.id, user.id, data, issues, clock())
        except Conflict as exc:
            return RedirectResponse(f"/documents/{doc_id}?flash=" + quote(str(exc)), status_code=303)
        if form.get("action") == "approve":
            errors = sum(1 for i in issues if i["level"] == "error")
            if errors and form.get("accept_errors") != "1":
                return RedirectResponse(f"/documents/{doc_id}?flash=" + quote(
                    f"Saved, not approved: {errors} error(s) left. Fix them or tick “approve anyway”."),
                    status_code=303)
            await store.decide(doc_id, user.id, True, "", clock())
            nxt = await store.documents("review", limit=1)
            target = f"/documents/{nxt[0].id}?flash=" + quote(f"Document #{doc_id} approved.") if nxt else \
                "/documents?status=approved"
            return RedirectResponse(target, status_code=303)
        return RedirectResponse(f"/documents/{doc_id}?flash=Saved.", status_code=303)

    @app.post("/documents/{doc_id}/reject")
    async def reject(request: Request, doc_id: int):
        user = await current_user(request)
        form = await checked_form(request)
        try:
            await store.decide(doc_id, user.id, False, str(form.get("reason", "")).strip() or "rejected", clock())
        except Conflict as exc:
            return RedirectResponse(f"/documents/{doc_id}?flash=" + quote(str(exc)), status_code=303)
        return RedirectResponse(f"/documents/{doc_id}?flash=Rejected.", status_code=303)

    @app.post("/documents/{doc_id}/reread")
    async def reread(request: Request, doc_id: int):
        await current_user(request)
        await checked_form(request)
        try:
            await store.requeue(doc_id, clock())
        except Conflict as exc:
            return RedirectResponse(f"/documents/{doc_id}?flash=" + quote(str(exc)), status_code=303)
        return RedirectResponse(f"/documents/{doc_id}?flash=" + quote("Queued for reading again."), status_code=303)

    # ------------------------------------------------------------ export and users
    @app.get("/export.xlsx")
    async def export_xlsx(request: Request):
        await current_user(request)
        docs = await store.approved()
        reviewers = {u.id: u.name for u in await store.users()}
        data = await asyncio.to_thread(export.build, docs, reviewers)
        name = f"documents-{datetime.fromtimestamp(clock()).strftime('%Y-%m-%d')}.xlsx"
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}"',
                                 "Cache-Control": "private, no-store"})

    @app.get("/users", response_class=HTMLResponse)
    async def users(request: Request):
        user = await current_user(request)
        if user.role != "admin":
            raise Forbidden
        return page(request, "users.html", user, users=await store.users(),
                    flash=request.query_params.get("flash", ""))

    @app.post("/users")
    async def add_user(request: Request):
        user = await current_user(request)
        if user.role != "admin":
            raise Forbidden
        form = await checked_form(request)
        email, name = str(form.get("email", "")).strip(), str(form.get("name", "")).strip()
        password, role = str(form.get("password", "")), str(form.get("role", "reviewer"))
        if "@" not in email or not name or len(password) < 10 or role not in ("admin", "reviewer"):
            return RedirectResponse("/users?flash=" + quote("E-mail, name and a password of 10+ characters needed."),
                                    status_code=303)
        try:
            await store.add_user(email, name, security.hash_password(password), role, clock())
        except Conflict as exc:
            return RedirectResponse("/users?flash=" + quote(str(exc)), status_code=303)
        return RedirectResponse("/users?flash=" + quote(f"{email} added."), status_code=303)

    @app.get("/health")
    async def health():
        counts = await store.counts()
        return JSONResponse({"status": "ok", "queued": counts["queued"], "to_review": counts["review"]})

    return app
