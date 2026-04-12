# Distributed Scraping Plan

## Architecture

One DigitalOcean droplet per (bench, year) pair. No coordination between
nodes — each owns its slice exclusively. A shared DO Managed PostgreSQL
instance acts as the central status board. DO Spaces (S3-compatible)
stores all PDFs.

```
                          ┌─────────────────────────┐
                          │   DO Managed PostgreSQL  │
                          │   (central status board) │
                          └────────┬────────────────┘
                                   │ read/write
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
  ┌─────┴──────┐            ┌──────┴─────┐            ┌───────┴────┐
  │  Droplet A │            │  Droplet B │            │  Droplet C │
  │  Chdgr/2020│            │  Chdgr/2021│            │  Delhi/2020│
  └─────┬──────┘            └──────┬─────┘            └───────┬────┘
        │                          │                          │
        │       upload PDFs        │                          │
        └──────────────────────────┼──────────────────────────┘
                                   │
                          ┌────────▼────────────────┐
                          │      DO Spaces          │
                          │   (S3-compatible)       │
                          │   itat-archive bucket   │
                          └─────────────────────────┘
                                   │
                          ┌────────▼────────────────┐
                          │   Dashboard App         │
                          │   (DO App Platform      │
                          │    or one droplet)      │
                          │   reads PostgreSQL      │
                          │   shows all nodes       │
                          └─────────────────────────┘
```

## Node assignment

Each droplet is configured via environment variables:

```bash
# On Droplet A
ITAT_BENCH=Chandigarh
ITAT_YEAR=2020
ITAT_SPACES_BUCKET=itat-archive
ITAT_SPACES_REGION=blr1
ITAT_DB_URL=postgresql://user:pass@db-host:25060/itat
ITAT_NODE_ID=chandigarh-2020       # auto-generated from bench+year if omitted
ITAT_RATE_PER_MINUTE=30            # per-node rate (each IP has its own budget)
ITAT_DEVICE=auto                   # or cpu if the droplet has no GPU
```

The worker reads these, constructs a `RunConfig` for exactly one bench
and one year, and starts scraping. No node ever touches another node's
bench/year prefix.

## DigitalOcean infrastructure

| Component | DO Product | Spec | Est. cost |
| --- | --- | --- | --- |
| Worker nodes | Droplets (Basic) | 1 vCPU, 2 GB RAM each | $12/mo each |
| PDF storage | Spaces | 250 GB included | $5/mo |
| Status database | Managed PostgreSQL | 1 vCPU, 1 GB RAM (basic) | $15/mo |
| Dashboard | App Platform (Basic) | or a $4/mo droplet | $4-7/mo |

For 10 nodes scraping 10 (bench, year) pairs: ~$144/mo total.

GPU droplets are not needed — `tiny.en` on CPU handles captchas in
<0.5s, and the bottleneck is network I/O + rate limiting, not
transcription speed.

## Database schema

Two tables. Workers write, dashboard reads.

