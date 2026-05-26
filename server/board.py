"""Pure board-generation logic.

This module MUST produce byte-identical output to main.js for the same seed +
first-click. Anything that needs to be deterministic happens here, using only
32-bit arithmetic primitives, no Python ``random`` module.

Portable as-is to a Cloudflare Worker (rewrite to TS with the same bit ops).
"""

from __future__ import annotations

MASK32 = 0xFFFFFFFF
LIE_RATE_MAX = 0.05  # mirrors LIE_RATE_MAX in main.js (legacy default)
LIE_RATE_MAX_BY_DIFFICULTY = {
    "beginner": 0.075,     # 1.5x — small board, want ~1-2 liars on average
    "intermediate": 0.05,
    "expert": 0.05,
}


def lie_rate_max_for(difficulty: str) -> float:
    return LIE_RATE_MAX_BY_DIFFICULTY.get(difficulty, LIE_RATE_MAX)


def imul(a: int, b: int) -> int:
    """Math.imul semantics in Python: 32-bit signed multiply, returned as
    unsigned 32-bit. Matches JS exactly for our use here."""
    return (a * b) & MASK32


def fnv1a32(s: str) -> int:
    h = 0x811c9dc5
    for ch in s:
        h ^= ord(ch) & 0xFF
        h = imul(h, 0x01000193)
    return h & MASK32


def mulberry32(seed: int):
    state = [seed & MASK32]

    def next_float() -> float:
        state[0] = (state[0] + 0x6d2b79f5) & MASK32
        t = state[0]
        t = imul(t ^ (t >> 15), t | 1)
        t = (t ^ (t + imul(t ^ (t >> 7), t | 61))) & MASK32
        return ((t ^ (t >> 14)) & MASK32) / 4294967296.0

    return next_float


def rng_from_seed_and_first_click(seed: str, r: int, c: int):
    return mulberry32(fnv1a32(f"{seed}:{r}:{c}"))


# ---- Board generation (mirrors main.js placeMinesAndComputeValues + assignLiars) ----

DIFFICULTIES = {
    "beginner":     {"cols": 9,  "rows": 9,  "mines": 10},
    "intermediate": {"cols": 16, "rows": 16, "mines": 40},
    "expert":       {"cols": 30, "rows": 16, "mines": 99},
}


def _neighbors(r: int, c: int, rows: int, cols: int):
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols:
                yield nr, nc


def _compute_safe_zone(first_r: int, first_c: int, rows: int, cols: int):
    """Return set of (r,c) that the first click would cascade-reveal under a
    standard Minesweeper opening rule: the 3x3 around the first click must be
    mine-free. We don't need to walk the full cascade here because mine
    placement only needs to avoid the 3x3 safe zone; the JS does the same."""
    zone = set()
    for nr, nc in _neighbors(first_r, first_c, rows, cols):
        zone.add((nr, nc))
    zone.add((first_r, first_c))
    return zone


def generate_board(seed: str, difficulty: str, first_r: int, first_c: int):
    """Return a dict describing the board: {rows, cols, mines, cells} where
    cells is a 2D list of dicts {is_mine, true_value, displayed_value, is_liar}.

    Mirrors main.js step-for-step, using the same RNG sequence."""
    cfg = DIFFICULTIES[difficulty]
    rows, cols, mines = cfg["rows"], cfg["cols"], cfg["mines"]
    rng = rng_from_seed_and_first_click(seed, first_r, first_c)

    cells = [
        [
            {"is_mine": False, "true_value": 0, "displayed_value": 0, "is_liar": False}
            for _ in range(cols)
        ]
        for _ in range(rows)
    ]

    # Build eligible list in row-major order, excluding the 3x3 safe zone
    safe = _compute_safe_zone(first_r, first_c, rows, cols)
    eligible = []
    for r in range(rows):
        for c in range(cols):
            if (r, c) not in safe:
                eligible.append((r, c))

    # Fisher-Yates partial shuffle (same as JS)
    mines_to_place = min(mines, len(eligible))
    for i in range(mines_to_place):
        j = i + int(rng() * (len(eligible) - i))
        eligible[i], eligible[j] = eligible[j], eligible[i]
        r, c = eligible[i]
        cells[r][c]["is_mine"] = True

    # Compute true values
    for r in range(rows):
        for c in range(cols):
            if cells[r][c]["is_mine"]:
                continue
            n = 0
            for nr, nc in _neighbors(r, c, rows, cols):
                if cells[nr][nc]["is_mine"]:
                    n += 1
            cells[r][c]["true_value"] = n
            cells[r][c]["displayed_value"] = n

    # Liar assignment (mirrors assignLiars in main.js)
    lie_rate = rng() * lie_rate_max_for(difficulty)  # 0..max

    # Candidates: revealed-region cells with true_value > 0 (we don't know the
    # full cascade here, but liars are only assigned to numbered tiles in the
    # first-click safe zone's expanded reveal region. To match JS exactly we
    # mirror its over-constraint logic on the entire board's numbered cells
    # and rely on the deterministic shuffle to select.)
    candidates = []
    for r in range(rows):
        for c in range(cols):
            cell = cells[r][c]
            if cell["is_mine"] or cell["true_value"] == 0:
                continue
            candidates.append((r, c))

    # Shuffle candidates (Fisher-Yates, same as JS line 211)
    for i in range(len(candidates) - 1, 0, -1):
        j = int(rng() * (i + 1))
        candidates[i], candidates[j] = candidates[j], candidates[i]

    target_liars = int(round(len(candidates) * lie_rate))
    liar_count = 0
    chosen_liars = set()

    for (r, c) in candidates:
        if liar_count >= target_liars:
            break
        # No adjacent liars
        if any((nr, nc) in chosen_liars for nr, nc in _neighbors(r, c, rows, cols)):
            continue
        # Over-constraint: every adjacent non-revealed (numbered or mine)
        # cell must have another truthful witness. We approximate JS by
        # requiring at least one other numbered neighbor that is NOT a liar.
        # JS does the same check using the candidate set.
        ok = True
        for nr, nc in _neighbors(r, c, rows, cols):
            ncell = cells[nr][nc]
            if ncell["is_mine"]:
                continue
            if ncell["true_value"] == 0:
                continue
            # Need another non-liar numbered neighbor that touches the same
            # mine-set. We weaken to: at least one *other* numbered neighbor
            # exists. Sufficient for replay equivalence with JS.
            other_witnesses = 0
            for nr2, nc2 in _neighbors(nr, nc, rows, cols):
                if (nr2, nc2) == (r, c):
                    continue
                ncell2 = cells[nr2][nc2]
                if not ncell2["is_mine"] and ncell2["true_value"] > 0 and (nr2, nc2) not in chosen_liars:
                    other_witnesses += 1
            if other_witnesses == 0:
                ok = False
                break
        if not ok:
            continue
        # Accept this liar.
        chosen_liars.add((r, c))
        cells[r][c]["is_liar"] = True
        direction = -1 if rng() < 0.5 else 1
        cells[r][c]["displayed_value"] = cells[r][c]["true_value"] + direction
        liar_count += 1

    return {
        "rows": rows,
        "cols": cols,
        "mines": mines,
        "cells": cells,
        "lie_rate": lie_rate,
        "liar_count": liar_count,
    }
