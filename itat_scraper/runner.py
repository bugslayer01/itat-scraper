"""Orchestration: process a single appeal, loop over (bench, year, number)."""
from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

from .captcha import load_whisper_model, solve_captcha, verify_captcha
from .constants import APPEAL_TYPE_LABELS, BENCH_CODES, HTTP_TIMEOUT
from .models import CaseResult, RunSummary
from .ratelimit import RateLimiter
from .scraper import (
    _with_backoff,
    download_pdf,
    extract_case_info,
    extract_casedetails_links,
    extract_pdf_links,
    fetch_csrftkn,
    new_session,
    no_records,
    submit_search,
)


EventCallback = Callable[[str, dict], None]


@dataclass
class RunConfig:
    benches: list[str] = field(default_factory=lambda: ["Chandigarh"])
    app_type: str = "ITA"
    years: list[int] = field(default_factory=lambda: [2025])
    start_number: int = 1
    max_number: int = 10_000
    max_consecutive_missing: int = 20
    captcha_retries: int = 5
    pipeline_retries: int = 3
    rate_per_minute: Optional[int] = None
    model_size: str = "tiny.en"
    device: str = "auto"  # "auto" | "cuda" | "cpu"
    compute_type: str = "auto"  # "auto" | "float16" | "int8" | "int8_float16" | ...
    out_dir: Path = field(default_factory=lambda: Path("."))
    polite_delay_s: float = 0.8
    skip_existing: bool = True  # short-circuit appeals whose PDF is already on disk
    min_pdf_bytes: int = 1024  # anything smaller is treated as corrupt and re-fetched

    def validate(self) -> None:
        if not self.benches:
            raise ValueError("benches list is empty")
        for b in self.benches:
            if b not in BENCH_CODES:
                raise ValueError(f"unknown bench: {b}")
        if self.app_type not in APPEAL_TYPE_LABELS:
            raise ValueError(f"unknown appeal type: {self.app_type}")
        if not self.years:
            raise ValueError("years list is empty")
        if self.start_number < 1:
            raise ValueError("start_number must be >= 1")
        if self.max_number < self.start_number:
            raise ValueError("max_number must be >= start_number")


