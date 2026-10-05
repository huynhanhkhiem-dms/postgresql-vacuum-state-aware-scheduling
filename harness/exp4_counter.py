"""E6: what the trigger's counter actually counts.

`n_dead_tup` (the quantity compared against the autovacuum threshold) is
incremented per update and is not reduced by opportunistic page pruning, so it
diverges from the dead storage actually present.  We measure both, plus the dead
footprint, for two placements.
"""
from pathlib import Path
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
import sys, time
from vacuum_lab import (connect, build, sizes, stats_row, pgstat, apply_updates,
                        jdump, apply_settings, PAGE)

OUT = sys.argv[2] if len(sys.argv) > 2 else str(DATA_DIR / "exp4.jsonl")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000


def main():
    conn = connect(); cur = conn.cursor(); apply_settings(cur)
    with open(OUT, "w") as fh:
        for dist, hf in (("uniform", 0.0), ("hot", 0.01)):
            build(cur, N, 2)
            h0, _ = sizes(cur)
            done = 0
            for _ in range(20):
                apply_updates(cur, N // 20, N, dist=dist, hot_frac=hf or 0.01)
                done += N // 20
                time.sleep(1.2)                      # let the stats flush interval pass
                st = stats_row(cur)
                sq = pgstat(cur)
                h, i = sizes(cur)
                rec = {"dist": dist, "hot_frac": hf, "updates": done,
                       "n_dead_tup": st["n_dead"], "n_tup_upd": st["n_upd"],
                       "n_tup_hot_upd": st["n_hot"],
                       "dead_tuple_count": float(sq["dead_tuple_count"]),
                       "dead_tuple_len": float(sq["dead_tuple_len"]),
                       "free_space": float(sq["free_space"]),
                       "heap_pages": h // PAGE, "index_pages": i // PAGE,
                       "base_heap_pages": h0 // PAGE}
                rec["implied_bytes"] = rec["n_dead_tup"] * 124.0
                rec["overstatement"] = rec["implied_bytes"] / max(1.0, rec["dead_tuple_len"])
                fh.write(jdump(rec) + "\n"); fh.flush()
                print(f"{dist:8s} upd={done:>8d} n_dead_tup={rec['n_dead_tup']:>9d} "
                      f"actual_dead={rec['dead_tuple_count']:>9.0f} "
                      f"dead_bytes={rec['dead_tuple_len']/1e6:7.2f}MB "
                      f"implied={rec['implied_bytes']/1e6:7.2f}MB "
                      f"overstated={rec['overstatement']:5.2f}x "
                      f"heap_growth={rec['heap_pages'] - rec['base_heap_pages']:>6d}", flush=True)


if __name__ == "__main__":
    main()