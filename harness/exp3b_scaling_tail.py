"""Extends the scaling grid upward so that the measured optimum is interior
rather than pinned to the largest threshold tried."""
from pathlib import Path
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
import sys, time
from vacuum_lab import connect, apply_settings, jdump
from exp2_policies import run_policy
from exp3_scaling import CountFixed

OUT = sys.argv[1] if len(sys.argv) > 1 else str(DATA_DIR / "exp3_scaling.jsonl")
EXTRA = {250_000: [300_000], 1_000_000: [300_000, 600_000],
         4_000_000: [300_000, 600_000, 1_200_000]}
UPDATES = 600_000


def main():
    conn = connect(); cur = conn.cursor(); apply_settings(cur)
    with open(OUT, "a") as fh:
        for n_rows, ds in EXTRA.items():
            for d in ds:
                if d > 1.5 * n_rows:
                    continue
                p = CountFixed(d)
                t0 = time.time()
                r = run_policy(cur, p, n_rows, 2, UPDATES, "uniform", step=10000,
                               probe_every=1000)
                r["wall_s"] = time.time() - t0
                r["threshold"] = d
                fh.write(jdump(r) + "\n"); fh.flush()
                print(f"N={n_rows:>9,d} D={d:>9,d} vacs={r['totals']['n_vacuums']:3d} "
                      f"pages/Mupd={r['vac_pages_per_Mupd']:11.0f} "
                      f"overhead={r['avg_overhead_pages']:8.0f} [{r['wall_s']:.0f}s]",
                      flush=True)


if __name__ == "__main__":
    main()