"""E4: how the empirical optimum scales with relation size (linear-space benchmark).
E5: futile passes under a held snapshot (Theorem 3).

Usage:  exp3_scaling.py scaling|removability  [out.jsonl]
"""
from pathlib import Path
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
import json, math, sys, time
from vacuum_lab import (connect, build, sizes, stats_row, pgstat, apply_updates,
                        vacuum, read_probe, jdump, apply_settings, PAGE)
from exp2_policies import Policy, run_policy


class CountFixed(Policy):
    """Absolute threshold: vacuum every D dead tuples."""
    def __init__(self, d, name=None):
        self.d = d
        self.name = name or f"fixed_{d}"
    def decide(self, ctx):
        return "AUTO" if ctx["n_dead"] >= self.d else None


class XminAware(Policy):
    """Count trigger that additionally refuses to run when the removability
    horizon has not advanced since the last futile pass."""
    def __init__(self, frac=0.20, base=50, name="xmin_aware"):
        self.frac, self.base, self.name = frac, base, name
        self.blocked_until_horizon = None
    def decide(self, ctx):
        if ctx["n_dead"] < self.base + self.frac * ctx["n_live"]:
            return None
        if self.blocked_until_horizon is not None and \
           ctx["xmin_horizon"] <= self.blocked_until_horizon:
            return None
        return "AUTO"
    def after_vacuum(self, ctx, v):
        if v.get("removed", 0) == 0 and v.get("not_removable", 0) > 0:
            self.blocked_until_horizon = ctx["xmin_horizon"]
        else:
            self.blocked_until_horizon = None


def scaling(out):
    conn = connect(); cur = conn.cursor(); apply_settings(cur)
    DS = [2000, 5000, 10000, 20000, 50000, 100000, 200000]
    with open(out, "w") as fh:
        for n_rows, updates in ((250_000, 600_000), (1_000_000, 600_000), (4_000_000, 600_000)):
            for d in DS:
                if d > 0.5 * n_rows:
                    continue
                p = CountFixed(d)
                t0 = time.time()
                r = run_policy(cur, p, n_rows, 2, updates, "uniform", step=10000, probe_every=1000)
                r["wall_s"] = time.time() - t0
                r["threshold"] = d
                fh.write(jdump(r) + "\n"); fh.flush()
                print(f"N={n_rows:>8d} D={d:>7d} vacs={r['totals']['n_vacuums']:3d} "
                      f"pages/Mupd={r['vac_pages_per_Mupd']:10.0f} "
                      f"overhead={r['avg_overhead_pages']:8.0f} "
                      f"rho={n_rows/max(1,r['base_heap_pages']):5.1f} "
                      f"[{r['wall_s']:.0f}s]", flush=True)


def removability(out):
    conn = connect(); cur = conn.cursor(); apply_settings(cur)
    blk_conn = connect(); blk = blk_conn.cursor()
    n_rows, updates, step = 1_000_000, 1_200_000, 20000

    def run(policy_factory, hold_snapshot, tag):
        p = policy_factory()
        build(cur, n_rows, 2)
        h0, i0 = sizes(cur)
        tot = {"pages_touched": 0, "buf_dirtied": 0, "n_vacuums": 0, "removed": 0,
               "futile": 0, "futile_pages": 0}
        done = 0
        if hold_snapshot:
            blk.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            blk.execute("SELECT 1"); blk.fetchall()
        holding = hold_snapshot
        series = []
        while done < updates:
            apply_updates(cur, step, n_rows, dist="uniform")
            done += step
            if holding and done >= updates // 2:      # release the snapshot half way
                blk.execute("COMMIT"); holding = False
            h, i = sizes(cur)
            st = stats_row(cur)
            cur.execute("SELECT coalesce(max(age(backend_xmin)), 0) "
                        "FROM pg_stat_activity WHERE backend_xmin IS NOT NULL")
            horizon = cur.fetchone()[0]     # age of the oldest live snapshot
            ctx = {"n_live": st["n_live"], "n_dead": st["n_dead"], "updates": done,
                   "heap_pages": h // PAGE, "index_pages": i // PAGE, "idx_debt": 0,
                   "xmin_horizon": -horizon}
            mode = p.decide(ctx)
            if mode:
                v = vacuum(cur, index_cleanup=mode)
                p.after_vacuum(ctx, v)
                tot["pages_touched"] += v.get("pages_touched", 0)
                tot["buf_dirtied"] += v.get("buf_dirtied", 0)
                tot["removed"] += v.get("removed", 0)
                tot["n_vacuums"] += 1
                if v.get("removed", 0) == 0:
                    tot["futile"] += 1
                    tot["futile_pages"] += v.get("pages_touched", 0)
            h2, i2 = sizes(cur)
            series.append({"updates": done, "heap_pages": h2 // PAGE,
                           "index_pages": i2 // PAGE, "n_dead": ctx["n_dead"],
                           "vacuumed": bool(mode)})
        if holding:
            blk.execute("COMMIT")
        rec = {"tag": tag, "hold_snapshot": hold_snapshot, "n_rows": n_rows,
               "updates": updates, "base_heap_pages": h0 // PAGE,
               "base_index_pages": i0 // PAGE, "totals": tot,
               "final_heap_pages": series[-1]["heap_pages"],
               "final_index_pages": series[-1]["index_pages"], "series": series}
        return rec

    with open(out, "w") as fh:
        for factory, hold, tag in (
                (lambda: CountFixed(20000, name="count_20k"), True, "count_held"),
                (lambda: XminAware(frac=0.0, base=20000), True, "xmin_held"),
                (lambda: CountFixed(20000, name="count_20k"), False, "count_free")):
            r = run(factory, hold, tag)
            fh.write(jdump(r) + "\n"); fh.flush()
            t = r["totals"]
            print(f"{tag:12s} vacs={t['n_vacuums']:3d} futile={t['futile']:3d} "
                  f"futile_pages={t['futile_pages']:9d} removed={t['removed']:9d} "
                  f"pages={t['pages_touched']:9d} final(h,i)="
                  f"({r['final_heap_pages']},{r['final_index_pages']})", flush=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scaling"
    out = sys.argv[2] if len(sys.argv) > 2 else str(DATA_DIR / f"exp3_{mode}.jsonl")
    (scaling if mode == "scaling" else removability)(out)