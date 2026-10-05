# State-Aware Scheduling for PostgreSQL VACUUM

Reproducibility repository for the study **“State-Aware Scheduling for PostgreSQL VACUUM: When Dead-Tuple Counts Misrepresent Reclamation Work.”**

This repository contains the archived experimental measurements, experiment harness, analysis scripts, headline validators, and a prototype external VACUUM controller used in the study.

## Repository contents

- `data/` — archived raw JSON/JSONL measurements and derived summary files used for the reported results.
- `harness/` — PostgreSQL experiment and simulation scripts.
- `analysis/` — analysis scripts, fail-fast validation, and figure-generation code.
- `vacuum_agent.py` — external cost-aware VACUUM controller prototype.
- `run_archived_analysis.py` — one-command reproduction of archived-data analyses, validation, and figures.
- `requirements.txt` — pinned Python package requirements for the validated environment.

The `figures/` directory is generated automatically when the archived analysis is run.

## Validated environment

- PostgreSQL 16; the archived measurements were produced with PostgreSQL 16.13.
- PostgreSQL contrib extensions: `pgstattuple`, `pageinspect`.
- Python 3.11.
- Python dependencies listed in `requirements.txt`.

## Quick reproduction from archived measurements

From the repository root:

```bash
python3 -m pip install -r requirements.txt
python3 run_archived_analysis.py
```

The command reruns the archived-data analyses, executes the fail-fast headline validator, and regenerates the study figures. A non-zero exit status from the validator indicates a mismatch between the archived evidence and a checked headline numerical claim.

## PostgreSQL scratch-server setup for fresh reruns

Use a scratch PostgreSQL instance. The reported experiments used autovacuum disabled so that every reclamation pass was issued explicitly by the harness. The harness also isolates reclamation work by suppressing freezing, parallel maintenance, and VACUUM cost delay as described in the study.

Representative setup:

```bash
export PATH=/usr/lib/postgresql/16/bin:$PATH
initdb -D "$PWD/pgdata" --auth=trust --locale=C
pg_ctl -D "$PWD/pgdata" -l pg.log \
  -o "-p 5433 -c shared_buffers=1GB -c maintenance_work_mem=256MB -c max_wal_size=4GB -c autovacuum=off" start
psql -p 5433 -c 'CREATE EXTENSION pgstattuple' -c 'CREATE EXTENSION pageinspect'
```

## Randomness and fresh reruns

The archived JSON/JSONL files are the evidentiary record used by the study and headline validator. The original archived PostgreSQL runs did not explicitly seed the server-side `random()` generator. The released harness calls `setseed(0.2718281828)` once per experiment session in `apply_settings()`, making the PostgreSQL random stream repeatable for fresh reruns while avoiding artificial reseeding before each workload batch. Python-side sampling in the policy experiment and all multi-relation simulation seeds are explicit.

Because the archived runs predate the PostgreSQL-side seed fix, fresh reruns are expected to reproduce the reported qualitative and aggregate results rather than the exact row identities of the archived run.

## Experiment reruns

From `harness/`, run the experiment scripts against a scratch PostgreSQL 16 server:

```bash
cd harness
python3 exp1_cost.py       2000000         ../data/exp1.jsonl
python3 exp1b_placement.py 2000000 2       ../data/exp1b.jsonl
python3 exp2_policies.py   1000000 1500000 ../data/exp2.jsonl - 2
python3 exp2_policies.py   1000000 1500000 ../data/exp2_k8.jsonl - 8
python3 exp3_scaling.py    scaling         ../data/exp3_scaling.jsonl
python3 exp3_scaling.py    removability    ../data/exp3_remov.jsonl
python3 exp4_counter.py    1000000         ../data/exp4.jsonl
python3 exp5_costcurve.py                  ../data/exp5.jsonl
python3 sim_multi.py                       ../data/sim_multi.json
```

## Measurement conventions

- Primary work metric: logical buffer pages touched/dirtied and WAL bytes reported by `VACUUM VERBOSE`.
- Workload time: applied updates, not wall-clock seconds.
- Space: heap plus index pages above the freshly built baseline, with `pgstattuple` measurements where used.
- The footprint-fit analysis uses the post-update heap size and the applied-update target, matching the definition reported in the study.

## Important interpretation notes

The archived placement experiment (`exp1b.jsonl`) predates direct capture of `pg_stat_user_tables` counters in each placement row. The released `exp1b_placement.py` now logs `n_live_tup`, `n_dead_tup`, `n_tup_upd`, and `n_tup_hot_upd` immediately after the update workload and before VACUUM. The archived placement experiment is therefore evidence about work variation at a fixed applied-update target, not a same-run proof of identical scheduler-visible statistics.

The archived-data validator checks numerical claims only. The study distinguishes the scheduler counter `D` from physical space overhead `S(D)`. The square-root threshold is the saturated-cost, linear-space benchmark. The count-only minimax result is conditional on two workloads exposing the same scheduler-visible count trajectory; the archived 435× placement-cost ratio is not used as a same-run instantiation of that premise.

## Controller example

```bash
python3 vacuum_agent.py \
  --dsn "host=/var/run/postgresql port=5433 dbname=postgres" \
  --table public.t \
  --lam 1e-3 \
  --interval 30 \
  --dry-run
```

The page-inspection sampler estimates a pre-decision obsolete-candidate page indicator. It is not identical to the eventual removable dead-item footprint reported by `VACUUM VERBOSE`. PostgreSQL retains responsibility for its internal index-cleanup, freezing, and wraparound-safety decisions.

## Data and scope

No proprietary dataset is used. All experimental workloads are generated locally by the harness. Empirical claims are restricted to the evaluated PostgreSQL 16.13 environment unless otherwise stated.

## Author

**Huynh Anh Khiem**  
Faculty of Information Technology, Ton Duc Thang University, Ho Chi Minh City, Vietnam  
ORCID: 0009-0007-7210-174X  
Email: huynhanhkhiem@tdtu.edu.vn

## Citation

If this repository supports your work, please cite the associated article after publication. Repository metadata are also provided in `CITATION.cff`.