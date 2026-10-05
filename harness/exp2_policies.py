"""E3: steady-state comparison of space-reclamation trigger policies.

Workload time = number of applied updates (machine independent).
Cost          = buffer pages touched / dirtied by VACUUM, WAL bytes.
Space         = heap + index pages above the clean baseline, integrated over
                workload time.
"""
from pathlib import Path
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
import json, math, os, random, sys, time
from vacuum_lab import (connect, build, sizes, stats_row, pgstat, apply_updates,
                        vacuum, read_probe, jdump, apply_settings, reltuples, PAGE)


# ----------------------------------------------------------------- policies --
class Policy:
    name = "base"
    needs_footprint = False
    index_cleanup = "AUTO"
    def start(self, ctx): pass
    def decide(self, ctx): raise NotImplementedError      # -> None | "AUTO" | "ON"
    def after_vacuum(self, ctx, v): pass


class CountFrac(Policy):
    """PostgreSQL's rule: n_dead >= base + frac * reltuples (capped optionally)."""
    def __init__(self, frac, base=50, cap=None, name=None):
        self.frac, self.base, self.cap = frac, base, cap
        self.name = name or f"count_{frac:g}"
    def decide(self, ctx):
        thr = self.base + self.frac * ctx["reltuples"]
        if self.cap is not None:
            thr = min(thr, self.cap)
        return "AUTO" if ctx["n_dead"] >= thr else None


class SqrtLaw(Policy):
    """Renewal-reward optimum for a count-based trigger (linear-space benchmark):
           D* = sqrt(2 * rho * Cbar / lam)
       rho  = live tuples per heap page (catalogue), Cbar = measured cost of the
       previous pass in pages, lam = price of one page of unreclaimed space per
       unit of workload time, in pages of I/O."""
    def __init__(self, lam, name=None):
        self.lam, self.Cbar = lam, None
        self.name = name or f"sqrt_{lam:g}"
    def start(self, ctx):
        self.Cbar = 2.0 * ctx["heap_pages"] + ctx["index_pages"]
    def decide(self, ctx):
        rho = max(1.0, ctx["reltuples"] / max(1.0, ctx["heap_pages"]))
        d_star = max(1000.0, math.sqrt(2.0 * rho * self.Cbar / self.lam))
        self.d_star = d_star
        return "AUTO" if ctx["n_dead"] >= d_star else None
    def after_vacuum(self, ctx, v):
        c = v.get("pages_touched")
        if c:
            self.Cbar = 0.5 * self.Cbar + 0.5 * c


class Footprint(Policy):
    """Marginal (renewal-reward) rule on the measured dead-page footprint A.

    Vacuum when the amortised fixed cost per dead tuple has fallen to the
    marginal price of holding the dead space:
        (C(D) - D*C'(D)) / D^2  <=  lam*s/2
    with C(D) = 2A + I*1{A > 0.02P} (+ index pass forced when index debt is dear).
    """
    needs_footprint = True
    def __init__(self, lam, cA=2.0, force_idx=False, idx_gamma=0.25, name=None):
        self.lam, self.cA, self.force_idx, self.idx_gamma = lam, cA, force_idx, idx_gamma
        self.name = name or (f"fpidx_{lam:g}" if force_idx else f"fp_{lam:g}")
        self.prev = None
    def decide(self, ctx):
        P = max(1.0, float(ctx["heap_pages"]))
        A = float(ctx["footprint_pages"])
        D = max(1.0, float(ctx["n_dead"]))
        rho = max(1.0, ctx["reltuples"] / P)
        bypass = A <= 0.02 * P
        C = self.cA * A + (0.0 if bypass else ctx["index_pages"])
        if self.prev and ctx["n_dead"] > self.prev[1] + 1:      # empirical dA/dD
            Ap = self.cA * (A - self.prev[0]) / (ctx["n_dead"] - self.prev[1])
        else:
            Ap = self.cA * math.exp(-D / P)
        self.prev = (A, ctx["n_dead"])
        lhs = (C - D * max(0.0, Ap)) / (D * D)
        if lhs > self.lam / (2.0 * rho):
            return None
        if self.force_idx and ctx["idx_debt"] > self.idx_gamma * ctx["reltuples"]:
            return "ON"
        return "AUTO"
    def after_vacuum(self, ctx, v):
        self.prev = None


