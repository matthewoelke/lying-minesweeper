"""End-to-end smoke test: generates a winning action log on the server side,
submits it via HTTP, and verifies it lands on the leaderboard.

Run after ``py -m server.app`` is up::

    py -m server.smoke_test

Override the target with ``LMS_SMOKE_URL`` (default ``http://127.0.0.1:8088``).
"""
from __future__ import annotations
import json
import os
import urllib.request
import sys
from . import board

API = os.environ.get("LMS_SMOKE_URL", "http://127.0.0.1:8088").rstrip("/") + "/api"


def http_json(method, path, body=None):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    # Smoke test runs as a same-origin caller. Mimic what a browser fetch()
    # would send so the server's same-origin policy lets us through.
    base_headers = {
        "Sec-Fetch-Site": "same-origin",
        "Origin": API[:-4],  # strip trailing "/api"
    }
    if data:
        base_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers=base_headers,
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8")), resp.status


def build_winning_log(b, first_r, first_c, duration_ms):
    """Walk every non-mine cell and reveal it. Spread the actions out across
    duration_ms so the server's min-time gate passes."""
    rows, cols = b["rows"], b["cols"]
    cells = b["cells"]
    log = [{"t": 50, "type": "reveal", "r": first_r, "c": first_c}]
    # Find all non-mine cells (excluding first click, which the cascade may cover)
    non_mines = [
        (r, c)
        for r in range(rows)
        for c in range(cols)
        if not cells[r][c]["is_mine"] and not (r == first_r and c == first_c)
    ]
    # Filter to cells the cascade did NOT auto-reveal. We simulate the same
    # cascade as the server-side replay to know what's left.
    revealed = set()

    def neighbors(r, c):
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols:
                    yield nr, nc

    def cascade(sr, sc):
        stack = [(sr, sc)]
        while stack:
            r, c = stack.pop()
            if (r, c) in revealed or cells[r][c]["is_mine"]:
                continue
            revealed.add((r, c))
            if cells[r][c]["true_value"] == 0:
                for nr, nc in neighbors(r, c):
                    if (nr, nc) not in revealed and not cells[nr][nc]["is_mine"]:
                        stack.append((nr, nc))

    cascade(first_r, first_c)

    remaining = [(r, c) for (r, c) in non_mines if (r, c) not in revealed]
    # Don't divide by remaining length — actions are skipped when cascade
    # covers them, so set timestamps relative to a fixed end time.
    end_t = max(duration_ms, 1600)
    step = max(50, (end_t - 100) // max(1, len(remaining)))
    t = 100
    for r, c in remaining:
        if (r, c) in revealed:
            continue
        t += step
        log.append({"t": t, "type": "reveal", "r": r, "c": c})
        cascade(r, c)
    # Pad final timestamp up to end_t to guarantee min-plausible-time clearance.
    if log[-1]["t"] < end_t:
        log[-1]["t"] = end_t
    return log


def main():
    seed, diff, fr, fc = "ABCDEFGH", "beginner", 3, 4
    b = board.generate_board(seed, diff, fr, fc)

    print(f"[1/4] POST /games/new (seed={seed}, diff={diff}, fc={fr},{fc})")
    resp, status = http_json("POST", "/games/new", {
        "seed": seed, "difficulty": diff, "first_click": {"r": fr, "c": fc},
    })
    print(f"      → {status} {resp}")
    game_id = resp["game_id"]

    log = build_winning_log(b, fr, fc, duration_ms=2500)
    print(f"[2/4] Built winning log: {len(log)} actions ending at t={log[-1]['t']}ms")

    print(f"[3/4] POST /games/{game_id}/submit (username=SmokeTester01)")
    resp, status = http_json("POST", f"/games/{game_id}/submit", {
        "username": "SmokeTester01", "action_log": log,
    })
    print(f"      → {status} {resp}")
    if not resp.get("verified"):
        print("      ✗ submission was not verified (likely a replay-parity bug)")
        sys.exit(1)

    print(f"[4/4] GET /leaderboards?kind=seed&seed={seed}&difficulty={diff}")
    resp, status = http_json("GET", f"/leaderboards?kind=seed&seed={seed}&difficulty={diff}")
    print(f"      → {status} {resp}")
    if not any(e["username"] == "SmokeTester01" for e in resp.get("entries", [])):
        print("      ✗ score did not land on per-seed leaderboard")
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
