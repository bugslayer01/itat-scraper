"""Textual TUI for the ITAT scraper.

Run:
    uv run python tui.py
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

from itat_scraper import APPEAL_TYPE_LABELS, BENCH_CODES, RunConfig, Runner
from itat_scraper.runner import parse_years

# Whisper model options surfaced in the TUI dropdown. Labels describe the
# tradeoffs so you can pick without context-switching.
WHISPER_MODEL_OPTIONS: list[tuple[str, str]] = [
    ("tiny.en — 39 MB, fastest, English", "tiny.en"),
    ("base.en — 74 MB, English", "base.en"),
    ("small.en — 244 MB, English, better accuracy", "small.en"),
    ("medium.en — 769 MB, high accuracy, English", "medium.en"),
    ("distil-large-v3 — 756 MB, distilled large (fast)", "distil-large-v3"),
    ("large-v3-turbo — 809 MB, fast turbo variant", "large-v3-turbo"),
    ("large-v3 — 1.5 GB, best multilingual", "large-v3"),
]

DEVICE_OPTIONS: list[tuple[str, str]] = [
    ("auto — detect GPU, fall back to CPU", "auto"),
    ("cuda — force GPU (float16)", "cuda"),
    ("cpu — force CPU (int8)", "cpu"),
]


class ItatTui(App):
    CSS = """
    Screen {
        layout: vertical;
    }

    #config-panel {
        height: auto;
        padding: 1 2;
        border: round $primary;
    }

    #top-config {
        height: auto;
    }

    #bench-col {
        width: 32;
        margin-right: 1;
    }

    #bench-col SelectionList {
        height: 24;
        border: round $accent;
    }

    #fields-col {
        width: 1fr;
    }

    .row {
        height: 5;
        margin-bottom: 1;
    }

    .field {
        width: 1fr;
        margin-right: 1;
        height: auto;
    }

    Label.field-label {
        color: $text-muted;
        margin-bottom: 0;
    }

    Label.title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }

    Label.section {
        text-style: bold;
        color: $secondary;
    }

    #controls {
        height: 3;
        margin-top: 1;
    }

    Button {
        margin-right: 2;
    }

    #stats-panel {
        height: auto;
        padding: 0 1;
        border: round $success;
        margin-top: 1;
    }

    #stats-panel Button {
        min-width: 16;
        margin: 0 1;
    }

    #split {
        height: 1fr;
        margin-top: 1;
    }

    #table-container {
        width: 1fr;
        border: round $primary;
        padding: 1;
    }

    #log-container {
        width: 1fr;
        border: round $warning;
        padding: 1;
        margin-left: 1;
    }

    DataTable {
        height: 1fr;
    }

    RichLog {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("ctrl+s", "start", "Start"),
        ("ctrl+x", "stop", "Stop"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.runner: Optional[Runner] = None
        self._stats = {
            "downloaded": 0, "skipped": 0, "nopdf": 0,
            "notfound": 0, "captcha": 0, "errors": 0, "total": 0,
        }
        self._appeals_by_category: dict[str, list[dict]] = {
            "downloaded": [], "skipped": [], "nopdf": [],
            "notfound": [], "captcha": [], "errors": [],
        }

    # ------------------------- layout -------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Vertical(id="config-panel"):
            yield Label("ITAT Appeal Scraper — configuration", classes="title")

            with Horizontal(id="top-config"):
                with Vertical(id="bench-col"):
                    yield Label("Benches (space to toggle)", classes="section")
                    yield SelectionList[str](
                        *[
                            Selection(name, name, initial_state=(name == "Chandigarh"))
                            for name in sorted(BENCH_CODES)
                        ],
                        id="benches",
                    )

                with Vertical(id="fields-col"):
                    yield Label("Years and range", classes="section")
                    with Horizontal(classes="row"):
                        with Vertical(classes="field"):
                            yield Label("Appeal type", classes="field-label")
                            yield Select(
                                [(f"{k} — {v}", k) for k, v in APPEAL_TYPE_LABELS.items()],
                                value="ITA",
                                id="app_type",
                            )
                        with Vertical(classes="field"):
                            yield Label("Years (e.g. 2025 / 2020-2026 / 2020,2023)", classes="field-label")
                            yield Input(value="2020-2026", id="years")
                    with Horizontal(classes="row"):
                        with Vertical(classes="field"):
                            yield Label("Start appeal #", classes="field-label")
                            yield Input(value="1", id="start")
                        with Vertical(classes="field"):
                            yield Label("Max appeal # per year", classes="field-label")
                            yield Input(value="10000", id="end")
                        with Vertical(classes="field"):
                            yield Label("Rate limit (appeals/hr, blank = unlimited)", classes="field-label")
                            yield Input(value="", id="rate")

                    yield Label("Tuning", classes="section")
                    with Horizontal(classes="row"):
                        with Vertical(classes="field"):
                            yield Label("Stop year after N consecutive misses", classes="field-label")
                            yield Input(value="20", id="max_miss")
                        with Vertical(classes="field"):
                            yield Label("Captcha retries per appeal", classes="field-label")
                            yield Input(value="5", id="captcha_retries")
                        with Vertical(classes="field"):
                            yield Label("Pipeline retries (network errors)", classes="field-label")
                            yield Input(value="3", id="pipeline_retries")
                    with Horizontal(classes="row"):
                        with Vertical(classes="field"):
                            yield Label("Whisper model", classes="field-label")
                            yield Select(
                                WHISPER_MODEL_OPTIONS,
                                value="tiny.en",
                                id="model",
                            )
                        with Vertical(classes="field"):
                            yield Label("Device", classes="field-label")
                            yield Select(
                                DEVICE_OPTIONS,
                                value="auto",
                                id="device",
                            )

                    yield Label("Download folder", classes="section")
                    yield Label(
                        f"Current dir: {Path.cwd()}   "
                        "Enter absolute or relative path. Examples: "
                        "./downloads  ~/itat_archive  /tmp/itat. "
                        "Missing folders are created automatically.",
                        classes="field-label",
                    )
                    with Horizontal(classes="row"):
                        yield Input(
                            value="./downloads",
                            placeholder="./downloads or ~/itat or /tmp/itat",
                            id="out",
                            classes="field",
                        )

            with Horizontal(id="controls"):
                yield Button("Start", id="start-btn", variant="success")
                yield Button("Stop", id="stop-btn", variant="error", disabled=True)

        yield Static("Status: idle", id="status-line")

        with Horizontal(id="stats-panel"):
            yield Button("Downloaded: 0", id="stat-downloaded", variant="success")
            yield Button("Skipped: 0", id="stat-skipped", variant="default")
            yield Button("No PDF: 0", id="stat-nopdf", variant="warning")
            yield Button("Not found: 0", id="stat-notfound", variant="default")
            yield Button("Captcha fail: 0", id="stat-captcha", variant="warning")
            yield Button("Errors: 0", id="stat-errors", variant="error")
            yield Button("Total: 0", id="stat-total", variant="primary")
            yield Button("Show All", id="stat-all", variant="default")

        with Horizontal(id="split"):
            with Vertical(id="table-container"):
                yield Label("Results (latest first)", classes="title")
                yield DataTable(id="results-table", zebra_stripes=True)
            with Vertical(id="log-container"):
                yield Label("Log", classes="title")
                yield RichLog(id="log", wrap=True, highlight=True, markup=True)

        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#results-table", DataTable)
        table.add_columns("Bench", "Year", "Appeal", "Status", "Parties", "Attempts", "Note")
        self.query_one("#log", RichLog).write(
            "[bold]Welcome.[/bold] Pick benches and years, then press [cyan]Start[/cyan] (Ctrl+S)."
        )

    # ------------------------- actions -------------------------

    def action_start(self) -> None:
        self._start_run()

    def action_stop(self) -> None:
        self._stop_run()

    _STAT_BUTTON_MAP = {
        "stat-downloaded": ("downloaded", "Downloaded"),
        "stat-skipped": ("skipped", "Skipped"),
        "stat-nopdf": ("nopdf", "No PDF"),
        "stat-notfound": ("notfound", "Not Found"),
        "stat-captcha": ("captcha", "Captcha Failed"),
        "stat-errors": ("errors", "Errors"),
    }

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            self._start_run()
        elif event.button.id == "stop-btn":
            self._stop_run()
        elif event.button.id == "stat-total":
            self._show_all_results()
        elif event.button.id == "stat-all":
            self._show_all_results()
        elif event.button.id in self._STAT_BUTTON_MAP:
            cat, title = self._STAT_BUTTON_MAP[event.button.id]
            self._show_category(cat, title)

    def _read_config(self) -> RunConfig:
        benches = list(self.query_one("#benches", SelectionList).selected)
        if not benches:
            raise ValueError("pick at least one bench")
        app_type = self.query_one("#app_type", Select).value
        years = parse_years(self.query_one("#years", Input).value or "2025")
        start = int(self.query_one("#start", Input).value or "1")
        end = int(self.query_one("#end", Input).value or "10000")
        rate_raw = self.query_one("#rate", Input).value.strip()
        rate = int(rate_raw) if rate_raw else None

        # Resolve download path relative to current working directory and
        # create it up front so the user sees confirmation instead of a
        # surprise path error later.
        raw_out = self.query_one("#out", Input).value or "./downloads"
        out_dir = Path(raw_out).expanduser()
        if not out_dir.is_absolute():
            out_dir = (Path.cwd() / out_dir).resolve()
        else:
            out_dir = out_dir.resolve()
        created = not out_dir.exists()
        out_dir.mkdir(parents=True, exist_ok=True)
        if created:
            self._log(f"[dim]created folder:[/dim] {out_dir}")
        else:
            self._log(f"[dim]using folder:[/dim] {out_dir}")

        model = self.query_one("#model", Select).value
        device = self.query_one("#device", Select).value
        max_miss = int(self.query_one("#max_miss", Input).value or "20")
        captcha_retries = int(self.query_one("#captcha_retries", Input).value or "5")
        pipeline_retries = int(self.query_one("#pipeline_retries", Input).value or "3")

        cfg = RunConfig(
            benches=benches,
            app_type=app_type,
            years=years,
            start_number=start,
            max_number=end,
            rate_per_hour=rate,
            out_dir=out_dir,
            model_size=model,
            device=device,
            max_consecutive_missing=max_miss,
            captcha_retries=captcha_retries,
            pipeline_retries=pipeline_retries,
        )
        cfg.validate()
        return cfg

    def _start_run(self) -> None:
        if self.runner is not None:
            return
        try:
            cfg = self._read_config()
        except Exception as e:
            self._log(f"[red]config error:[/red] {e}")
            return

        self.query_one("#start-btn", Button).disabled = True
        self.query_one("#stop-btn", Button).disabled = False
        self._clear_table()
        self._reset_stats()
        self._log(
            f"[cyan]Starting run[/cyan]  benches={cfg.benches}  years={cfg.years}  "
            f"range={cfg.start_number}..{cfg.max_number}  rate={cfg.rate_per_hour or 'unlimited'}/hr"
        )
        self._log(f"[dim]root:[/dim] {cfg.out_dir}")

        # Distributed mode: pick up S3 + DB if env vars are present
        s3_uploader = None
        db_reporter = None
        try:
            from itat_scraper.storage import create_uploader
            s3_uploader = create_uploader()
        except ImportError:
            pass
        try:
            from itat_scraper.reporter import create_reporter
            db_reporter = create_reporter()
        except ImportError:
            pass

        self.runner = Runner(
            cfg,
            on_event=self._on_runner_event,
            s3_uploader=s3_uploader,
            db_reporter=db_reporter,
        )
        self._run_in_background()

    def _stop_run(self) -> None:
        if self.runner is not None:
            self.runner.stop()
            self._log("[yellow]Stop requested…[/yellow]")

    @work(thread=True, exclusive=True)
    def _run_in_background(self) -> None:
        try:
            self.runner.run()
        except Exception as e:
            self.call_from_thread(
                self._log, f"[red]runner error:[/red] {type(e).__name__}: {e}"
            )
        finally:
            self.call_from_thread(self._finish)

    def _finish(self) -> None:
        self.runner = None
        self.query_one("#start-btn", Button).disabled = False
        self.query_one("#stop-btn", Button).disabled = True
        self._status("idle")

    # ------------------------- event handling -------------------------

    def _on_runner_event(self, kind: str, payload: dict) -> None:
        self.call_from_thread(self._handle_event, kind, payload)

    def _handle_event(self, kind: str, payload: dict) -> None:
        if kind == "model_loading":
            self._status(f"loading whisper ({payload['size']}) on {payload['device']}…")
            self._log(
                f"[blue]whisper[/blue] loading {payload['size']} on "
                f"[bold]{payload['device']}[/bold]"
            )
        elif kind == "model_warning":
            self._log(f"[red]whisper WARNING:[/red] {payload['warning']}")
        elif kind == "model_ready":
            self._log(
                f"[blue]whisper[/blue] ready on [bold]{payload['device']}[/bold]"
            )
        elif kind == "run_start":
            self._status(f"running benches={payload['benches']}")
            self._log(f"[cyan]root output:[/cyan] {payload['out']}")
        elif kind == "bench_start":
            self._log(
                f"\n[bold magenta]— bench {payload['index'] + 1}/{payload['total']}: "
                f"{payload['bench']} —[/bold magenta]"
            )
        elif kind == "bench_end":
            self._log(f"[magenta]bench done:[/magenta] {payload['bench']}")
        elif kind == "year_start":
            self._log(
                f"[bold green]— {payload['bench']} / {payload['year']} —[/bold green] "
                f"start={payload['start']}"
            )
            self._log(f"[dim]folder:[/dim] {payload['folder']}")
        elif kind == "year_end":
            self._log(
                f"[green]{payload['bench']} / {payload['year']} done:[/green] "
                f"last=#{payload['last_number']}  ({payload['reason']})"
            )
        elif kind == "appeal_start":
            self._status(
                f"processing {payload['bench']} / {payload['year']} / #{payload['number']}"
                f"  — solving captcha…"
            )
        elif kind == "captcha_attempt":
            self._status(
                f"processing {payload['bench']} / {payload['year']} / #{payload['number']}"
                f"  — captcha attempt {payload['attempt']}: {payload['guess']}"
            )
            self._log(
                f"  #{payload['number']} captcha try {payload['attempt']}: "
                f"[dim]{payload['guess']}[/dim]"
            )
        elif kind == "stage":
            self._status(
                f"processing {payload['bench']} / {payload['year']} / #{payload['number']}"
                f"  — {payload['stage']}"
            )
        elif kind == "retry":
            self._log(
                f"  [yellow]retry[/yellow] {payload['bench']}/{payload['year']}/"
                f"#{payload['number']} attempt {payload['attempt']}: {payload['reason']}"
            )
        elif kind == "appeal_done":
            r = payload["result"]
            self._add_result_row(r)
            self._bump_stats(r)
            tag, color = self._classify_tag(r)
            # Update status line with running totals
            s = self._stats
            self._status(
                f"processed #{r['appeal_number']}  "
                f"OK:{s['downloaded']} SKIP:{s['skipped']} MISS:{s['notfound']} "
                f"NO-PDF:{s['nopdf']} CAPTCHA:{s['captcha']} ERR:{s['errors']}"
            )
            # Log detail for every non-skip result
            if tag == "OK":
                self._log(
                    f"  [green]OK[/green] #{r['appeal_number']}  "
                    f"{(r.get('parties') or '')[:70]}"
                )
            elif tag == "SKIP":
                pass  # don't clutter the log with skips
            elif tag == "NO-PDF":
                self._log(
                    f"  [yellow]NO-PDF[/yellow] #{r['appeal_number']}  "
                    f"{(r.get('parties') or '')[:50]}  — {r['note']}"
                )
            elif tag == "MISS":
                pass  # consecutive misses are expected, keep log clean
            else:
                self._log(
                    f"  [red]{tag}[/red] #{r['appeal_number']}  — {r['note']}"
                )
        elif kind == "cleanup":
            self._log(
                f"[blue]cleanup:[/blue] removed {payload['removed_mp3s']} "
                f"captcha mp3 file(s)"
            )
        elif kind == "run_end":
            s = payload["summary"]
            self._log(
                f"[bold]SUMMARY[/bold]  downloaded={s['downloaded']}  "
                f"no-pdf={s['missing_pdf']}  not-found={s['not_found']}  "
                f"errors={s['errors']}  total={s['total_processed']}"
            )

    # ------------------------- UI helpers -------------------------

    def _log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def _status(self, msg: str) -> None:
        self.query_one("#status-line", Static).update(f"Status: {msg}")

    def _clear_table(self) -> None:
        self.query_one("#results-table", DataTable).clear()

    def _add_result_row(self, r: dict) -> None:
        tag, color = self._classify_tag(r)
        status = f"[{color}]{tag}[/{color}]"
        parties = (r.get("parties") or "")[:60]
        note = r["note"][:60]
        self.query_one("#results-table", DataTable).add_row(
            r["bench"],
            str(r["year"]),
            str(r["appeal_number"]),
            status,
            parties,
            str(r["attempts"]),
            note,
        )

    def _classify_tag(self, r: dict) -> tuple[str, str]:
        """Return (tag, color) for a result dict."""
        note = r.get("note", "")
        if note == "skipped (existing)":
            return "SKIP", "cyan"
        if r["saved_files"]:
            return "OK", "green"
        if r["found"]:
            return "NO-PDF", "yellow"
        if note == "no records":
            return "MISS", "dim"
        if "captcha failed" in note:
            return "CAPTCHA", "magenta"
        return "ERR", "red"

    def _reset_stats(self) -> None:
        self._stats = {
            "downloaded": 0, "skipped": 0, "nopdf": 0,
            "notfound": 0, "captcha": 0, "errors": 0, "total": 0,
        }
        # Track appeal numbers per category for drill-down
        self._appeals_by_category: dict[str, list[dict]] = {
            "downloaded": [], "skipped": [], "nopdf": [],
            "notfound": [], "captcha": [], "errors": [],
        }
        self._refresh_stats()

    def _bump_stats(self, r: dict) -> None:
        if not hasattr(self, "_stats"):
            self._reset_stats()
        self._stats["total"] += 1
        tag, _ = self._classify_tag(r)
        entry = {
            "number": r["appeal_number"],
            "bench": r["bench"],
            "year": r["year"],
            "note": r.get("note", ""),
            "parties": (r.get("parties") or "")[:80],
        }
        if tag == "OK":
            self._stats["downloaded"] += 1
            self._appeals_by_category["downloaded"].append(entry)
        elif tag == "SKIP":
            self._stats["skipped"] += 1
            self._appeals_by_category["skipped"].append(entry)
        elif tag == "NO-PDF":
            self._stats["nopdf"] += 1
            self._appeals_by_category["nopdf"].append(entry)
        elif tag == "CAPTCHA":
            self._stats["captcha"] += 1
            self._appeals_by_category["captcha"].append(entry)
        elif tag == "MISS":
            self._stats["notfound"] += 1
            self._appeals_by_category["notfound"].append(entry)
        else:
            self._stats["errors"] += 1
            self._appeals_by_category["errors"].append(entry)
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        self.query_one("#stat-downloaded", Button).label = f"Downloaded: {self._stats['downloaded']}"
        self.query_one("#stat-skipped", Button).label = f"Skipped: {self._stats['skipped']}"
        self.query_one("#stat-nopdf", Button).label = f"No PDF: {self._stats['nopdf']}"
        self.query_one("#stat-notfound", Button).label = f"Not found: {self._stats['notfound']}"
        self.query_one("#stat-captcha", Button).label = f"Captcha fail: {self._stats['captcha']}"
        self.query_one("#stat-errors", Button).label = f"Errors: {self._stats['errors']}"
        self.query_one("#stat-total", Button).label = f"Total: {self._stats['total']}"

    def _show_category(self, category: str, title: str) -> None:
        """Filter the results table to show only appeals in the given category."""
        if not hasattr(self, "_appeals_by_category"):
            return
        table = self.query_one("#results-table", DataTable)
        table.clear()
        entries = self._appeals_by_category.get(category, [])
        if not entries:
            self._log(f"[dim]No {title.lower()} appeals to show.[/dim]")
            return
        for e in entries:
            tag_map = {
                "downloaded": ("[green]OK[/green]"),
                "skipped": ("[cyan]SKIP[/cyan]"),
                "nopdf": ("[yellow]NO PDF[/yellow]"),
                "notfound": ("[dim]MISS[/dim]"),
                "captcha": ("[magenta]CAPTCHA[/magenta]"),
                "errors": ("[red]ERR[/red]"),
            }
            table.add_row(
                e["bench"],
                str(e["year"]),
                str(e["number"]),
                tag_map.get(category, "?"),
                e["parties"][:60],
                "",
                e["note"][:60],
            )
        self._log(f"[bold]Showing {len(entries)} {title.lower()} appeal(s)[/bold]")

    def _show_all_results(self) -> None:
        """Repopulate the results table with all appeals."""
        if not hasattr(self, "_appeals_by_category"):
            return
        table = self.query_one("#results-table", DataTable)
        table.clear()
        tag_map = {
            "downloaded": "[green]OK[/green]",
            "skipped": "[cyan]SKIP[/cyan]",
            "nopdf": "[yellow]NO PDF[/yellow]",
            "notfound": "[dim]MISS[/dim]",
            "captcha": "[magenta]CAPTCHA[/magenta]",
            "errors": "[red]ERR[/red]",
        }
        for category in ("downloaded", "skipped", "nopdf", "notfound", "captcha", "errors"):
            for e in self._appeals_by_category.get(category, []):
                table.add_row(
                    e["bench"],
                    str(e["year"]),
                    str(e["number"]),
                    tag_map.get(category, "?"),
                    e["parties"][:60],
                    "",
                    e["note"][:60],
                )


def main() -> None:
    ItatTui().run()


if __name__ == "__main__":
    main()
