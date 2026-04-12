# Architecture

How the scraper is structured, how modules talk to each other, and the
non-obvious bits about the ITAT website you need to know to read the code.

## Module map

```
main.py   ──┐
            ├──► itat_scraper.Runner ──┬──► captcha.py  (audio + whisper)
tui.py    ──┘                          ├──► scraper.py  (http + html + pdf)
                                       ├──► ratelimit.py
                                       ├──► models.py   (dataclasses)
                                       └──► constants.py (codes, urls)
```

`main.py` and `tui.py` are two alternative front-ends over the same `Runner`
class. Neither of them knows anything about HTTP, Whisper, or the ITAT
HTML — they just construct a `RunConfig`, create a `Runner`, and subscribe
to its event stream.

| File | Responsibility | Depends on |
| --- | --- | --- |
| `itat_scraper/constants.py` | URLs, user-agent, timeouts, `BENCH_CODES`, `APPEAL_TYPE_LABELS` | (none) |
| `itat_scraper/models.py` | `CaseResult`, `RunSummary` dataclasses | (none) |
| `itat_scraper/ratelimit.py` | `RateLimiter` (sliding 60s window) | stdlib |
| `itat_scraper/captcha.py` | `load_whisper_model` (GPU detect + warmup + CPU fallback), `solve_captcha`, `verify_captcha`, `normalize_transcription`, `_preload_nvidia_wheels` | `faster_whisper`, `ctranslate2`, `requests`, `constants` |
| `itat_scraper/scraper.py` | `new_session`, `fetch_csrftkn`, `submit_search`, HTML extractors, `download_pdf` | `requests`, `bs4`, `constants` |
| `itat_scraper/runner.py` | `Runner`, `RunConfig`, `parse_years`, `parse_benches`, `EventCallback` | all of the above + `models` |
| `main.py` | CLI: argparse → `RunConfig` → `Runner.run`; `_on_event` prints progress | `itat_scraper` |
| `tui.py` | Textual `App` with inputs/table/log; runs `Runner` in worker thread | `textual`, `itat_scraper` |

## RunConfig

A single `RunConfig` drives the whole run. The important fields:

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `benches` | `list[str]` | `["Chandigarh"]` | Order preserved. Each bench is processed in turn. |
| `app_type` | `str` | `"ITA"` | Appeal type code, e.g. `ITA`, `CO`, `SA`. |
| `years` | `list[int]` | `[2025]` | Processed in order for every bench. |
| `start_number` | `int` | `1` | Applies **only** to the first `(bench, year)` pair processed. |
| `max_number` | `int` | `10_000` | Upper bound on appeal numbers scanned per year. |
| `max_consecutive_missing` | `int` | `20` | Stop a year after N consecutive "no records" responses. |
| `captcha_retries` | `int` | `5` | Per-appeal captcha attempts before giving up. |
| `pipeline_retries` | `int` | `3` | Per-appeal outer retries on network errors. |
| `rate_per_minute` | `int?` | `None` | Global cap on completed appeals per minute. |
| `model_size` | `str` | `"tiny.en"` | faster-whisper model size. Seven options from `tiny.en` (39 MB) through `large-v3` (1.5 GB). |
| `device` | `str` | `"auto"` | `auto` (detect GPU, fall back to CPU), `cuda`, or `cpu`. |
| `compute_type` | `str` | `"auto"` | `auto` picks `float16` on cuda, `int8` on cpu. Override with any CTranslate2 compute type. |
| `out_dir` | `Path` | `Path(".")` | Root download directory. Created automatically if missing. |
| `polite_delay_s` | `float` | `0.8` | Sleep between appeals. |

## Output layout

The runner writes directly into `out_dir` using a two-level nested layout:

```
<out_dir>/
├── Chandigarh/
│   ├── 2020/
│   │   ├── Chandigarh_ITA_1_2020_order1.pdf
│   │   ├── Chandigarh_ITA_2_2020_order1.pdf
│   │   ├── manifest.jsonl
│   │   └── missing_pdfs.csv
│   └── 2025/…
├── Delhi/
│   └── 2024/…
└── .itat_tmp/        (only exists during a run)
```

Every `<bench>/<year>/` leaf gets its own `manifest.jsonl` (every
`CaseResult` for that slice) and `missing_pdfs.csv` (cases with no PDF or
hard failures). Manifests are rewritten after every single appeal, so
partial runs are always safe to resume.

`.itat_tmp/` is a single temp directory shared across the whole run,
created at the start and removed on exit. Every captcha MP3 is unlinked
in `captcha.solve_captcha()`'s `finally` block immediately after it's
transcribed, and `Runner.run()` has an outer `try/finally` that always
calls `_cleanup_tmp()` — so no stragglers survive a crash.

## Data flow: one appeal, start to finish