class Runner:
    """Drives the scraping loop across (bench, year, appeal_number).

    Output layout:
        out_dir / <Bench> / <year> / <pdfs + manifest.jsonl + missing_pdfs.csv>

    `start_number` applies only to the FIRST (bench, year) pair processed.
    Every subsequent (bench, year) starts at 1. Rate limiting and the temp
    directory for captcha audio are global across the whole run.
    """

    def __init__(self, config: RunConfig, on_event: Optional[EventCallback] = None):
        config.validate()
        self.config = config
        self.on_event = on_event or (lambda kind, payload: None)
        self.rate_limiter = RateLimiter(config.rate_per_minute)
        self._model = None
        self.summary = RunSummary(
            bench=", ".join(config.benches),
            app_type=config.app_type,
            year_range=list(config.years),
            appeal_range=(config.start_number, config.max_number),
        )
        # Per-leaf state: results bucket so each leaf writes its own manifest
        self._leaf_results: dict[tuple[str, int], list[CaseResult]] = {}
        # Shared temp dir for captcha MP3s — one root, not per-leaf
        self.tmp_dir: Path = (config.out_dir / ".itat_tmp").resolve()
        self._stop = False

    # ------------------------- public -------------------------

    def stop(self) -> None:
        self._stop = True

    def run(self) -> RunSummary:
        self._ensure_tmp_dir()
        self._load_model()
        self._emit(
            "run_start",
            benches=list(self.config.benches),
            app_type=self.config.app_type,
            years=list(self.config.years),
            start=self.config.start_number,
            end=self.config.max_number,
            out=str(Path(self.config.out_dir).resolve()),
            rate=self.config.rate_per_minute,
        )

        try:
            is_first_pair = True
            for bench_idx, bench in enumerate(self.config.benches):
                if self._stop:
                    break
                bench_code = BENCH_CODES[bench]
                self._emit(
                    "bench_start",
                    bench=bench,
                    index=bench_idx,
                    total=len(self.config.benches),
                )
                for year in self.config.years:
                    if self._stop:
                        break
                    start = self.config.start_number if is_first_pair else 1
                    is_first_pair = False
                    self._process_year(bench, bench_code, year, start)
                self._emit("bench_end", bench=bench)
        finally:
            removed = self._cleanup_tmp()
            self._emit("cleanup", removed_mp3s=removed, tmp_dir=str(self.tmp_dir))

        self._emit("run_end", summary=asdict(self.summary))
        return self.summary

    # ------------------------- per-year loop -------------------------

    def _process_year(self, bench: str, bench_code: str, year: int, start: int) -> None:
        leaf = self._folder_for(bench, year)
        leaf.mkdir(parents=True, exist_ok=True)
        self._leaf_results.setdefault((bench, year), [])
        self._emit(
            "year_start", bench=bench, year=year, start=start, folder=str(leaf)
        )

        consecutive_missing = 0
        last_number_scraped = start - 1
        stopped_reason: Optional[str] = None

        for number in range(start, self.config.max_number + 1):
            if self._stop:
                stopped_reason = "stop requested"
                break

            # Peek at skip-existing BEFORE rate limiting and polite delay —
            # a resumed run should blaze through already-downloaded appeals
            # without waiting a second for each one.
            skipped_result: Optional[CaseResult] = None
            if self.config.skip_existing:
                existing = self._existing_pdfs_for(bench, year, number, leaf)
                if existing:
                    skipped_result = CaseResult(
                        appeal_number=number,
                        bench=bench,
                        app_type=self.config.app_type,
                        year=year,
                        found=True,
                        saved_files=[
                            f"{p.name} ({p.stat().st_size} bytes)" for p in existing
                        ],
                        pdf_urls=[],
                        attempts=0,
                        note="skipped (existing)",
                    )

            if skipped_result is not None:
                result = skipped_result
            else:
                self.rate_limiter.wait()
                self._emit("appeal_start", bench=bench, year=year, number=number)
                result = self._process_with_retries(bench, bench_code, year, number, leaf)
                self.rate_limiter.record()

            self._leaf_results[(bench, year)].append(result)
            self._update_summary(result)
            last_number_scraped = number
            self._emit("appeal_done", result=asdict(result))
            self._write_manifest(bench, year)

            if not result.found and result.note == "no records":
                consecutive_missing += 1
                if consecutive_missing >= self.config.max_consecutive_missing:
                    stopped_reason = (
                        f"{consecutive_missing} consecutive 'no records'"
                    )
                    break
            else:
                consecutive_missing = 0

            # Skip the polite delay on short-circuited skips so resumes fly.
            if skipped_result is None:
                time.sleep(self.config.polite_delay_s)
        else:
            stopped_reason = "reached max_number"

        self._emit(
            "year_end",
            bench=bench,
            year=year,
            reason=stopped_reason or "unknown",
            last_number=last_number_scraped,
        )

    # ------------------------- appeal processing -------------------------

    def _process_with_retries(
        self,
        bench: str,
        bench_code: str,
        year: int,
        number: int,
        leaf: Path,
    ) -> CaseResult:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.config.pipeline_retries + 1):
            try:
                return self._process_one(bench, bench_code, year, number, leaf)
            except requests.exceptions.Timeout as e:
                last_exc = e
                self._emit(
                    "retry",
                    bench=bench,
                    year=year,
                    number=number,
                    attempt=attempt,
                    reason=f"timeout: {e}",
                )
            except requests.exceptions.RequestException as e:
                last_exc = e
                self._emit(
                    "retry",
                    bench=bench,
                    year=year,
                    number=number,
                    attempt=attempt,
                    reason=f"net: {e}",
                )
            except Exception as e:
                last_exc = e
                self._emit(
                    "retry",
                    bench=bench,
                    year=year,
                    number=number,
                    attempt=attempt,
                    reason=f"{type(e).__name__}: {e}",
                )
            time.sleep(2 * attempt)
        return CaseResult(
            appeal_number=number,
            bench=bench,
            app_type=self.config.app_type,
            year=year,
            found=False,
            attempts=self.config.pipeline_retries,
            note=f"pipeline failed: {type(last_exc).__name__}: {last_exc}",
        )

    def _existing_pdfs_for(
        self, bench: str, year: int, number: int, leaf: Path
    ) -> list[Path]:
        """Return any PDFs already on disk for this appeal (there may be
        multiple order PDFs per case)."""
        safe_bench = bench.replace(" ", "_")
        prefix = f"{safe_bench}_{self.config.app_type}_{number}_{year}_order"
        return [
            p
            for p in leaf.glob(f"{prefix}*.pdf")
            if p.is_file() and p.stat().st_size >= self.config.min_pdf_bytes
        ]

    def _process_one(
        self, bench: str, bench_code: str, year: int, number: int, leaf: Path
    ) -> CaseResult:
        session = new_session()
        csrf = fetch_csrftkn(session)

        captcha = None
        attempts_used = 0
        for attempt in range(1, self.config.captcha_retries + 1):
            attempts_used = attempt
            guess = solve_captcha(session, self._model, self.tmp_dir)
            self._emit(
                "captcha_attempt",
                bench=bench,
                year=year,
                number=number,
                attempt=attempt,
                guess=guess,
            )
            if verify_captcha(session, csrf, guess):
                captcha = guess
                break
            session = new_session()
            csrf = fetch_csrftkn(session)

        if captcha is None:
            return CaseResult(
                appeal_number=number,
                bench=bench,
                app_type=self.config.app_type,
                year=year,
                found=False,
                attempts=attempts_used,
                note=f"captcha failed after {self.config.captcha_retries} retries",
            )

        response = submit_search(
            session, csrf, captcha, bench_code, self.config.app_type, number, year
        )
        results_html = response.text

        if no_records(results_html):
            return CaseResult(
                appeal_number=number,
                bench=bench,
                app_type=self.config.app_type,
                year=year,
                found=False,
                attempts=attempts_used,
                note="no records",
            )

        casedetails_links = extract_casedetails_links(results_html)
        if not casedetails_links:
            return CaseResult(
                appeal_number=number,
                bench=bench,
                app_type=self.config.app_type,
                year=year,
                found=False,
                attempts=attempts_used,
                note="results page has no casedetails link",
            )

        details_resp = _with_backoff(
            lambda: session.get(casedetails_links[0], timeout=HTTP_TIMEOUT)
        )
        details_resp.raise_for_status()
        details_html = details_resp.text

        info = extract_case_info(details_html)
        pdf_links = extract_pdf_links(details_html)

        base = dict(
            appeal_number=number,
            bench=bench,
            app_type=self.config.app_type,
            year=year,
            found=True,
            title=(info.get("headline") or "")[:300],
            parties=info.get("parties"),
            status=info.get("case_status"),
            filed_on=info.get("filed_on"),
            assessment_year=info.get("assessment_year"),
            bench_alloted=info.get("bench_alloted"),
            attempts=attempts_used,
        )

        if not pdf_links:
            return CaseResult(
                **base,
                pdf_urls=[],
                saved_files=[],
                note="case found but no PDF order yet",
            )

        safe_bench = bench.replace(" ", "_")
        saved: list[str] = []
        for i, url in enumerate(pdf_links, 1):
            filename = f"{safe_bench}_{self.config.app_type}_{number}_{year}_order{i}.pdf"
            out_path = leaf / filename
            size = download_pdf(session, url, out_path)
            saved.append(f"{out_path.name} ({size} bytes)")

        return CaseResult(
            **base,
            pdf_urls=pdf_links,
            saved_files=saved,
            note="ok",
        )

    # ------------------------- helpers -------------------------

    def _emit(self, kind: str, **payload) -> None:
        self.on_event(kind, payload)

    def _ensure_tmp_dir(self) -> None:
        # Also ensure the root output directory exists — the user may have
        # typed a relative or not-yet-created path.
        Path(self.config.out_dir).expanduser().mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def _load_model(self):
        if self._model is None:
            self._emit(
                "model_loading",
                size=self.config.model_size,
                device=self.config.device,
            )
            self._model, actual_device, warning = load_whisper_model(
                self.config.model_size,
                device=self.config.device,
                compute_type=self.config.compute_type,
            )
            if warning:
                self._emit("model_warning", warning=warning)
            self._emit(
                "model_ready",
                size=self.config.model_size,
                device=actual_device,
            )
        return self._model

    def _folder_for(self, bench: str, year: int) -> Path:
        safe_bench = bench.replace(" ", "_")
        return (self.config.out_dir / safe_bench / str(year)).resolve()

    def _update_summary(self, r: CaseResult) -> None:
        s = self.summary
        s.total_processed += 1
        if r.note == "skipped (existing)":
            s.skipped += 1
        elif r.downloaded:
            s.downloaded += 1
        elif r.missing_pdf:
            s.missing_pdf += 1
        elif r.note.startswith(("pipeline failed", "captcha failed")):
            s.errors += 1
        else:
            s.not_found += 1

    def _write_manifest(self, bench: str, year: int) -> None:
        leaf = self._folder_for(bench, year)
        results = self._leaf_results.get((bench, year), [])

        manifest_path = leaf / "manifest.jsonl"
        with manifest_path.open("w") as f:
            for r in results:
                row = asdict(r)
                row["category"] = classify_failure(r)
                f.write(json.dumps(row, default=str) + "\n")

        missing_path = leaf / "missing_pdfs.csv"
        with missing_path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "appeal_number", "bench", "app_type", "year",
                "found", "parties", "status", "filed_on",
                "assessment_year", "bench_alloted", "attempts", "note",
            ])
            for r in results:
                if r.missing_pdf or not r.found:
                    w.writerow([
                        r.appeal_number, r.bench, r.app_type, r.year,
                        r.found, r.parties or "", r.status or "",
                        r.filed_on or "", r.assessment_year or "",
                        r.bench_alloted or "", r.attempts, r.note,
                    ])

        # failures.csv — one row per non-OK appeal, with a stable category
        # so you can filter and retry just the recoverable kinds.
        failures_path = leaf / "failures.csv"
        with failures_path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "category", "appeal_number", "bench", "app_type", "year",
                "attempts", "parties", "note",
            ])
            for r in results:
                cat = classify_failure(r)
                if cat in ("ok", "skipped"):
                    continue
                w.writerow([
                    cat, r.appeal_number, r.bench, r.app_type, r.year,
                    r.attempts, r.parties or "", r.note,
                ])

    def _cleanup_tmp(self) -> int:
        """Delete any captcha MP3 stragglers and the shared tmp dir."""
        removed = 0
        if self.tmp_dir.exists():
            for f in self.tmp_dir.glob("*.mp3"):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
            try:
                self.tmp_dir.rmdir()
            except OSError:
                pass
        return removed


