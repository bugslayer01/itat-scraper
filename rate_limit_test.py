"""Rate limit probe for itat.gov.in — parallel full-flow simulation.

Runs N parallel workers each doing the REAL scraper pipeline
(session → CSRF → captcha solve on GPU → verify → search → case details).
Increases worker count every 2 minutes to push throughput higher.
Uses WARP VPN to rotate IPs on blocks. Saves state between runs.

The Whisper model is loaded once and shared across all threads — CTranslate2
handles concurrent GPU inference internally. tiny.en is 39MB so 8GB VRAM
can handle many parallel transcriptions easily.

Usage:
    uv run python rate_limit_test.py           # start or resume
    uv run python rate_limit_test.py --reset   # fresh start
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import requests

from itat_scraper.captcha import load_whisper_model, solve_captcha, verify_captcha
from itat_scraper.constants import BASE, BENCH_CODES, FORM_URL, HTTP_TIMEOUT, USER_AGENT
from itat_scraper.scraper import (
    extract_casedetails_links,
    fetch_csrftkn,
    new_session,
    no_records,
    submit_search,
)

STEP_INTERVAL_S = 120     # 2 minutes per step
INITIAL_WORKERS = 1       # start with 1 parallel worker
WORKER_STEP = 2           # add 2 workers each step
BLOCK_THRESHOLD = 3       # 3 blocked responses in a window = rate limited
WINDOW_S = 60.0           # sliding window for block detection
WARP_COOLDOWN_S = 5

# Real test case
TEST_BENCH = "Chandigarh"
TEST_BENCH_CODE = BENCH_CODES[TEST_BENCH]
TEST_APP_TYPE = "ITA"
TEST_YEAR = 2024
TEST_NUMBER = 1

STATE_FILE = Path(__file__).parent / ".probe_state.json"
TMP_DIR = Path(__file__).parent / ".probe_tmp"


@dataclass
class ProbeState:
    current_workers: int = INITIAL_WORKERS
    total_appeals: int = 0
    total_ok: int = 0
    total_blocked: int = 0
    total_captcha_ok: int = 0
    total_captcha_fail: int = 0
    total_errors: int = 0
    blocked_codes: dict[str, int] = field(default_factory=dict)
    step: int = 1
    max_safe_workers: int = 0
    max_safe_throughput: float = 0.0  # measured appeals/hr at max safe
    warp_rotations: int = 0
    current_ip: str = ""
    found_limit: bool = False
    blocked_at_workers: int = 0

    def save(self) -> None:
        data = {
            "max_safe_workers": self.max_safe_workers,
            "max_safe_throughput": self.max_safe_throughput,
            "resume_workers": self.current_workers,
            "total_appeals": self.total_appeals,
            "total_ok": self.total_ok,
            "total_blocked": self.total_blocked,
            "total_captcha_ok": self.total_captcha_ok,
            "total_captcha_fail": self.total_captcha_fail,
            "total_errors": self.total_errors,
            "blocked_codes": self.blocked_codes,
            "step": self.step,
            "warp_rotations": self.warp_rotations,
            "found_limit": self.found_limit,
            "blocked_at_workers": self.blocked_at_workers,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        STATE_FILE.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls) -> ProbeState | None:
        if not STATE_FILE.exists():
            return None
        try:
            data = json.loads(STATE_FILE.read_text())
            state = cls()
            for key in (
                "max_safe_workers", "max_safe_throughput", "total_appeals",
                "total_ok", "total_blocked", "total_captcha_ok",
                "total_captcha_fail", "total_errors", "blocked_codes",
                "step", "warp_rotations", "found_limit", "blocked_at_workers",
            ):
                if key in data:
                    setattr(state, key, data[key])
            state.current_workers = data.get("resume_workers", INITIAL_WORKERS)
            return state
        except Exception:
            return None


# Thread-safe counters
_lock = threading.Lock()
_recent_blocks: deque[float] = deque()
_stop_flag = threading.Event()


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def get_public_ip() -> str:
    try:
        return requests.get("https://ifconfig.me/ip", timeout=10).text.strip()
    except Exception:
        try:
            return requests.get("https://api.ipify.org", timeout=10).text.strip()
        except Exception:
            return "unknown"


def rotate_warp() -> bool:
    try:
        log("  WARP: cycling IP...")
        subprocess.run(["warp-cli", "disconnect"], capture_output=True, timeout=10)
        time.sleep(2)
        subprocess.run(["warp-cli", "connect"], capture_output=True, timeout=10)
        time.sleep(WARP_COOLDOWN_S)
        result = subprocess.run(
            ["warp-cli", "status"], capture_output=True, text=True, timeout=10
        )
        if "Connected" not in result.stdout:
            subprocess.run(["warp-cli", "connect"], capture_output=True, timeout=10)
            time.sleep(WARP_COOLDOWN_S)
        new_ip = get_public_ip()
        log(f"  WARP: new IP {new_ip}")
        return True
    except Exception as e:
        log(f"  WARP: error: {e}")
        return False


def worker_appeal(model, tmp_dir: Path, worker_id: int, state: ProbeState) -> str:
    """Run one full appeal pipeline. Returns outcome string.

    Each worker gets its own tmp subdirectory to avoid MP3 filename
    collisions (captcha.py names files by millisecond timestamp).
    """
    if _stop_flag.is_set():
        return "stopped"

    # Per-worker tmp dir so MP3 filenames never collide
    worker_tmp = tmp_dir / f"w{worker_id}"
    worker_tmp.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    try:
        return _do_worker_appeal(model, worker_tmp, worker_id, state, t0)
    except Exception as e:
        elapsed = time.time() - t0
        log(f"  [W{worker_id}] ERROR {type(e).__name__}: {e} ({elapsed:.1f}s)")
        return "error"


def _do_worker_appeal(
    model, worker_tmp: Path, worker_id: int, state: ProbeState, t0: float,
) -> str:
    session = new_session()

    # 1. CSRF
    try:
        csrf = fetch_csrftkn(session)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        if status in (403, 429, 503):
            return f"blocked:{status}"
        return "error"
    except Exception:
        return "error"

    if _stop_flag.is_set():
        return "stopped"

    # 2. Captcha solve (GPU — CTranslate2 handles concurrent access)
    # The server may return corrupt/empty MP3 when overwhelmed — this IS
    # a form of rate limiting even without 403/429 status codes.
    try:
        guess = solve_captcha(session, model, worker_tmp)
    except Exception as e:
        elapsed = time.time() - t0
        err_name = type(e).__name__
        if "InvalidData" in err_name or "corrupt" in str(e).lower():
            log(f"  [W{worker_id}] CORRUPT AUDIO — server throttling captcha ({elapsed:.1f}s)")
            return "blocked:captcha_corrupt"
        raise

    # 3. Verify
    if not verify_captcha(session, csrf, guess):
        elapsed = time.time() - t0
        log(f"  [W{worker_id}] CAPTCHA FAIL ({elapsed:.1f}s)")
        return "captcha_fail"

    if _stop_flag.is_set():
        return "stopped"

    # 4. Search
    try:
        response = submit_search(
            session, csrf, guess,
            TEST_BENCH_CODE, TEST_APP_TYPE, TEST_NUMBER, TEST_YEAR,
        )
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        if status in (403, 429, 503):
            return f"blocked:{status}"
        return "error"
    except Exception:
        return "error"

    # 5. Results
    if no_records(response.text):
        elapsed = time.time() - t0
        log(f"  [W{worker_id}] OK (no records, pipeline worked) ({elapsed:.1f}s)")
        return "ok"

    # 6. Case details
    links = extract_casedetails_links(response.text)
    if links:
        try:
            r = session.get(links[0], timeout=HTTP_TIMEOUT)
            if r.status_code in (403, 429, 503):
                return f"blocked:{r.status_code}"
        except Exception:
            pass

    elapsed = time.time() - t0
    log(f"  [W{worker_id}] OK ({elapsed:.1f}s)")
    return "ok"


def run_probe(state: ProbeState) -> ProbeState:
    global _recent_blocks
    _recent_blocks = deque()
    _stop_flag.clear()

    state.current_ip = get_public_ip()
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    log("=" * 55)
    log("  RATE LIMIT PROBE — PARALLEL FULL FLOW")
    log("=" * 55)
    log(f"Target: {BASE}")
    log(f"Test case: {TEST_BENCH} / {TEST_APP_TYPE} / #{TEST_NUMBER} / {TEST_YEAR}")
    log(f"Current IP: {state.current_ip}")
    log(f"Starting workers: {state.current_workers} (step {state.step})")
    if state.max_safe_workers > 0:
        log(f"Resuming — safe with {state.max_safe_workers} workers "
            f"({state.max_safe_throughput:.0f} appeals/hr)")
    log(f"Step: +{WORKER_STEP} workers every {STEP_INTERVAL_S}s")
    log(f"Block threshold: {BLOCK_THRESHOLD} blocks in {WINDOW_S}s")
    log("")

    # Load Whisper model ONCE — shared across all threads
    log("Loading Whisper model (shared across all workers)...")
    model, device, warning = load_whisper_model("tiny.en", device="auto")
    if warning:
        log(f"  WARNING: {warning}")
    log(f"  Whisper ready on {device}")
    log("")

    step_start = time.time()
    step_ok = 0
    step_blocked = 0
    step_appeals = 0

    while not state.found_limit:
        # Launch current_workers appeals in parallel
        futures = []
        with ThreadPoolExecutor(max_workers=state.current_workers) as pool:
            for w in range(state.current_workers):
                if _stop_flag.is_set():
                    break
                futures.append(
                    pool.submit(worker_appeal, model, TMP_DIR, w + 1, state)
                )

            for future in as_completed(futures):
                outcome = future.result()
                if outcome == "stopped":
                    continue

                with _lock:
                    state.total_appeals += 1
                    step_appeals += 1

                if outcome == "ok" or outcome == "no_records":
                    with _lock:
                        state.total_ok += 1
                        state.total_captcha_ok += 1
                        step_ok += 1

                elif outcome == "captcha_fail":
                    with _lock:
                        state.total_captcha_fail += 1

                elif outcome.startswith("blocked:"):
                    block_type = outcome.split(":", 1)[1]
                    is_captcha_corrupt = block_type == "captcha_corrupt"
                    with _lock:
                        state.total_blocked += 1
                        step_blocked += 1
                        state.blocked_codes[block_type] = \
                            state.blocked_codes.get(block_type, 0) + 1
                        _recent_blocks.append(time.time())

                        cutoff = time.time() - WINDOW_S
                        while _recent_blocks and _recent_blocks[0] < cutoff:
                            _recent_blocks.popleft()

                        if len(_recent_blocks) >= BLOCK_THRESHOLD:
                            state.found_limit = True
                            state.blocked_at_workers = state.current_workers
                            _stop_flag.set()
                            state.save()
                            break

                    # Only rotate WARP for HTTP blocks, not captcha corruption
                    # (corrupt audio = server overload, new IP won't help)
                    if not is_captcha_corrupt:
                        log(f"  Block — rotating WARP...")
                        if rotate_warp():
                            state.warp_rotations += 1
                            state.current_ip = get_public_ip()

                elif outcome == "error":
                    with _lock:
                        state.total_errors += 1

        if state.found_limit:
            break

        # Check if step is done
        elapsed_step = time.time() - step_start
        if elapsed_step >= STEP_INTERVAL_S:
            throughput = (step_ok / elapsed_step) * 3600 if elapsed_step > 0 else 0
            state.max_safe_workers = state.current_workers
            state.max_safe_throughput = throughput

            log(f"\n{'='*55}")
            log(f"  Step {state.step} complete — {state.current_workers} workers is SAFE")
            log(f"  Throughput: {throughput:.0f} appeals/hr "
                f"(measured over {elapsed_step:.0f}s)")
            log(f"  This step: OK={step_ok}  Blocked={step_blocked}  "
                f"Appeals={step_appeals}")
            log(f"  Captcha rate: {state.total_captcha_ok}/"
                f"{state.total_captcha_ok + state.total_captcha_fail} "
                f"({100*state.total_captcha_ok/max(1, state.total_captcha_ok+state.total_captcha_fail):.0f}%)")
            log(f"  Totals: OK={state.total_ok}  Blocked={state.total_blocked}  "
                f"Errors={state.total_errors}  Appeals={state.total_appeals}")
            log(f"  WARP rotations: {state.warp_rotations}")

            state.current_workers += WORKER_STEP
            state.step += 1
            step_start = time.time()
            step_ok = 0
            step_blocked = 0
            step_appeals = 0
            _recent_blocks.clear()
            state.save()

            log(f"  >>> Increasing to {state.current_workers} parallel workers")
            log(f"  State saved.")
            log(f"{'='*55}\n")

        # Small delay between batches to avoid pure spin-loop
        time.sleep(0.5)

    # Final report
    log("")
    log(f"{'#'*55}")
    log(f"###  PROBE COMPLETE — LIMIT FOUND")
    log(f"{'#'*55}")
    log(f"Total appeals: {state.total_appeals}")
    log(f"OK: {state.total_ok}  Blocked: {state.total_blocked}  "
        f"Errors: {state.total_errors}")
    log(f"Captcha: {state.total_captcha_ok}/"
        f"{state.total_captcha_ok + state.total_captcha_fail}")
    log(f"Block codes: {dict(state.blocked_codes)}")
    log(f"WARP rotations: {state.warp_rotations}")
    log(f"")
    log(f">>> MAX SAFE: {state.max_safe_workers} parallel workers <<<")
    log(f">>> MEASURED THROUGHPUT: {state.max_safe_throughput:.0f} appeals/hr <<<")
    log(f">>> BLOCKED AT: {state.blocked_at_workers} workers <<<")
    log(f"")
    log(f"Set rate limit to {int(state.max_safe_throughput)} req/hr in the TUI")
    state.save()

    # Cleanup per-worker tmp dirs
    import shutil
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    return state


def main() -> None:
    reset = "--reset" in sys.argv

    if reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        log("State cleared — starting fresh.")

    saved = ProbeState.load()
    if saved and not reset:
        if saved.found_limit:
            log(f"Previous run found the limit!")
            log(f"  Max safe: {saved.max_safe_workers} workers "
                f"({saved.max_safe_throughput:.0f} appeals/hr)")
            log(f"  Blocked at: {saved.blocked_at_workers} workers")
            log(f"Run with --reset to start over.")
            return
        log(f"Resuming from step {saved.step} with {saved.current_workers} workers")
        state = saved
    else:
        state = ProbeState()

    run_probe(state)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\nInterrupted — state saved")
        sys.exit(1)
