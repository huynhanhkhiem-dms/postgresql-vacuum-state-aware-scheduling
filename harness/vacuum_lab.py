"""vacuum_lab: a measurement harness for deferred space-reclamation triggers
in PostgreSQL.

All cost metrics are logical (buffer pages touched / dirtied, WAL bytes) and all
time integration is in *workload time* (number of applied updates), so results
are reproducible and independent of the speed of the underlying disk.
"""
from pathlib import Path
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
import json, math, re, sys, time
from decimal import Decimal
import psycopg2
import psycopg2.extensions

DSN = "host=/var/run/postgresql port=5433 dbname=postgres user=postgres"
PAGE = 8192


SETTINGS = ["SET maintenance_work_mem='256MB'",
            "SET client_min_messages='INFO'",
            "SET max_parallel_maintenance_workers=0",   # deterministic, single-threaded
            "SET vacuum_freeze_min_age=1000000000",     # suppress freezing work
            "SET vacuum_freeze_table_age=2000000000",
            "SET vacuum_cost_delay=0"]


def apply_settings(cur):
    for s in SETTINGS:
        cur.execute(s)
    # Seed PostgreSQL's session-local random() generator once per experiment
    # session. This makes fresh reruns repeatable without resetting the stream
    # before every workload batch.
    cur.execute("SELECT setseed(0.2718281828)")


def connect(autocommit=True):
    c = psycopg2.connect(DSN)
    if autocommit:
        c.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    return c


def jdump(o):
    return json.dumps(o, default=lambda x: float(x) if isinstance(x, Decimal) else str(x))


# ---------------------------------------------------------------- schema ----
IDX_COLS = ["a", "b", "c", "d", "e", "f", "g", "h"]


def build(cur, n_rows, k_idx, fillfactor=100, payload=60, table="t"):
    cur.execute(f"DROP TABLE IF EXISTS {table}")
    cols = ", ".join(f"{c} int" for c in IDX_COLS)
    cur.execute(f"""CREATE TABLE {table} (id int PRIMARY KEY, {cols}, pad text)
                    WITH (fillfactor={fillfactor}, autovacuum_enabled=off,
                          toast.autovacuum_enabled=off)""")
    vals = ", ".join("i" for _ in IDX_COLS)
    cur.execute(f"""INSERT INTO {table} SELECT i, {vals}, repeat('x',{payload})
                    FROM generate_series(1,{n_rows}) i""")
    for j in range(k_idx):
        cur.execute(f"CREATE INDEX {table}_idx_{IDX_COLS[j]} ON {table} ({IDX_COLS[j]})")
    cur.execute(f"VACUUM (FREEZE, ANALYZE) {table}")
    cur.execute("CHECKPOINT")


def sizes(cur, table="t"):
    cur.execute(f"""SELECT pg_relation_size('{table}'),
        (SELECT coalesce(sum(pg_relation_size(indexrelid)),0) FROM pg_index
          WHERE indrelid='{table}'::regclass)""")
    heap, idx = cur.fetchone()
    return int(heap), int(idx)


def reltuples(cur, table="t"):
    cur.execute(f"SELECT reltuples, relpages FROM pg_class WHERE oid='{table}'::regclass")
    rt, rp = cur.fetchone()
    return float(rt), int(rp)


def stats_row(cur, table="t"):
    try:
        cur.execute("SELECT pg_stat_force_next_flush()")
    except Exception:
        pass
    cur.execute(f"""SELECT n_live_tup, n_dead_tup, n_tup_upd, n_tup_hot_upd, vacuum_count
                      FROM pg_stat_user_tables WHERE relname='{table}'""")
    r = cur.fetchone()
    return dict(zip(("n_live", "n_dead", "n_upd", "n_hot", "vac_count"), r))


def pgstat(cur, table="t", approx=False):
    fn = "pgstattuple_approx" if approx else "pgstattuple"
    cur.execute(f"SELECT * FROM {fn}('{table}')")
    cols = [d[0] for d in cur.description]
    d = dict(zip(cols, cur.fetchone()))
    # normalise the approximate variant's column names
    for a, b in (("approx_free_space", "free_space"), ("approx_free_percent", "free_percent"),
                 ("approx_tuple_count", "tuple_count"), ("approx_tuple_len", "tuple_len")):
        if a in d and b not in d:
            d[b] = d[a]
    return d


# ------------------------------------------------------------- workload ----
def _upd_stmt(cur, table, col, lo_hi, m):
    """One statement updating m DISTINCT random ids in [1, hi]."""
    hi = lo_hi
    cur.execute(f"UPDATE {table} SET {col} = {col} + 1 WHERE id IN "
                f"(SELECT (random()*{hi-1})::int + 1 FROM generate_series(1,{m}))")
    return cur.rowcount