```
Runner._process_one(bench, bench_code, year, number, leaf)
  │
  ├─► scraper.new_session()                        fresh requests.Session
  ├─► scraper.fetch_csrftkn(session)               GET /judicial/casestatus → parse #csrftkn1
  │
  ├─► loop (captcha_retries times):
  │     captcha.solve_captcha(session, model, tmp)
  │       ├─► GET /captcha/show                    ***seeds the captcha state***
  │       ├─► GET /captcha/listen/                 MP3 bytes
  │       ├─► WhisperModel.transcribe(mp3)         "r, y, 5, b, 4, c."
  │       ├─► normalize_transcription()            "RY5B4C"
  │       └─► unlink(mp3)                          per-captcha cleanup
  │     captcha.verify_captcha(session, csrf, guess)
  │       └─► POST /Ajax/checkCaptcha               {rslt: "true"} or fail
  │     if not verified: new session, retry
  │
  ├─► scraper.submit_search(session, csrf, captcha, bench_code, ...)
  │     POST /judicial/casestatus
  │     body: hp, csrftkn, c1=captcha, bench_name_1, app_type_1, app_number, app_year_1, bt1=true
  │
  ├─► scraper.no_records(results_html) ──► CaseResult(found=False, note="no records")
  ├─► scraper.extract_casedetails_links(results_html)
  │     grabs the "More Details" href → /judicial/casedetails?cid=<opaque>
  │
  ├─► session.get(casedetails_url) → details_html
  ├─► scraper.extract_case_info(details_html)      title, parties, status, AY, filed_on, bench
  ├─► scraper.extract_pdf_links(details_html)      /public/files/upload/<ts>-<tok>-<num>-TO.pdf
  │
  └─► for each pdf_url:
        scraper.download_pdf(session, url, leaf / filename)
```

## The outer loop

```
Runner.run()
  │
  ├─► _load_model()                                 lazy faster-whisper load
  ├─► _ensure_tmp_dir()                             creates <out>/.itat_tmp
  ├─► emit run_start
  │
  └─► for bench_idx, bench in enumerate(config.benches):
        ├─► emit bench_start
        └─► for year in config.years:
              ├─► compute leaf = out_dir / bench / year
              ├─► start = start_number if (first bench and first year) else 1
              ├─► emit year_start
              ├─► _process_year(...)                    ← walks 1..max_number
              │     ├─► rate_limiter.wait()             honors --rate globally
              │     ├─► _process_with_retries(...)      pipeline retries
              │     │     └─► _process_one(...)         captcha + submit + parse + download
              │     ├─► rate_limiter.record()
              │     ├─► append to _leaf_results[(bench, year)]
              │     ├─► _write_manifest(bench, year)    per-leaf manifest/csv
              │     └─► early-stop on N consecutive "no records"
              └─► emit year_end
        └─► emit bench_end
  │
  └─► finally: _cleanup_tmp() + emit cleanup + emit run_end
```

`start_number` only applies to the very first `(bench, year)` pair that
the runner processes. Every subsequent pair resets to `1`. This is the
"resume from mid-year" semantics — you can pick up where you left off on
one year without skipping the first appeal of every subsequent year.

## Two-step form submission (non-obvious)

ITAT's form uses JavaScript that pre-validates the captcha via an AJAX
endpoint **before** it submits the real form. We replay both steps:

1. `POST /Ajax/checkCaptcha` with body `captcha=<guess>` and header
   `X-CSRF-TOKEN: <csrftkn1 value>`. Returns JSON `{"rslt": "true"}` when
   the captcha is correct.
2. `POST /judicial/casestatus` with the full form payload (including
   `c1=<captcha>` and `bt1=true`, which is what the JS sets after step 1
   succeeds).

The response to step 2 is a **search-results page**, not the case details
page. It contains a summary table with a "More Details" link that points
to `/judicial/casedetails?cid=<encoded>`. We follow that link to get the
PDF download URL. The `cid` is opaque/encrypted, so we cannot construct it
directly — every appeal has to go through this two-hop flow.

## The captcha state quirk

`GET /captcha/listen/` alone returns an empty body. The server only
generates captcha state when `GET /captcha/show` is called first. Our
`solve_captcha()` always calls `/captcha/show` before `/captcha/listen/`
to force the state, even though we discard the image bytes — we only care
about the audio.

## Whisper model loading, GPU detection, and fallback

`captcha.load_whisper_model(size, device, compute_type)` is the single
entry point and does four things in sequence:

1. **Preload NVIDIA wheels via `ctypes`.** If `nvidia-cublas-cu12` and
   `nvidia-cudnn-cu12` are installed (via `uv sync --group gpu`), their
   `.so` files live inside `site-packages/nvidia/*/lib/`, which is not
   on `LD_LIBRARY_PATH`. `_preload_nvidia_wheels()` walks the namespace
   package, appends each lib directory to `LD_LIBRARY_PATH` for child
   processes, and `ctypes.CDLL(..., RTLD_GLOBAL)`s the libraries into
   the current process so CTranslate2 finds them when it initialises
   its CUDA backend.
