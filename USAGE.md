# Usage Guide

Download ITAT Final Tribunal Order PDFs in bulk. The scraper solves the
audio captcha with a tiny Whisper model, replays the search form, follows
the case-details link, and saves PDFs to disk.

## Setup

Dependencies are managed with `uv`. Two install flavours:

```sh
# CPU-only install. Smallest venv. Works on any Linux/macOS host.
uv sync

# GPU install. Adds ~1 GB of NVIDIA wheels (cuBLAS, cuDNN, nvrtc) to the
# venv so faster-whisper can run on CUDA without any system CUDA install.
uv sync --group gpu
```

Both installs include `faster-whisper`, `requests`, `beautifulsoup4`,
`lxml`, and `textual`. The Whisper model itself (`tiny.en`, ~39 MB)
downloads on first use.

Which one should I run?

- **Local laptops, shared servers, hosts without an NVIDIA card:** use
  `uv sync`. Leave `--device` at its default of `auto`; it will detect
  no GPU and run on CPU. For this captcha that is plenty fast.
- **GPU boxes where you want cuda to actually be used:** use
  `uv sync --group gpu`. `--device auto` will then find the preloaded
  libraries and run on the GPU. If anything fails at load time (wrong
  driver, out-of-memory, missing cuDNN version), the scraper logs a
  warning and falls back to CPU automatically.
- **Unsure?** `uv sync --group gpu` is safe — it just costs you ~1 GB of
  extra venv space. On a CPU-only host the GPU libraries are harmless
  and the auto-detect still lands on CPU.

You also need `ffmpeg` on your `PATH` — faster-whisper shells out to it
to decode the captcha MP3. Every mainstream Linux distro has it
(`pacman -S ffmpeg`, `apt install ffmpeg`, `brew install ffmpeg`, etc).

### GPU notes

The `gpu` dependency group installs the CUDA 12 runtime as self-contained
Python wheels into the venv. Nothing gets installed system-wide, no root
required. `itat_scraper/captcha.py` preloads the `.so` files via
`ctypes` at startup so CTranslate2 finds them without needing
`LD_LIBRARY_PATH` set manually.

The host still needs a recent NVIDIA driver compatible with CUDA 12.
Check with `nvidia-smi`; anything on driver `535.x` or newer will work.
The venv does not (and cannot) bundle the driver itself — that's a
host-level concern.

## Two ways to run

- **CLI** (`main.py`) — scripting, cron jobs, one-shot runs.
- **TUI** (`tui.py`) — interactive dashboard, good for exploring and
  watching progress live with multi-bench selection.

Both do the exact same work under the hood; pick whichever feels better.

---

## CLI

Minimal example — download Chandigarh income-tax appeals 1 through 100
for the year 2025:

```sh
uv run python main.py --benches Chandigarh --years 2025 --from 1 --to 100
```

Full Chandigarh archive from 2020 through 2026, throttled to 30 per minute:

```sh
uv run python main.py --benches Chandigarh --years 2020-2026 --rate 30
```

Multi-bench sweep — Chandigarh, Delhi, and Mumbai all years:

```sh
uv run python main.py --benches Chandigarh,Delhi,Mumbai --years 2020-2026 --rate 30
```

Resume a specific bench/year from mid-way (e.g. Delhi 2024 died at appeal 847):

```sh
uv run python main.py --benches Delhi --years 2024 --from 847
```

### All flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--benches LIST` | `Chandigarh` | Comma-separated list of benches. Valid names: Agra, Ahmedabad, Allahabad, Amritsar, Bangalore, Chandigarh, Chennai, Cochin, Cuttack, Dehradun, Delhi, Guwahati, Hyderabad, Indore, Jabalpur, Jaipur, Jodhpur, Kolkata, Lucknow, Mumbai, Nagpur, Panaji, Patna, Pune, Raipur, Rajkot, Ranchi, Surat, Varanasi, Visakhapatnam. Processed in the order you list them. |
| `--bench NAME` | — | Alias for `--benches` accepting a single bench. Kept for backward compatibility. |
| `--type CODE` | `ITA` | Appeal type code. Common: `ITA` (Income Tax Appeal), `CO` (Cross Objection), `SA` (Stay Application), `MA` (Miscellaneous Application), `WTA` (Wealth Tax). See `itat_scraper/constants.py` for the full list. |
| `--years SPEC` | `2025` | Single year (`2025`), inclusive range (`2020-2026`), or comma list (`2020,2023,2025`). |
| `--from N` | `1` | Start from this appeal number. **Only applies to the very first (bench, year) pair processed.** Every subsequent bench/year combination resets to 1. |
| `--to N` | `10000` | Upper bound on appeal numbers scanned in each year. The actual stopping point may be earlier — see `--max-consecutive-missing`. |
| `--rate N` | unlimited | Global throttle: at most `N` appeals per minute, measured as a sliding 60-second window across the entire run (all benches and all years share the same limiter). |
| `--out PATH` | `.` (cwd) | Root download directory. Files land under `<out>/<Bench>/<year>/`. |
| `--model SIZE` | `tiny.en` | faster-whisper model size. Choices: `tiny.en` (39 MB, fastest), `base.en` (74 MB), `small.en` (244 MB), `medium.en` (769 MB), `distil-large-v3` (756 MB), `large-v3-turbo` (809 MB), `large-v3` (1.5 GB, best). For this captcha `tiny.en` is already perfect; bigger is only interesting if you're running multiple workers in parallel. |
| `--device D` | `auto` | `auto` (detect GPU and fall back to CPU), `cuda` (force GPU, error if unavailable), or `cpu` (force CPU). |
| `--compute-type T` | `auto` | CTranslate2 compute type. `auto` picks `float16` for cuda and `int8` for cpu. Override with `float16`, `int8`, `int8_float16`, or `float32` if you know what you want. |
| `--captcha-retries N` | `5` | Number of captcha re-fetch-and-retranscribe attempts before giving up on an appeal. |
| `--pipeline-retries N` | `3` | Outer retries on network errors, timeouts, and unexpected exceptions. |
| `--max-consecutive-missing N` | `20` | Stop a year early after `N` consecutive "no records" responses. This is the auto-detection for "appeals for this year have ended". Set very high to disable. |

