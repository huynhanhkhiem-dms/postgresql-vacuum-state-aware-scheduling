"""Generate the manuscript figures from the archived measurements.
Every series is directly labelled, so colour is not the sole carrier of identity."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
import json, math, os, re, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = str(ROOT / "figures")
Path(OUT).mkdir(parents=True, exist_ok=True)
os.makedirs(OUT, exist_ok=True)
C = ["#2a78d6", "#eb6834", "#1baf7a", "#4a3aa7"]
GRID = dict(color="#d9d8d4", lw=0.6)
plt.rcParams.update({
    "figure.dpi": 200, "savefig.dpi": 600, "font.size": 9,
    "axes.edgecolor": "#52514e", "axes.labelcolor": "#0b0b0b",
    "xtick.color": "#52514e", "ytick.color": "#52514e",
    "axes.spines.top": False, "axes.spines.right": False,
    "figure.facecolor": "white",
})

PAT_IDX = re.compile(r"index scan (needed|bypassed|not needed): (\d+) pages? from table "
                     r"\(([\d.]+)% of total\) ha[dves]+ (\d+) dead item identifiers")


def load(path, strip_series=True):
    rows = []
    for line in open(path):
        r = json.loads(line)
        if strip_series:
            r.pop("series", None)
        raw = r.get("raw", "")
        m = PAT_IDX.search(raw)
        if m:
            r["idx_mode"], r["lpdead_pages"] = m.group(1), int(m.group(2))
        v = r.get("vac", {})
        r["touched"] = v.get("pages_touched", 0)
        r["dirtied"] = v.get("buf_dirtied", 0)
        r["misses"] = v.get("buf_misses", 0)
        r["cost_units"] = (v.get("buf_hits", 0) + 2 * v.get("buf_misses", 0)
                           + 20 * v.get("buf_dirtied", 0))
        r["D_applied"] = r.get("d_target") or r.get("applied_updates") or r.get("dead1")   # workload-time/trigger state
        rows.append(r)
    return rows


def fig1_cost_structure(path=str(DATA_DIR / "exp1.jsonl")):
    rows = [r for r in load(path) if r.get("tag") == "A_cost" and r["dist"] == "uniform"]
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.9))
    ax = axes[0]
    for c, k in zip(C, (0, 2, 8)):
        sub = sorted([r for r in rows if r["k_idx"] == k], key=lambda r: r["D_applied"])
        x = [r["D_applied"] for r in sub]; y = [r["touched"] for r in sub]
        ax.plot(x, y, "-o", color=c, lw=2, ms=4)
        ax.annotate(f"{k} secondary indexes" if k else "no secondary index",
                    (x[-1], y[-1]), textcoords="offset points", xytext=(-4, 6),
                    ha="right", color=c, fontsize=8, fontweight="bold")
    ax.set_xscale("log"); ax.grid(True, **GRID); ax.set_axisbelow(True)
    ax.set_xlabel("dead tuples accumulated before the pass, $D$")
    ax.set_ylabel("pages touched by the pass")
    ax = axes[1]
    for c, k in zip(C, (0, 2, 8)):
        sub = sorted([r for r in rows if r["k_idx"] == k], key=lambda r: r["D_applied"])
        x = [r["D_applied"] for r in sub]
        y = [r["touched"] / r["D_applied"] for r in sub]
        ax.plot(x, y, "-o", color=c, lw=2, ms=4)
        ax.annotate(f"k={k}", (x[-1], y[-1]), textcoords="offset points", xytext=(4, 0),
                    color=c, fontsize=8, fontweight="bold")
    ax.set_xscale("log"); ax.set_yscale("log"); ax.grid(True, **GRID); ax.set_axisbelow(True)
    ax.set_xlabel("dead tuples accumulated before the pass, $D$")
    ax.set_ylabel("pages touched per dead tuple")
    fig.tight_layout(); fig.savefig(f"{OUT}/fig1_cost_structure.png"); plt.close(fig)


def fig2_placement(path=str(DATA_DIR / "exp1b.jsonl")):
    rows = load(path)
    fig, axes = plt.subplots(1, 2, figsize=(7.4, 3.0))
    ax = axes[0]
    for c, d, dy in zip(C, (10000, 50000, 100000), (-14, 4, 20)):
        sub = sorted([r for r in rows if r["d_target"] == d], key=lambda r: r["hot_frac"])
        x = [r["hot_frac"] for r in sub]; y = [r["touched"] for r in sub]
        ax.plot(x, y, "-o", color=c, lw=2, ms=4)
        ax.annotate(f"$D$={d//1000}k updates", (x[0], y[0]), textcoords="offset points",
                    xytext=(8, dy), ha="left", va="center", color=c, fontsize=8,
                    fontweight="bold")
    ax.axvline(0.02, color="#52514e", ls=":", lw=1)
    ax.annotate("index-vacuum bypass\nthreshold (2 % of pages)", (0.021, 300),
                fontsize=7.5, color="#52514e")
    ax.set_xscale("log"); ax.set_yscale("log"); ax.grid(True, **GRID); ax.set_axisbelow(True)
    ax.set_xlabel("fraction of the relation the updates touch")
    ax.set_ylabel("pages touched by the pass")
    ax = axes[1]
    sub = sorted([r for r in rows if r["d_target"] == 50000], key=lambda r: r["hot_frac"])
    x = np.arange(len(sub))
    cols = [C[2] if r.get("idx_mode") == "bypassed" else C[0] for r in sub]
    ax.bar(x, [r["touched"] for r in sub], color=cols, width=0.62)
    for i, r in enumerate(sub):
        ax.annotate(f"{int(r['touched']):,}", (i, r["touched"]), ha="center",
                    va="bottom", fontsize=7.5, color="#0b0b0b")
    ax.set_xticks(x)
    ax.set_xticklabels([f"{r['hot_frac']:g}" for r in sub], fontsize=8)
    ax.set_yscale("log"); ax.set_ylim(top=ax.get_ylim()[1] * 3)
    ax.grid(True, axis="y", **GRID); ax.set_axisbelow(True)
    ax.set_xlabel("fraction of the relation updated")
    ax.set_ylabel("pages touched")
    ax.annotate("index pass bypassed", (0.5, sub[1]["touched"]), textcoords="offset points",
                xytext=(0, 26), ha="center", fontsize=7.5, color=C[2], fontweight="bold")
    ax.annotate("index pass taken", (4.0, sub[5]["touched"]), textcoords="offset points",
                xytext=(-10, 44), ha="center", fontsize=7.5, color=C[0], fontweight="bold")
    fig.tight_layout(); fig.savefig(f"{OUT}/fig2_placement.png"); plt.close(fig)


def fig3_pareto(path=str(DATA_DIR / "exp2.jsonl")):
    seen = {}
    for r in load(path, strip_series=True):
        seen[(r["dist"], round(r["hot_frac"], 6), r["policy"])] = r
    rows = list(seen.values())
    wls = sorted({(r["dist"], r["hot_frac"]) for r in rows})
    from matplotlib.ticker import FuncFormatter
    fig, axes = plt.subplots(1, len(wls), figsize=(3.7 * len(wls), 3.3), squeeze=False)
    for ax, wl in zip(axes[0], wls):
        sub = [r for r in rows if (r["dist"], r["hot_frac"]) == wl]
        cf = sorted([r for r in sub if r["policy"].startswith(("pg_default", "count_"))],
                    key=lambda r: r["avg_overhead_pages"])
        ax.annotate("count-based rules", (cf[len(cf)//2]["avg_overhead_pages"],
                                          cf[len(cf)//2]["vac_pages_per_Mupd"] / 1e6),
                    textcoords="offset points", xytext=(8, 8), fontsize=8,
                    color=C[0], fontweight="bold")
        ax.plot([r["avg_overhead_pages"] for r in cf],
                [r["vac_pages_per_Mupd"] / 1e6 for r in cf], "-o", color=C[0], lw=2, ms=4)
        d = [r for r in cf if r["policy"] == "pg_default"]
        if d:
            ax.annotate("PostgreSQL default", (d[0]["avg_overhead_pages"],
                                               d[0]["vac_pages_per_Mupd"] / 1e6),
                        textcoords="offset points", xytext=(-6, 8), ha="right",
                        fontsize=8, color=C[0], fontweight="bold")
        for c, pref, lab in ((C[1], "sqrt_", "square-root law"),
                             (C[2], "fp2_", "footprint rule")):
            pts = sorted([r for r in sub if r["policy"].startswith(pref)
                          and not r["policy"].startswith("fpidx")],
                         key=lambda r: r["avg_overhead_pages"])
            if not pts:
                continue
            ax.plot([r["avg_overhead_pages"] for r in pts],
                    [r["vac_pages_per_Mupd"] / 1e6 for r in pts], "-s", color=c, lw=2, ms=4)
            ax.annotate(lab, (pts[-1]["avg_overhead_pages"], pts[-1]["vac_pages_per_Mupd"] / 1e6),
                        textcoords="offset points", xytext=(4, -10), fontsize=8,
                        color=c, fontweight="bold")
        ax.set_xscale("log"); ax.set_yscale("log"); ax.grid(True, **GRID); ax.set_axisbelow(True)
        fmt = FuncFormatter(lambda v, _: (f"{v:,.0f}" if v >= 1 else f"{v:g}"))
        ax.xaxis.set_major_formatter(fmt); ax.xaxis.set_minor_formatter(FuncFormatter(lambda v, _: ""))
        ax.yaxis.set_major_formatter(fmt); ax.yaxis.set_minor_formatter(FuncFormatter(lambda v, _: ""))
        xs = [r["avg_overhead_pages"] for r in sub]
        ax.set_xticks([t for t in (100, 200, 400, 800, 1600, 3200) if min(xs) * 0.8 <= t <= max(xs) * 1.25])
        ax.set_xlabel("average space overhead (pages)")
        ax.set_ylabel("vacuum pages per $10^6$ updates (millions)")
    fig.tight_layout(); fig.savefig(f"{OUT}/fig3_pareto.png"); plt.close(fig)


def fig4_scaling(path=str(DATA_DIR / "exp5.jsonl"),
                 lams=(3e-4, 1e-3, 1e-2)):
    """Left: the measured cost curve C(D) at three relation sizes.
       Right: the optimum located on those curves vs the square-root law."""
    rows = [json.loads(l) for l in open(path)]
    for r in rows:
        r["touched"] = r["vac"].get("pages_touched", 0)
    Ns = sorted({r["n_rows"] for r in rows})
    fig, axes = plt.subplots(1, 2, figsize=(7.4, 3.2))
    ax = axes[0]
    for c, n in zip(C, Ns):
        sub = sorted([r for r in rows if r["n_rows"] == n], key=lambda r: r["applied"])
        x = [r["applied"] for r in sub]; y = [r["touched"] for r in sub]
        ax.plot(x, y, "-o", color=c, lw=2, ms=4)
        ax.annotate(f"N={n//1000:,}k rows", (x[0], y[0]), textcoords="offset points",
                    xytext=(6, -10), fontsize=8, color=c, fontweight="bold")
    ax.set_xscale("log"); ax.set_yscale("log"); ax.grid(True, **GRID); ax.set_axisbelow(True)
    ax.set_xlabel("updates before the pass, $D$")
    ax.set_ylabel("pages touched by the pass, $C(D)$")

    ax = axes[1]
    import numpy as _np
    for c, lam in zip(C, lams):
        xs, ys = [], []
        for n in Ns:
            sub = sorted([r for r in rows if r["n_rows"] == n], key=lambda r: r["applied"])
            P = sub[0]["heap_pages0"]; rho = n / P
            D = _np.array([r["applied"] for r in sub], float)
            Cc = _np.array([r["touched"] for r in sub], float)
            grid = _np.exp(_np.linspace(_np.log(D.min()), _np.log(D.max()), 2000))
            Ci = _np.exp(_np.interp(_np.log(grid), _np.log(D), _np.log(Cc)))
            J = Ci / grid + lam * grid / (2.0 * rho)
            xs.append(n); ys.append(float(grid[int(_np.argmin(J))]))
        ax.plot(xs, ys, "-o", color=c, lw=2, ms=5)
        ax.annotate(f"$\\lambda$={lam:g}", (xs[-1], ys[-1]), textcoords="offset points",
                    xytext=(5, -2), fontsize=8, color=c, fontweight="bold")
    Ns_a = _np.array(Ns, float)
    ax.plot(Ns_a, 0.2 * Ns_a, "--", color="#52514e", lw=1.2)
    ax.annotate("PostgreSQL default ($0.2N$, slope 1)", (Ns_a[1], 0.2 * Ns_a[1]),
                textcoords="offset points", xytext=(-4, -16), ha="right",
                fontsize=8, color="#52514e")
    ref = 1.3e5 * (Ns_a / Ns_a[1]) ** 0.5
    ax.plot(Ns_a, ref, ":", color="#52514e", lw=1.2)
    ax.annotate("slope $1/2$", (Ns_a[0], ref[0]), textcoords="offset points",
                xytext=(6, -12), fontsize=8, color="#52514e")
    ax.set_xscale("log"); ax.set_yscale("log"); ax.grid(True, **GRID); ax.set_axisbelow(True)
    ax.set_xlabel("relation size $N$ (rows)")
    ax.set_ylabel("cost-minimising threshold $D^*$")
    fig.tight_layout(); fig.savefig(f"{OUT}/fig4_scaling.png"); plt.close(fig)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "1"): fig1_cost_structure()
    if which in ("all", "2"): fig2_placement()
    if which in ("all", "3"): fig3_pareto()
    if which in ("all", "4") and os.path.exists(str(DATA_DIR / "exp5.jsonl")):
        fig4_scaling()
    print("figures written to", OUT)