"""E2b: cost and benefit of a VACUUM pass at fixed applied-update targets under
different update-placement regimes (hot fraction), including the index-cleanup bypass
regime. The released harness logs scheduler-visible pg_stat_user_tables counters in
the same placement record for future integrated replications. Archived exp1b.jsonl
predates this field, so the manuscript does not treat the archive as a same-run
identical-statistics counterexample. Freezing is suppressed to isolate reclamation work.
"""
from pathlib import Path
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
import sys, time
from vacuum_lab import (connect, build, sizes, stats_row, pgstat, apply_updates,
                        vacuum, read_probe, jdump, apply_settings, PAGE)

OUT = sys.argv[3] if len(sys.argv) > 3 else str(DATA_DIR / "exp1b.jsonl")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 2_000_000
K = int(sys.argv[2]) if len(sys.argv) > 2 else 2

HOT_FRACS = [0.002, 0.005, 0.01, 0.02, 0.05, 0.20, 1.00]
DS = [10_000, 50_000, 100_000, 200_000]


def main():
    conn = connect(); cur = conn.cursor(); apply_settings(cur)
    with open(OUT, "w") as fh:
        for hf in HOT_FRACS:
            for d in DS:
                dist = "uniform" if hf >= 1.0 else "hot"
                build(cur, N, K)
                h0, i0 = sizes(cur); st0 = pgstat(cur)
                apply_updates(cur, d, N, dist=dist, hot_frac=hf)
                h1, i1 = sizes(cur); st1 = pgstat(cur)
                sched1 = stats_row(cur)
                v = vacuum(cur)
                h2, i2 = sizes(cur); st2 = pgstat(cur)
                probe = read_probe(cur, N)
                rec = {"tag": "placement", "n_rows": N, "k_idx": K, "hot_frac": hf,
                       "d_target": d, "dist": dist,
                       "heap_pages0": h0 // PAGE, "heap_pages1": h1 // PAGE,
                       "heap_pages2": h2 // PAGE, "index_pages0": i0 // PAGE,
                       "index_pages2": i2 // PAGE,
                       "dead1": st1["dead_tuple_count"], "dead2": st2["dead_tuple_count"],
                       "sched_n_live_tup": sched1["n_live_tup"],
                       "sched_n_dead_tup": sched1["n_dead_tup"],
                       "sched_n_tup_upd": sched1["n_tup_upd"],
                       "sched_n_tup_hot_upd": sched1["n_tup_hot_upd"],
                       "free1": st1["free_space"], "free2": st2["free_space"],
                       "probe": probe,
                       "vac": {k: val for k, val in v.items() if k != "raw"}, "raw": v["raw"]}
                fh.write(jdump(rec) + "\n"); fh.flush()
                reclaimed = (st2["free_space"] - st1["free_space"]) + \
                            (rec["heap_pages1"] - rec["heap_pages2"]) * PAGE
                print(f"hf={hf:<5g} D={d:>7d} dead={int(rec['dead1']):>7d} "
                      f"touched={v.get('pages_touched',0):>8d} dirtied={v.get('buf_dirtied',0):>7d} "
                      f"idx={v.get('idx_mode','?'):<10s} lpd_pages={v.get('lpdead_pages','-'):>7} "
                      f"reclaimed_MB={reclaimed/1e6:8.1f} "
                      f"pages_per_MB={v.get('pages_touched',0)/max(1e-9,reclaimed/1e6):9.0f}", flush=True)


if __name__ == "__main__":
    main()