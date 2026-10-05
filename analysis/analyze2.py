"""Analysis of E3 (policy comparison): Pareto frontiers, dominance, and the
square-root law check."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
import json, math, sys
import numpy as np

PATH = sys.argv[1] if len(sys.argv) > 1 else str(DATA_DIR / "exp2.jsonl")


def load(path):
    """Keep the last record per (workload, policy); report duplicates as a
    reproducibility check."""
    seen, order, dups = {}, [], []
    for line in open(path):
        r = json.loads(line)
        k = (r["dist"], round(r["hot_frac"], 6), r["policy"])
        if k in seen:
            dups.append((seen[k], r))
        else:
            order.append(k)
        seen[k] = r
    if dups:
        print("=== reproducibility: repeated cells (first vs repeat) ===")
        d_io, d_ov = [], []
        for a, b in dups:
            rio = abs(b["vac_pages_per_Mupd"] - a["vac_pages_per_Mupd"]) / max(1, a["vac_pages_per_Mupd"])
            rov = abs(b["avg_overhead_pages"] - a["avg_overhead_pages"]) / max(1, a["avg_overhead_pages"])
            d_io.append(rio); d_ov.append(rov)
            print(f"  {a['dist']:7s} {a['policy']:14s} I/O {a['vac_pages_per_Mupd']:11.0f} vs "
                  f"{b['vac_pages_per_Mupd']:11.0f} ({100*rio:5.2f}%)   overhead "
                  f"{a['avg_overhead_pages']:8.0f} vs {b['avg_overhead_pages']:8.0f} ({100*rov:5.2f}%)")
        print(f"  n={len(dups)}  median |delta| I/O {100*np.median(d_io):.2f}%  "
              f"space {100*np.median(d_ov):.2f}%\n")
    return [seen[k] for k in order]


def key(r):
    return (r["dist"], r["hot_frac"])


def corrected_overhead_pages(r):
    """Recompute average heap+index overhead from archived series.

    The original runner stored averages using the first sampled state as the
    time-zero value.  The clean baseline is the correct initial state.  This
    function repairs that small first-interval integration bias without
    altering any raw measurements.
    """
    series = r.get("series") or []
    if not series:
        return r["avg_overhead_pages"]

    def integ(field, initial):
        total, prev_u, prev_v = 0.0, 0, float(initial)
        for p in series:
            u, v = p["updates"], float(p[field])
            total += 0.5 * (prev_v + v) * (u - prev_u)
            prev_u, prev_v = u, v
        return total / max(1, prev_u)

    bh, bi = r["base_heap_pages"], r["base_index_pages"]
    return (integ("heap_pages", bh) - bh) + (integ("index_pages", bi) - bi)


def dominated(a, b):
    """True if b dominates a: >= as good on both axes and better on one."""
    return (b["vac_pages_per_Mupd"] <= a["vac_pages_per_Mupd"] and
            b["avg_overhead_pages"] <= a["avg_overhead_pages"] and
            (b["vac_pages_per_Mupd"] < a["vac_pages_per_Mupd"] or
             b["avg_overhead_pages"] < a["avg_overhead_pages"]))


def main():
    rows = load(PATH)
    for r in rows:
        r["avg_overhead_pages"] = corrected_overhead_pages(r)
    wls = sorted({key(r) for r in rows})
    summary = {}
    for wl in wls:
        sub = [r for r in rows if key(r) == wl]
        print(f"\n=== workload {wl[0]} (hot_frac={wl[1]:g}) ===")
        print(f"{'policy':16s} {'vacs':>5s} {'idxpass':>7s} {'pages/Mupd':>12s} "
              f"{'dirty/Mupd':>11s} {'WAL MB/Mupd':>11s} {'overhead_pg':>11s} "
              f"{'heap_final':>10s} {'idx_final':>9s} {'seq_pg':>8s}")
        for r in sorted(sub, key=lambda r: r["vac_pages_per_Mupd"]):
            print(f"{r['policy']:16s} {r['totals']['n_vacuums']:5d} "
                  f"{r['totals']['index_passes']:7d} "
                  f"{r['vac_pages_per_Mupd']:12.0f} {r['vac_dirtied_per_Mupd']:11.0f} "
                  f"{r['wal_MB_per_Mupd']:11.1f} {r['avg_overhead_pages']:11.0f} "
                  f"{r['final_heap_pages']:10d} {r['final_index_pages']:9d} "
                  f"{r.get('avg_seq_pages', float('nan')):8.0f}")
        # dominance of the derived policies over the count family
        counts = [r for r in sub if r["policy"].startswith(("pg_default", "count_"))]
        derived = [r for r in sub if not r["policy"].startswith(("pg_default", "count_"))]
        print("\n  dominance check (does a derived policy dominate a count policy?)")
        found = []
        for d in derived:
            dom = [c["policy"] for c in counts if dominated(c, d)]
            if dom:
                found.append((d["policy"], dom))
                print(f"   {d['policy']:14s} dominates: {', '.join(dom)}")
        if not found:
            print("   none")
        summary[f"{wl[0]}_{wl[1]:g}"] = {
            "rows": [{k: r[k] for k in ("policy", "vac_pages_per_Mupd", "vac_dirtied_per_Mupd",
                                        "wal_MB_per_Mupd", "avg_overhead_pages",
                                        "final_heap_pages", "final_index_pages")
                      } | {"n_vacuums": r["totals"]["n_vacuums"],
                           "index_passes": r["totals"]["index_passes"],
                           "avg_seq_pages": r.get("avg_seq_pages")}
                     for r in sub],
            "dominance": found}

        # cost at a common space budget: interpolate the count family at the
        # overhead achieved by each derived policy
        cf = sorted(counts, key=lambda r: r["avg_overhead_pages"])
        xs = [c["avg_overhead_pages"] for c in cf]
        ys = [c["vac_pages_per_Mupd"] for c in cf]
        print("\n  I/O at matched space (count-family interpolation):")
        for d in derived:
            x = d["avg_overhead_pages"]
            if x < min(xs) or x > max(xs):
                note = " (outside count-family range)"
                y = np.interp(x, xs, ys)
            else:
                note = ""
                y = np.interp(x, xs, ys)
            print(f"   {d['policy']:14s} overhead={x:8.0f}  own I/O={d['vac_pages_per_Mupd']:10.0f}  "
                  f"count-family I/O={y:10.0f}  ratio={y/max(1,d['vac_pages_per_Mupd']):5.2f}x{note}")

        # does the derived policy sit against the linear-space benchmark?
        print("\n  linear-space benchmark in situ: for each lambda, the count-family J-minimiser vs "
              "the derived policy")
        for lam in (1e-4, 1e-3, 1e-2):
            Jc = [(r["vac_pages_per_Mupd"] / 1e6 + lam * r["avg_overhead_pages"], r)
                  for r in counts]
            best = min(Jc, key=lambda t: t[0])
            for pref in ("sqrt_", "fp_", "fp2_", "fpidx_"):
                pol = [r for r in derived if r["policy"] == f"{pref}{lam:g}"]
                if not pol:
                    continue
                p0 = pol[0]
                Jp = p0["vac_pages_per_Mupd"] / 1e6 + lam * p0["avg_overhead_pages"]
                print(f"   lambda={lam:<7g} best count rule = {best[1]['policy']:12s} "
                      f"J={best[0]:9.3f} | {p0['policy']:12s} J={Jp:9.3f} "
                      f"-> {best[0]/Jp:5.2f}x " +
                      ("(derived better)" if Jp < best[0] else "(count better)"))


    # ---- the decisive comparison: ONE policy across ALL workloads --------
    print("\n\n=== one policy, three workloads (count-only comparison in practice) ===")
    print("For each price of space lambda, a count rule must commit to a single")
    print("fraction for every relation; the derived rules adapt per relation.")
    wl_keys = sorted({key(r) for r in rows})
    by = {}
    for r in rows:
        by[(key(r), r["policy"])] = r
    cross_tbl = {}
    for lam in (1e-4, 1e-3, 1e-2):
        def J(r):
            return r["vac_pages_per_Mupd"] / 1e6 + lam * r["avg_overhead_pages"]
        pol_names = sorted({r["policy"] for r in rows})
        totals = {}
        for pol in pol_names:
            vals = [by.get((w, pol)) for w in wl_keys]
            if any(v is None for v in vals):
                continue
            totals[pol] = (sum(J(v) for v in vals), [J(v) for v in vals])
        counts_only = {p: v for p, v in totals.items()
                       if p.startswith(("pg_default", "count_"))}
        best_count = min(counts_only.items(), key=lambda kv: kv[1][0])
        print(f"\n lambda = {lam:g}   (J = vacuum pages per update + lambda * space pages)")
        print(f"  {'policy':14s} " + " ".join(f"{w[0][:7]:>10s}" for w in wl_keys) + f" {'TOTAL':>10s}")
        show = [best_count[0], "pg_default"] + [f"{p}{lam:g}" for p in ("sqrt_", "fp_", "fp2_", "fpidx_")]
        for pol in dict.fromkeys(show):
            if pol not in totals:
                continue
            tot_, per = totals[pol]
            print(f"  {pol:14s} " + " ".join(f"{v:10.3f}" for v in per) + f" {tot_:10.3f}"
                  + ("   <- best count rule" if pol == best_count[0] else ""))
        for pref in ("sqrt_", "fp_", "fp2_", "fpidx_"):
            pol = f"{pref}{lam:g}"
            if pol in totals:
                print(f"   best count rule / {pol}: {best_count[1][0]/totals[pol][0]:.2f}x")
        cross_tbl[f"{lam:g}"] = {"best_count": best_count[0],
                                 "totals": {p: v[0] for p, v in totals.items()},
                                 "per_workload": {p: v[1] for p, v in totals.items()},
                                 "workloads": [f"{w[0]}_{w[1]:g}" for w in wl_keys]}

        # Stricter comparator: allow the count family to select a different
        # threshold for each workload/relation.  This is the fair oracle used
        # in the revised manuscript to bound the benefit of automatic
        # cross-relation adaptation.
        oracle_total = 0.0
        oracle_detail = []
        sqrt_pol = f"sqrt_{lam:g}"
        sqrt_total = 0.0
        for w in wl_keys:
            wc = [r for r in rows if key(r) == w and
                  r["policy"].startswith(("pg_default", "count_"))]
            best_w = min(wc, key=J)
            oracle_total += J(best_w)
            sqrt_row = by[(w, sqrt_pol)]
            sqrt_total += J(sqrt_row)
            oracle_detail.append({"workload": f"{w[0]}_{w[1]:g}",
                                  "best_count": best_w["policy"],
                                  "count_J": J(best_w),
                                  "sqrt_J": J(sqrt_row)})
        ratio = oracle_total / sqrt_total
        print(f"   per-workload count oracle / {sqrt_pol}: {ratio:.4f}x "
              f"({(ratio-1)*100:+.2f}% vs square-root)")
        cross_tbl[f"{lam:g}"]["per_workload_count_oracle"] = {
            "total_J": oracle_total, "sqrt_total_J": sqrt_total,
            "oracle_over_sqrt": ratio, "detail": oracle_detail}
    summary["cross_workload"] = cross_tbl

    json.dump(summary, open(PATH.replace(".jsonl", "_summary.json"), "w"), indent=1, default=float)
    print(f"\nwrote {PATH.replace('.jsonl', '_summary.json')}")


if __name__ == "__main__":
    main()