"""The one type every resolver produces and every downloader consumes.

Resolvers answer "where is the media on this page?"; downloaders answer "how do I
pull these bytes fast and intact?". Neither knows the other exists — they only
share Candidate. Adding a resolver (say, an API for one site) never touches the
download code, and swapping the downloader never touches resolution.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Kind = Literal["file", "hls", "dash", "site", "capture", "gallery"]
# file : one progressive URL (mp4/webm/mp3...) -> segmented downloader
# hls  : .m3u8 manifest                         -> yt-dlp fragment downloader
# dash : .mpd manifest                          -> yt-dlp fragment downloader
# site : a page yt-dlp has a dedicated extractor for; `info` carries its result
# capture: bytes recorded inside the page (MediaSource appendBuffer / Blob objects);
#          `tracks` holds the local files, the downloader only has to mux them
# gallery: an image/mixed post (tweet, IG post...) resolved by gallery-dl; `info` holds the count


@dataclass
class Candidate:
    url: str
    kind: Kind
    source: str  # which resolver found it: direct | ytdlp | sniff | dom
    page_url: str = ""
    title: str | None = None
    mime: str | None = None
    size: int | None = None
    # Headers the browser actually sent. Many CDNs 403 a bare request that lacks
    # the Referer/Origin/Cookie of the page that embedded the player.
    headers: dict[str, str] = field(default_factory=dict)
    is_master: bool = False  # HLS master playlist (has variants) vs a single rendition
    drm: str | None = None  # key system name if the stream is DRM-protected
    score: float = 0.0
    note: str = ""
    info: dict[str, Any] | None = None  # raw yt-dlp ie_result for kind == "site"
    tracks: list[tuple[str, str]] = field(default_factory=list)  # (local path, mime) for kind == "capture"

    def label(self) -> str:
        bits = [self.kind.upper()]
        if self.is_master:
            bits.append("master")
        if self.mime:
            bits.append(self.mime)
        if self.size:
            bits.append(human_size(self.size))
        if self.drm:
            bits.append(f"DRM:{self.drm}")
        return " · ".join(bits)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("info", None)
        # Never print cookies to stdout/JSON logs.
        d["headers"] = {k: v for k, v in self.headers.items() if k.lower() != "cookie"}
        return d


def human_size(n: float | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return "?"
