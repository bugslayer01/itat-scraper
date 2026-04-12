"""CLI entry point for the ITAT scraper.

Examples:
    uv run python main.py --benches Chandigarh --years 2025 --from 1 --to 100
    uv run python main.py --benches Chandigarh,Mumbai --years 2020-2026 --rate 30
    uv run python main.py --benches Delhi --years 2024 --from 350 --rate 20
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from itat_scraper import APPEAL_TYPE_LABELS, BENCH_CODES, RunConfig, Runner
from itat_scraper.runner import parse_benches, parse_years


_console = Console()


class ProgressReporter:
    """Drive a rich.Progress bar on top of the event stream.

    Per-year progress bar tracks how many appeals have been attempted out
    of --max-number as a soft ceiling (the real total is unknown until
    we've seen max-consecutive-missing in a row). The bar's description
    carries live OK/SKIP/MISS/ERR counts, throughput, and ETA.
    """

    def __init__(self, max_number: int, verbose: bool = True) -> None:
        self.max_number = max_number
        self.verbose = verbose
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            TextColumn("•"),
            TimeRemainingColumn(),
            console=_console,
            transient=False,
        )
        self._task_ids: dict[tuple[str, int], int] = {}
        self._stats = {"ok": 0, "skip": 0, "miss": 0, "nopdf": 0, "err": 0}
        self._current_key: Optional[tuple[str, int]] = None

    def _key_desc(self, bench: str, year: int) -> str:
        s = self._stats
        return (
            f"{bench}/{year}  OK {s['ok']}  SKIP {s['skip']}  "
            f"MISS {s['miss']}  NO-PDF {s['nopdf']}  ERR {s['err']}"
        )

    def on_event(self, kind: str, payload: dict) -> None:
        if kind == "model_loading":
            _console.print(f"[blue]whisper[/blue] loading [bold]{payload['size']}[/bold] "
                           f"on [bold]{payload['device']}[/bold]")
        elif kind == "model_warning":
            _console.print(f"[red]whisper WARNING:[/red] {payload['warning']}")
        elif kind == "model_ready":
            _console.print(f"[blue]whisper[/blue] ready on [bold]{payload['device']}[/bold]")
            self.progress.start()
        elif kind == "run_start":
            _console.print(
                f"[cyan]run[/cyan] benches={payload['benches']} "
                f"years={payload['years']} "
                f"range={payload['start']}..{payload['end']} "
                f"rate={payload['rate'] or 'unlimited'}/min"
            )
            _console.print(f"[cyan]out[/cyan] {payload['out']}")
        elif kind == "bench_start":
            _console.print(
                f"\n[magenta]── bench {payload['index'] + 1}/{payload['total']}: "
                f"{payload['bench']} ──[/magenta]"
            )
        elif kind == "bench_end":
            _console.print(f"[magenta]bench done:[/magenta] {payload['bench']}")
        elif kind == "year_start":
            bench = payload["bench"]
            year = payload["year"]
            key = (bench, year)
            # Reset per-year counters
            self._stats = {"ok": 0, "skip": 0, "miss": 0, "nopdf": 0, "err": 0}
            self._current_key = key
            tid = self.progress.add_task(
                self._key_desc(bench, year),
                total=self.max_number,
                completed=payload["start"] - 1,
            )
            self._task_ids[key] = tid
            _console.print(
                f"[green]── {bench}/{year} ──[/green] start={payload['start']}"
            )
        elif kind == "year_end":
            key = (payload["bench"], payload["year"])
            tid = self._task_ids.get(key)
            if tid is not None:
                # Finalise the bar at the last number we actually scraped
                self.progress.update(tid, completed=payload["last_number"])
            _console.print(
                f"[green]{payload['bench']}/{payload['year']} done:[/green] "
                f"last=#{payload['last_number']}  ({payload['reason']})"
            )
        elif kind == "retry":
            if self.verbose:
                _console.print(
                    f"  [yellow]retry[/yellow] {payload['bench']}/{payload['year']}/"
                    f"#{payload['number']} attempt {payload['attempt']}: "
                    f"{payload['reason']}",
                    highlight=False,
                )
        elif kind == "appeal_done":
            r = payload["result"]
            if r["note"] == "skipped (existing)":
                self._stats["skip"] += 1
                tag = "SKIP"
            elif r["saved_files"]:
                self._stats["ok"] += 1
                tag = "OK"
            elif r["found"]:
                self._stats["nopdf"] += 1
                tag = "NO-PDF"
            elif r["note"] == "no records":
                self._stats["miss"] += 1
                tag = "MISS"
            else:
                self._stats["err"] += 1
                tag = "ERR"
            key = (r["bench"], r["year"])
            tid = self._task_ids.get(key)
            if tid is not None:
                self.progress.update(
                    tid,
                    completed=r["appeal_number"],
                    description=self._key_desc(r["bench"], r["year"]),
                )
            if self.verbose and tag in ("ERR", "NO-PDF"):
                parties = f" [{(r.get('parties') or '')[:60]}]"
                _console.print(
                    f"  [{'red' if tag == 'ERR' else 'yellow'}]{tag}[/]  "
                    f"{r['bench']}/{r['year']}/#{r['appeal_number']}"
                    f"{parties}",
                    highlight=False,
                )
        elif kind == "cleanup":
            _console.print(
                f"[blue]cleanup:[/blue] removed {payload['removed_mp3s']} "
                f"captcha mp3 file(s)"
            )
        elif kind == "run_end":
            self.progress.stop()
            s = payload["summary"]
            _console.print(
                f"\n[bold]SUMMARY[/bold]  downloaded={s['downloaded']}  "
                f"skipped={s.get('skipped', 0)}  no-pdf={s['missing_pdf']}  "
                f"not-found={s['not_found']}  errors={s['errors']}  "
                f"total={s['total_processed']}"
            )


def _on_event(kind: str, payload: dict) -> None:
    if kind == "model_loading":
        print(
            f"[whisper] loading model: {payload['size']}  device={payload['device']}",
            flush=True,
        )
    elif kind == "model_warning":
        print(f"[whisper] WARNING: {payload['warning']}", flush=True)
    elif kind == "model_ready":
        print(f"[whisper] model ready on {payload['device']}", flush=True)
    elif kind == "run_start":
        print(
            f"[run] benches={payload['benches']} type={payload['app_type']} "
            f"years={payload['years']} range={payload['start']}..{payload['end']} "
            f"rate={payload['rate'] or 'unlimited'}/min",
            flush=True,
        )
        print(f"[run] out_dir: {payload['out']}", flush=True)
    elif kind == "bench_start":
        print(
            f"\n[bench {payload['index'] + 1}/{payload['total']}] "
            f"====== {payload['bench']} ======",
            flush=True,
        )
    elif kind == "bench_end":
        print(f"[bench] {payload['bench']} done", flush=True)
    elif kind == "year_start":
        print(
            f"\n[{payload['bench']} / {payload['year']}] "
            f"starting at appeal #{payload['start']}  ->  {payload['folder']}",
            flush=True,
        )
    elif kind == "year_end":
        print(
            f"[{payload['bench']} / {payload['year']}] ended at #{payload['last_number']} "
            f"— {payload['reason']}",
            flush=True,
        )
    elif kind == "appeal_start":
        print(
            f"[{payload['bench']} / {payload['year']} / #{payload['number']}] starting...",
            flush=True,
        )
    elif kind == "captcha_attempt":
        print(
            f"  captcha try #{payload['attempt']}: {payload['guess']!r}",
            flush=True,
        )
    elif kind == "retry":
        print(
            f"  retry #{payload['attempt']}: {payload['reason']}",
            flush=True,
        )
    elif kind == "appeal_done":
        r = payload["result"]
        if r["note"] == "skipped (existing)":
            status = "SKIP"
        elif r["saved_files"]:
            status = "OK"
        elif r["found"]:
            status = "NO-PDF"
        elif r["note"] == "no records":
            status = "MISS"
        else:
            status = "ERR"
        parties = f" [{r['parties'][:80]}]" if r.get("parties") else ""
        print(f"  => {status}: {r['note']}{parties}", flush=True)
        for s in r["saved_files"]:
            print(f"     saved: {s}", flush=True)
    elif kind == "cleanup":
        print(
            f"[cleanup] removed {payload['removed_mp3s']} captcha mp3 file(s)",
            flush=True,
        )
    elif kind == "run_end":
        s = payload["summary"]
        print("\n" + "=" * 60, flush=True)
        print(
            f"SUMMARY  downloaded={s['downloaded']}  skipped={s.get('skipped', 0)}  "
            f"no-pdf={s['missing_pdf']}  not-found={s['not_found']}  "
            f"errors={s['errors']}  total={s['total_processed']}",
            flush=True,
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="ITAT appeal PDF scraper")
    p.add_argument(
        "--benches",
        default="Chandigarh",
        help=(
            "comma-separated list of benches (e.g. 'Chandigarh,Mumbai'). "
            f"valid: {', '.join(sorted(BENCH_CODES))}"
        ),
    )
    # Backward-compatible alias for the old single-bench flag
    p.add_argument("--bench", dest="bench_alias", default=None,
                   help="alias for --benches (single bench)")
    p.add_argument(
        "--type",
        default="ITA",
        dest="app_type",
        help=f"appeal type code, one of {sorted(APPEAL_TYPE_LABELS)}",
    )
    p.add_argument(
        "--years",
        default="2025",
        help="single year, inclusive range '2020-2026', or list '2020,2023,2025'",
    )
    p.add_argument(
        "--from",
        dest="start_number",
        type=int,
        default=1,
        help="start from this appeal number (applies only to the first bench+year pair)",
    )
    p.add_argument(
        "--to",
        dest="max_number",
        type=int,
        default=10_000,
        help="max appeal number to scan (per year)",
    )
    p.add_argument(
        "--rate",
        dest="rate_per_minute",
        type=int,
        default=None,
        help="global cap on appeals processed per minute (default: unlimited)",
    )
    p.add_argument(
        "--out",
        default=".",
        help=(
            "root download directory. Files land under "
            "<out>/<Bench>/<year>/ (default: cwd)"
        ),
    )
    p.add_argument(
        "--model",
        dest="model_size",
        default="tiny.en",
        help=(
            "faster-whisper model size. Options: tiny.en (39 MB, fastest), "
            "base.en (74 MB), small.en (244 MB), medium.en (769 MB), "
            "large-v3 (1.5 GB, best), distil-large-v3 (756 MB), "
            "large-v3-turbo (809 MB). Default: tiny.en"
        ),
    )
    p.add_argument(
        "--device",
        default="auto",
        choices=["auto", "cuda", "cpu"],
        help="faster-whisper device: auto (detect GPU), cuda, or cpu",
    )
    p.add_argument(
        "--compute-type",
        default="auto",
        help=(
            "CTranslate2 compute type (auto picks float16 on cuda, int8 on cpu). "
            "Overrides: float16, int8, int8_float16, float32."
        ),
    )
    p.add_argument("--captcha-retries", type=int, default=5)
    p.add_argument("--pipeline-retries", type=int, default=3)
    p.add_argument(
        "--max-consecutive-missing",
        type=int,
        default=20,
        help="stop a year after N consecutive 'no records' results (default: 20)",
    )
    p.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        default=True,
        help="re-download appeals even if their PDF is already on disk (default: skip)",
    )
    p.add_argument(
        "--no-progress",
        action="store_true",
        help="disable the rich progress bar even when running in a TTY",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    benches_spec = args.bench_alias or args.benches
    try:
        benches = parse_benches(benches_spec)
        years = parse_years(args.years)
    except ValueError as e:
        print(f"invalid input: {e}", file=sys.stderr)
        return 2

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Use a rich progress bar when running in a real TTY, otherwise fall
    # back to the plain line-based logger so redirected output stays
    # readable (cron jobs, `tee`, etc).
    if sys.stdout.isatty() and not args.no_progress:
        reporter = ProgressReporter(max_number=args.max_number)
        on_event = reporter.on_event
    else:
        on_event = _on_event

    cfg = RunConfig(
        benches=benches,
        app_type=args.app_type,
        years=years,
        start_number=args.start_number,
        max_number=args.max_number,
        rate_per_minute=args.rate_per_minute,
        out_dir=out_dir,
        model_size=args.model_size,
        device=args.device,
        compute_type=args.compute_type,
        captcha_retries=args.captcha_retries,
        pipeline_retries=args.pipeline_retries,
        max_consecutive_missing=args.max_consecutive_missing,
        skip_existing=args.skip_existing,
    )
    try:
        cfg.validate()
    except ValueError as e:
        print(f"invalid config: {e}", file=sys.stderr)
        return 2

    Runner(cfg, on_event=on_event).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