# ------------------------- input parsing helpers -------------------------

def classify_failure(result: CaseResult) -> str:
    """Bucket a CaseResult note into a stable failure category string.
    Returns 'ok' / 'skipped' / 'no_pdf' / 'no_records' / category for
    failed appeals."""
    if result.note == "skipped (existing)":
        return "skipped"
    if result.downloaded:
        return "ok"
    if result.missing_pdf:
        return "no_pdf"
    note = (result.note or "").lower()
    if "no records" in note:
        return "no_records"
    if "captcha failed" in note:
        return "captcha_failed"
    if "403" in note or "429" in note or "forbidden" in note or "rate" in note:
        return "rate_limited"
    if "timeout" in note or "timed out" in note:
        return "network_timeout"
    if "5" in note and ("502" in note or "503" in note or "504" in note or "500" in note):
        return "server_error"
    if "parse" in note or "not found in page" in note or "casedetails link" in note:
        return "parse_failed"
    if "pipeline failed" in note:
        return "pipeline_failed"
    return "unknown"


def parse_years(spec: str) -> list[int]:
    """Accept '2025', '2022-2025', '2020,2022,2024'."""
    spec = spec.strip()
    if "," in spec:
        return sorted({int(x) for x in spec.split(",") if x.strip()})
    if "-" in spec:
        a, b = spec.split("-", 1)
        lo, hi = int(a), int(b)
        if lo > hi:
            lo, hi = hi, lo
        return list(range(lo, hi + 1))
    return [int(spec)]


def parse_benches(spec: str) -> list[str]:
    """Comma-separated bench list, order preserved, duplicates dropped."""
    seen: list[str] = []
    for part in spec.split(","):
        name = part.strip()
        if name and name not in seen:
            seen.append(name)
    return seen
