"""The shared-password gate in front of the whole server.

A shared secret for one laptop on one local network, handed out verbally at the
field; not a user system. Browsers sign in once and hold a signed cookie. The
CLI sends `Authorization: Bearer <secret>`, where the secret is either the
password or the per-process token `apex serve` writes for local tools.

* Comparisons use `hmac.compare_digest`.
* Sessions are signed with a random per-process key, so restarting the server
  (perhaps with a new password) signs everyone out.
* Repeated failures from one address are throttled.

The QR code `apex serve` prints carries the password as `?key=`, so one scan
signs a phone in. The gate swaps it for a session cookie and redirects to the
same URL without it, so the key leaves the address bar before anything renders
and is not kept in history or bookmarks. `apex serve --no-qr-login` omits it.
"""

import base64
import hmac
import secrets
import threading
import time
from collections import defaultdict
from hashlib import sha256
from urllib.parse import quote, urlencode

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

COOKIE_NAME = "apex_session"
KEY_PARAM = "key"
MIN_PASSWORD_LENGTH = 4
SESSION_HOURS = 16.0

MAX_FAILURES = 8
FAILURE_WINDOW_SEC = 300.0
LOCKOUT_SEC = 60.0

PUBLIC_PATHS = frozenset({"/login", "/healthz"})
PUBLIC_PREFIXES = ("/static/",)


class WeakPassword(ValueError):
    pass


def check_password_usable(password: str) -> str:
    if not password or not password.strip():
        raise WeakPassword("the password may not be empty")
    if password != password.strip():
        raise WeakPassword("the password has leading or trailing whitespace")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"the password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


class Auth:
    def __init__(self, password: str, *, session_hours: float = SESSION_HOURS):
        check_password_usable(password)
        self._digest = sha256(password.encode()).digest()
        self._secret = secrets.token_bytes(32)
        self.local_token = secrets.token_urlsafe(24)
        self._ttl_sec = session_hours * 3600.0
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def password_matches(self, offered: str) -> bool:
        return hmac.compare_digest(self._digest, sha256(offered.encode()).digest())

    def bearer_valid(self, offered: str) -> bool:
        token_ok = hmac.compare_digest(self.local_token.encode(), offered.encode())
        return token_ok or self.password_matches(offered)

    # ── sessions ─────────────────────────────────────────────────────────────

    def issue_session(self) -> str:
        expiry = str(int(time.time() + self._ttl_sec))
        return f"{expiry}.{self._sign(expiry)}"

    def session_valid(self, token: str | None) -> bool:
        if not token:
            return False
        expiry, _, mac = token.partition(".")
        if not expiry.isdigit() or not hmac.compare_digest(self._sign(expiry), mac):
            return False
        return int(expiry) > time.time()

    def _sign(self, payload: str) -> str:
        raw = hmac.new(self._secret, payload.encode(), sha256).digest()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    @property
    def max_age_sec(self) -> int:
        return int(self._ttl_sec)

    # ── throttle ─────────────────────────────────────────────────────────────

    def lockout_remaining(self, client: str) -> float:
        now = time.monotonic()
        with self._lock:
            recent = self._prune(client, now)
            if len(recent) < MAX_FAILURES:
                return 0.0
            return max(0.0, recent[-1] + LOCKOUT_SEC - now)

    def record_failure(self, client: str) -> None:
        with self._lock:
            self._prune(client, time.monotonic()).append(time.monotonic())

    def clear_failures(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)

    def _prune(self, client: str, now: float) -> list[float]:
        kept = [t for t in self._failures[client] if now - t < FAILURE_WINDOW_SEC]
        self._failures[client] = kept
        return kept


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def set_session(response: Response, auth: Auth) -> Response:
    # Not Secure: the field laptop serves plain HTTP on a LAN.
    response.set_cookie(
        COOKIE_NAME,
        auth.issue_session(),
        max_age=auth.max_age_sec,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


def safe_next(target: str | None) -> str:
    """A post-login redirect restricted to a path on this server."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


class PasswordGate(BaseHTTPMiddleware):
    def __init__(self, app, auth: Auth):
        super().__init__(app)
        self.auth = auth

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        if (offered := request.query_params.get(KEY_PARAM)) is not None:
            return self._key_login(request, offered)

        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            client = client_key(request)
            if self.auth.lockout_remaining(client) > 0:
                return JSONResponse({"detail": "too many failed attempts"}, status_code=429)
            if self.auth.bearer_valid(header[7:].strip()):
                self.auth.clear_failures(client)
                return await call_next(request)
            self.auth.record_failure(client)
            return JSONResponse({"detail": "wrong password or token"}, status_code=401)

        if self.auth.session_valid(request.cookies.get(COOKIE_NAME)):
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(path)}", status_code=303)

    def _key_login(self, request: Request, offered: str) -> Response:
        client = client_key(request)
        if (delay := self.auth.lockout_remaining(client)) > 0:
            return RedirectResponse(f"/login?locked={max(1, int(delay))}", status_code=303)
        if not self.auth.password_matches(offered):
            self.auth.record_failure(client)
            return RedirectResponse("/login?error=1", status_code=303)
        self.auth.clear_failures(client)
        kept = [(k, v) for k, v in request.query_params.multi_items() if k != KEY_PARAM]
        clean = request.url.path + (f"?{urlencode(kept)}" if kept else "")
        return set_session(RedirectResponse(clean, status_code=303), self.auth)