### Reading the output

A run produces a tree rooted at `--out`:

```
<out>/
├── Chandigarh/
│   ├── 2020/
│   │   ├── Chandigarh_ITA_1_2020_order1.pdf
│   │   ├── Chandigarh_ITA_2_2020_order1.pdf
│   │   ├── …
│   │   ├── manifest.jsonl
│   │   └── missing_pdfs.csv
│   ├── 2021/
│   │   └── …
│   └── 2025/
│       └── …
└── Delhi/
    └── 2025/
        └── …
```

Every `<Bench>/<year>/` leaf is self-contained. `manifest.jsonl` has one
JSON object per appeal processed (downloaded or not), with fields like
`appeal_number`, `bench`, `parties`, `status`, `filed_on`, `assessment_year`,
`pdf_urls`, `saved_files`, `attempts`, and `note`. `missing_pdfs.csv` is
the narrower CSV listing only appeals where no PDF was saved, so you can
see at a glance which cases need a follow-up.

Manifests are rewritten after every single appeal, so partial runs are
never lost. If a run dies in the middle of `Chandigarh/2023/`, the
manifest and PDFs up to that point are already on disk.

### Resuming after a failure

Because each leaf is self-contained, resuming is just another narrow
invocation. If Delhi 2024 died around appeal 847, rerun:

```sh
uv run python main.py --benches Delhi --years 2024 --from 847
```

The existing PDFs and manifest under `Delhi/2024/` get overwritten by the
new run (which restarts at 847), so the final state is as if the first
run never stopped.

---

## TUI

Launch with:

```sh
uv run python tui.py
```

You get an interactive terminal dashboard. Left side is a checkbox list
of benches (use space to toggle, pick as many as you want). Right side is
the rest of the configuration — appeal type, years, appeal number range,
rate limit, retry tuning, Whisper model, and the download folder.

```
┌─ Configuration ────────────────────────────────────────────────┐
│ Benches (space to toggle)   │  Years and range                  │
│ [x] Chandigarh              │  [ ITA ▾ ] [ 2020-2026         ]  │
│ [ ] Delhi                   │  [ 1 ] [ 10000 ] [ rate/min    ]  │
│ [ ] Mumbai                  │                                   │
│ [ ] Bangalore               │  Tuning                           │
│ …                           │  [ 20 ] [ 5 ] [ 3 ] [ tiny.en ▾]  │
│                             │                                   │
│                             │  Download folder                  │
│                             │  [ /home/you/itat_archive      ]  │
│  [ Start ]   [ Stop ]                                           │
└────────────────────────────────────────────────────────────────┘
 Status: idle
┌─ Stats ────────────────────────────────────────────────────────┐
│ Downloaded 0   No PDF 0   Not found 0   Errors 0   Total 0     │
└────────────────────────────────────────────────────────────────┘
┌─ Results ──────────────────────┐ ┌─ Log ───────────────────────┐
│ Bench  Year  App  Status  …    │ │ — bench 1/1: Chandigarh —   │
│ Chdgr  2020   1   OK      …    │ │ — Chandigarh / 2020 —       │
│ Chdgr  2020   2   OK      …    │ │   saved: …                  │
│ …                              │ │ …                           │
└────────────────────────────────┘ └─────────────────────────────┘
```

### Fields

Every input has a visible label above it so you don't have to guess
what the numbers mean.

- **Benches (space to toggle)** — checklist of every tribunal bench.
  Move with `↑`/`↓`, press `space` to toggle. Pick one bench or many;
  they're processed in the order shown. Chandigarh is pre-ticked.
