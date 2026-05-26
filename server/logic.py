"""Pure replay/validation/score logic.

Given a generated board and a client-supplied action log, replay the moves
server-side and decide whether the score is verified or rejected.

Has no I/O dependencies. Portable as-is to a Worker.
"""

from __future__ import annotations
from typing import List, Dict, Tuple

# Tunable per-difficulty minimum plausible solve time in ms.
# Anything faster than this is rejected as a likely bot/replay/script.
MIN_PLAUSIBLE_MS = {
    "beginner":     1500,
    "intermediate": 5000,
    "expert":       15000,
}

# Hard upper bound for sanity (24h is way more than any real session).
MAX_PLAUSIBLE_MS = 24 * 60 * 60 * 1000


def _neighbors(r, c, rows, cols):
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols:
                yield nr, nc


def replay(board, action_log: List[Dict]) -> Dict:
    """Replay the log on the board. Returns a dict with:
        ok: bool
        reason: str (if not ok)
        won: bool
        duration_ms: int (last action timestamp)
        tricked: bool (loss was adjacent to a liar)
        revealed_count: int
    """
    rows, cols = board["rows"], board["cols"]
    cells = board["cells"]
    total_non_mine = rows * cols - board["mines"]

    revealed = [[False] * cols for _ in range(rows)]
    flagged = [[False] * cols for _ in range(rows)]
    revealed_count = 0
    last_t = 0
    won = False
    lost = False
    tricked = False
    hit_rc = None

    if not isinstance(action_log, list) or len(action_log) == 0:
        return {"ok": False, "reason": "empty_log"}

    for idx, act in enumerate(action_log):
        if not isinstance(act, dict):
            return {"ok": False, "reason": f"action_{idx}_not_object"}
        t = act.get("t")
        typ = act.get("type")
        r = act.get("r")
        c = act.get("c")
        if not isinstance(t, (int, float)) or t < 0:
            return {"ok": False, "reason": f"action_{idx}_bad_t"}
        if t < last_t:
            return {"ok": False, "reason": f"action_{idx}_nonmonotonic_t"}
        if t > MAX_PLAUSIBLE_MS:
            return {"ok": False, "reason": f"action_{idx}_t_overflow"}
        if not (isinstance(r, int) and isinstance(c, int)):
            return {"ok": False, "reason": f"action_{idx}_bad_rc"}
        if not (0 <= r < rows and 0 <= c < cols):
            return {"ok": False, "reason": f"action_{idx}_out_of_bounds"}
        last_t = t

        if lost or won:
            return {"ok": False, "reason": f"action_{idx}_after_end"}

        if typ == "flag":
            if revealed[r][c] or flagged[r][c]:
                return {"ok": False, "reason": f"action_{idx}_invalid_flag"}
            flagged[r][c] = True
        elif typ == "unflag":
            if not flagged[r][c]:
                return {"ok": False, "reason": f"action_{idx}_invalid_unflag"}
            flagged[r][c] = False
        elif typ == "reveal":
            if revealed[r][c] or flagged[r][c]:
                return {"ok": False, "reason": f"action_{idx}_invalid_reveal"}
            cell = cells[r][c]
            if cell["is_mine"]:
                revealed[r][c] = True
                lost = True
                hit_rc = (r, c)
                # "Tricked" = the clicked bomb is in the 8-neighborhood of an
                # *already-revealed* liar. Every liar has a bomb nearby by
                # construction; what we score is whether the player clicked
                # one of those bombs while the wrong number was on screen.
                for nr, nc in _neighbors(r, c, rows, cols):
                    if revealed[nr][nc] and cells[nr][nc]["is_liar"]:
                        tricked = True
                        break
            else:
                # Cascade reveal: same rule as client (cascade when trueValue==0)
                stack = [(r, c)]
                while stack:
                    cr, cc = stack.pop()
                    if revealed[cr][cc] or flagged[cr][cc]:
                        continue
                    if cells[cr][cc]["is_mine"]:
                        continue
                    revealed[cr][cc] = True
                    revealed_count += 1
                    if cells[cr][cc]["true_value"] == 0:
                        for nr, nc in _neighbors(cr, cc, rows, cols):
                            if not revealed[nr][nc] and not flagged[nr][nc]:
                                stack.append((nr, nc))
                if revealed_count >= total_non_mine:
                    won = True
        else:
            return {"ok": False, "reason": f"action_{idx}_bad_type"}

    if not (won or lost):
        return {"ok": False, "reason": "incomplete_game"}

    return {
        "ok": True,
        "won": won,
        "lost": lost,
        "duration_ms": int(last_t),
        "tricked": tricked,
        "revealed_count": revealed_count,
        "hit": hit_rc,
    }


def validate_min_time(difficulty: str, duration_ms: int) -> bool:
    floor = MIN_PLAUSIBLE_MS.get(difficulty, 1500)
    return duration_ms >= floor


def iso_week_bucket(epoch_ms: int) -> str:
    """Return 'YYYY-Www' for the given unix ms (UTC)."""
    import datetime
    dt = datetime.datetime.utcfromtimestamp(epoch_ms / 1000.0)
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"
