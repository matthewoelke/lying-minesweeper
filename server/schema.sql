-- Lying Minesweeper leaderboard schema.
-- This file is intentionally written to be portable between SQLite (local)
-- and Cloudflare D1 (future) without changes.

CREATE TABLE IF NOT EXISTS games (
  id              TEXT PRIMARY KEY,           -- uuid4 hex
  seed            TEXT NOT NULL,              -- 8-char base32
  difficulty      TEXT NOT NULL,              -- beginner|intermediate|expert
  first_click_r   INTEGER NOT NULL,
  first_click_c   INTEGER NOT NULL,
  created_at      INTEGER NOT NULL,           -- unix ms (server clock)
  closed_at       INTEGER,                    -- unix ms once submit succeeds
  status          TEXT NOT NULL DEFAULT 'open' -- open|verified|rejected
);
CREATE INDEX IF NOT EXISTS idx_games_seed ON games(seed, difficulty);

CREATE TABLE IF NOT EXISTS scores (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id         TEXT NOT NULL REFERENCES games(id),
  username        TEXT NOT NULL,              -- already validated regex
  difficulty      TEXT NOT NULL,
  seed            TEXT NOT NULL,
  duration_ms     INTEGER NOT NULL,           -- server-derived from action log
  won             INTEGER NOT NULL,           -- 0|1
  tricked         INTEGER NOT NULL DEFAULT 0, -- 1 if loss was on a tile adjacent to a liar
  liar_count      INTEGER NOT NULL DEFAULT 0,
  lie_rate_bp     INTEGER NOT NULL DEFAULT 0, -- basis points (0.01%)
  submitted_at    INTEGER NOT NULL,           -- unix ms
  week_bucket     TEXT NOT NULL               -- ISO week, e.g. '2026-W22'
);
CREATE INDEX IF NOT EXISTS idx_scores_seed_dur  ON scores(seed, difficulty, won, duration_ms, submitted_at);
CREATE INDEX IF NOT EXISTS idx_scores_week_dur  ON scores(week_bucket, difficulty, won, duration_ms, submitted_at);
CREATE INDEX IF NOT EXISTS idx_scores_week_user ON scores(week_bucket, username);

CREATE TABLE IF NOT EXISTS cheaters (
  username        TEXT PRIMARY KEY,
  first_offense_at INTEGER NOT NULL,
  last_offense_at  INTEGER NOT NULL,
  offense_count    INTEGER NOT NULL DEFAULT 1,
  expires_at       INTEGER NOT NULL            -- unix ms; row purged after this
);
CREATE INDEX IF NOT EXISTS idx_cheaters_expiry ON cheaters(expires_at, last_offense_at);

CREATE TABLE IF NOT EXISTS server_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
