"""FastAPI web server — replaces the Textual TUI with a browser-based UI.

Run:
    uvicorn web.app:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from itat_scraper import APPEAL_TYPE_LABELS, BENCH_CODES, RunConfig, Runner
from itat_scraper.captcha import is_model_cached

from .state import AppState, RunState, app_state

app = FastAPI(title="ITAT Scraper", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- WebSocket manager ----

class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def broadcast_sync(self, message: dict) -> None:
        """Call from a non-async thread to broadcast."""
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(message), self._loop)

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop


manager = ConnectionManager()


@app.on_event("startup")
async def startup() -> None:
    manager.set_loop(asyncio.get_running_loop())


# ---- Pydantic models ----

class StartRequest(BaseModel):
    benches: list[str]
    app_type: str = "ITA"
    years: list[int]
    start_number: int = 1
    max_number: int = 10000
    rate_per_hour: Optional[int] = None
    max_workers: int = 1
    max_consecutive_missing: int = 20
    captcha_retries: int = 5
    pipeline_retries: int = 3
    model_size: str = "tiny.en"
    device: str = "auto"
    captcha_refetch: bool = True
    out_dir: str = "./downloads"


# ---- Runner event handler ----

def on_runner_event(kind: str, payload: dict) -> None:
    """Called from runner threads — bridges events to WebSocket + stats."""
    state = app_state

    # Update stats on appeal_done
    if kind == "appeal_done":
        r = payload.get("result", {})
        state.bump_stats(r)

    # Track captcha retries
    if kind == "captcha_attempt":
        if payload.get("attempt", 1) > 1:
            state.bump_captcha_retries()

    # Build log message
    log_level = "info"
    log_msg = ""

    if kind == "model_loading":
        log_msg = f"loading whisper {payload['size']} on {payload['device']}…"
    elif kind == "model_progress":
        log_msg = payload.get("message", "")
    elif kind == "model_warning":
        log_msg = payload.get("warning", "")
        log_level = "warning"
    elif kind == "model_ready":
        log_msg = f"whisper ready on {payload['device']}"
        log_level = "success"
    elif kind == "run_start":
        log_msg = f"run started — benches={payload.get('benches')} years={payload.get('years')}"
    elif kind == "bench_start":
        log_msg = f"bench {payload.get('index', 0) + 1}/{payload.get('total', 0)}: {payload.get('bench')}"
    elif kind == "bench_end":
        log_msg = f"bench done: {payload.get('bench')}"
    elif kind == "year_start":
        log_msg = f"{payload.get('bench')} / {payload.get('year')} — start={payload.get('start')}"
    elif kind == "year_end":
        log_msg = f"{payload.get('bench')} / {payload.get('year')} done — last=#{payload.get('last_number')} ({payload.get('reason')})"
    elif kind == "appeal_done":
        r = payload.get("result", {})
        tag, _ = state.classify_tag(r)
        if tag == "OK":
            log_msg = f"OK #{r.get('appeal_number')} {(r.get('parties') or '')[:70]}"
            log_level = "success"
        elif tag == "NO-PDF":
            log_msg = f"NO-PDF #{r.get('appeal_number')} — {r.get('note')}"
            log_level = "warning"
        elif tag in ("SKIP", "MISS"):
            pass  # quiet
        else:
            log_msg = f"{tag} #{r.get('appeal_number')} — {r.get('note')}"
            log_level = "error"
    elif kind == "captcha_attempt":
        log_msg = f"#{payload.get('number')} captcha try {payload.get('attempt')}: {payload.get('guess')}"
        log_level = "dim"
    elif kind == "captcha_refetch":
        log_msg = f"#{payload.get('number')} captcha failed — refetching (attempt {payload.get('attempt')})"
        log_level = "warning"
    elif kind == "captcha_corrupt":
        log_msg = f"#{payload.get('number')} corrupt audio — server overloaded, backing off"
        log_level = "error"
    elif kind == "retry":
        log_msg = f"retry {payload.get('bench')}/{payload.get('year')}/#{payload.get('number')} attempt {payload.get('attempt')}: {payload.get('reason')}"
        log_level = "warning"
    elif kind == "stage":
        log_msg = f"#{payload.get('number')} {payload.get('stage')}"
        log_level = "dim"
    elif kind == "cleanup":
        log_msg = f"cleanup: removed {payload.get('removed_mp3s', 0)} captcha mp3(s)"
    elif kind == "run_end":
        s = payload.get("summary", {})
        log_msg = (
            f"SUMMARY  downloaded={s.get('downloaded', 0)}  "
            f"no-pdf={s.get('missing_pdf', 0)}  not-found={s.get('not_found', 0)}  "
            f"errors={s.get('errors', 0)}  total={s.get('total_processed', 0)}"
        )
        log_level = "success"
        state.run_state = RunState.IDLE

    if log_msg:
        state.add_log(log_level, log_msg)

    # Broadcast event + current stats to all WebSocket clients
    ws_message = {
        "type": kind,
        "payload": _safe_payload(payload),
        "stats": state.get_stats(),
        "state": state.run_state.value,
    }
    if log_msg:
        ws_message["log"] = {"level": log_level, "message": log_msg}

    manager.broadcast_sync(ws_message)


def _safe_payload(payload: dict) -> dict:
    """Make payload JSON-serializable."""
    safe = {}
    for k, v in payload.items():
        if isinstance(v, Path):
            safe[k] = str(v)
        elif isinstance(v, (str, int, float, bool, type(None))):
            safe[k] = v
        elif isinstance(v, (list, tuple)):
            safe[k] = [str(x) if isinstance(x, Path) else x for x in v]
        elif isinstance(v, dict):
            safe[k] = _safe_payload(v)
        else:
            safe[k] = str(v)
    return safe


# ---- API endpoints ----

@app.get("/api/config")
def get_config():
    """Available benches, appeal types, models, and cached model status."""
    models = [
        {"value": "tiny.en", "label": "tiny.en — 39 MB, fastest", "cached": is_model_cached("tiny.en")},
        {"value": "base.en", "label": "base.en — 74 MB", "cached": is_model_cached("base.en")},
        {"value": "small.en", "label": "small.en — 244 MB, better accuracy", "cached": is_model_cached("small.en")},
        {"value": "medium.en", "label": "medium.en — 769 MB, high accuracy", "cached": is_model_cached("medium.en")},
        {"value": "distil-large-v3", "label": "distil-large-v3 — 756 MB", "cached": is_model_cached("distil-large-v3")},
        {"value": "large-v3-turbo", "label": "large-v3-turbo — 809 MB, fast turbo", "cached": is_model_cached("large-v3-turbo")},
        {"value": "large-v3", "label": "large-v3 — 1.5 GB, best accuracy", "cached": is_model_cached("large-v3")},
    ]
    return {
        "benches": sorted(BENCH_CODES.keys()),
        "appeal_types": {k: v for k, v in APPEAL_TYPE_LABELS.items()},
        "models": models,
        "devices": ["auto", "cuda", "cpu"],
    }


@app.get("/api/status")
def get_status():
    return app_state.get_status()


@app.get("/api/stats")
def get_stats():
    return app_state.get_stats()


@app.get("/api/results/{category}")
def get_results(category: str):
    return app_state.get_results(category)


@app.get("/api/logs")
def get_logs(limit: int = 200):
    with app_state._lock:
        return app_state.log_messages[-limit:]


@app.post("/api/start")
def start_run(req: StartRequest):
    if app_state.run_state != RunState.IDLE:
        return {"error": "A run is already in progress", "state": app_state.run_state.value}

    out_dir = Path(req.out_dir).expanduser()
    if not out_dir.is_absolute():
        out_dir = (Path.cwd() / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    captcha_retries = req.captcha_retries
    if not req.captcha_refetch:
        captcha_retries = 1

    cfg = RunConfig(
        benches=req.benches,
        app_type=req.app_type,
        years=req.years,
        start_number=req.start_number,
        max_number=req.max_number,
        rate_per_hour=req.rate_per_hour,
        max_workers=max(1, min(req.max_workers, 60)),
        max_consecutive_missing=req.max_consecutive_missing,
        captcha_retries=captcha_retries,
        pipeline_retries=req.pipeline_retries,
        model_size=req.model_size,
        device=req.device,
        out_dir=out_dir,
    )
    cfg.validate()

    app_state.config = cfg
    app_state.reset_stats()
    app_state.run_state = RunState.RUNNING

    # Distributed mode (optional)
    s3_uploader = None
    db_reporter = None
    try:
        from itat_scraper.storage import create_uploader
        s3_uploader = create_uploader()
    except (ImportError, Exception):
        pass
    try:
        from itat_scraper.reporter import create_reporter
        db_reporter = create_reporter()
    except (ImportError, Exception):
        pass

    app_state.runner = Runner(
        cfg,
        on_event=on_runner_event,
        s3_uploader=s3_uploader,
        db_reporter=db_reporter,
    )

    def run_thread():
        try:
            app_state.runner.run()
        except Exception as e:
            app_state.add_log("error", f"runner error: {type(e).__name__}: {e}")
            manager.broadcast_sync({
                "type": "run_error",
                "payload": {"error": str(e)},
                "stats": app_state.get_stats(),
                "state": "idle",
            })
        finally:
            app_state.run_state = RunState.IDLE
            app_state.runner = None

    t = threading.Thread(target=run_thread, daemon=True)
    t.start()

    return {"status": "started", "config": _safe_payload(vars(cfg))}


@app.post("/api/stop")
def stop_run():
    if app_state.runner is None:
        return {"error": "No run in progress"}
    app_state.runner.stop()
    app_state.add_log("warning", "Stop requested…")
    return {"status": "stopping"}


@app.post("/api/pause")
def toggle_pause():
    if app_state.runner is None:
        return {"error": "No run in progress"}
    if app_state.runner.is_paused:
        app_state.runner.resume()
        app_state.run_state = RunState.RUNNING
        app_state.add_log("info", "Resumed")
        manager.broadcast_sync({"type": "resumed", "payload": {}, "stats": app_state.get_stats(), "state": "running"})
        return {"status": "resumed"}
    else:
        app_state.runner.pause()
        app_state.run_state = RunState.PAUSED
        app_state.add_log("warning", "Paused")
        manager.broadcast_sync({"type": "paused", "payload": {}, "stats": app_state.get_stats(), "state": "paused"})
        return {"status": "paused"}


# ---- WebSocket ----

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    # Send current state on connect
    await ws.send_json({
        "type": "init",
        "payload": {},
        "stats": app_state.get_stats(),
        "state": app_state.run_state.value,
    })
    try:
        while True:
            # Keep connection alive — client can send pings
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ---- Serve React frontend (production) ----

FRONTEND_DIR = Path(__file__).parent.parent / "frontend" / "dist"

if FRONTEND_DIR.is_dir():
    # Serve static assets (JS, CSS, images)
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/{path:path}")
    def serve_frontend(path: str = ""):
        # SPA: serve index.html for all non-API routes
        index = FRONTEND_DIR / "index.html"
        if index.exists():
            return FileResponse(index)
        return {"error": "frontend not built"}
