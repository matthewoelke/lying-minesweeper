# Lying Minesweeper — Server

Stdlib-only Python HTTP server. Serves both the static UI and the JSON API
on a single port (default `8088`).

See the top-level [`README.md`](../README.md) for environment variables,
security model, and how to run.

## Module layout

| File              | Responsibility                                       |
| ----------------- | ---------------------------------------------------- |
| `app.py`          | HTTP entry, static file dispatch, request routing    |
| `routes.py`       | `/api/*` endpoint handlers, JSON response helper     |
| `logic.py`        | Pure replay / validation / week-bucket helpers       |
| `board.py`        | Deterministic board generation (mirrors `main.js`)   |
| `db.py`           | SQLite access (WAL mode)                             |
| `security.py`     | Same-origin policy, rate limiter, response headers   |
| `schema.sql`      | DB schema (auto-applied on first launch)             |
| `parity_check.py` | RNG / board oracle for client–server parity          |
| `smoke_test.py`   | End-to-end POST a winning replay and verify it lands |

## Endpoints

| Method | Path                       | Description                                                       |
| ------ | -------------------------- | ----------------------------------------------------------------- |
| GET    | `/api/health`              | liveness probe (exempt from same-origin check)                    |
| POST   | `/api/games/new`           | register `{seed, difficulty, first_click}` → `{game_id}`          |
| POST   | `/api/games/{id}/submit`   | submit `{username, action_log, outcome}` for replay verification  |
| GET    | `/api/leaderboards`        | `?kind=seed&seed=…` / `?kind=week` / `?kind=tricked` / `?kind=cheaters` |
| GET    | `/api/game-of-day`         | today's deterministic puzzle for the requested difficulty         |

Every non-health `/api/*` request must pass the same-origin check; see the
[security model](../README.md#security-model). Writes are rate-limited
per client IP via `LMS_RATE_LIMIT_PER_MIN` (default 60/min).

## Anti-cheat

Wins are fully replayed:

1. Every action must be valid (in-bounds, not a double-reveal, timestamps
   monotonic).
2. The replay must reach a terminal state (win or mine-hit loss).
3. Total duration must be ≥ the per-difficulty minimum plausible time
   (`logic.MIN_PLAUSIBLE_MS` — 1.5 s / 5 s / 15 s).

Any failure returns a silent `{ok: true}` and refreshes the username's entry
on the cheater wall (24 h cooldown to avoid spamming the table). Banned
usernames continue to receive silent successes so detection rules can't be
probed. Losses are recorded without anti-cheat — there's no incentive to
cheat one — but a "tricked" flag is only awarded when replay confirms that
the mine the player hit had a *revealed* liar as an 8-neighbor.

## Self-tests

```powershell
py -m server.parity_check     # prints RNG outputs + generated board
py -m server.smoke_test       # POST a winning replay end-to-end
```

`smoke_test` reads `LMS_SMOKE_URL` (default `http://127.0.0.1:8088`) to
target the running server.
