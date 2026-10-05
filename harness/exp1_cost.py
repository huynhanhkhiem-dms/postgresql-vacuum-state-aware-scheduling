"""E1/E2: per-pass VACUUM cost structure vs dead-tuple count D, index count k,
dead-tuple placement, HOT updates, and snapshot-blocked (non-removable) dead tuples.
"""
from pathlib import Path
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
import json, sys, time
import psycopg2
from vacuum_lab import (connect, build, sizes, stats_row, pgstat, apply_updates,
                        vacuum, read_probe, jdump, PAGE)

OUT = sys.argv[2] if len(sys.argv) > 2 else str(DATA_DIR / "exp1.jsonl")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 2_000_000

FRACS = [0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.10, 0.20]


def one(cur, n_rows, k, d, dist, fillfactor=100, col="a", blocker=None, tag=""):
    build(cur, n_rows, k, fillfactor=fillfactor)
    h0, i0 = sizes(cur)
    st0 = pgstat(cur)
    if blocker is not None:          # snapshot must predate the updates
        blocker.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        blocker.execute("SELECT 1")
        blocker.fetchall()
    t0 = time.time()
    apply_updates(cur, d, n_rows, dist=dist, col=col)
    upd_s = time.time() - t0
    h1, i1 = sizes(cur)
    st1 = pgstat(cur)
    srow = stats_row(cur)
    v = vacuum(cur)
    if blocker is not None:
        blocker.execute("COMMIT")
    h2, i2 = sizes(cur)
    st2 = pgstat(cur)
    rec = {"tag": tag, "n_rows": n_rows, "k_idx": k, "d_target": d, "dist": dist,
           "fillfactor": fillfactor, "col": col, "blocked": blocker is not None,
           "upd_s": upd_s,
           "heap_pages0": h0 // PAGE, "index_pages0": i0 // PAGE,
           "heap_pages1": h1 // PAGE, "index_pages1": i1 // PAGE,
           "heap_pages2": h2 // PAGE, "index_pages2": i2 // PAGE,
           "n_dead_stat": srow["n_dead"], "n_hot_upd": srow["n_hot"], "n_upd": srow["n_upd"],
           "free0": st0["free_space"], "free1": st1["free_space"], "free2": st2["free_space"],
           "dead1": st1.get("dead_tuple_count"), "dead2": st2.get("dead_tuple_count"),
           "vac": {kk: vv for kk, vv in v.items() if kk != "raw"},
           "raw": v["raw"]}
    return rec


def main():
    conn = connect(); cur = conn.cursor()
    cur.execute("SET maintenance_work_mem='256MB'")
    cur.execute("SET client_min_messages='INFO'")
    blk_conn = connect(autocommit=True); blk = blk_conn.cursor()
    ds = sorted({max(1000, int(N * f)) for f in FRACS})
    with open(OUT, "w") as fh:
        # --- A. cost structure: k x D x placement -------------------------
        for k in (0, 1, 2, 4, 8):
            for dist in ("uniform", "hot"):
                for d in ds:
                    r = one(cur, N, k, d, dist, tag="A_cost")
                    fh.write(jdump(r) + "\n"); fh.flush()
                    v = r["vac"]
                    print(f"A k={k} {dist:8s} D={d:>7d} touched={v.get('pages_touched',0):>8d} "
                          f"dirtied={v.get('buf_dirtied',0):>7d} wal={v.get('wal_bytes',0)/1e6:7.1f}MB "
                          f"lpd_pg={v.get('lpdead_pages','-')} bypass={v.get('index_bypassed')} "
                          f"cpu={v.get('cpu_user',0)+v.get('cpu_sys',0):5.2f}s", flush=True)
        # --- B. HOT updates (non-indexed column, fillfactor 70) -----------
        for d in ds:
            r = one(cur, N, 2, d, "uniform", fillfactor=70, col="h", tag="B_hot")
            fh.write(jdump(r) + "\n"); fh.flush()
            v = r["vac"]
            print(f"B HOT  D={d:>7d} touched={v.get('pages_touched',0):>8d} "
                  f"hot_upd={r['n_hot_upd']} heap_growth={r['heap_pages1']-r['heap_pages0']:>6d} "
                  f"bypass={v.get('index_bypassed')}", flush=True)
        # --- C. snapshot-blocked dead tuples ------------------------------
        for d in ds:
            r = one(cur, N, 2, d, "uniform", blocker=blk, tag="C_blocked")
            fh.write(jdump(r) + "\n"); fh.flush()
            v = r["vac"]
            print(f"C BLK  D={d:>7d} touched={v.get('pages_touched',0):>8d} "
                  f"removable={v.get('removable')} nonremovable={v.get('nonremovable')} "
                  f"freed_pages={r['heap_pages1']-r['heap_pages2']}", flush=True)


if __name__ == "__main__":
    main()