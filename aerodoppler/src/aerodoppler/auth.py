"""Fail-closed access control. The token exists only in the process environment."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

from .config import Settings

COOKIE = "ad_session"
SESSION_CONTEXT = b"aerodoppler-v14-session"


def session_value(token: str) -> str:
    return hmac.new(token.encode("utf-8"), SESSION_CONTEXT, hashlib.sha256).hexdigest()


def token_matches(presented: str, token: str) -> bool:
    if not presented or not token:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))


def cookie_matches(presented: str | None, token: str) -> bool:
    if not presented or not token:
        return False
    return hmac.compare_digest(presented, session_value(token))


class LoginLimiter:
    def __init__(self, limit: int = 8, window_s: float = 600.0):
        self.limit = limit
        self.window_s = window_s
        self._attempts: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        bucket = self._attempts[key]
        while bucket and moment - bucket[0] > self.window_s:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(moment)
        return True


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def request_authorized(request: Request, settings: Settings) -> bool:
    token = settings.access_token
    if token is None:
        return False
    if token_matches(bearer_token(request) or "", token):
        return True
    return cookie_matches(request.cookies.get(COOKIE), token)


def origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return False
    try:
        host = origin.split("://", 1)[1].split("/", 1)[0]
    except IndexError:
        return False
    request_host = request.headers.get("host", "")
    return host == request_host


def unauthorized(message: str = "Acceso no autorizado.", code: str = "auth", status: int = 401):
    return JSONResponse(status_code=status, content={"error": code, "message": message})
