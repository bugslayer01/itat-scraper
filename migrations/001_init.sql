-- ITAT Distributed Scraper — initial schema
-- Run once against the shared PostgreSQL instance:
--   psql "$ITAT_DB_URL" -f migrations/001_init.sql

BEGIN;

CREATE TABLE IF NOT EXISTS appeal_results (
    id            BIGSERIAL PRIMARY KEY,
    node_id       TEXT NOT NULL,
    bench         TEXT NOT NULL,
    year          INT NOT NULL,
    appeal_number INT NOT NULL,
    category      TEXT NOT NULL,
    parties       TEXT,
    s3_key        TEXT,
    pdf_bytes     BIGINT,
    attempts      INT NOT NULL DEFAULT 0,
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (bench, year, appeal_number)
);

CREATE INDEX IF NOT EXISTS idx_results_node     ON appeal_results (node_id);
CREATE INDEX IF NOT EXISTS idx_results_category ON appeal_results (category);
CREATE INDEX IF NOT EXISTS idx_results_bench_year ON appeal_results (bench, year);

CREATE TABLE IF NOT EXISTS node_health (
    node_id         TEXT PRIMARY KEY,
    bench           TEXT NOT NULL,
    year            INT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'starting',
    current_appeal  INT,
    ip_address      TEXT,

    ok_count        INT NOT NULL DEFAULT 0,
    skipped_count   INT NOT NULL DEFAULT 0,
    miss_count      INT NOT NULL DEFAULT 0,
    nopdf_count     INT NOT NULL DEFAULT 0,
    error_count     INT NOT NULL DEFAULT 0,
    total_count     INT NOT NULL DEFAULT 0,

    http_403_last_5m INT NOT NULL DEFAULT 0,

    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);

COMMIT;
