"""Audio captcha solver built on faster-whisper.

The ITAT captcha endpoint serves an MP3 where each character is spoken
one at a time with clear gaps, so even the tiniest Whisper model nails
it. We normalise words like "eight" -> "8" and strip punctuation.
"""
from __future__ import annotations

import re
import struct
import tempfile
import time
import wave
from pathlib import Path

import requests
from faster_whisper import WhisperModel

from .constants import AUDIO_URL, CHECK_URL, HTTP_TIMEOUT, IMG_URL

_WORD_TO_DIGIT = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "oh": "0",
}


def normalize_transcription(text: str) -> str:
    """Whisper may output 'r, it, k, m, g, h.' -> 'RITKMGH'."""
    text = text.lower()
    for w, d in _WORD_TO_DIGIT.items():
        text = re.sub(rf"\b{w}\b", d, text)
    return re.sub(r"[^a-z0-9]", "", text).upper()


def _preload_nvidia_wheels() -> None:
    """Make the pip-installed NVIDIA libraries visible to the dynamic linker.

    When users install cuBLAS/cuDNN via the `nvidia-cublas-cu12` /
    `nvidia-cudnn-cu12` wheels, the .so files land inside
    `site-packages/nvidia/*/lib/` which is NOT on LD_LIBRARY_PATH. We
    explicitly dlopen them here so CTranslate2 can find them when it
    initialises its CUDA backend.

    No-op on macOS (no CUDA), or if the wheels aren't installed.
    """
    import os
    import sys

    # macOS has no CUDA — skip entirely
    if sys.platform == "darwin":
        return

    import ctypes

    candidates = []
    try:
        import nvidia  # type: ignore
    except ImportError:
        return

    for ns_path in getattr(nvidia, "__path__", []):
        for subdir in ("cublas/lib", "cudnn/lib", "cuda_nvrtc/lib"):
            lib_dir = os.path.join(ns_path, subdir)
            if os.path.isdir(lib_dir):
                candidates.append(lib_dir)
                cur = os.environ.get("LD_LIBRARY_PATH", "")
                if lib_dir not in cur.split(":"):
                    os.environ["LD_LIBRARY_PATH"] = (
                        f"{lib_dir}:{cur}" if cur else lib_dir
                    )

    load_order = [
        "libcublasLt.so.12",
        "libcublas.so.12",
        "libcudnn.so.9",
        "libcudnn_ops.so.9",
        "libcudnn_graph.so.9",
    ]
    for lib_dir in candidates:
        for name in load_order:
            full = os.path.join(lib_dir, name)
            if os.path.isfile(full):
                try:
                    ctypes.CDLL(full, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


def _detect_cuda() -> bool:
    """Check whether CTranslate2 (faster-whisper's backend) can see a CUDA GPU."""
    _preload_nvidia_wheels()
    try:
        import ctranslate2  # type: ignore

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def resolve_device(device: str) -> tuple[str, str]:
    """Take a requested device/compute_type pair and return what we should
    actually ask faster-whisper for. Accepts 'auto', 'cuda', or 'cpu'."""
    requested = (device or "auto").lower()
    if requested == "auto":
        actual_device = "cuda" if _detect_cuda() else "cpu"
    else:
        actual_device = requested
    # Pick a compute type that matches the device if the caller didn't
    # override it. float16 is the sweet spot on modern NVIDIA cards; int8
    # keeps CPU runs fast.
    compute_type = "float16" if actual_device == "cuda" else "int8"
    return actual_device, compute_type


def _write_silent_wav(path: Path, seconds: float = 0.5, rate: int = 16000) -> None:
    """Write a short mono 16-bit PCM silent WAV — used for warmup inference."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(rate)
        n_frames = int(seconds * rate)
        wf.writeframes(struct.pack("<" + "h" * n_frames, *([0] * n_frames)))


def _try_load_and_warmup(
    model_path: str, device: str, compute_type: str,
) -> WhisperModel:
    """Construct the model from a LOCAL path and run a dummy transcription
    so any runtime library errors surface now instead of on the first captcha."""
    model = WhisperModel(model_path, device=device, compute_type=compute_type)
    with tempfile.TemporaryDirectory() as tmp:
        silent = Path(tmp) / "silent.wav"
        _write_silent_wav(silent)
        segs, _ = model.transcribe(str(silent), beam_size=1, language="en", vad_filter=False)
        for _ in segs:
            break
    return model


def is_model_cached(size: str) -> bool:
    """Check if a faster-whisper model is fully downloaded (including model.bin)."""
    from faster_whisper.utils import download_model

    try:
        path = download_model(size, local_files_only=True)
        # The HF cache may have metadata but not the actual model weights
        model_bin = Path(path) / "model.bin"
        return model_bin.is_file() and model_bin.stat().st_size > 0
    except Exception:
        return False


_MODEL_SIZES = {
    "tiny.en": "39 MB", "tiny": "39 MB", "base.en": "74 MB", "base": "74 MB",
    "small.en": "244 MB", "small": "244 MB", "medium.en": "769 MB", "medium": "769 MB",
    "distil-large-v3": "756 MB", "large-v3-turbo": "809 MB",
    "large-v3": "1.5 GB", "large-v2": "1.5 GB", "large": "1.5 GB",
}


def ensure_model_downloaded(
    size: str,
    on_progress: object = None,
) -> str:
    """Download the model if not cached. Returns the LOCAL model path.

    Args:
        size: Model name (e.g. 'tiny.en', 'large-v3-turbo')
        on_progress: Optional callback(str) for status messages
    """
    import os
    import sys
    from faster_whisper.utils import download_model

    emit = on_progress or (lambda msg: None)

    if is_model_cached(size):
        emit(f"model {size} found in cache")
        return download_model(size, local_files_only=True)

    human_size = _MODEL_SIZES.get(size, "unknown size")
    emit(f"downloading model {size} ({human_size}) — please wait…")

    if sys.platform == "darwin":
        # macOS: HF hub's default downloader spawns subprocesses from threads
        # causing "bad value(s) in fds_to_keep". Use huggingface_hub directly
        # with max_workers=1 to avoid subprocess/fork issues.
        path = _download_model_single_thread(size)
    else:
        path = download_model(size)

    emit(f"model {size} download complete")
    return path


def _download_model_single_thread(size: str) -> str:
    """Download a faster-whisper model using single-threaded HF hub download.
    Avoids subprocess spawning that breaks on macOS threads."""
    import re
    from faster_whisper.utils import _MODELS

    if re.match(r".*/.*", size):
        repo_id = size
    else:
        repo_id = _MODELS.get(size)
        if repo_id is None:
            raise ValueError(f"Invalid model size '{size}'")

    from huggingface_hub import snapshot_download

    return snapshot_download(
        repo_id,
        allow_patterns=[
            "config.json",
            "preprocessor_config.json",
            "model.bin",
            "tokenizer.json",
            "vocabulary.*",
        ],
        max_workers=1,  # single thread — no subprocess spawning
    )


def load_whisper_model(
    size: str = "tiny.en",
    device: str = "auto",
    compute_type: str = "auto",
    on_progress: object = None,
) -> tuple[WhisperModel, str, str]:
    """Load a faster-whisper model. Returns (model, actual_device, warning).

    Tries the requested device first, runs a warmup transcription to catch
    missing CUDA libs or other runtime failures, and falls back to CPU on
    any error. The returned warning string, when non-empty, should be
    surfaced to the user.

    On macOS, always uses CPU with int8 (no CUDA available).
    """
    emit = on_progress or (lambda msg: None)

    # Download first (with progress) — returns the local path.
    # Passing the local path to WhisperModel prevents it from doing
    # its own silent download that blocks with no feedback.
    model_path = ensure_model_downloaded(size, on_progress=emit)

    actual_device, default_compute = resolve_device(device)
    ct = compute_type if compute_type and compute_type != "auto" else default_compute

    emit(f"loading {size} on {actual_device} ({ct})…")
    try:
        model = _try_load_and_warmup(model_path, actual_device, ct)
        return model, actual_device, ""
    except Exception as e:
        if actual_device == "cuda":
            warning = (
                f"CUDA load failed ({type(e).__name__}: {e}); "
                "falling back to CPU. To use the GPU, install cuBLAS/cuDNN 12 "
                "(e.g. `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` or "
                "distro packages), or pass --device cpu to silence this warning."
            )
            emit("CUDA failed, falling back to CPU…")
            try:
                model = _try_load_and_warmup(model_path, "cpu", "int8")
                return model, "cpu", warning
            except Exception as e2:
                raise RuntimeError(
                    f"Both CUDA and CPU whisper load failed. "
                    f"cuda: {e}  cpu: {e2}"
                ) from e2
        raise


def solve_captcha(
    session: requests.Session, model: WhisperModel, tmp_dir: Path
) -> str:
    """Fetch a fresh captcha and transcribe the audio. Returns the guess
    (may be empty on error)."""
    # /captcha/show seeds the server-side captcha state for the session;
    # /captcha/listen/ alone returns an empty body otherwise.
    session.get(IMG_URL, timeout=HTTP_TIMEOUT)
    audio = session.get(AUDIO_URL, allow_redirects=True, timeout=HTTP_TIMEOUT).content
    if not audio:
        return ""

    audio_path = tmp_dir / f"captcha_{int(time.time() * 1000)}.mp3"
    audio_path.write_bytes(audio)
    try:
        segments, _ = model.transcribe(
            str(audio_path),
            beam_size=5,
            language="en",
            vad_filter=False,
        )
        raw = "".join(s.text for s in segments).strip()
    finally:
        audio_path.unlink(missing_ok=True)
    return normalize_transcription(raw)


def verify_captcha(session: requests.Session, csrf: str, guess: str) -> bool:
    if not guess or not (4 <= len(guess) <= 10):
        return False
    r = session.post(
        CHECK_URL,
        data={"captcha": guess},
        headers={"X-CSRF-TOKEN": csrf, "X-Requested-With": "XMLHttpRequest"},
        timeout=HTTP_TIMEOUT,
    )
    try:
        return r.json().get("rslt") == "true"
    except Exception:
        return False
