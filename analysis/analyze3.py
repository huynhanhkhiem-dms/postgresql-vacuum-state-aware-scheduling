"""Analysis of E4 (scaling) and E5 (removability)."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
import json, math, sys
import numpy as np


def load(path):
    rows = []
    for line in open(path):
        r = json.loads(line)
        r.pop("series", None)
        rows.append(r)
    return rows


def scaling(path=str(DATA_DIR / "exp3_scaling.jsonl")):
    rows = load(path)
    Ns = sorted({r["n_rows"] for r in rows})
    print("=== measured pass cost and the implied optimum ===")
    out = {}
    for n in Ns:
        sub = sorted([r for r in rows if r["n_rows"] == n], key=lambda r: r["threshold"])
        P = sub[0]["base_heap_pages"]; I = sub[0]["base_index_pages"]
        rho = n / P
        print(f"\n N={n:>9,d}  heap_pages={P:>7,d} index_pages={I:>7,d} rho={rho:.1f}")
        print(f"  {'D':>8s} {'vacs':>5s} {'io/upd':>9s} {'Cbar_est':>10s} {'overhead_pg':>11s}")
        Cbars = []
        for r in sub:
            io_per_upd = r["vac_pages_per_Mupd"] / 1e6
            Cbar = io_per_upd * r["threshold"]
            Cbars.append((r["threshold"], Cbar, io_per_upd, r["avg_overhead_pages"],
                          r["totals"]["n_vacuums"]))
            print(f"  {r['threshold']:>8,d} {r['totals']['n_vacuums']:>5d} {io_per_upd:>9.3f} "
                  f"{Cbar:>10,.0f} {r['avg_overhead_pages']:>11,.0f}")
        # Use the largest threshold that actually triggered at least one pass.
        # A threshold above the finite run horizon has zero observed passes and
        # must not be interpreted as a zero saturated pass cost.
        positive = [x for x in Cbars if x[4] > 0 and x[1] > 0]
        Cbar_sat = positive[-1][1] if positive else float("nan")
        row = {"n_rows": n, "P": P, "I": I, "rho": rho, "Cbar_sat": Cbar_sat, "opt": {}}
        for lam in (1e-4, 3e-4, 1e-3, 3e-3, 1e-2):
            J = [(io + lam * ov, D) for (D, _, io, ov, _) in Cbars]
            d_emp = min(J)[1]
            d_thy = math.sqrt(2 * rho * Cbar_sat / lam)
            row["opt"][f"{lam:g}"] = {"empirical": d_emp, "theory": d_thy}
            print(f"   lambda={lam:<7g} argmin_D(measured)={d_emp:>8,d}   "
                  f"sqrt(2*rho*Cbar/lam)={d_thy:>10,.0f}")
        out[n] = row
    print("\n=== scaling of the optimum with N ===")
    for lam in (1e-4, 3e-4, 1e-3, 3e-3, 1e-2):
        xs = np.log([out[n]["n_rows"] for n in Ns])
        ye = np.log([out[n]["opt"][f"{lam:g}"]["empirical"] for n in Ns])
        yt = np.log([out[n]["opt"][f"{lam:g}"]["theory"] for n in Ns])
        se = np.polyfit(xs, ye, 1)[0]
        st = np.polyfit(xs, yt, 1)[0]
        print(f"  lambda={lam:<7g} d log D*/d log N :  measured {se:+.3f}   model {st:+.3f} "
              f"(0.5 = square-root law, 1.0 = PostgreSQL's linear rule)")
    json.dump(out, open(path.replace(".jsonl", "_summary.json"), "w"), indent=1, default=float)


def removability(path=str(DATA_DIR / "exp3_remov.jsonl")):
    rows = load(path)
    print("\n=== E5: passes under a held snapshot ===")
    for r in rows:
        t = r["totals"]
        print(f" {r['tag']:12s} snapshot_held={str(r['hold_snapshot']):5s} "
              f"passes={t['n_vacuums']:3d} futile={t['futile']:3d} "
              f"futile_pages={t['futile_pages']:>9,d} ({100*t['futile_pages']/max(1,t['pages_touched']):4.1f}% of all pages) "
              f"removed={t['removed']:>9,d} "
              f"final_heap={r['final_heap_pages']:>7,d} (base {r['base_heap_pages']:,d})")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    if which in ("both", "scaling"):
        scaling()
    if which in ("both", "removability"):
        removability()