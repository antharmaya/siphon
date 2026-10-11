"""Machine-readable progress for anything that runs siphon as a subprocess.

`siphon --progress-json URL` writes one JSON object per line to stdout; human output stays
on stderr. `siphon serve`, the queue and the browser extension's host read these instead of
scraping console text (which wraps, changes wording, and carries colour codes).

    {"event": "resolving", "url": "…"}
    {"event": "progress", "done": 1048576, "total": 8388608, "percent": 12.5, "speed": 524288.0}
    {"event": "saved", "file": "/abs/path.mkv", "bytes": 8388608}
    {"event": "error", "message": "…"}
    {"event": "done", "ok": true}
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

_on = False
_out = None
_lock = threading.Lock()
_last = 0.0


def enable() -> None:
    """Take a private copy of stdout for events, then point fd 1 at stderr: yt-dlp, gallery-dl
    and child ffmpeg all print to stdout, and none of that may reach the JSON channel."""
    global _on, _out
    sys.stdout.flush()
    _out = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    _on = True


def enabled() -> bool:
    return _on


def emit(event: str, **fields) -> None:
    if not _on:
        return
    rec = {"event": event, **{k: v for k, v in fields.items() if v is not None}}
    with _lock:
        _out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _out.flush()


def progress(done: int, total: int | float | None, speed: float | None = None) -> None:
    """Throttled to ~4/s, but the final tick (done == total) always goes out."""
    global _last
    if not _on:
        return
    total = int(total) if total else None
    now = time.monotonic()
    if now - _last < 0.25 and not (total and done >= total):
        return
    _last = now
    pct = round(min(100.0, done * 100 / total), 1) if total else None
    emit("progress", done=int(done), total=total, percent=pct, speed=round(speed, 1) if speed else None)


def ytdlp_hook(d: dict) -> None:
    if d.get("status") == "downloading":
        progress(d.get("downloaded_bytes") or 0, d.get("total_bytes") or d.get("total_bytes_estimate"), d.get("speed"))
