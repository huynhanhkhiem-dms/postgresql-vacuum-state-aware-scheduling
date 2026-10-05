"""E7 analysis: the optimum computed from the MEASURED cost curve C(D).

For each relation size we have C(D) measured directly (build, D updates, one
pass).  The renewal-reward objective is
    J(D) = C(D)/D + lambda * D / (2 rho)
which we minimise over a log-interpolation of the measured C(D).  This avoids the
short-run bias of the steady-state search in E4.
"""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
import json, math, sys
import numpy as np

PATH = sys.argv[1] if len(sys.argv) > 1 else str(DATA_DIR / "exp5.jsonl")
LAMS = (1e-4, 3e-4, 1e-3, 3e-3, 1e-2)


def main():
    rows = [json.loads(l) for l in open(PATH)]
    for r in rows:
        r["touched"] = r["vac"].get("pages_touched", 0)
    Ns = sorted({r["n_rows"] for r in rows})
    out = {}
    print("=== measured cost curve C(D) ===")
    for n in Ns:
        sub = sorted([r for r in rows if r["n_rows"] == n], key=lambda r: r["applied"])
        P = sub[0]["heap_pages0"]; I = sub[0]["index_pages0"]
        rho = n / P
        print(f"\n N={n:>9,d}  P={P:>7,d}  I={I:>7,d}  rho={rho:.1f}")
        for r in sub:
            print(f"   D={r['applied']:>9,d}  C={r['touched']:>9,d}  "
                  f"C/D={r['touched']/r['applied']:7.3f}")
        D = np.array([r["applied"] for r in sub], float)
        C = np.array([r["touched"] for r in sub], float)
        grid = np.exp(np.linspace(np.log(D.min()), np.log(D.max()), 2000))
        Ci = np.exp(np.interp(np.log(grid), np.log(D), np.log(C)))
        row = {"n_rows": n, "P": P, "I": I, "rho": rho,
               "C_sat": float(C[-1]), "opt": {}}
        for lam in LAMS:
            J = Ci / grid + lam * grid / (2.0 * rho)
            i = int(np.argmin(J))
            interior = 0 < i < len(grid) - 1
            d_meas = float(grid[i])
            d_thy = math.sqrt(2 * rho * C[-1] / lam)
            row["opt"][f"{lam:g}"] = {"measured": d_meas, "theory": d_thy,
                                      "interior": interior}
            print(f"   lambda={lam:<7g} argmin over measured C(D) = {d_meas:>10,.0f}"
                  f"{'' if interior else '  (grid edge)'}"
                  f"   sqrt(2 rho Csat/lam) = {d_thy:>10,.0f}")
        out[n] = row

    print("\n=== scaling of the measured optimum with N ===")
    xs = np.log(np.array(Ns, float))
    for lam in LAMS:
        ym = np.log([out[n]["opt"][f"{lam:g}"]["measured"] for n in Ns])
        yt = np.log([out[n]["opt"][f"{lam:g}"]["theory"] for n in Ns])
        edge = any(not out[n]["opt"][f"{lam:g}"]["interior"] for n in Ns)
        sm = np.polyfit(xs, ym, 1)[0]; st = np.polyfit(xs, yt, 1)[0]
        print(f"  lambda={lam:<7g}  d log D*/d log N : measured {sm:+.3f}   "
              f"closed form {st:+.3f}{'   [edge]' if edge else ''}")
    print("  (0.5 = square-root law, 1.0 = PostgreSQL's linear rule)")

    print("\n=== regret of PostgreSQL's default at each size ===")
    print(f"  {'N':>10s} {'lambda':>8s} {'D_pg=0.2N':>11s} {'D*':>10s} {'x':>7s} {'regret (x+1/x)/2':>17s}")
    for n in Ns:
        for lam in LAMS:
            d_pg = 0.2 * n
            d_st = out[n]["opt"][f"{lam:g}"]["measured"]
            x = d_pg / d_st
            print(f"  {n:>10,d} {lam:>8g} {d_pg:>11,.0f} {d_st:>10,.0f} {x:>7.2f} "
                  f"{(x + 1 / x) / 2:>17.2f}")
    json.dump(out, open(PATH.replace(".jsonl", "_summary.json"), "w"), indent=1, default=float)


if __name__ == "__main__":
    main()