"""Single-process HTTP entry point for Lying Minesweeper.

Serves both the static UI (``index.html`` / ``main.js`` / ``style.css``) and
the JSON API (``/api/*``) on one port. Uses only the stdlib.

Run with::

    py -m server.app

Environment variables:

* ``LMS_HOST``     — bind address (default ``0.0.0.0``)
* ``LMS_PORT``     — listening port (default ``8088``)
* ``LMS_DB_PATH``  — SQLite path (default ``server/leaderboard.db``)
"""

from __future__ import annotations
import os
import sys
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db
from . import routes
from . import security


SECRET_KEY = "hmac_secret"
HOST = os.environ.get("LMS_HOST", "0.0.0.0")
PORT = int(os.environ.get("LMS_PORT", "8088"))

# Static files live at the repo root, one level above this package.
STATIC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Whitelist of static assets the server will serve. Anything outside this
# set returns 404 — no directory traversal, no accidental disclosure of
# server/*.py or *.db files.
STATIC_FILES = {
    "/":           ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/main.js":    ("main.js",    "application/javascript; charset=utf-8"),
    "/style.css":  ("style.css",  "text/css; charset=utf-8"),
}


def _load_or_create_secret() -> bytes:
    s = db.get_meta(SECRET_KEY)
    if not s:
        s = secrets.token_hex(32)
        db.set_meta(SECRET_KEY, s)
    return s.encode("utf-8")


SERVER_SECRET: bytes = b""


class Handler(BaseHTTPRequestHandler):
    server_version = "LyingMinesweeper/1.0"

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("[server] " + (fmt % args) + "\n")

    def do_OPTIONS(self):
        # CORS preflight. Same-origin policy still applies — only respond
        # affirmatively to allowed origins, otherwise 403.
        allowed_origin = security.validated_origin_for_cors(self)
        if not allowed_origin:
            return routes.json_response(self, 403, {"error": "forbidden"})
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", allowed_origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        security.apply_security_headers(self)
        self.end_headers()

    def _serve_static(self, path: str) -> bool:
        entry = STATIC_FILES.get(path)
        if not entry:
            return False
        rel, ctype = entry
        full = os.path.join(STATIC_ROOT, rel)
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            return False
        is_html = ctype.startswith("text/html")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        security.apply_security_headers(self, is_html=is_html)
        self.end_headers()
        self.wfile.write(body)
        return True

    def _enforce_api_policy(self, path: str, *, is_write: bool) -> bool:
        """Run same-origin + rate-limit checks for /api/*. Health is exempt.
        Returns True if the request may proceed; otherwise writes a response
        and returns False.
        """
        if path == "/api/health":
            return True
        if not security.is_same_origin(self):
            routes.json_response(self, 403, {"error": "forbidden"})
            return False
        if is_write and not security.write_limiter.allow(security.client_ip(self)):
            routes.json_response(self, 429, {"error": "rate_limited"})
            return False
        return True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        # API routes
        if path.startswith("/api/"):
            if not self._enforce_api_policy(path, is_write=False):
                return
            if path == "/api/health":
                return routes.json_response(self, 200, {"ok": True})
            if path == "/api/leaderboards":
                return routes.get_leaderboards(self, query)
            if path == "/api/game-of-day":
                return routes.get_game_of_day(self, SERVER_SECRET, query)
            return routes.json_response(self, 404, {"error": "not_found"})

        # Static UI
        if self._serve_static(path):
            return

        return routes.json_response(self, 404, {"error": "not_found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/"):
            if not self._enforce_api_policy(path, is_write=True):
                return
            if path == "/api/games/new":
                return routes.post_new_game(self)
            if path.startswith("/api/games/") and path.endswith("/submit"):
                game_id = path[len("/api/games/"):-len("/submit")]
                if game_id:
                    return routes.post_submit(self, game_id)

        return routes.json_response(self, 404, {"error": "not_found"})


def main():
    global SERVER_SECRET
    db.init_db()
    SERVER_SECRET = _load_or_create_secret()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write(f"[server] Listening on http://{HOST}:{PORT}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
