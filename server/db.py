"""Thin SQLite wrapper. Each function does ONE query.

Designed so the Worker port replaces this entire file with a D1 equivalent
(``env.DB.prepare(...).bind(...).run()/.all()``) without touching routes.py.
"""

from __future__ import annotations
import sqlite3
import os
import time

_DB_PATH = os.environ.get("LMS_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "leaderboard.db"
)


def _connect():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as f:
        ddl = f.read()
    with _connect() as conn:
        conn.executescript(ddl)


def get_meta(key: str):
    with _connect() as conn:
        row = conn.execute("SELECT value FROM server_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO server_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def insert_open_game(game_id: str, seed: str, difficulty: str, fr: int, fc: int):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO games (id, seed, difficulty, first_click_r, first_click_c, "
            "created_at, status) VALUES (?, ?, ?, ?, ?, ?, 'open')",
            (game_id, seed, difficulty, fr, fc, int(time.time() * 1000)),
        )


def get_game(game_id: str):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        return dict(row) if row else None


def close_game(game_id: str, status: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE games SET status = ?, closed_at = ? WHERE id = ?",
            (status, int(time.time() * 1000), game_id),
        )


def insert_score(score: dict):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO scores (game_id, username, difficulty, seed, duration_ms, "
            "won, tricked, liar_count, lie_rate_bp, submitted_at, week_bucket) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                score["game_id"], score["username"], score["difficulty"], score["seed"],
                score["duration_ms"], score["won"], score["tricked"], score["liar_count"],
                score["lie_rate_bp"], score["submitted_at"], score["week_bucket"],
            ),
        )


def query_seed_top_n(seed: str, difficulty: str, limit: int = 20):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT username, duration_ms, submitted_at, liar_count, tricked "
            "FROM scores WHERE seed = ? AND difficulty = ? AND won = 1 "
            "ORDER BY duration_ms ASC, submitted_at ASC LIMIT ?",
            (seed, difficulty, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def query_week_top_n(week_bucket: str, difficulty: str, limit: int = 20):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT username, seed, duration_ms, submitted_at, tricked "
            "FROM scores WHERE week_bucket = ? AND difficulty = ? AND won = 1 "
            "ORDER BY duration_ms ASC, submitted_at ASC LIMIT ?",
            (week_bucket, difficulty, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def query_week_tricked(week_bucket: str, difficulty: str, limit: int = 20):
    """Most-tricked board: counts losses where tricked=1 in the given week."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT username, COUNT(*) AS trick_count "
            "FROM scores WHERE week_bucket = ? AND difficulty = ? AND tricked = 1 "
            "GROUP BY username ORDER BY trick_count DESC, username ASC LIMIT ?",
            (week_bucket, difficulty, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_cheater_cooldown(username: str, now_ms: int, ttl_ms: int, cooldown_ms: int):
    """Insert or refresh cheater entry but only if last_offense_at is older
    than cooldown_ms ago. Offense count is left alone — we just bump the
    timestamp so the user re-appears at the top of the wall."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_offense_at FROM cheaters WHERE username = ?",
            (username,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO cheaters (username, first_offense_at, last_offense_at, "
                "offense_count, expires_at) VALUES (?, ?, ?, 1, ?)",
                (username, now_ms, now_ms, now_ms + ttl_ms),
            )
            return
        if now_ms - row["last_offense_at"] < cooldown_ms:
            # Within cooldown — do nothing (avoid DB spam)
            return
        conn.execute(
            "UPDATE cheaters SET last_offense_at = ?, expires_at = ? WHERE username = ?",
            (now_ms, now_ms + ttl_ms, username),
        )


def query_cheaters(limit: int = 20):
    now_ms = int(time.time() * 1000)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT username, last_offense_at, first_offense_at "
            "FROM cheaters WHERE expires_at > ? "
            "ORDER BY last_offense_at DESC LIMIT ?",
            (now_ms, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def is_cheater(username: str) -> bool:
    now_ms = int(time.time() * 1000)
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM cheaters WHERE username = ? AND expires_at > ?",
            (username, now_ms),
        ).fetchone()
        return row is not None
