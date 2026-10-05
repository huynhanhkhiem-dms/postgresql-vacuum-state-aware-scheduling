"""Section 10: many relations, one reclamation worker.

A discrete-event simulation of a launcher that must choose which relation to
reclaim next.  Relation cost and footprint follow the model calibrated in §6
(C = cA*A + I*1{A>0.02P}, A(D) = hP(1-exp(-D/(hP)))), so the simulation inherits
the measured structure rather than inventing one.

Policies
  pg16      : eligible iff D >= 0.2*N; among eligible, catalogue (index) order
  pg19      : eligible iff D >= 0.2*N; among eligible, highest normalised score
              max(D/(0.2N), ...) -- the shape proposed for PostgreSQL 19
  ratio     : eligible iff D >= D*_j (linear-space benchmark); among eligible, highest
              benefit-per-unit-work  (lam_j * D_j / (2 rho_j)) / C_j(D_j)
"""
from pathlib import Path
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
import json, math, random, sys

CA = 2.0
BYPASS = 0.02


def make_relations(n, seed=1):
    rng = random.Random(seed)
    rels = []
    for j in range(n):
        P = int(10 ** rng.uniform(2.5, 5.5))          # 300 .. 300k pages
        rho = rng.choice([20, 40, 60, 90])
        k = rng.choice([0, 1, 2, 4])
        I = int(P * (0.15 + 0.35 * k))
        h = rng.choice([0.005, 0.02, 0.1, 1.0])       # placement
        u = 10 ** rng.uniform(-1, 1.5)                # updates per tick
        lam = 10 ** rng.uniform(-3.5, -2.0)
        rels.append({"id": j, "P": P, "rho": rho, "I": I, "h": h, "u": u,
                     "lam": lam, "D": 0.0, "N": P * rho})
    return rels


def cost(r, D):
    hP = max(1.0, r["h"] * r["P"])
    A = hP * (1 - math.exp(-D / hP))
    return CA * A + (r["I"] if A > BYPASS * r["P"] else 0.0), A


def d_star(r):
    Cbar, _ = cost(r, 10 * max(1.0, r["h"] * r["P"]))
    return math.sqrt(2.0 * r["rho"] * Cbar / r["lam"])


def simulate(rels, policy, ticks=200_000, B=50.0, seed=1):
    rels = [dict(r) for r in rels]
    io = 0.0
    space = 0.0
    busy_until, victim = 0.0, None
    for t in range(ticks):
        for r in rels:
            r["D"] += r["u"]
            space += r["lam"] * r["D"] / (2.0 * r["rho"])
        if t < busy_until:
            continue
        if victim is not None:                       # a pass just finished
            victim["D"] = 0.0
            victim = None
        if policy in ("pg16", "pg19", "ratio", "star_catalog", "count_ratio"):
            pass
        if policy == "pg16":
            elig = [r for r in rels if r["D"] >= 0.2 * r["N"]]
            pick = min(elig, key=lambda r: r["id"]) if elig else None
        elif policy == "pg19":
            elig = [r for r in rels if r["D"] >= 0.2 * r["N"]]
            pick = max(elig, key=lambda r: r["D"] / (0.2 * r["N"])) if elig else None
        elif policy in ("ratio", "star_catalog", "count_ratio"):
            if policy == "count_ratio":
                elig = [r for r in rels if r["D"] >= 0.2 * r["N"]]
            else:
                elig = [r for r in rels if r["D"] >= d_star(r)]
            def prio(r):
                c, _ = cost(r, r["D"])
                return (r["lam"] * r["D"] / (2.0 * r["rho"])) / max(1.0, c)
            if policy == "star_catalog":
                pick = min(elig, key=lambda r: r["id"]) if elig else None
            else:
                pick = max(elig, key=prio) if elig else None
        else:
            raise ValueError(policy)
        if pick is not None:
            c, _ = cost(pick, pick["D"])
            io += c
            busy_until = t + c / B
            victim = pick
    return {"policy": policy, "io_per_tick": io / ticks,
            "space_per_tick": space / ticks,
            "total_per_tick": io / ticks + space / ticks,
            "final_D": {r["id"]: r["D"] for r in rels}}


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else str(DATA_DIR / "sim_multi.json")
    res = []
    for seed in range(5):
        rels = make_relations(40, seed=seed)
        row = {"seed": seed}
        for pol in ("pg16", "pg19", "count_ratio", "star_catalog", "ratio"):
            r = simulate(rels, pol, seed=seed)
            row[pol] = {k: r[k] for k in ("io_per_tick", "space_per_tick", "total_per_tick")}
        res.append(row)
        print(f"seed={seed}  " + "  ".join(
            f"{p}={row[p]['total_per_tick']:8.2f}"
            for p in ("pg16", "pg19", "count_ratio", "star_catalog", "ratio")), flush=True)
    pols = ("pg16", "pg19", "count_ratio", "star_catalog", "ratio")
    tot = {p: sum(r[p]["total_per_tick"] for r in res) / len(res) for p in pols}
    io = {p: sum(r[p]["io_per_tick"] for r in res) / len(res) for p in pols}
    sp = {p: sum(r[p]["space_per_tick"] for r in res) / len(res) for p in pols}
    print("\nmean per tick (io / space / total):")
    for p in pols:
        print(f"  {p:13s} io={io[p]:8.2f} space={sp[p]:9.2f} total={tot[p]:9.2f} "
              f"(x{tot[p]/tot['ratio']:.2f} vs ratio)")
    print("\nattribution: ordering alone (pg16 -> pg19):", round(tot["pg16"]/tot["pg19"], 3), "x")
    print("             ordering alone (star_catalog -> ratio):", round(tot["star_catalog"]/tot["ratio"], 3), "x")
    print("             eligibility alone (pg16 -> star_catalog):", round(tot["pg16"]/tot["star_catalog"], 3), "x")
    json.dump({"runs": res, "means": tot}, open(out, "w"), indent=1)


if __name__ == "__main__":
    main()