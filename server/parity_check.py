"""Standalone parity helper. Prints the first 10 RNG outputs and full board for
a known seed+first-click. Cross-check against the JS by opening the static
page at /?d=beginner&seed=ABCDEFGH&fc=3,4 and inspecting state.cells in
DevTools — mine positions and liar positions must match exactly.

Run:
    py -m server.parity_check
"""
from __future__ import annotations
from . import board


def main():
    seed, fr, fc = "ABCDEFGH", 3, 4
    rng = board.rng_from_seed_and_first_click(seed, fr, fc)
    print(f"Seed/first-click: {seed} ({fr},{fc})")
    print("First 10 rng() outputs (must match JS exactly):")
    for i in range(10):
        v = rng()
        print(f"  [{i}] {v:.10f}")

    b = board.generate_board(seed, "beginner", fr, fc)
    print(f"\nBoard: {b['rows']}x{b['cols']}, {b['mines']} mines")
    print(f"lie_rate={b['lie_rate']:.6f}  liar_count={b['liar_count']}")
    print("\nMines:")
    for r in range(b["rows"]):
        for c in range(b["cols"]):
            if b["cells"][r][c]["is_mine"]:
                print(f"  ({r},{c})")
    print("\nLiars (r,c,true,displayed):")
    for r in range(b["rows"]):
        for c in range(b["cols"]):
            if b["cells"][r][c]["is_liar"]:
                cell = b["cells"][r][c]
                print(f"  ({r},{c}) true={cell['true_value']} shown={cell['displayed_value']}")


if __name__ == "__main__":
    main()
