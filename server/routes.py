"""HTTP routing layer. Pure handlers — they assume the caller has already
performed any cross-cutting concerns (CORS / same-origin / rate limiting)."""

from __future__ import annotations
import json
import re
import time
import uuid
import hmac
import hashlib
import base64
import datetime

from . import db
from . import board
from . import logic
from . import security

USERNAME_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")
SEED_RE = re.compile(r"^[A-Z2-7]{8}$")
SEED_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
DIFFICULTIES = ("beginner", "intermediate", "expert")

CHEATER_TTL_MS = 7 * 24 * 60 * 60 * 1000  # 7 days
CHEATER_COOLDOWN_MS = 24 * 60 * 60 * 1000  # 24h — don't re-log offenses within this window


def json_response(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    # CORS: only echo the Origin if it passed the same-origin check.
    # Health check requests (curl / Docker) have no Origin and that's fine.
    allowed_origin = security.validated_origin_for_cors(handler)
    if allowed_origin:
        handler.send_header("Access-Control-Allow-Origin", allowed_origin)
        handler.send_header("Vary", "Origin")
    handler.send_header("Access-Control-Allow-Credentials", "false")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    security.apply_security_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler, max_bytes=64 * 1024):
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return None
    if length > max_bytes:
        return None
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _ok(s, regex, max_len=None):
    return isinstance(s, str) and regex.match(s) and (max_len is None or len(s) <= max_len)


def _log_rejection(username, game_id, reason, extra=None):
    import sys
    extra_s = f" {extra}" if extra else ""
    sys.stderr.write(f"[reject] user={username} game={game_id} reason={reason}{extra_s}\n")
    sys.stderr.flush()


def _mark_cheater_with_cooldown(username, now_ms):
    """Add/refresh cheater entry, but only if 24h since last offense (anti-spam)."""
    db.upsert_cheater_cooldown(username, now_ms, CHEATER_TTL_MS, CHEATER_COOLDOWN_MS)


# ---- Game-of-day seed (HMAC over UTC date) -------------------------------

def game_of_day_seed(secret: bytes, when=None, difficulty: str = "intermediate"):
    when = when or datetime.datetime.utcnow().date()
    msg = (when.isoformat() + "|" + difficulty).encode("utf-8")
    digest = hmac.new(secret, msg, hashlib.sha256).digest()
    # First 5 bytes → 40 bits → 8 base32 chars
    bits = "".join(f"{b:08b}" for b in digest[:5])
    return "".join(SEED_ALPHABET[int(bits[i*5:(i+1)*5], 2)] for i in range(8))


# Center first-click per difficulty
_DAILY_FIRST_CLICK = {
    "beginner":     {"r": 4, "c": 4},   # center of 9x9
    "intermediate": {"r": 8, "c": 8},   # center of 16x16
    "expert":       {"r": 8, "c": 15},  # center of 16x30
}


# ---- Endpoints -----------------------------------------------------------

def post_new_game(handler):
    body = read_json_body(handler) or {}
    seed = body.get("seed")
    difficulty = body.get("difficulty")
    fc = body.get("first_click") or {}
    fr = fc.get("r") if isinstance(fc, dict) else None
    fcc = fc.get("c") if isinstance(fc, dict) else None

    if difficulty not in DIFFICULTIES:
        return json_response(handler, 400, {"error": "bad_difficulty"})
    if not (isinstance(seed, str) and SEED_RE.match(seed)):
        return json_response(handler, 400, {"error": "bad_seed"})
    cfg = board.DIFFICULTIES[difficulty]
    if not (isinstance(fr, int) and isinstance(fcc, int)
            and 0 <= fr < cfg["rows"] and 0 <= fcc < cfg["cols"]):
        return json_response(handler, 400, {"error": "bad_first_click"})

    game_id = uuid.uuid4().hex
    db.insert_open_game(game_id, seed, difficulty, fr, fcc)
    return json_response(handler, 200, {"game_id": game_id})


