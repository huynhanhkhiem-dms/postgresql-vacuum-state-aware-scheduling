"""Fail-fast checks for the manuscript's headline numeric claims.

This script reads only archived raw measurements.  It does not rerun PostgreSQL.
A non-zero exit status means the reproducibility package and manuscript should
not be submitted together until the discrepancy is resolved.
"""
from pathlib import Path
import json, math

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

def jsonl(name):
    return [json.loads(line) for line in open(DATA / name, encoding="utf-8") if line.strip()]

def near(x, target, tol, label):
    if abs(x-target) > tol:
        raise AssertionError(f"{label}: got {x}, expected {target} ± {tol}")
    print(f"PASS  {label}: {x:.4g}")

# 1) Batching result in Fig. 1 / cost-structure discussion.
e1 = jsonl("exp1.jsonl")
ratios = {}
for k in (0, 2, 8):
    sub = sorted((r for r in e1 if r.get("tag") == "A_cost" and r.get("dist") == "uniform" and r.get("k_idx") == k),
                 key=lambda r: r["d_target"])
    small, large = sub[0], sub[-1]
    c_small = small["vac"]["pages_touched"] / small["d_target"]
    c_large = large["vac"]["pages_touched"] / large["d_target"]
    ratios[k] = c_small / c_large
near(ratios[0], 92.0, 0.2, "batching ratio, k=0")
near(ratios[2], 114.3, 0.2, "batching ratio, k=2")
near(ratios[8], 173.8, 0.3, "batching ratio, k=8")

# 2) Same trigger-visible count, placement-only spread at D=50,000.
e1b = jsonl("exp1b.jsonl")
sub = [r for r in e1b if r.get("d_target") == 50000 and r.get("k_idx") == 2]
vals = [r["vac"]["pages_touched"] for r in sub]
spread = max(vals) / min(vals)
near(min(vals), 251, 0, "placement minimum pages")
near(max(vals), 109195, 0, "placement maximum pages")
near(spread, 435.04, 0.2, "placement cost spread")

# 3) n_dead_tup-as-bytes overstatement at one million updates.
e4 = jsonl("exp4.jsonl")
for dist, target in (("uniform", 38.94), ("hot", 410.90)):
    r = next(r for r in e4 if r["dist"] == dist and r["updates"] == 1_000_000)
    near(r["overstatement"], target, 0.05, f"dead-byte overstatement, {dist}")


# 3a) Statistics-visible count follows the applied-update target in the archived
# counter experiment for both uniform and hot placement. This supports the
# manuscript's cross-experiment linkage without pretending exp1b logged n_dead_tup.
for dist in ("uniform", "hot"):
    r = next(r for r in e4 if r["dist"] == dist and r["updates"] == 50_000)
    if int(r["n_dead_tup"]) != 50_000:
        raise AssertionError(f"{dist}: expected n_dead_tup=50000, got {r['n_dead_tup']}")
print("PASS  counter linkage: n_dead_tup=50,000 for uniform and hot at 50,000 updates")

# 4) Held-snapshot futile passes and avoided work.
e3 = jsonl("exp3_remov.jsonl")
count = next(r for r in e3 if r["tag"] == "count_held")
guard = next(r for r in e3 if r["tag"] == "xmin_held")
if count["totals"]["n_vacuums"] != 60 or count["totals"]["futile"] != 29:
    raise AssertionError("held-snapshot count trigger should have 60 passes, 29 futile")
frac = count["totals"]["futile_pages"] / count["totals"]["pages_touched"]
near(frac, 0.324, 0.002, "futile work share")
if guard["totals"]["n_vacuums"] != 32 or guard["totals"]["futile"] != 1:
    raise AssertionError("horizon guard should have 32 passes, 1 futile")
if (count["final_heap_pages"], count["final_index_pages"]) != (guard["final_heap_pages"], guard["final_index_pages"]):
    raise AssertionError("count and horizon-guard runs should end at the same relation size")
print("PASS  horizon guard: 60/29 -> 32/1 passes/futile; identical final relation size")