class Footprint2(Footprint):
    """Footprint rule with saturation repair: the slope dA/dD is obtained from the occupancy model
    fitted to the CURRENT observation rather than from differencing two noisy
    footprint estimates.

    Differencing is the defect diagnosed in the initial estimator: with m sampled pages the standard
    error of A is P*sqrt(a(1-a)/m), which is of the same order as the increment of
    A between two observations, so the estimated slope is noise-dominated (and
    frequently negative, hence clipped to zero, which makes the rule fire early).
    Solving A = S(1 - exp(-D/S)) for the saturation level S uses one observation
    and no differencing; the slope is then exp(-D/S).
    """
    needs_footprint = True
    def __init__(self, lam, cA=2.0, force_idx=False, idx_gamma=0.25, name=None):
        super().__init__(lam, cA=cA, force_idx=force_idx, idx_gamma=idx_gamma,
                         name=name or (f"fp2idx_{lam:g}" if force_idx else f"fp2_{lam:g}"))
    @staticmethod
    def _saturation(A, D, P):
        """Solve A = S(1 - exp(-D/S)) for S in [A, P] by bisection."""
        if A <= 0 or D <= 0:
            return max(A, 1.0)
        lo, hi = max(A, 1.0), max(A * 1.0001, float(P))
        f = lambda S: S * (1.0 - math.exp(-D / S)) - A
        if f(hi) < 0:            # even a full-relation footprint is not enough
            return hi
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if f(mid) > 0:
                hi = mid
            else:
                lo = mid
        return 0.5 * (lo + hi)
    def decide(self, ctx):
        P = max(1.0, float(ctx["heap_pages"]))
        A = float(ctx["footprint_pages"])
        D = max(1.0, float(ctx["n_dead"]))
        rho = max(1.0, ctx["reltuples"] / P)
        bypass = A <= 0.02 * P
        C = self.cA * A + (0.0 if bypass else ctx["index_pages"])
        S = self._saturation(A, D, P)
        Ap = self.cA * math.exp(-D / S)
        lhs = (C - D * Ap) / (D * D)
        if lhs > self.lam / (2.0 * rho):
            return None
        if self.force_idx and ctx["idx_debt"] > self.idx_gamma * ctx["reltuples"]:
            return "ON"
        return "AUTO"


# ------------------------------------------------------------------ runner --
def footprint_estimate(cur, heap_pages, sample=150, table="t"):
    """Unbiased estimate of the number of heap pages holding >=1 obsolete item."""
    n = min(sample, heap_pages)
    blocks = random.sample(range(heap_pages), n)
    vals = ",".join(str(b) for b in blocks)
    cur.execute(f"""SELECT count(*) FROM (
                      SELECT b FROM unnest(ARRAY[{vals}]) b
                       WHERE EXISTS (SELECT 1 FROM heap_page_items(get_raw_page('{table}', b::int)) i
                                      WHERE i.lp_flags = 3
                                         OR (i.t_xmax IS NOT NULL AND i.t_xmax <> 0))) q""")
    hits = cur.fetchone()[0]
    return heap_pages * hits / n


