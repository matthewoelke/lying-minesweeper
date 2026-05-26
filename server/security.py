"""Security helpers: same-origin check and per-IP rate limiting.

The same-origin check uses `Sec-Fetch-Site` (preferred — sent by all modern
browsers on every fetch) with a fallback to `Origin` and `Referer` for older
clients. External callers (curl, scripts, other sites) will fail all three.
"""

from __future__ import annotations
import os
import threading
import time
from urllib.parse import urlparse


# -- Same-origin policy ----------------------------------------------------

def _allowed_origin_hosts() -> set[str]:
    raw = os.environ.get("LMS_ALLOWED_ORIGINS", "")
    out: set[str] = set()
    for s in raw.split(","):
        s = s.strip()
        if not s:
            continue
        p = urlparse(s if "://" in s else "http://" + s)
        if p.hostname:
            host = p.hostname.lower()
            if p.port:
                host = f"{host}:{p.port}"
            out.add(host)
    return out


_ALLOWED_HOSTS = _allowed_origin_hosts()


def _host_of(value: str) -> str | None:
    if not value:
        return None
    try:
        p = urlparse(value if "://" in value else "http://" + value)
    except Exception:
        return None
    if not p.hostname:
        return None
    host = p.hostname.lower()
    if p.port:
        host = f"{host}:{p.port}"
    return host


def is_same_origin(handler) -> bool:
    """True if the request appears to originate from the same site that's
    serving us (or from an explicitly allow-listed origin).

    Rules:
      * If ``Sec-Fetch-Site`` is present, it MUST be ``same-origin``.
        (Modern browsers always send this; curl/scripts do not.)
      * Otherwise fall back to Origin → Referer; their host must match the
        request's ``Host`` header or be in ``LMS_ALLOWED_ORIGINS``.
      * Missing both signals = reject.
    """
    headers = handler.headers
    host = (headers.get("Host") or "").lower()
    allowed = {host} | _ALLOWED_HOSTS

    sfs = headers.get("Sec-Fetch-Site")
    if sfs is not None:
        return sfs == "same-origin"

    origin = headers.get("Origin")
    if origin:
        return _host_of(origin) in allowed

    referer = headers.get("Referer")
    if referer:
        return _host_of(referer) in allowed

    return False


def validated_origin_for_cors(handler) -> str | None:
    """Return the Origin header iff it passes the same-origin check.

    Used to echo the Origin back in `Access-Control-Allow-Origin` (never `*`).
    """
    origin = handler.headers.get("Origin")
    if not origin:
        return None
    return origin if is_same_origin(handler) else None


# -- Rate limit (token bucket per client IP) -------------------------------

class _Bucket:
    __slots__ = ("tokens", "updated_at")

    def __init__(self, tokens: float, t: float):
        self.tokens = tokens
        self.updated_at = t


class RateLimiter:
    def __init__(self, per_minute: int):
        self.capacity = float(max(1, per_minute))
        self.refill_per_sec = self.capacity / 60.0
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._last_gc = time.monotonic()

    def allow(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(ip)
            if b is None:
                b = _Bucket(self.capacity, now)
                self._buckets[ip] = b
            else:
                elapsed = now - b.updated_at
                b.tokens = min(self.capacity, b.tokens + elapsed * self.refill_per_sec)
                b.updated_at = now
            allowed = b.tokens >= 1.0
            if allowed:
                b.tokens -= 1.0
            if now - self._last_gc > 300:
                self._gc(now)
                self._last_gc = now
            return allowed

    def _gc(self, now: float):
        # Drop fully-refilled idle buckets to keep memory bounded.
        stale = [
            ip for ip, b in self._buckets.items()
            if b.tokens >= self.capacity - 0.001 and now - b.updated_at > 300
        ]
        for ip in stale:
            self._buckets.pop(ip, None)


def client_ip(handler) -> str:
    return handler.client_address[0] if handler.client_address else "unknown"


_DEFAULT_RATE = int(os.environ.get("LMS_RATE_LIMIT_PER_MIN", "60"))
write_limiter = RateLimiter(_DEFAULT_RATE)


# -- Security response headers --------------------------------------------

# Applied to every response. CSP is sent only on HTML so non-HTML responses
# don't carry irrelevant directives.
COMMON_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), interest-cohort=()",
}

HTML_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'none'"
)


def apply_security_headers(handler, *, is_html: bool = False):
    for k, v in COMMON_SECURITY_HEADERS.items():
        handler.send_header(k, v)
    if is_html:
        handler.send_header("Content-Security-Policy", HTML_CSP)
