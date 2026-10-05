#!/usr/bin/env python3
"""vacuum_agent - research prototype for cost-aware PostgreSQL reclamation.

The prototype supplements PostgreSQL's built-in safety machinery. It estimates a
pre-decision *obsolete-candidate page footprint proxy* by sampling heap pages with
pageinspect; this proxy is not the same object as the exact removable dead-item
footprint later reported by VACUUM VERBOSE.

For each managed relation the controller reads:
    Ahat  sampled obsolete-candidate page footprint proxy,
    I     total index pages,
    P     heap pages,
    rho   live tuples per heap page,
    D     pg_stat_user_tables.n_dead_tup.

The slope dA/dD is obtained from the one-observation saturation fit used by the
paper's repaired footprint controller, rather than by differencing two noisy
samples. The prototype uses the observed 2% page-footprint branch to forecast the
index component, but issues VACUUM with INDEX_CLEANUP AUTO so PostgreSQL retains
control of its complete internal bypass rule (including the TID-store memory
condition).

A retry guard is also included. After a VACUUM VERBOSE pass reports zero tuples
removed and tuples "dead but not yet removable", the agent records the reported
removable cutoff. It suppresses another *space-reclamation* retry while the current
snapshot xmin remains at that cutoff. This guard does not disable autovacuum's
transaction-ID wraparound/freeze obligations; use this prototype only as an
additional research scheduler alongside those safety mechanisms.

Usage:
    python3 vacuum_agent.py --dsn "host=/var/run/postgresql dbname=app" \
        --table public.orders --lam 1e-3 --interval 30 [--dry-run]
"""
import argparse
import math
import random
import re
import sys
import time

try:
    import psycopg2
    import psycopg2.extensions
except ImportError:  # pragma: no cover
    sys.exit("psycopg2 is required: pip install psycopg2-binary")

CA = 2.0
BYPASS = 0.02


def connect(dsn):
    c = psycopg2.connect(dsn)
    c.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    return c


def relation_state(cur, table):
    cur.execute(
        """SELECT c.relpages, c.reltuples,
                  coalesce((SELECT sum(ic.relpages) FROM pg_index i
                              JOIN pg_class ic ON ic.oid = i.indexrelid
                             WHERE i.indrelid = c.oid), 0)
             FROM pg_class c WHERE c.oid = %s::regclass""",
        (table,),
    )
    relpages, reltuples, idxpages = cur.fetchone()
    cur.execute(
        """SELECT n_live_tup, n_dead_tup, last_vacuum, last_autovacuum
             FROM pg_stat_user_tables WHERE relid = %s::regclass""",
        (table,),
    )
    row = cur.fetchone() or (0, 0, None, None)
    cur.execute("SELECT pg_relation_size(%s::regclass) / 8192", (table,))
    heap_pages = cur.fetchone()[0]
    return {
        "heap_pages": max(1, int(heap_pages)),
        "index_pages": int(idxpages),
        "reltuples": float(reltuples),
        "n_live": int(row[0]),
        "n_dead": int(row[1]),
    }


def footprint_proxy(cur, table, heap_pages, sample=150):
    """Estimate the sampled-page obsolete-candidate indicator.

    A sampled page is a hit if pageinspect finds LP_DEAD or a tuple with t_xmax.
    The estimator is unbiased for this indicator under uniform page sampling;
    it is deliberately not labeled an unbiased estimator of VACUUM's exact
    future removable dead-item footprint.
    """
    n = min(sample, heap_pages)
    blocks = random.sample(range(heap_pages), n)
    cur.execute(
        f"""SELECT count(*) FROM (
              SELECT b FROM unnest(%s::int[]) b
               WHERE EXISTS (
                 SELECT 1 FROM heap_page_items(get_raw_page(%s, b)) i
                  WHERE i.lp_flags = 3
                     OR (i.t_xmax IS NOT NULL AND i.t_xmax <> 0)
               )) q""",
        (blocks, table),
    )
    hits = cur.fetchone()[0]
    p = hits / n
    est = heap_pages * p
    se = heap_pages * math.sqrt(max(p * (1.0 - p), 1e-9) / n)
    return est, se


def saturation_level(A, D, P):
    """Solve A = S(1-exp(-D/S)) for S in [A, P] by bisection."""
    if A <= 0 or D <= 0:
        return max(A, 1.0)
    lo, hi = max(A, 1.0), max(A * 1.0001, float(P))

    def f(S):
        return S * (1.0 - math.exp(-D / S)) - A

    if f(hi) < 0:
        return hi
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def current_snapshot_xmin(cur):
    """Return the current snapshot xmin as an integer, or None if unavailable."""
    try:
        cur.execute("SELECT pg_snapshot_xmin(pg_current_snapshot())::text")
        v = cur.fetchone()[0]
        return int(v) if v is not None else None
    except Exception:
        return None