def _verify_loss_minimal(b, action_log):
    """Lightweight loss verifier: returns (tricked, duration_ms).

    No cheat detection — we just walk the log to find the last reveal that
    hit a mine and check whether any 8-neighbor of that mine is a liar.
    Falls back to (False, 0) if the log is malformed.
    """
    if not isinstance(action_log, list) or not action_log:
        return False, 0
    rows, cols = b["rows"], b["cols"]
    cells = b["cells"]
    last_t = 0
    hit_rc = None
    for act in action_log:
        if not isinstance(act, dict):
            continue
        t = act.get("t")
        if isinstance(t, (int, float)) and t >= 0:
            last_t = max(last_t, t)
        if act.get("type") != "reveal":
            continue
        r, c = act.get("r"), act.get("c")
        if not (isinstance(r, int) and isinstance(c, int)):
            continue
        if not (0 <= r < rows and 0 <= c < cols):
            continue
        if cells[r][c]["is_mine"]:
            hit_rc = (r, c)
    tricked = False
    if hit_rc is not None:
        hr, hc = hit_rc
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = hr + dr, hc + dc
                if 0 <= nr < rows and 0 <= nc < cols and cells[nr][nc]["is_liar"]:
                    tricked = True
                    break
            if tricked:
                break
    return tricked, last_t


def post_submit(handler, game_id):
    body = read_json_body(handler) or {}
    username = body.get("username")
    action_log = body.get("action_log")
    client_outcome = body.get("outcome")  # "won" | "lost" — client hint

    if not _ok(username, USERNAME_RE):
        return json_response(handler, 400, {"error": "bad_username"})
    if not isinstance(action_log, list):
        return json_response(handler, 400, {"error": "bad_log"})

    game = db.get_game(game_id)
    if not game:
        return json_response(handler, 404, {"error": "game_not_found"})
    if game["status"] != "open":
        return json_response(handler, 409, {"error": "game_closed"})

    now = int(time.time() * 1000)

    # --- LOSS PATH: no anti-cheat. There's no incentive to cheat a loss
    # (you don't compete on time when you die), so we just record it.
    # We still run the full replay so the "tricked" flag is honest — a player
    # only counts as tricked if a *revealed* liar tile was adjacent to the
    # mine they clicked. If the log is malformed, we fall back to the
    # minimal walker (no trick credit, just record a duration).
    if client_outcome == "lost":
        b = board.generate_board(
            game["seed"], game["difficulty"],
            game["first_click_r"], game["first_click_c"],
        )
        replay = logic.replay(b, action_log)
        if replay.get("ok") and replay.get("lost"):
            tricked = bool(replay.get("tricked"))
            duration_ms = int(replay.get("duration_ms", 0))
        else:
            # Bad log on a loss — keep the score (don't punish the player) but
            # don't award trick credit since we can't verify it.
            _, duration_ms = _verify_loss_minimal(b, action_log)
            tricked = False
            _log_rejection(
                username, game_id, "loss_replay_failed",
                extra=replay.get("reason"),
            )
        db.insert_score({
            "game_id": game_id, "username": username,
            "difficulty": game["difficulty"], "seed": game["seed"],
            "duration_ms": int(duration_ms),
            "won": 0,
            "tricked": 1 if tricked else 0,
            "liar_count": b["liar_count"],
            "lie_rate_bp": int(round(b["lie_rate"] * 10000)),
            "submitted_at": now,
            "week_bucket": logic.iso_week_bucket(now),
        })
        db.close_game(game_id, "verified")
        return json_response(handler, 200, {"ok": True, "verified": True})

    # --- WIN PATH: full replay + min-time gate + cheater logic.
    # Cheater silent-success: pretend OK, refresh entry (with 24h cooldown)
    if db.is_cheater(username):
        _mark_cheater_with_cooldown(username, now)
        _log_rejection(username, game_id, "already_cheater")
        return json_response(handler, 200, {"ok": True})

    # Reconstruct board and replay
    b = board.generate_board(
        game["seed"], game["difficulty"], game["first_click_r"], game["first_click_c"]
    )
    result = logic.replay(b, action_log)

    if not result.get("ok"):
        db.close_game(game_id, "rejected")
        _mark_cheater_with_cooldown(username, now)
        _log_rejection(username, game_id, "replay_failed", extra=result.get("reason"))
        return json_response(handler, 200, {"ok": True})  # silent success

    if not result.get("won"):
        # Client claimed a win but replay says they lost. Reject quietly.
        db.close_game(game_id, "rejected")
        _mark_cheater_with_cooldown(username, now)
        _log_rejection(username, game_id, "win_claim_mismatch")
        return json_response(handler, 200, {"ok": True})

    duration_ms = result["duration_ms"]
    if not logic.validate_min_time(game["difficulty"], duration_ms):
        db.close_game(game_id, "rejected")
        _mark_cheater_with_cooldown(username, now)
        _log_rejection(username, game_id, "min_time_violation",
                       extra=f"duration_ms={duration_ms} diff={game['difficulty']}")
        return json_response(handler, 200, {"ok": True})

    db.insert_score({
        "game_id": game_id,
        "username": username,
        "difficulty": game["difficulty"],
        "seed": game["seed"],
        "duration_ms": duration_ms,
        "won": 1 if result["won"] else 0,
        "tricked": 1 if result.get("tricked") else 0,
        "liar_count": b["liar_count"],
        "lie_rate_bp": int(round(b["lie_rate"] * 10000)),
        "submitted_at": now,
        "week_bucket": logic.iso_week_bucket(now),
    })
    db.close_game(game_id, "verified")
    return json_response(handler, 200, {"ok": True, "verified": True})