def run_policy(cur, policy, n_rows, k_idx, total_updates, dist, step=10000,
               hot_frac=0.01, probe_every=10, fillfactor=100, verbose=True):
    build(cur, n_rows, k_idx, fillfactor=fillfactor)
    h0, i0 = sizes(cur)
    base_heap, base_idx = h0 // PAGE, i0 // PAGE
    tot = {"pages_touched": 0, "buf_dirtied": 0, "wal_bytes": 0, "cpu": 0.0,
           "n_vacuums": 0, "bypassed": 0, "index_passes": 0, "removed": 0}
    series, done, idx_debt, vac_log = [], 0, 0, []
    st = stats_row(cur)
    rt, _ = reltuples(cur)
    policy.start({"heap_pages": base_heap, "index_pages": base_idx, "reltuples": rt,
                  "n_live": st["n_live"], "n_dead": st["n_dead"]})
    while done < total_updates:
        m = min(step, total_updates - done)
        applied = apply_updates(cur, m, n_rows, dist=dist, hot_frac=hot_frac)
        done += applied
        idx_debt += applied                 # one dead index entry set per update
        h, i = sizes(cur)
        st = stats_row(cur)
        rt, _ = reltuples(cur)
        ctx = {"n_live": st["n_live"], "n_dead": st["n_dead"], "updates": done,
               "reltuples": rt,
               "heap_pages": h // PAGE, "index_pages": i // PAGE, "idx_debt": idx_debt}
        if policy.needs_footprint:
            ctx["footprint_pages"] = footprint_estimate(cur, ctx["heap_pages"])
        mode = policy.decide(ctx)
        did = False
        if mode:
            v = vacuum(cur, index_cleanup=mode)
            policy.after_vacuum(ctx, v)
            for a, b in (("pages_touched", "pages_touched"), ("buf_dirtied", "buf_dirtied"),
                         ("wal_bytes", "wal_bytes"), ("removed", "removed")):
                tot[a] += v.get(b, 0)
            tot["cpu"] += v.get("cpu_user", 0) + v.get("cpu_sys", 0)
            tot["n_vacuums"] += 1
            if v.get("index_bypassed"):
                tot["bypassed"] += 1
            else:
                tot["index_passes"] += 1
                idx_debt = 0
            did = True
            vac_log.append({"updates": done, "D": ctx["n_dead"], "mode": mode,
                            "pages": v.get("pages_touched", 0),
                            "dirtied": v.get("buf_dirtied", 0),
                            "idx_mode": v.get("idx_mode"),
                            "lpdead_pages": v.get("lpdead_pages"),
                            "removed": v.get("removed"),
                            "footprint_est": ctx.get("footprint_pages")})
        h2, i2 = sizes(cur)
        ap = pgstat(cur, approx=True)
        point = {"updates": done, "heap_pages": h2 // PAGE, "index_pages": i2 // PAGE,
                 "n_dead": ctx["n_dead"], "vacuumed": did,
                 "dead_bytes": float(ap.get("dead_tuple_len", 0)),
                 "free_bytes": float(ap.get("free_space", 0))}
        if len(series) % probe_every == 0:
            point["probe"] = read_probe(cur, n_rows)
        series.append(point)

    def integ(key, initial):
        # Integrate from the clean baseline at workload time zero.  Earlier
        # package revisions initialized prev_v from the first sampled point,
        # which slightly biased the first trapezoid upward.
        tot_, prev_u, prev_v = 0.0, 0, initial
        for p in series:
            tot_ += 0.5 * (prev_v + p[key]) * (p["updates"] - prev_u)
            prev_u, prev_v = p["updates"], p[key]
        return tot_ / max(1, prev_u)

    res = {"policy": policy.name, "n_rows": n_rows, "k_idx": k_idx, "dist": dist,
           "hot_frac": hot_frac, "total_updates": total_updates,
           "base_heap_pages": base_heap, "base_index_pages": base_idx, "totals": tot,
           "avg_heap_pages": integ("heap_pages", base_heap),
           "avg_index_pages": integ("index_pages", base_idx),
           "avg_dead_bytes": integ("dead_bytes", 0.0),
           "avg_free_bytes": integ("free_bytes", 0.0),
           "final_heap_pages": series[-1]["heap_pages"],
           "final_index_pages": series[-1]["index_pages"], "series": series,
           "vac_log": vac_log}
    mu = total_updates / 1e6
    res["vac_pages_per_Mupd"] = tot["pages_touched"] / mu
    res["vac_dirtied_per_Mupd"] = tot["buf_dirtied"] / mu
    res["wal_MB_per_Mupd"] = tot["wal_bytes"] / 1e6 / mu
    res["avg_overhead_pages"] = (res["avg_heap_pages"] - base_heap) + (res["avg_index_pages"] - base_idx)
    probes = [p["probe"] for p in series if "probe" in p]
    if probes:
        res["avg_seq_pages"] = sum(p["seq_pages"] for p in probes) / len(probes)
        res["avg_lookup_pages"] = sum(p["lookup_pages"] for p in probes) / len(probes)
    return res


def main():
    n_rows = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    total = int(sys.argv[2]) if len(sys.argv) > 2 else 3_000_000
    out = sys.argv[3] if len(sys.argv) > 3 else str(DATA_DIR / "exp2.jsonl")
    only = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] != "-" else None
    k_idx = int(sys.argv[5]) if len(sys.argv) > 5 else 2
    conn = connect(); cur = conn.cursor(); apply_settings(cur)
    cur.execute("CREATE EXTENSION IF NOT EXISTS pageinspect")
    random.seed(7)

    LAMS = [1e-4, 1e-3, 1e-2]         # price of one page of space, in pages of I/O
    def policies():
        ps = [CountFrac(0.20, name="pg_default"),
              CountFrac(0.10), CountFrac(0.05), CountFrac(0.02),
              CountFrac(0.01), CountFrac(0.005)]
        ps += [SqrtLaw(l) for l in LAMS]
        ps += [Footprint(l) for l in LAMS]
        ps += [Footprint(l, force_idx=True) for l in (1e-3, 1e-2)]
        ps += [Footprint2(l) for l in LAMS]
        return ps

    workloads = [("uniform", 0.0), ("hot", 0.002), ("mixed", 0.01)]
    done_keys = set()
    if os.path.exists(out):                       # resume: skip finished cells
        for line in open(out):
            try:
                r = json.loads(line)
                done_keys.add((r["dist"], round(r["hot_frac"], 6), r["policy"]))
            except Exception:
                pass
        print(f"resuming: {len(done_keys)} cells already done", flush=True)
    with open(out, "a") as fh:
        for dist, hf in workloads:
            for p in policies():
                if only and only not in p.name:
                    continue
                if (dist, round(hf, 6), p.name) in done_keys:
                    continue
                t0 = time.time()
                r = run_policy(cur, p, n_rows, k_idx, total, dist, hot_frac=hf or 0.002)
                r["wall_s"] = time.time() - t0
                fh.write(jdump(r) + "\n"); fh.flush()
                print(f"{dist:7s}/{hf:g} {p.name:14s} vacs={r['totals']['n_vacuums']:3d} "
                      f"(idx {r['totals']['index_passes']:3d}) "
                      f"pages/Mupd={r['vac_pages_per_Mupd']:9.0f} "
                      f"dirty/Mupd={r['vac_dirtied_per_Mupd']:8.0f} "
                      f"overhead_pages={r['avg_overhead_pages']:8.0f} "
                      f"final(h,i)=({r['final_heap_pages']},{r['final_index_pages']}) "
                      f"[{r['wall_s']:.0f}s]", flush=True)


if __name__ == "__main__":
    main()