```sql
-- Tracks every appeal attempt across all nodes.
-- This is the source of truth for "which files skipped / downloaded /
-- errored / not available" per node.
CREATE TABLE appeal_results (
    id            BIGSERIAL PRIMARY KEY,
    node_id       TEXT NOT NULL,            -- e.g. "chandigarh-2020"
    bench         TEXT NOT NULL,
    year          INT NOT NULL,
    appeal_number INT NOT NULL,
    category      TEXT NOT NULL,            -- ok, skipped, no_pdf, no_records,
                                            -- rate_limited, network_timeout,
                                            -- captcha_failed, parse_failed,
                                            -- pipeline_failed, unknown
    parties       TEXT,
    s3_key        TEXT,                     -- e.g. "Chandigarh/2020/Chandigarh_ITA_1_2020_order1.pdf"
    pdf_bytes     BIGINT,
    attempts      INT NOT NULL DEFAULT 0,
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (bench, year, appeal_number)     -- one row per appeal, upsert on retry
);

-- Index for dashboard queries
CREATE INDEX idx_results_node ON appeal_results (node_id);
CREATE INDEX idx_results_category ON appeal_results (category);
CREATE INDEX idx_results_bench_year ON appeal_results (bench, year);

-- Heartbeat table. Each node upserts every 30 seconds.
-- If last_seen is older than 2 minutes, the node is considered dead.
CREATE TABLE node_health (
    node_id         TEXT PRIMARY KEY,
    bench           TEXT NOT NULL,
    year            INT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'starting',  -- starting, running, idle, done, error
    current_appeal  INT,
    ip_address      TEXT,

    -- Rolling counters since node started
    ok_count        INT NOT NULL DEFAULT 0,
    skipped_count   INT NOT NULL DEFAULT 0,
    miss_count      INT NOT NULL DEFAULT 0,
    nopdf_count     INT NOT NULL DEFAULT 0,
    error_count     INT NOT NULL DEFAULT 0,
    total_count     INT NOT NULL DEFAULT 0,

    -- Rate-limit pressure indicator
    http_403_last_5m INT NOT NULL DEFAULT 0,

    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
```

## S3 (DO Spaces) key layout

```
itat-archive/
├── Chandigarh/
│   ├── 2020/
│   │   ├── Chandigarh_ITA_1_2020_order1.pdf
│   │   ├── Chandigarh_ITA_2_2020_order1.pdf
│   │   ├── manifest.jsonl
│   │   ├── failures.csv
│   │   └── missing_pdfs.csv
│   ├── 2021/
│   │   └── ...
│   └── ...
├── Delhi/
│   └── ...
└── Mumbai/
    └── ...
```

Each node writes ONLY to `{bench}/{year}/`. No conflicts between nodes.

## What the worker does (modified Runner)

The existing `Runner` changes minimally:

1. **Read config from env vars** instead of (or in addition to) CLI args.

2. **After saving a PDF locally, upload it to Spaces.**
   ```python
   import boto3
   s3 = boto3.client("s3",
       endpoint_url=f"https://{region}.digitaloceanspaces.com",
       aws_access_key_id=os.environ["ITAT_SPACES_KEY"],
       aws_secret_access_key=os.environ["ITAT_SPACES_SECRET"],
   )
   s3.upload_file(local_path, bucket, s3_key)
   ```
   The local copy stays on the droplet as a cache for skip-existing.

3. **After each appeal, upsert a row into `appeal_results`.**
   ```python
   INSERT INTO appeal_results (node_id, bench, year, appeal_number, category, ...)
   VALUES (%s, %s, %s, %s, %s, ...)
   ON CONFLICT (bench, year, appeal_number)
   DO UPDATE SET category = EXCLUDED.category, ...
   ```

4. **Every 30 seconds (background thread), upsert `node_health`.**
   ```python
   INSERT INTO node_health (node_id, bench, year, status, current_appeal, ...)
   VALUES (%s, %s, %s, %s, %s, ...)
   ON CONFLICT (node_id)
   DO UPDATE SET status = EXCLUDED.status, last_seen = now(), ...
   ```

5. **On completion or crash, update `node_health.status`** to `done` or
   `error` and set `finished_at`.

6. **Skip-existing checks local disk first** (fast), then optionally
   checks the DB for appeals completed by a previous run on a different
   droplet (if you ever re-assign a bench/year to a new droplet).

## Monitoring dashboard

A single FastAPI app (or even a static page with JS polling an API)
that queries the two PostgreSQL tables.

### Dashboard views

**1. Fleet overview (landing page)**

Shows every node at a glance. One row per node, auto-refreshes every
10 seconds.

