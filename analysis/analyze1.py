"""Analysis of E1/E2: cost structure, footprint model, placement effect."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
import json, math, sys, re
import numpy as np

PATH = sys.argv[1] if len(sys.argv) > 1 else str(DATA_DIR / "exp1.jsonl")
PAT_IDX = re.compile(r"index scan (needed|bypassed|not needed): (\d+) pages? from table "
                     r"\(([\d.]+)% of total\) ha[dves]+ (\d+) dead item identifiers")
PAT_SCAN = re.compile(r"(\d+) removable, (\d+) nonremovable row versions in (\d+) out of (\d+) pages")


def load(path):
    rows = []
    for line in open(path):
        r = json.loads(line)
        raw = r.get("raw", "")
        m = PAT_IDX.search(raw)
        if m:
            r["idx_mode"] = m.group(1)
            r["lpdead_pages"] = int(m.group(2))
            r["lpdead_pct"] = float(m.group(3))
            r["lpdead_items"] = int(m.group(4))
        m = PAT_SCAN.search(raw)
        if m:
            r["removable"] = int(m.group(1))
            r["nonremovable"] = int(m.group(2))
            r["pages_scanned"] = int(m.group(3))
            r["rel_pages"] = int(m.group(4))
        v = r.get("vac", {})
        r["touched"] = v.get("pages_touched", 0)
        r["dirtied"] = v.get("buf_dirtied", 0)
        r["wal"] = v.get("wal_bytes", 0)
        r["wal_fpi"] = v.get("wal_fpi", 0)
        r["wal_nonfpi"] = r["wal"] - r["wal_fpi"] * 8192
        r["cpu"] = v.get("cpu_user", 0) + v.get("cpu_sys", 0)
        r["bypass"] = v.get("index_bypassed", False)
        # D in this experiment is the applied-update target accumulated before the pass.
        # `dead1` is a post-workload/surviving dead-tuple measurement and can be reduced
        # by opportunistic pruning; using it as the denominator would mix the trigger
        # state with the physical residue being measured.
        r["D_applied"] = r.get("d_target") or r.get("applied_updates") or r.get("n_dead_stat") or r.get("dead1")
        rows.append(r)
    return rows


def fit_affine(x, y):
    x = np.asarray(x, float); y = np.asarray(y, float)
    A = np.vstack([np.ones_like(x), x]).T
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    pred = A @ coef
    ss_res = ((y - pred) ** 2).sum(); ss_tot = ((y - y.mean()) ** 2).sum()
    return coef[0], coef[1], 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")


def main():
    rows = load(PATH)
    out = {}
    print(f"loaded {len(rows)} runs\n")

    print("=== A. cost structure  C(D) = alpha + beta*D   (pages touched; D = applied updates) ===")
    tbl = []
    for k in sorted({r["k_idx"] for r in rows if r["tag"] == "A_cost"}):
        for dist in ("uniform", "hot"):
            sub = [r for r in rows if r["tag"] == "A_cost" and r["k_idx"] == k and r["dist"] == dist]
            if len(sub) < 3:
                continue
            sub.sort(key=lambda r: r["d_target"])
            a, b, r2 = fit_affine([r["D_applied"] for r in sub], [r["touched"] for r in sub])
            Pheap = sub[0]["heap_pages0"]; Pidx = sub[0]["index_pages0"]
            # Compare the smallest and largest batching targets directly.  This is
            # the quantity reported in the manuscript: logical page work per applied
            # update accumulated before the pass.
            small, large = sub[0], sub[-1]
            per_max = small["touched"] / small["D_applied"]
            per_min = large["touched"] / large["D_applied"]
            tbl.append({"k": k, "dist": dist, "alpha": a, "beta": b, "R2": r2,
                        "heap_pages": Pheap, "index_pages": Pidx,
                        "cost_per_update_small_D": per_max, "cost_per_update_large_D": per_min,
                        "ratio": per_max / per_min})
            print(f" k={k} {dist:8s} alpha={a:9.0f} beta={b:7.3f} R2={r2:5.3f} "
                  f"P={int(Pheap):6d} I={int(Pidx):6d}  pages/applied-update: {per_max:8.2f} -> {per_min:6.3f} "
                  f"({per_max/per_min:6.1f}x cheaper when batched)")
    out["cost_structure"] = tbl

    print("\n=== fixed cost alpha vs index pages ===")
    uni = [t for t in tbl if t["dist"] == "uniform"]
    if len(uni) >= 3:
        a0, a1, r2 = fit_affine([t["index_pages"] for t in uni], [t["alpha"] for t in uni])
        print(f" alpha ~= {a0:.0f} + {a1:.3f} * index_pages   (R2={r2:.3f})")
        print(f" heap pages = {uni[0]['heap_pages']}, so intercept/heap = {a0/uni[0]['heap_pages']:.2f}")
        out["alpha_vs_index"] = {"intercept": a0, "slope": a1, "R2": r2}

    print("\n=== B. placement: same D, uniform vs hot(5%) ===")
    cmp_rows = []
    for k in sorted({r["k_idx"] for r in rows if r["tag"] == "A_cost"}):
        for d in sorted({r["d_target"] for r in rows if r["tag"] == "A_cost"}):
            u = [r for r in rows if r["tag"] == "A_cost" and r["k_idx"] == k and r["dist"] == "uniform" and r["d_target"] == d]
            h = [r for r in rows if r["tag"] == "A_cost" and r["k_idx"] == k and r["dist"] == "hot" and r["d_target"] == d]
            if u and h and h[0]["touched"] > 0:
                cmp_rows.append({"k": k, "D": d, "uniform": u[0]["touched"], "hot": h[0]["touched"],
                                 "ratio": u[0]["touched"] / h[0]["touched"],
                                 "uni_lpdead_pages": u[0].get("lpdead_pages"),
                                 "hot_lpdead_pages": h[0].get("lpdead_pages"),
                                 "uni_bypass": u[0]["bypass"], "hot_bypass": h[0]["bypass"]})
    for c in cmp_rows:
        print(f" k={c['k']} D={int(c['D']):>7d} uniform={int(c['uniform']):>8d} hot={int(c['hot']):>8d} "
              f"ratio={c['ratio']:6.2f}x  lpdead_pages {c['uni_lpdead_pages']} vs {c['hot_lpdead_pages']}")
    out["placement"] = cmp_rows

    print("\n=== C. footprint model A(D) = P(1-exp(-D/P)) ===")
    fp = []
    for r in rows:
        if r["tag"] == "A_cost" and r["dist"] == "uniform" and "lpdead_pages" in r:
            P = r["heap_pages1"]; D = r["d_target"]
            pred = P * (1 - math.exp(-D / P))
            fp.append({"D": D, "P": P, "obs": r["lpdead_pages"], "pred": pred,
                       "rel_err": (r["lpdead_pages"] - pred) / max(1, pred)})
    if fp:
        errs = [abs(f["rel_err"]) for f in fp]
        print(f" n={len(fp)}  median |rel err| = {np.median(errs):.3f}  max = {max(errs):.3f}")
        for f in fp[:10]:
            print(f"   D={f['D']:>8.0f} obs={f['obs']:>7d} pred={f['pred']:>9.0f} err={f['rel_err']:+.3f}")
    out["footprint_fit"] = fp

    print("\n=== D. HOT updates (tag B_hot) ===")
    for r in [r for r in rows if r["tag"] == "B_hot"]:
        print(f" D={r['d_target']:>7d} hot_upd={r['n_hot_upd']:>8} touched={r['touched']:>8d} "
              f"heap_growth={r['heap_pages1'] - r['heap_pages0']:>6d} bypass={r['bypass']} "
              f"lpdead_pages={r.get('lpdead_pages')}")

    print("\n=== E. snapshot-blocked dead tuples (tag C_blocked) ===")
    for r in [r for r in rows if r["tag"] == "C_blocked"]:
        freed = r["heap_pages1"] - r["heap_pages2"]
        print(f" D={r['d_target']:>7d} touched={r['touched']:>8d} removable={r.get('removable')} "
              f"nonremovable={r.get('nonremovable')} freed_pages={freed} "
              f"free_bytes_delta={r['free2'] - r['free1']:>12.0f}")

    json.dump(out, open(PATH.replace(".jsonl", "_summary.json"), "w"), indent=1, default=float)
    print(f"\nwrote {PATH.replace('.jsonl', '_summary.json')}")


if __name__ == "__main__":
    main()