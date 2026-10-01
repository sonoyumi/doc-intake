"""Passwords, signed session cookies and CSRF tokens — standard library only.

* Passwords: scrypt (memory-hard, so guessing on GPUs is expensive) with a random salt per user.
* Session cookie: ``<user_id>.<expires>.<signature>``; the signature is HMAC-SHA256 with SECRET_KEY,
  so a user cannot change the id or extend the expiry without the key.
* CSRF token: HMAC of the session cookie itself. Another site can make the browser send the cookie,
  but it cannot read the page, so it cannot know the token that every form must carry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
SESSION_TTL = 12 * 3600  # one working day


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _sign(key: str, message: str) -> str:
    return _b64(hmac.new(key.encode(), message.encode(), hashlib.sha256).digest())


def make_session(key: str, user_id: int, now: float | None = None, ttl: int = SESSION_TTL) -> str:
    expires = int((now if now is not None else time.time()) + ttl)
    body = f"{user_id}.{expires}"
    return f"{body}.{_sign(key, 'session:' + body)}"


def read_session(key: str, cookie: str | None, now: float | None = None) -> int | None:
    """User id from a valid, unexpired cookie; otherwise None."""
    if not cookie or cookie.count(".") != 2:
        return None
    op, expires, signature = cookie.split(".")
    if not hmac.compare_digest(signature, _sign(key, f"session:{op}.{expires}")):
        return None
    try:
        if int(expires) < (now if now is not None else time.time()):
            return None
        return int(op)
    except ValueError:
        return None


def csrf_token(key: str, cookie: str) -> str:
    return _sign(key, "csrf:" + cookie)


def check_csrf(key: str, cookie: str | None, token: str | None) -> bool:
    return bool(cookie and token) and hmac.compare_digest(csrf_token(key, cookie), token)