```
┌──────────────────────┬────────┬────────┬───────┬──────┬─────┬───────┬─────┬───────────┬──────────┐
│ Node                 │ Status │ Appeal │  OK   │ SKIP │MISS │NO-PDF │ ERR │ 403s/5min │ Last seen│
├──────────────────────┼────────┼────────┼───────┼──────┼─────┼───────┼─────┼───────────┼──────────┤
│ chandigarh-2020      │ ✅ done│  372   │  291  │   0  │  52 │  12   │  17 │     0     │ 2m ago   │
│ chandigarh-2021      │ ✅ done│  716   │  361  │   0  │ 198 │  84   │  73 │     0     │ 2m ago   │
│ chandigarh-2022      │ 🔄 run │  234   │  142  │   0  │  48 │  21   │  23 │     8     │ 5s ago   │
│ delhi-2020           │ 🔄 run │   89   │   67  │   0  │  12 │   4   │   6 │     2     │ 3s ago   │
│ delhi-2021           │ ⚠️ stale│  412   │  298  │   0  │  71 │  19   │  24 │    35     │ 3m ago   │
│ mumbai-2020          │ 🔴 dead│  156   │  120  │   0  │  22 │   8   │   6 │     0     │ 8m ago   │
└──────────────────────┴────────┴────────┴───────┴──────┴─────┴───────┴─────┴───────────┴──────────┘
```

Status logic:
- ✅ **done**: `node_health.status = 'done'`
- 🔄 **running**: `last_seen` within 2 minutes
- ⚠️ **stale**: `last_seen` 2-5 minutes ago (probably rate-limited hard, or slow)
- 🔴 **dead**: `last_seen` > 5 minutes ago (droplet crashed or network down)

**2. Node detail page (`/node/chandigarh-2020`)**

Shows every appeal attempted by that node, pulled from `appeal_results`:

```
Chandigarh / 2020  —  Node: chandigarh-2020  —  Status: done

Filter: [All ▾]  [OK] [SKIP] [MISS] [NO-PDF] [ERR]

┌────────┬──────────┬──────────────────────────────────────────┬──────────┬───────────┐
│ Appeal │ Category │ Parties                                  │ Attempts │ S3 Link   │
├────────┼──────────┼──────────────────────────────────────────┼──────────┼───────────┤
│   1    │ ok       │ M/S STATE BANK OF PATIALA VS. ITO TDS   │    1     │ [PDF ↗]   │
│   2    │ ok       │ M/S MANAV MANGAL SOCIETY VS. DCIT       │    3     │ [PDF ↗]   │
│   3    │ no_pdf   │ RAMAN KUMAR VS. ACIT                    │    1     │    —      │
│   4    │ rate_limited │ —                                    │    3     │    —      │
│   5    │ ok       │ BHUSHAN GUPTA VS. ITO WARD 5(3)         │    2     │ [PDF ↗]   │
│  ...   │          │                                          │          │           │
└────────┴──────────┴──────────────────────────────────────────┴──────────┴───────────┘

Summary: 291 OK, 52 MISS, 12 NO-PDF, 17 ERR (5 rate_limited, 8 network_timeout, 4 captcha_failed)
```

**3. Error drilldown (`/errors`)**

Aggregates errors across all nodes, grouped by category:

```sql
-- The query behind this view
SELECT category, bench, year, count(*), array_agg(appeal_number ORDER BY appeal_number)
FROM appeal_results
WHERE category NOT IN ('ok', 'skipped', 'no_records')
GROUP BY category, bench, year
ORDER BY count(*) DESC;
```

```
Error breakdown (all nodes):

rate_limited:      47  — Chandigarh/2021 (23), Chandigarh/2022 (14), Delhi/2020 (10)
network_timeout:   31  — Chandigarh/2021 (18), Delhi/2021 (13)
captcha_failed:    12  — Chandigarh/2020 (5), Mumbai/2020 (7)
no_pdf:           116  — (cases exist but no order PDF uploaded by tribunal yet)
```

**4. Retry queue (`/retry`)**