def apply_updates(cur, n_upd, n_rows, dist="uniform", hot_frac=0.05, seed=None,
                  table="t", col="a", batch=20000):
    """Apply ~n_upd updates and return the number of rows actually updated.

    A single statement can update a given row at most once, so when the key range
    is small the statement is split into sub-batches no larger than half the range
    — repeated updates of the same hot row then happen across statements, which is
    what a hot workload does in reality.  Without this, a 'hot 1 %' workload would
    silently apply ten times fewer updates than requested.
    """
    done = 0
    while done < n_upd:
        want = min(batch, n_upd - done)
        if dist == "uniform":
            ranges = [(n_rows, want)]
        elif dist == "hot":
            ranges = [(max(2, int(n_rows * hot_frac)), want)]
        elif dist == "mixed":                       # 80 % hot, 20 % uniform
            hi = max(2, int(n_rows * hot_frac))
            ranges = [(hi, int(want * 0.8)), (n_rows, want - int(want * 0.8))]
        elif dist == "zipf":
            cur.execute(f"UPDATE {table} SET {col} = {col} + 1 WHERE id IN "
                        f"(SELECT least({n_rows}, (exp(random()*ln({n_rows})))::int) "
                        f"FROM generate_series(1,{want}))")
            done += cur.rowcount
            continue
        else:
            raise ValueError(dist)
        for hi, want_r in ranges:
            left = want_r
            cap = max(1, hi // 2)                   # distinct rows per statement
            while left > 0:
                m = min(left, cap)
                done += _upd_stmt(cur, table, col, hi, m)
                left -= m
    return done


# --------------------------------------------------------------- vacuum ----
PAT_BUF = re.compile(r"buffer usage: (\d+) hits, (\d+) misses, (\d+) dirtied")
PAT_WAL = re.compile(r"WAL usage: (\d+) records, (\d+) full page images, (\d+) bytes")
PAT_REM = re.compile(r"tuples: (\d+) removed, (\d+) remain, (\d+) are dead but not yet removable")
PAT_PAGES = re.compile(r"pages: (\d+) removed, (\d+) remain, (\d+) scanned \(([\d.]+)% of total\)")
PAT_IDXMODE = re.compile(r"index scan (needed|bypassed|not needed): (\d+) pages? from table \(([\d.]+)% of total\) ha[dves]+ (\d+) dead item identifiers")
PAT_LPD = re.compile(r"(\d+) pages? from table \(([\d.]+)% of total\) ha[sve]+ (\d+) dead item identifiers")
PAT_SYS = re.compile(r"system usage: CPU: user: ([\d.]+) s, system: ([\d.]+) s, elapsed: ([\d.]+) s")


def vacuum(cur, table="t", index_cleanup="AUTO", checkpoint_first=True, verbose=True):
    if checkpoint_first:
        cur.execute("CHECKPOINT")
    cur.connection.notices.clear()
    t0 = time.time()
    cur.execute(f"VACUUM (VERBOSE, INDEX_CLEANUP {index_cleanup}) {table}")
    wall = time.time() - t0
    txt = "\n".join(cur.connection.notices)
    out = {"wall_s": wall, "raw": txt}
    m = PAT_BUF.search(txt)
    if m:
        out["buf_hits"], out["buf_misses"], out["buf_dirtied"] = map(int, m.groups())
        out["pages_touched"] = out["buf_hits"] + out["buf_misses"]
    m = PAT_WAL.search(txt)
    if m:
        out["wal_records"], out["wal_fpi"], out["wal_bytes"] = map(int, m.groups())
    m = PAT_REM.search(txt)
    if m:
        out["removed"], out["remain"], out["not_removable"] = (int(m.group(1)),
                                                               int(m.group(2)), int(m.group(3)))
    m = PAT_PAGES.search(txt)
    if m:
        out["pages_removed"], out["rel_pages"] = int(m.group(1)), int(m.group(2))
        out["pages_scanned"], out["scanned_pct"] = int(m.group(3)), float(m.group(4))
    m = PAT_IDXMODE.search(txt)
    if m:
        out["idx_mode"] = m.group(1)
        out["lpdead_pages"], out["lpdead_pct"], out["lpdead_items"] = (
            int(m.group(2)), float(m.group(3)), int(m.group(4)))
    out["index_bypassed"] = "index scan bypassed" in txt
    m = PAT_SYS.search(txt)
    if m:
        out["cpu_user"], out["cpu_sys"] = float(m.group(1)), float(m.group(2))
    return out


# ---------------------------------------------------------- read probes ----
def read_probe(cur, n_rows, table="t", n_lookups=2000):
    """Cost of a sequential scan and of index lookups, in pages touched."""
    cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT count(*) FROM {table}")
    plan = cur.fetchone()[0][0]["Plan"]
    seq_pages = plan.get("Shared Hit Blocks", 0) + plan.get("Shared Read Blocks", 0)

    def walk(p):
        tot = p.get("Shared Hit Blocks", 0) + p.get("Shared Read Blocks", 0)
        for sp in p.get("Plans", []):
            tot += walk(sp)
        return tot

    cur.execute(f"""EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                    SELECT count(t2.id) FROM (SELECT (random()*{n_rows-1})::int + 1 AS k
                      FROM generate_series(1,{n_lookups})) s
                    JOIN {table} t2 ON t2.id = s.k""")
    plan2 = cur.fetchone()[0][0]["Plan"]
    idx_pages = walk(plan2)
    return {"seq_pages": seq_pages, "lookup_pages": idx_pages, "lookups": n_lookups}