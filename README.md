# Lying Minesweeper

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Container image](https://img.shields.io/badge/image-ghcr.io-blue?logo=docker)](#run-with-docker)
[![Python: 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/)
[![No deps](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen)](#)

Browser-based Minesweeper variant where a small fraction of numbered tiles
**lie** — they display a value that's ±1 off the true adjacent-mine count.
All other Minesweeper mechanics are preserved.

The game ships as a small stdlib-only Python HTTP server that serves the
static UI (`index.html`, `style.css`, `main.js`) and a JSON API
(`/api/*`) on a single port.

## Quick start

```powershell
cd lying-minesweeper
py -m server.app
# Open http://localhost:8088
```

The first launch creates `server/leaderboard.db` (SQLite, WAL mode) and
generates an HMAC secret used for the daily-puzzle signature.

### Configuration

| Env var                    | Default                       | Notes                                              |
| -------------------------- | ----------------------------- | -------------------------------------------------- |
| `LMS_HOST`                 | `0.0.0.0`                     | Bind address                                       |
| `LMS_PORT`                 | `8088`                        | Listening port                                     |
| `LMS_DB_PATH`              | `server/leaderboard.db`       | SQLite file path                                   |
| `LMS_RATE_LIMIT_PER_MIN`   | `60`                          | Per-IP token bucket for `POST /api/*`              |
| `LMS_ALLOWED_ORIGINS`      | *(empty)*                     | Extra hostnames allowed past the same-origin check |

### Self-tests

```powershell
py -m server.parity_check     # RNG / board-generation oracle
py -m server.smoke_test       # POST a winning replay end-to-end
```

## Run with Docker

A multi-arch image (`linux/amd64`, `linux/arm64`) is published to GitHub
Container Registry on every tagged release:

```bash
docker run --rm -p 8088:8088 -v lms-data:/data \
  ghcr.io/OWNER/lying-minesweeper:latest
# Open http://localhost:8088
```

Replace `OWNER` with the GitHub user/org that owns the repo.

Or with the bundled compose file (clones data into `./data`):

```bash
docker compose up -d
```

The image is non-root, exposes `8088`, persists SQLite under `/data`, and
ships a `HEALTHCHECK` against `/api/health`. Override any of the env vars
from the table above to tune host/port/DB path/rate limit/allowed origins.

## Publishing (manual GitHub upload)

You don't need git installed locally. Use github.com directly:

1. **Create the repo**
   - github.com → **New repository** → name it `lying-minesweeper`.
   - Visibility: **Public** (required for free GHCR pulls without auth).
   - **Don't** initialize with README/license — this repo already has them.

2. **Upload the project files**
   - On the empty-repo landing page click **"uploading an existing file"**.
   - Drag the project folder into the browser. **Skip these** (they're
     runtime artifacts, not source):
     - `server/__pycache__/`
     - `server/leaderboard.db`, `server/leaderboard.db-shm`,
       `server/leaderboard.db-wal`
     - any local `data/` folder created by `docker compose`
   - Commit directly to `main` with message `Initial commit`.

3. **Cut the first release to build the image**
   - **Releases → Create a new release → Choose a tag → `v1.0.0` →
     Publish release**.
   - This pushes the tag, which triggers
     `.github/workflows/publish-image.yml`. Watch it under the
     **Actions** tab — first build takes ~3–5 minutes.
   - The image appears at
     `ghcr.io/<your-user>/lying-minesweeper:v1.0.0` (and `latest`,
     `1`, `1.0`, plus a short-SHA tag).

4. **One-time: make the package public**
   - Your GitHub profile → **Packages** tab → `lying-minesweeper` →
     **Package settings** → **Change visibility** → **Public**. Without
     this step, `docker pull` requires `docker login ghcr.io`.

Subsequent releases: bump the tag (`v1.0.1`, `v1.1.0`, …) via the
**Releases** UI and the workflow republishes automatically.

## How the game works

- **Lie rate**: rolled fresh each game, capped per difficulty (≈15 % beginner,
  ≈10 % intermediate / expert). Liar count is shown post-game.
- **Liar placement** keeps every game solvable:
  1. No two liars are 8-adjacent (every liar has truthful neighbors).
  2. Every liar's adjacent unrevealed cells are also touched by a truthful
     numbered tile (over-constraint guarantee).
  3. No liars in the first-click safe zone or its immediate frontier.
- **First click is always safe** — and so are its 8 neighbors. Mines are
  placed lazily after the first click.
- **Cascade reveal** uses the *true* adjacency count, not the displayed
  value, so a true-1 tile that lies as 0 does not auto-cascade into a mine.
- **Win**: reveal every non-mine cell.

## Controls

| Action        | Desktop                    | Touch                  |
| ------------- | -------------------------- | ---------------------- |
| Reveal        | Left-click                 | Short tap              |
| Flag / unflag | Right-click                | Long-press (≥ 0.35 s)  |
| New game      | Button or difficulty change | Same                   |

## Leaderboards & daily puzzle

The server tracks per-seed, weekly, and "most tricked" leaderboards plus a
silent cheater wall. The daily puzzle seed is HMAC'd from the UTC date with
the server's secret, so every player gets the same board.

## Security model

The API is intended to be reachable **only** from the game UI served from
the same origin. The server enforces this with multiple checks:

- **Same-origin policy on every `/api/*` request** (except `/api/health`):
  - Prefer `Sec-Fetch-Site: same-origin` (all modern browsers).
  - Fall back to `Origin` / `Referer` whose host matches the request's
    `Host` (or any entry in `LMS_ALLOWED_ORIGINS`).
  - Missing all three signals → `403`.
- **CORS** never returns `*` — the validated Origin is echoed back, with
  `Allow-Credentials: false`.
- **Per-IP rate limit** on writes (token bucket, configurable).
- **Replay-verified anti-cheat** for wins (full board replay + minimum-time
  gate). Failures yield a silent success and bank the username on the
  cheater wall.
- **Hardened response headers** on everything: `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`,
  `Permissions-Policy` (geolocation/microphone/camera/topics off), and a
  strict `Content-Security-Policy` on the HTML.
- **No external dependencies** — Python stdlib only, no JS frameworks, no
  CDN-loaded assets.

## Repository layout

```
lying-minesweeper/
├── index.html              # markup + CSP + viewport
├── style.css               # styling, dark mode, responsive sizing
├── main.js                 # all game logic, no framework
├── README.md
├── LICENSE                 # MIT
├── Dockerfile              # python:3.13-slim, non-root, healthcheck
├── docker-compose.yml      # one-command local run
├── .dockerignore
├── .gitignore
├── .github/
│   └── workflows/
│       └── publish-image.yml   # build + push GHCR on v* tag
└── server/
    ├── app.py              # HTTP entry point (static UI + /api/*)
    ├── routes.py           # endpoint handlers
    ├── logic.py            # pure replay / validation / week bucketing
    ├── board.py            # pure board generation (mirrors main.js)
    ├── db.py               # SQLite access
    ├── security.py         # same-origin, rate limit, response headers
    ├── parity_check.py     # RNG / board oracle
    ├── smoke_test.py       # end-to-end submit test
    └── README.md           # server internals
```