def parse_vacuum_notice(notices):
    """Extract (removed, not_yet_removable, removable_cutoff) from VACUUM VERBOSE."""
    text = "\n".join(notices)
    m = re.search(
        r"tuples:\s*([0-9]+)\s+removed,.*?([0-9]+)\s+are dead but not yet removable",
        text,
        flags=re.I | re.S,
    )
    x = re.search(r"(?:removable cutoff|oldest xmin):\s*([0-9]+)", text, flags=re.I)
    removed = int(m.group(1)) if m else None
    blocked = int(m.group(2)) if m else None
    cutoff = int(x.group(1)) if x else None
    return removed, blocked, cutoff


class Controller:
    def __init__(self, lam, cA=CA, sample=150, idx_gamma=0.25):
        self.lam = lam
        self.cA = cA
        self.sample = sample
        self.idx_gamma = idx_gamma
        self.blocked_xmin = None

    def retry_blocked(self, cur):
        """Suppress only a repeated space-reclamation retry under the same xmin."""
        if self.blocked_xmin is None:
            return False
        now = current_snapshot_xmin(cur)
        if now is None or now != self.blocked_xmin:
            self.blocked_xmin = None
            return False
        return True

    def decide(self, st, A_proxy, idx_debt=None):
        P = float(st["heap_pages"])
        D = float(max(1, st["n_dead"]))
        rho = max(1.0, st["n_live"] / P)

        # Forecast only the observed page-footprint branch. The actual VACUUM is
        # issued with INDEX_CLEANUP AUTO, which retains PostgreSQL's full rule.
        page_bypass_forecast = A_proxy < BYPASS * P
        C = self.cA * A_proxy + (0.0 if page_bypass_forecast else st["index_pages"])

        S = saturation_level(A_proxy, D, P)
        slope = self.cA * math.exp(-D / S)
        lhs = (C - D * slope) / (D * D)
        fire = lhs <= self.lam / (2.0 * rho)
        want_index = fire and idx_debt is not None and idx_debt > self.idx_gamma * st["n_live"]
        return fire, ("ON" if want_index else "AUTO"), {
            "C": C,
            "slope": slope,
            "lhs": lhs,
            "rhs": self.lam / (2.0 * rho),
            "page_bypass_forecast": page_bypass_forecast,
        }

    def observe_vacuum(self, notices):
        removed, blocked, cutoff = parse_vacuum_notice(notices)
        if removed == 0 and blocked is not None and blocked > 0 and cutoff is not None:
            self.blocked_xmin = cutoff
        elif removed is not None and removed > 0:
            self.blocked_xmin = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--table", required=True, action="append",
                    help="qualified relation name; repeat for several")
    ap.add_argument("--lam", type=float, default=1e-3,
                    help="price of one page of unreclaimed space, in pages of I/O")
    ap.add_argument("--sample", type=int, default=150)
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--min-dead", type=int, default=1000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    conn = connect(args.dsn)
    cur = conn.cursor()
    cur.execute("CREATE EXTENSION IF NOT EXISTS pageinspect")
    ctrls = {t: Controller(args.lam, sample=args.sample) for t in args.table}
    last_pass_upd = {t: 0 for t in args.table}

    while True:
        for t in args.table:
            st = relation_state(cur, t)
            if st["n_dead"] < args.min_dead:
                continue

            ctrl = ctrls[t]
            if ctrl.retry_blocked(cur):
                print(f"{time.strftime('%H:%M:%S')} {t}: RETRY-GUARD (same removable horizon)", flush=True)
                continue

            A, se = footprint_proxy(cur, t, st["heap_pages"], args.sample)
            cur.execute(
                "SELECT n_tup_upd + n_tup_del FROM pg_stat_user_tables "
                "WHERE relid = %s::regclass",
                (t,),
            )
            upd = int(cur.fetchone()[0])
            fire, mode, dbg = ctrl.decide(st, A, idx_debt=upd - last_pass_upd[t])
            stamp = time.strftime("%H:%M:%S")
            print(
                f"{stamp} {t}: D={st['n_dead']:>9d} Aproxy={A:>9.0f}+-{se:.0f} "
                f"P={st['heap_pages']:>8d} I={st['index_pages']:>8d} "
                f"Cforecast={dbg['C']:>9.0f} lhs={dbg['lhs']:.3e} rhs={dbg['rhs']:.3e} "
                f"{'PAGE-BYPASS' if dbg['page_bypass_forecast'] else 'PAGE-INDEX '} "
                f"{'FIRE' if fire else '-'}{' (' + mode + ')' if fire else ''}",
                flush=True,
            )

            if fire and not args.dry_run:
                del conn.notices[:]
                cur.execute(f"VACUUM (VERBOSE, INDEX_CLEANUP {mode}) {t}")
                ctrl.observe_vacuum(conn.notices)
                last_pass_upd[t] = upd

        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()