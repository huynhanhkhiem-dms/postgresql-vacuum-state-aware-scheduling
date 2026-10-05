"""E7: the measured cost curve C(D) at three relation sizes.

Short steady-state runs cannot resolve the optimum for large thresholds (too few
passes per run).  Here we measure C(D) directly -- build, apply exactly D updates,
one pass -- for each relation size, and then minimise the renewal-reward objective
    J(D) = C(D)/D + lambda * D/(2 rho)
over the MEASURED curve.  This evaluates the linear-space benchmark on the measured pass-work curve.
"""
from pathlib import Path
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
import sys, time
from vacuum_lab import (connect, build, sizes, pgstat, apply_updates, vacuum,
                        jdump, apply_settings, reltuples, PAGE)

OUT = sys.argv[1] if len(sys.argv) > 1 else str(DATA_DIR / "exp5.jsonl")
SIZES = (250_000, 1_000_000, 4_000_000)
FRACS = (0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.4)


def main():
    conn = connect(); cur = conn.cursor(); apply_settings(cur)
    with open(OUT, "w") as fh:
        for n_rows in SIZES:
            for f in FRACS:
                d = int(n_rows * f)
                build(cur, n_rows, 2)
                h0, i0 = sizes(cur)
                st0 = pgstat(cur)
                applied = apply_updates(cur, d, n_rows, dist="uniform")
                h1, i1 = sizes(cur)
                st1 = pgstat(cur)
                v = vacuum(cur)
                h2, i2 = sizes(cur)
                st2 = pgstat(cur)
                rt, _ = reltuples(cur)
                rec = {"n_rows": n_rows, "frac": f, "d_target": d, "applied": applied,
                       "reltuples": rt,
                       "heap_pages0": h0 // PAGE, "index_pages0": i0 // PAGE,
                       "heap_pages1": h1 // PAGE, "heap_pages2": h2 // PAGE,
                       "index_pages2": i2 // PAGE,
                       "free1": float(st1["free_space"]), "free2": float(st2["free_space"]),
                       "dead1": float(st1["dead_tuple_count"]),
                       "vac": {k: val for k, val in v.items() if k != "raw"},
                       "raw": v["raw"]}
                fh.write(jdump(rec) + "\n"); fh.flush()
                print(f"N={n_rows:>9,d} D={d:>9,d} applied={applied:>9,d} "
                      f"touched={v.get('pages_touched', 0):>9,d} "
                      f"dirtied={v.get('buf_dirtied', 0):>8,d} "
                      f"idx={v.get('idx_mode', '?'):<10s} "
                      f"heap_growth={rec['heap_pages1'] - rec['heap_pages0']:>7,d}",
                      flush=True)


if __name__ == "__main__":
    main()