Shows appeals worth retrying (transient failures only):

```sql
SELECT * FROM appeal_results
WHERE category IN ('rate_limited', 'network_timeout', 'pipeline_failed')
ORDER BY bench, year, appeal_number;
```

A "Retry all" button could re-enqueue these by resetting their rows,
or you can SSH into the relevant droplet and re-run with the same
bench/year — skip-existing will blast through the successes and only
re-attempt the failures.

## Deployment

### Per-droplet setup (scripted)

```bash
#!/bin/bash
# deploy_worker.sh — run on each droplet

# 1. Clone the repo
git clone https://github.com/you/capcha.git /opt/itat
cd /opt/itat

# 2. Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Install dependencies (CPU-only on droplets — no GPU needed)
uv sync

# 4. Create .env from the droplet's assignment
cat > .env <<'ENVEOF'
ITAT_BENCH=Chandigarh
ITAT_YEAR=2020
ITAT_SPACES_BUCKET=itat-archive
ITAT_SPACES_REGION=blr1
ITAT_SPACES_KEY=DO00...
ITAT_SPACES_SECRET=...
ITAT_DB_URL=postgresql://itat:pass@db-host:25060/itat?sslmode=require
ITAT_NODE_ID=chandigarh-2020
ITAT_RATE_PER_MINUTE=30
ITAT_DEVICE=cpu
ENVEOF

# 5. Run as a systemd service so it survives SSH disconnects and
#    auto-restarts on crash
cat > /etc/systemd/system/itat-worker.service <<'SVCEOF'
[Unit]
Description=ITAT Scraper Worker
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/itat
EnvironmentFile=/opt/itat/.env
ExecStart=/root/.local/bin/uv run main.py \
    --benches $ITAT_BENCH \
    --years $ITAT_YEAR \
    --rate $ITAT_RATE_PER_MINUTE \
    --device $ITAT_DEVICE \
    --out /opt/itat/data \
    --no-progress
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable --now itat-worker
```

### Spinning up N droplets at once

Use the DO CLI (`doctl`) to create droplets in bulk:

```bash
# Create 7 droplets for Chandigarh 2020-2026
for year in 2020 2021 2022 2023 2024 2025 2026; do
    doctl compute droplet create "itat-chandigarh-${year}" \
        --region blr1 \
        --size s-1vcpu-2gb \
        --image ubuntu-24-04-x64 \
        --user-data-file "./cloud-init/chandigarh-${year}.yaml" \
        --tag-name itat-worker \
        --wait
done
```

With cloud-init, the droplet bootstraps itself on first boot: installs
`uv`, clones the repo, writes the `.env`, enables the systemd service.
Zero manual SSH needed.

### Dashboard deployment

```bash
# Deploy dashboard to DO App Platform
doctl apps create --spec dashboard-app.yaml
```

Or run it on a $4 droplet:
```bash
uv run uvicorn dashboard:app --host 0.0.0.0 --port 8080
```

## Implementation phases

### Phase 1: S3 upload + DB reporting (core distributed capability)

Changes to existing code:

| File | Change |
| --- | --- |
| `pyproject.toml` | Add `boto3` to deps, `psycopg[binary]` to a new `distributed` dep group |
| `itat_scraper/storage.py` | **New file.** `S3Uploader` class: `upload_pdf(local_path, s3_key)`, `upload_manifest(...)` |
| `itat_scraper/reporter.py` | **New file.** `DBReporter` class: `report_appeal(result)`, `heartbeat()`, `mark_done()` |
| `itat_scraper/runner.py` | Accept optional `S3Uploader` and `DBReporter`. After local PDF save, call `uploader.upload_pdf()`. After each appeal, call `reporter.report_appeal()`. Background thread calls `reporter.heartbeat()` every 30s. |
| `main.py` | Read `ITAT_*` env vars. If `ITAT_DB_URL` is set, create `DBReporter`. If `ITAT_SPACES_BUCKET` is set, create `S3Uploader`. Pass both to `Runner`. |
| `migrations/001_init.sql` | **New file.** The CREATE TABLE statements from the schema section above. |

