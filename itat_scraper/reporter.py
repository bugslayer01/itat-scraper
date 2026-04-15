"""PostgreSQL status reporter for distributed scraping."""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import CaseResult

log = logging.getLogger(__name__)

_UPSERT_APPEAL = """
INSERT INTO appeal_results
    (node_id, bench, year, appeal_number, category, parties, s3_key, pdf_bytes, attempts, note, updated_at)
VALUES
    (%(node_id)s, %(bench)s, %(year)s, %(appeal_number)s, %(category)s,
     %(parties)s, %(s3_key)s, %(pdf_bytes)s, %(attempts)s, %(note)s, now())
ON CONFLICT (bench, year, appeal_number)
DO UPDATE SET
    node_id   = EXCLUDED.node_id,
    category  = EXCLUDED.category,
    parties   = EXCLUDED.parties,
    s3_key    = EXCLUDED.s3_key,
    pdf_bytes = EXCLUDED.pdf_bytes,
    attempts  = EXCLUDED.attempts,
    note      = EXCLUDED.note,
    updated_at = now();
"""

_UPSERT_HEALTH = """
INSERT INTO node_health
    (node_id, bench, year, status, current_appeal, ip_address,
     ok_count, skipped_count, miss_count, nopdf_count, error_count, total_count,
     http_403_last_5m, last_seen)
VALUES
    (%(node_id)s, %(bench)s, %(year)s, %(status)s, %(current_appeal)s, %(ip_address)s,
     %(ok_count)s, %(skipped_count)s, %(miss_count)s, %(nopdf_count)s,
     %(error_count)s, %(total_count)s, %(http_403_last_5m)s, now())
ON CONFLICT (node_id)
DO UPDATE SET
    status          = EXCLUDED.status,
    current_appeal  = EXCLUDED.current_appeal,
    ok_count        = EXCLUDED.ok_count,
    skipped_count   = EXCLUDED.skipped_count,
    miss_count      = EXCLUDED.miss_count,
    nopdf_count     = EXCLUDED.nopdf_count,
    error_count     = EXCLUDED.error_count,
    total_count     = EXCLUDED.total_count,
    http_403_last_5m = EXCLUDED.http_403_last_5m,
    last_seen       = now();
"""

_MARK_FINISHED = """
UPDATE node_health
SET status = %(status)s, finished_at = now(), last_seen = now()
WHERE node_id = %(node_id)s;
"""


def _get_public_ip() -> str:
    """Best-effort public IP for display in the dashboard."""
    try:
        import requests
        return requests.get("https://ifconfig.me", timeout=5).text.strip()
    except Exception:
        return "unknown"


class DBReporter:
    """Reports appeal results and node health to a shared PostgreSQL instance.

    Configured via environment variables:
        ITAT_DB_URL   — PostgreSQL connection string
        ITAT_NODE_ID  — unique node identifier (e.g. "chandigarh-2020")
        ITAT_BENCH    — bench name this node is scraping
        ITAT_YEAR     — year this node is scraping
    """

    def __init__(self) -> None:
        import psycopg

        self.node_id = os.environ["ITAT_NODE_ID"]
        self.bench = os.environ["ITAT_BENCH"]
        self.year = int(os.environ["ITAT_YEAR"])
        self._conn = psycopg.connect(os.environ["ITAT_DB_URL"], autocommit=True)
        self._ip = _get_public_ip()

        # Rolling counters for the heartbeat
        self._stats = {
            "ok": 0, "skipped": 0, "miss": 0, "nopdf": 0, "error": 0, "total": 0
        }
        self._current_appeal = 0
        self._http_403_count = 0
        self._lock = threading.Lock()

        # Background heartbeat thread
        self._stop_event = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True
        )
        self._heartbeat_thread.start()

    def report_appeal(self, result: "CaseResult", category: str,
                      s3_key: str | None = None) -> None:
        """Upsert a single appeal result into the DB."""
        total_pdf_bytes = 0
        if result.saved_files:
            for f in result.saved_files:
                # saved_files entries look like "name.pdf (12345 bytes)"
                if "(" in f and "bytes)" in f:
                    try:
                        total_pdf_bytes += int(f.split("(")[1].split()[0])
                    except (ValueError, IndexError):
                        pass

        params = {
            "node_id": self.node_id,
            "bench": result.bench,
            "year": result.year,
            "appeal_number": result.appeal_number,
            "category": category,
            "parties": result.parties or None,
            "s3_key": s3_key,
            "pdf_bytes": total_pdf_bytes or None,
            "attempts": result.attempts,
            "note": result.note,
        }
        try:
            self._conn.execute(_UPSERT_APPEAL, params)
        except Exception:
            log.exception("failed to report appeal %s/%s/#%d to DB",
                          result.bench, result.year, result.appeal_number)

        # Update rolling counters
        with self._lock:
            self._stats["total"] += 1
            self._current_appeal = result.appeal_number
            if category == "ok":
                self._stats["ok"] += 1
            elif category == "skipped":
                self._stats["skipped"] += 1
            elif category == "no_records":
                self._stats["miss"] += 1
            elif category == "no_pdf":
                self._stats["nopdf"] += 1
            else:
                self._stats["error"] += 1

    def record_403(self) -> None:
        """Increment the 403 counter (called from the backoff layer)."""
        with self._lock:
            self._http_403_count += 1

    def mark_done(self) -> None:
        """Mark this node as finished successfully."""
        self._finish("done")

    def mark_error(self, reason: str = "") -> None:
        """Mark this node as finished with an error."""
        self._finish("error")

    def shutdown(self) -> None:
        """Stop the heartbeat thread and close the DB connection."""
        self._stop_event.set()
        self._heartbeat_thread.join(timeout=5)
        self._conn.close()

    def _finish(self, status: str) -> None:
        try:
            self._conn.execute(_MARK_FINISHED, {
                "node_id": self.node_id,
                "status": status,
            })
        except Exception:
            log.exception("failed to mark node %s as %s", self.node_id, status)
        self.shutdown()

    def _heartbeat_loop(self) -> None:
        """Send a heartbeat to the DB every 30 seconds."""
        while not self._stop_event.wait(timeout=30):
            self._send_heartbeat("running")
        # Final heartbeat before exit
        self._send_heartbeat("stopping")

    def _send_heartbeat(self, status: str) -> None:
        with self._lock:
            params = {
                "node_id": self.node_id,
                "bench": self.bench,
                "year": self.year,
                "status": status,
                "current_appeal": self._current_appeal,
                "ip_address": self._ip,
                "ok_count": self._stats["ok"],
                "skipped_count": self._stats["skipped"],
                "miss_count": self._stats["miss"],
                "nopdf_count": self._stats["nopdf"],
                "error_count": self._stats["error"],
                "total_count": self._stats["total"],
                "http_403_last_5m": self._http_403_count,
            }
            # Reset 403 counter each heartbeat (approximates "last 5 min" at 30s intervals)
            self._http_403_count = 0
        try:
            self._conn.execute(_UPSERT_HEALTH, params)
        except Exception:
            log.exception("heartbeat failed for node %s", self.node_id)


def create_reporter() -> DBReporter | None:
    """Create a DBReporter if the required env vars are set, else None."""
    required = ("ITAT_DB_URL", "ITAT_NODE_ID", "ITAT_BENCH", "ITAT_YEAR")
    if all(os.environ.get(k) for k in required):
        return DBReporter()
    return None
