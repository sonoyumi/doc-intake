"""Settings from .env in the current folder. Every mistake is reported at once."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

AI_MODES = ("off", "claude")


class ConfigError(Exception):
    """Wrong or missing settings; the message lists every problem."""


@dataclass(frozen=True)
class Settings:
    secret_key: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8097
    cookie_secure: bool = False
    data_dir: Path = Path("data")
    company_name: str = "Doc intake"
    ai_mode: str = "off"  # off = text layer of PDFs only; claude = Claude reads scans and photos too
    anthropic_api_key: str | None = field(default=None, repr=False)
    claude_model: str = "claude-opus-5-5"
    ai_daily_limit: int = 200  # documents per day sent to Claude: a hard cap on the bill
    max_file_mb: int = 15
    max_pages: int = 10

    @property
    def database_path(self) -> Path:
        return self.data_dir / "doc-intake.db"

    @property
    def storage_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"


def _int(name: str, default: int, errors: list[str], low: int = 1, high: int = 10**9) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name}: нужно целое число, получено {raw!r}")
        return default
    if not low <= value <= high:
        errors.append(f"{name}: допустимо от {low} до {high}, получено {value}")
        return default
    return value


def load_settings(env_file: Path | None = Path(".env")) -> Settings:
    if env_file is not None:
        load_dotenv(env_file)
    errors: list[str] = []
    secret = os.getenv("SECRET_KEY", "").strip()
    if len(secret) < 32:
        errors.append("SECRET_KEY: случайная строка не короче 32 символов "
                      "(python3 -c \"import secrets; print(secrets.token_hex(32))\")")
    ai_mode = (os.getenv("AI_MODE", "").strip() or "off").lower()
    key = os.getenv("ANTHROPIC_API_KEY", "").strip() or None
    if ai_mode not in AI_MODES:
        errors.append(f"AI_MODE: одно из {', '.join(AI_MODES)}, получено {ai_mode!r}")
    elif ai_mode == "claude" and not key:
        errors.append("AI_MODE=claude: нужен ANTHROPIC_API_KEY (или AI_MODE=off — только PDF с текстовым слоем)")
    settings = Settings(
        secret_key=secret,
        host=os.getenv("HOST", "").strip() or "127.0.0.1",
        port=_int("PORT", 8097, errors, 1, 65535),
        cookie_secure=os.getenv("COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes"},
        data_dir=Path(os.getenv("DATA_DIR", "").strip() or "data"),
        company_name=os.getenv("COMPANY_NAME", "").strip() or "Doc intake",
        ai_mode=ai_mode,
        anthropic_api_key=key,
        claude_model=os.getenv("CLAUDE_MODEL", "").strip() or "claude-opus-5-5",
        ai_daily_limit=_int("AI_DAILY_LIMIT", 200, errors, 0),
        max_file_mb=_int("MAX_FILE_MB", 15, errors, 1, 30),
        max_pages=_int("MAX_PAGES", 10, errors, 1, 50),
    )
    if errors:
        raise ConfigError("Ошибки в .env:\n  - " + "\n  - ".join(errors))
    return settings