Estimated effort: 1-2 sessions.

### Phase 2: Monitoring dashboard

| File | Change |
| --- | --- |
| `dashboard/app.py` | **New file.** FastAPI app with routes: `/` (fleet overview), `/node/{id}` (detail), `/errors` (drilldown), `/api/nodes` (JSON for auto-refresh) |
| `dashboard/templates/` | **New dir.** Jinja2 HTML templates. One page per view. Auto-refresh via `<meta http-equiv="refresh">` or a 10-second JS fetch. |
| `dashboard-app.yaml` | **New file.** DO App Platform spec for deploying the dashboard. |

Estimated effort: 1 session.

### Phase 3: Operational tooling

| Tool | Purpose |
| --- | --- |
| `scripts/deploy_fleet.sh` | Create N droplets from a `fleet.yaml` manifest that lists (bench, year) assignments |
| `scripts/teardown_fleet.sh` | Destroy all droplets tagged `itat-worker` |
| `scripts/retry_failures.sh` | Read `appeal_results` for transient failures, SSH into the relevant droplet (or spin a new one), re-run with the same bench/year — skip-existing handles the rest |
| `scripts/generate_report.py` | Query the DB, produce a summary CSV/PDF of the entire scrape: per-bench totals, error rates, completion percentages, estimated remaining work |

Estimated effort: 1 session.

## Monitoring queries (useful from `psql` before the dashboard exists)

```sql
-- Fleet status at a glance
SELECT node_id, status, bench, year, current_appeal,
       ok_count, error_count, http_403_last_5m,
       age(now(), last_seen) AS silence
FROM node_health
ORDER BY bench, year;

-- How complete is each bench/year?
SELECT bench, year, category, count(*)
FROM appeal_results
GROUP BY bench, year, category
ORDER BY bench, year, category;

-- Which appeals failed and are worth retrying?
SELECT bench, year, appeal_number, category, note
FROM appeal_results
WHERE category IN ('rate_limited', 'network_timeout', 'pipeline_failed')
ORDER BY bench, year, appeal_number;

-- Total PDFs in S3 per bench
SELECT bench, count(*), pg_size_pretty(sum(pdf_bytes))
FROM appeal_results
WHERE category = 'ok'
GROUP BY bench;

-- Dead nodes (no heartbeat in 5 min)
SELECT node_id, bench, year, current_appeal, last_seen
FROM node_health
WHERE last_seen < now() - interval '5 minutes'
  AND status NOT IN ('done', 'error');
```

## Cost estimate for a full ITAT scrape

All 30+ benches, 7 years each = ~210 (bench, year) pairs.
At ~1500 appeals per pair average = ~315,000 appeals total.

| Item | Estimate |
| --- | --- |
| Droplets: 30 concurrent (rotate through 210 pairs) | 30 x $12 = $360/mo |
| Spaces: ~50 GB of PDFs | $5/mo |
| Managed PostgreSQL | $15/mo |
| Dashboard | $4/mo |
| **Total** | **~$384/mo** |

With 30 droplets running 7 pairs each sequentially, total runtime is
roughly 7 x (avg hours per pair). If each pair takes ~4 hours at
rate=30/min, that's ~28 hours per droplet = done in ~2 days. The
entire ITAT archive (all benches, all years) in 2 days for under $30
in compute (pro-rated for 2 days of a monthly plan).

## What stays the same

- The `Runner`, `_process_one`, captcha solving, PDF download — all
  untouched. The distributed layer wraps around the existing logic.
- Skip-existing still works (local disk first, DB fallback).
- The TUI still works for local single-machine runs.
- `uv sync` (without `--group distributed`) gives you the original
  single-machine scraper with no new dependencies.
