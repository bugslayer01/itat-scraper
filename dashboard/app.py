"""Monitoring dashboard for distributed ITAT scraping fleet.

Run:
    ITAT_DB_URL=postgresql://... uv run uvicorn dashboard.app:app --host 0.0.0.0 --port 8080

Reads from the shared PostgreSQL instance populated by worker nodes.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="ITAT Scraper Dashboard")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DB_URL = os.environ["ITAT_DB_URL"]


def _conn():
    return psycopg.connect(DB_URL, autocommit=True)


def _age_str(dt: datetime | None) -> str:
    """Human-readable age string like '3s ago', '2m ago', '1h ago'."""
    if dt is None:
        return "never"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (now - dt).total_seconds()
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    return f"{delta / 3600:.1f}h ago"


def _node_status(row: dict) -> str:
    """Derive display status from node_health row."""
    if row["status"] in ("done", "error"):
        return row["status"]
    if row["last_seen"] is None:
        return "unknown"
    now = datetime.now(timezone.utc)
    last = row["last_seen"]
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (now - last).total_seconds()
    if age < 120:
        return "running"
    if age < 300:
        return "stale"
    return "dead"


# ─── Fleet overview ───────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def fleet_overview(request: Request):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM node_health ORDER BY bench, year"
        ).fetchall()
        columns = [desc.name for desc in conn.execute(
            "SELECT * FROM node_health LIMIT 0"
        ).description]

    nodes = []
    for row in rows:
        d = dict(zip(columns, row))
        d["display_status"] = _node_status(d)
        d["last_seen_ago"] = _age_str(d.get("last_seen"))
        nodes.append(d)

    return templates.TemplateResponse("fleet.html", {
        "request": request,
        "nodes": nodes,
        "now": datetime.now(timezone.utc),
    })


# ─── Node detail ─────────────────────────────────────────────

@app.get("/node/{node_id}", response_class=HTMLResponse)
def node_detail(
    request: Request,
    node_id: str,
    category: str = Query(default="all"),
):
    with _conn() as conn:
        # Node health
        health = conn.execute(
            "SELECT * FROM node_health WHERE node_id = %s", (node_id,)
        ).fetchone()
        health_cols = [desc.name for desc in conn.execute(
            "SELECT * FROM node_health LIMIT 0"
        ).description]

        # Appeal results
        if category == "all":
            results = conn.execute(
                "SELECT * FROM appeal_results WHERE node_id = %s ORDER BY appeal_number",
                (node_id,)
            ).fetchall()
        else:
            results = conn.execute(
                "SELECT * FROM appeal_results WHERE node_id = %s AND category = %s ORDER BY appeal_number",
                (node_id, category)
            ).fetchall()
        result_cols = [desc.name for desc in conn.execute(
            "SELECT * FROM appeal_results LIMIT 0"
        ).description]

        # Category counts for filter buttons
        counts = conn.execute(
            "SELECT category, count(*) FROM appeal_results WHERE node_id = %s GROUP BY category ORDER BY count(*) DESC",
            (node_id,)
        ).fetchall()

    node = dict(zip(health_cols, health)) if health else {}
    if node:
        node["display_status"] = _node_status(node)
        node["last_seen_ago"] = _age_str(node.get("last_seen"))

    appeals = [dict(zip(result_cols, r)) for r in results]
    cat_counts = {row[0]: row[1] for row in counts}

    return templates.TemplateResponse("node.html", {
        "request": request,
        "node": node,
        "node_id": node_id,
        "appeals": appeals,
        "category": category,
        "cat_counts": cat_counts,
    })


# ─── Error drilldown ─────────────────────────────────────────

@app.get("/errors", response_class=HTMLResponse)
def errors(request: Request):
    with _conn() as conn:
        rows = conn.execute("""
            SELECT category, bench, year, count(*) as cnt,
                   array_agg(appeal_number ORDER BY appeal_number) as appeals
            FROM appeal_results
            WHERE category NOT IN ('ok', 'skipped', 'no_records')
            GROUP BY category, bench, year
            ORDER BY cnt DESC
        """).fetchall()

    errors = []
    for row in rows:
        errors.append({
            "category": row[0],
            "bench": row[1],
            "year": row[2],
            "count": row[3],
            "appeals": row[4][:20] if row[4] else [],  # cap display at 20
            "total_appeals": len(row[4]) if row[4] else 0,
        })

    return templates.TemplateResponse("errors.html", {
        "request": request,
        "errors": errors,
    })


# ─── Retry queue ──────────────────────────────────────────────

@app.get("/retry", response_class=HTMLResponse)
def retry_queue(request: Request):
    with _conn() as conn:
        rows = conn.execute("""
            SELECT node_id, bench, year, appeal_number, category, attempts, note
            FROM appeal_results
            WHERE category IN ('rate_limited', 'network_timeout', 'pipeline_failed', 'server_error')
            ORDER BY bench, year, appeal_number
        """).fetchall()
        cols = ["node_id", "bench", "year", "appeal_number", "category", "attempts", "note"]

    retryable = [dict(zip(cols, r)) for r in rows]

    return templates.TemplateResponse("retry.html", {
        "request": request,
        "retryable": retryable,
        "total": len(retryable),
    })


# ─── JSON API for programmatic access / auto-refresh ──────────

@app.get("/api/nodes")
def api_nodes():
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM node_health ORDER BY bench, year"
        ).fetchall()
        columns = [desc.name for desc in conn.execute(
            "SELECT * FROM node_health LIMIT 0"
        ).description]

    nodes = []
    for row in rows:
        d = dict(zip(columns, row))
        d["display_status"] = _node_status(d)
        d["last_seen_ago"] = _age_str(d.get("last_seen"))
        # Serialize datetimes
        for k in ("started_at", "last_seen", "finished_at"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        nodes.append(d)
    return nodes


@app.get("/api/summary")
def api_summary():
    with _conn() as conn:
        row = conn.execute("""
            SELECT
                count(*) FILTER (WHERE category = 'ok') as ok,
                count(*) FILTER (WHERE category = 'skipped') as skipped,
                count(*) FILTER (WHERE category = 'no_records') as no_records,
                count(*) FILTER (WHERE category = 'no_pdf') as no_pdf,
                count(*) FILTER (WHERE category NOT IN ('ok', 'skipped', 'no_records', 'no_pdf')) as errors,
                count(*) as total
            FROM appeal_results
        """).fetchone()
    return {
        "ok": row[0], "skipped": row[1], "no_records": row[2],
        "no_pdf": row[3], "errors": row[4], "total": row[5],
    }