- **Appeal type** — drop-down of appeal type codes with labels (default:
  `ITA`).
- **Years** — single year (`2025`), inclusive range (`2020-2026`), or
  comma list (`2020,2023,2025`).
- **Start appeal #** — first appeal number, applied only to the very
  first bench+year pair. Every subsequent pair restarts at 1. Matches
  `--from` in the CLI.
- **Max appeal # per year** — upper bound on the scan within each year.
  Matches `--to`.
- **Rate limit (appeals/min)** — global sliding-window cap. Blank means
  unlimited.
- **Stop year after N consecutive misses** — auto-rollover threshold.
  Default 20.
- **Captcha retries per appeal** — how many times to re-fetch and
  re-transcribe a captcha before giving up on an appeal.
- **Pipeline retries (network errors)** — outer retries for timeouts,
  connection errors, and unexpected exceptions.
- **Whisper model** — drop-down with seven options and their sizes:
  `tiny.en` (39 MB, default and perfect for this captcha), `base.en`
  (74 MB), `small.en` (244 MB), `medium.en` (769 MB),
  `distil-large-v3` (756 MB), `large-v3-turbo` (809 MB), and
  `large-v3` (1.5 GB). For the ITAT captcha `tiny.en` has never missed
  for me — the bigger options exist in case you want to experiment.
- **Device** — drop-down: `auto` detects CUDA and falls back to CPU,
  `cuda` forces GPU (errors if unavailable), `cpu` forces CPU.
- **Download folder** — the root directory. Can be absolute
  (`/home/you/itat`), home-relative (`~/itat_archive`), or relative to
  the current working directory shown above the input (`./downloads`).
  Missing folders are created automatically when you press Start, and
  the log pane shows exactly which path was used.

### Keybindings

| Key | Action |
| --- | --- |
| `Ctrl+S` | Start the run (same as the **Start** button) |
| `Ctrl+X` | Request a graceful stop |
| `Ctrl+Q` | Quit the application |
| `space`   | Toggle the highlighted bench in the Benches list |
| `↑` / `↓` | Move through the Benches list |

The **Start** button is disabled while a run is active; **Stop** is
disabled while idle.

### Live panes

- **Status line** — current state: idle, loading whisper, or
  "processing `<bench>/<year>/#<number>`".
- **Stats row** — running counts updated after every appeal.
- **Results table** — one row per appeal showing bench, year, number,
  OK/NO-PDF/MISS/ERR status, parties, attempts taken, and the note.
- **Log pane** — a rich text log of every event, including bench/year
  transitions, captcha attempts, retries, cleanup, and the final summary.

---

## Stopping a run safely

The CLI can be interrupted with `Ctrl+C`. The TUI offers `Ctrl+X` (or the
Stop button) which signals the runner to finish the current appeal and
then exit cleanly.

In either case, the `manifest.jsonl` and `missing_pdfs.csv` files in the
current leaf are already up to date — they're written after every single
appeal, not at the end of the run — so nothing is lost. You can re-run
for the specific failed bench/year with a narrower `--from` to resume.

---

## Troubleshooting

**"No PDF saved" for an appeal that looks valid.**
The tribunal order may not be uploaded yet. Check `missing_pdfs.csv` —
these cases are found but have no download link, usually because they
are still in progress. Try again later.

**Every captcha attempt fails.**
Rare, but can happen if the tribunal site updates the audio. Try
upgrading the Whisper model with `--model base.en` or `--model small.en`.

**GPU load fails and falls back to CPU.**
You'll see a `whisper WARNING:` line explaining why — missing
`libcublas.so.12`, mismatched CUDA version, out-of-memory, etc. The run
continues on CPU. To actually use the GPU, make sure you ran
`uv sync --group gpu` (installs the CUDA 12 wheels into the venv) and
that `nvidia-smi` reports a working driver. If you want to silence the
warning on a CPU-only host, pass `--device cpu` explicitly.

**Hangs on network.**
All HTTP calls have explicit timeouts (10s connect, 60s read, 180s for
PDF downloads). A hang means you're offline or the tribunal site is down.
The pipeline-retry layer will back off and try again.

**Captcha MP3s left on disk.**
Should not happen — each MP3 is deleted right after transcription, and
the runner sweeps `<out>/.itat_tmp/` on exit. If you ever find strays,
run the scraper again on any small range and the cleanup step will clear
them.

**Rate limit hits from the tribunal site.**
Add `--rate 20` or `--rate 10` to throttle down. The limit is global
across all benches and years in the run.

**A year ended earlier than expected.**
The default `--max-consecutive-missing 20` ends a year after 20 appeals
in a row come back "no records". If a real filing year has gaps bigger
than that, bump the flag. Inspect the manifest to see whether you hit a
genuine end-of-year or a pothole.
