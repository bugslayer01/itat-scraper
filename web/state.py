"""Shared server state — single runner instance, stats, drill-down data."""
from __future__ import annotations

import threading
from dataclasses import asdict
from enum import Enum
from typing import Optional

from itat_scraper.models import CaseResult
from itat_scraper.runner import Runner, RunConfig


class RunState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"


class AppState:
    """Singleton that holds the current run, stats, and drill-down data.

    Thread-safe: all mutations go through methods that acquire _lock.
    """

    def __init__(self) -> None:
        self.runner: Optional[Runner] = None
        self.run_state: RunState = RunState.IDLE
        self.config: Optional[RunConfig] = None
        self._lock = threading.Lock()
        self.reset_stats()

    # ---- stats ----

    def reset_stats(self) -> None:
        with self._lock if hasattr(self, "_lock") else threading.Lock():
            self.stats = {
                "downloaded": 0,
                "skipped": 0,
                "nopdf": 0,
                "notfound": 0,
                "captcha": 0,
                "captcha_retries": 0,
                "errors": 0,
                "total": 0,
            }
            self.appeals_by_category: dict[str, list[dict]] = {
                "downloaded": [],
                "skipped": [],
                "nopdf": [],
                "notfound": [],
                "captcha": [],
                "errors": [],
            }
            self.log_messages: list[dict] = []

    def classify_tag(self, r: dict) -> tuple[str, str]:
        """Return (tag, category_key) for a result dict."""
        note = r.get("note", "")
        if note == "skipped (existing)":
            return "SKIP", "skipped"
        if r.get("saved_files"):
            return "OK", "downloaded"
        if r.get("found"):
            return "NO-PDF", "nopdf"
        if note == "no records":
            return "MISS", "notfound"
        if "captcha failed" in note:
            return "CAPTCHA", "captcha"
        return "ERR", "errors"

    def bump_stats(self, r: dict) -> None:
        with self._lock:
            self.stats["total"] += 1
            tag, cat = self.classify_tag(r)
            entry = {
                "number": r.get("appeal_number"),
                "bench": r.get("bench"),
                "year": r.get("year"),
                "app_type": r.get("app_type", ""),
                "note": r.get("note", ""),
                "parties": r.get("parties") or "",
                "attempts": r.get("attempts", 0),
            }
            if cat == "downloaded":
                self.stats["downloaded"] += 1
            elif cat == "skipped":
                self.stats["skipped"] += 1
            elif cat == "nopdf":
                self.stats["nopdf"] += 1
            elif cat == "notfound":
                self.stats["notfound"] += 1
            elif cat == "captcha":
                self.stats["captcha"] += 1
            else:
                self.stats["errors"] += 1
            self.appeals_by_category[cat].append(entry)

    def bump_captcha_retries(self) -> None:
        with self._lock:
            self.stats["captcha_retries"] += 1

    def add_log(self, level: str, message: str) -> None:
        with self._lock:
            self.log_messages.append({"level": level, "message": message})
            # Keep last 2000 log lines
            if len(self.log_messages) > 2000:
                self.log_messages = self.log_messages[-1500:]

    def get_results(self, category: str) -> list[dict]:
        with self._lock:
            if category == "all":
                all_results = []
                for cat in ("downloaded", "skipped", "nopdf", "notfound", "captcha", "errors"):
                    for e in self.appeals_by_category.get(cat, []):
                        all_results.append({**e, "category": cat})
                return all_results
            return list(self.appeals_by_category.get(category, []))

    def get_stats(self) -> dict:
        with self._lock:
            return dict(self.stats)

    def get_status(self) -> dict:
        return {
            "state": self.run_state.value,
            "stats": self.get_stats(),
            "config": asdict(self.config) if self.config else None,
        }


# Module-level singleton
app_state = AppState()