# 5) Corrected policy-analysis totals used in Tables 4-5.
def corrected_overhead_pages(r):
    series = r.get("series") or []
    if not series:
        return r["avg_overhead_pages"]

    def integ(field, initial):
        total, prev_u, prev_v = 0.0, 0, float(initial)
        for p in series:
            u, vv = p["updates"], float(p[field])
            total += 0.5 * (prev_v + vv) * (u - prev_u)
            prev_u, prev_v = u, vv
        return total / max(1, prev_u)

    bh, bi = r["base_heap_pages"], r["base_index_pages"]
    return (integ("heap_pages", bh) - bh) + (integ("index_pages", bi) - bi)

def policy_totals(name, lam):
    rows = jsonl(name)
    # Match analyze2.py: keep the last record per workload/policy.
    dedup = {}
    for r in rows:
        dedup[(r["dist"], round(r["hot_frac"], 6), r["policy"])] = r
    rows = list(dedup.values())
    for r in rows:
        r["avg_overhead_pages"] = corrected_overhead_pages(r)
    workloads = sorted({(r["dist"], round(r["hot_frac"], 6)) for r in rows})
    def J(r):
        return r["vac_pages_per_Mupd"] / 1e6 + lam * r["avg_overhead_pages"]
    count_names = sorted({r["policy"] for r in rows if r["policy"].startswith(("pg_default", "count_"))})
    totals = {}
    for pol in count_names:
        vals=[]
        for w in workloads:
            rr=[r for r in rows if (r["dist"], round(r["hot_frac"], 6))==w and r["policy"]==pol]
            if not rr: break
            vals.append(J(rr[0]))
        if len(vals)==len(workloads): totals[pol]=sum(vals)
    best=min(totals.items(), key=lambda kv: kv[1])
    sqrt=[]
    for w in workloads:
        rr=next(r for r in rows if (r["dist"], round(r["hot_frac"], 6))==w and r["policy"]==f"sqrt_{lam:g}")
        sqrt.append(J(rr))
    return best[0], best[1], sum(sqrt)

for name, targets in (
    ("exp2.jsonl", [(1e-4, "pg_default", 1.132, 0.998), (1e-3, "count_0.1", 3.091, 2.981), (1e-2, "count_0.02", 14.637, 14.367)]),
    ("exp2_k8.jsonl", [(1e-4, "pg_default", 1.441, 1.266), (1e-3, "count_0.1", 4.156, 3.971), (1e-2, "count_0.02", 18.625, 18.074)]),
):
    for lam, pol, best_target, sqrt_target in targets:
        got_pol, best, sqrt = policy_totals(name, lam)
        if got_pol != pol:
            raise AssertionError(f"{name}, lambda={lam:g}: best count policy {got_pol}, expected {pol}")
        near(best, best_target, 0.0006, f"{name} corrected best-count J at lambda={lam:g}")
        near(sqrt, sqrt_target, 0.0006, f"{name} corrected square-root J at lambda={lam:g}")


# 6) Multi-relation seed variability reported in the revised manuscript.
import statistics
sim = json.load(open(DATA / "sim_multi.json", encoding="utf-8"))
for pol, target_sd in (("pg16", 52.1179), ("pg19", 52.1374), ("count_ratio", 52.0509),
                       ("star_catalog", 73.4886), ("ratio", 12.5869)):
    vals = [r[pol]["total_per_tick"] for r in sim["runs"]]
    sd = statistics.stdev(vals)
    near(sd, target_sd, 0.001, f"multi-relation total SD, {pol}")
paired = [r["star_catalog"]["total_per_tick"] - r["ratio"]["total_per_tick"] for r in sim["runs"]]
if not all(x > 0 for x in paired):
    raise AssertionError("D* + ratio ordering should beat D* + catalog in all five archived seeds")
near(sum(paired)/len(paired), 38.6764, 0.001, "paired D* catalog-minus-ratio mean")
print("PASS  D* + ratio ordering improves all five archived seeds")

print("\nAll archived-data headline checks passed.")