2. **Detect CUDA.** `ctranslate2.get_cuda_device_count() > 0` decides
   whether a GPU is usable. If `device="auto"` and this returns True,
   we target cuda; otherwise cpu.
3. **Warmup inference.** `_try_load_and_warmup()` constructs the
   `WhisperModel` AND runs a dummy transcription on a silent WAV. This
   is critical: construction succeeds even when `libcublas.so.12` is
   missing, but inference blows up at runtime. Running a warmup
   surfaces every possible failure at load time.
4. **Fallback.** If the warmup raises *anything* on cuda (missing lib,
   cuDNN mismatch, out-of-memory, driver skew), the function catches it,
   emits a warning string, and tries again on `cpu` with `int8`. The
   caller receives `(model, actual_device, warning)`.

The `Runner` surfaces `warning` as a `model_warning` event so the TUI
and CLI can display it without having to know anything about CUDA.

## Retry layers

There are two concentric retry loops, each with a different purpose:

| Layer | Where | Reason | Default |
| --- | --- | --- | --- |
| **Captcha retries** | Inside `_process_one` | Whisper occasionally returns a guess that fails `/Ajax/checkCaptcha` | 5 |
| **Pipeline retries** | `_process_with_retries` wraps `_process_one` | Network timeouts, 5xx, unexpected exceptions | 3 |

On captcha failure the session is discarded and a new one is created
because `csrftkn1` and the captcha state are bound to the original session.
Pipeline retries back off linearly (`sleep(2 * attempt)`).

## Event stream (how the TUI/CLI see progress)

`Runner.__init__` takes an `on_event(kind: str, payload: dict)` callback.
The runner emits events during the run; the CLI and TUI each subscribe
and render them their own way.

| Event | When | Key payload fields |
| --- | --- | --- |
| `model_loading` | before Whisper model is loaded | `size, device` |
| `model_warning` | GPU load failed, falling back to CPU | `warning` |
| `model_ready` | after Whisper is loaded (and warmed up) | `size, device` (actual, post-fallback) |
| `run_start` | run begins | `benches, app_type, years, start, end, out, rate` |
| `bench_start` | new bench in outer loop | `bench, index, total` |
| `bench_end` | bench finished | `bench` |
| `year_start` | new year for the current bench | `bench, year, start, folder` |
| `year_end` | year finished or stopped | `bench, year, reason, last_number` |
| `appeal_start` | starting a single appeal | `bench, year, number` |
| `captcha_attempt` | after each whisper transcription try | `bench, year, number, attempt, guess` |
| `retry` | pipeline-level retry triggered | `bench, year, number, attempt, reason` |
| `appeal_done` | `CaseResult` is ready | `result` (asdict) |
| `cleanup` | temp dir swept on exit | `removed_mp3s, tmp_dir` |
| `run_end` | run finished | `summary` (RunSummary asdict) |

In `tui.py` the callback goes through `app.call_from_thread(...)` because
the runner executes inside `@work(thread=True)` — the TUI widgets can
only be updated from the main thread.

## Multi-year iteration

Inside each bench, `Runner` iterates over `config.years` in order. Inside
each year it iterates appeal numbers from `start` to `max_number`
(`start` is `config.start_number` for the first pair, `1` otherwise).
When the server reports "no records" for `max_consecutive_missing`
appeals in a row (default 20), the runner concludes the year is exhausted,
emits `year_end`, and moves to the next year. Any non-"no records" result
resets the counter, so a single gap inside a year doesn't prematurely end
it.

## Rate limiting

`RateLimiter` (in `ratelimit.py`) is a sliding window over the last 60
seconds of completed appeals. The limiter is **shared across all
(bench, year, number) tuples** — `--rate 30` means at most 30 total
appeals per minute across the entire run, not 30 per year or 30 per
bench. Each iteration of the runner loop calls `wait()` before starting
an appeal and `record()` after it finishes. If `rate_per_minute` is
`None` or `0`, the limiter is a no-op.

## Where to change things

| I want to… | Edit |
| --- | --- |
| Add/rename a bench or appeal type | `constants.py` |
| Change HTTP timeouts | `constants.py` (`HTTP_TIMEOUT`, `PDF_TIMEOUT`) |
| Tune the captcha normaliser | `captcha.normalize_transcription` |
| Parse a new field from the details page | `scraper.extract_case_info` regex list |
| Change the PDF filename pattern | `Runner._process_one` (the `filename = f"..."` line) |
| Change the leaf-folder layout | `Runner._folder_for` |
| Add a new event type | emit from `Runner`, handle in both `main._on_event` and `tui._handle_event` |
| Swap Whisper for something else | `captcha.load_whisper_model` and `solve_captcha` |
