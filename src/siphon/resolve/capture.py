"""Receives bytes recorded inside the page and writes one file per track.

Page side (in sniff.INIT_JS): every SourceBuffer.appendBuffer() and every media
Blob handed to URL.createObjectURL() is base64'd and pushed through a Playwright
binding as (track_id, mime, seq, data). Bindings arrive in call order, but the
reorder buffer below makes that an assumption we don't depend on.

Why this works: what a player appends to a SourceBuffer is the already-demuxed
container stream (fMP4 init segment + moof/mdat fragments, or WebM clusters).
Concatenated in append order, that IS a playable file. ffmpeg only remuxes it.

Where it breaks, stated up front:
  * Encrypted (EME/DRM) streams append ciphertext. We refuse those upstream.
  * Seeking / quality switches append out-of-timeline or new-init fragments; the
    result can have gaps or a resolution change mid-file.
  * MSE running inside a Worker isn't hooked (init scripts don't reach workers).
"""

from __future__ import annotations

import base64
import threading
from dataclasses import dataclass, field
from pathlib import Path

EXT_BY_MIME = {"video/mp4": "mp4", "audio/mp4": "m4a", "video/webm": "webm", "audio/webm": "weba",
               "video/mp2t": "ts", "audio/mpeg": "mp3", "audio/aac": "aac", "video/x-matroska": "mkv"}


def ext_for(mime: str) -> str:
    return EXT_BY_MIME.get(mime.split(";")[0].strip().lower(), "bin")


@dataclass
class Track:
    id: str
    mime: str
    kind: str  # "mse" | "blob"
    path: Path
    bytes: int = 0
    next_seq: int = 0
    pending: dict[int, bytes] = field(default_factory=dict)
    done: bool = False  # blob fully streamed


class CaptureSink:
    def __init__(self, root: Path):
        self.root = root
        self.tracks: dict[str, Track] = {}
        self._lock = threading.Lock()

    def chunk(self, track_id: str, mime: str, kind: str, seq: int, b64: str) -> None:
        data = base64.b64decode(b64)
        with self._lock:
            t = self.tracks.get(track_id)
            if t is None:
                self.root.mkdir(parents=True, exist_ok=True)
                t = Track(track_id, mime, kind, self.root / f"{kind}-{track_id}.{ext_for(mime)}")
                self.tracks[track_id] = t
            t.pending[int(seq)] = data
            with open(t.path, "ab") as f:
                while t.next_seq in t.pending:
                    piece = t.pending.pop(t.next_seq)
                    f.write(piece)
                    t.bytes += len(piece)
                    t.next_seq += 1

    def finish(self, track_id: str) -> None:
        with self._lock:
            if track_id in self.tracks:
                self.tracks[track_id].done = True

    @property
    def total(self) -> int:
        return sum(t.bytes for t in self.tracks.values())

    def mse_tracks(self) -> list[Track]:
        return [t for t in self.tracks.values() if t.kind == "mse" and t.bytes > 0]

    def blob_tracks(self) -> list[Track]:
        return [t for t in self.tracks.values() if t.kind == "blob" and t.done and t.bytes > 0]