def get_leaderboards(handler, query):
    """query is a dict from parse_qs."""
    kind = (query.get("kind") or ["seed"])[0]
    difficulty = (query.get("difficulty") or ["intermediate"])[0]
    if difficulty not in DIFFICULTIES:
        return json_response(handler, 400, {"error": "bad_difficulty"})
    limit = 20
    try:
        limit = max(1, min(50, int((query.get("limit") or ["20"])[0])))
    except Exception:
        pass

    if kind == "seed":
        seed = (query.get("seed") or [""])[0]
        if not SEED_RE.match(seed):
            return json_response(handler, 400, {"error": "bad_seed"})
        return json_response(handler, 200, {
            "kind": "seed", "seed": seed, "difficulty": difficulty,
            "entries": db.query_seed_top_n(seed, difficulty, limit),
        })
    if kind == "week":
        bucket = (query.get("week") or [logic.iso_week_bucket(int(time.time() * 1000))])[0]
        return json_response(handler, 200, {
            "kind": "week", "week": bucket, "difficulty": difficulty,
            "entries": db.query_week_top_n(bucket, difficulty, limit),
        })
    if kind == "tricked":
        bucket = (query.get("week") or [logic.iso_week_bucket(int(time.time() * 1000))])[0]
        return json_response(handler, 200, {
            "kind": "tricked", "week": bucket, "difficulty": difficulty,
            "entries": db.query_week_tricked(bucket, difficulty, limit),
        })
    if kind == "cheaters":
        return json_response(handler, 200, {
            "kind": "cheaters",
            "entries": db.query_cheaters(limit),
        })
    return json_response(handler, 400, {"error": "bad_kind"})


def get_game_of_day(handler, secret, query=None):
    difficulty = "intermediate"
    if query:
        difficulty = (query.get("difficulty") or ["intermediate"])[0]
        if difficulty not in DIFFICULTIES:
            difficulty = "intermediate"
    seed = game_of_day_seed(secret, difficulty=difficulty)
    return json_response(handler, 200, {
        "seed": seed,
        "difficulty": difficulty,
        "first_click": _DAILY_FIRST_CLICK[difficulty],
        "date_utc": datetime.datetime.utcnow().date().isoformat(),
    })
