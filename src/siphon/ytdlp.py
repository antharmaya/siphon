"""yt-dlp as a library: 1,700+ site extractors and a battle-tested HLS/DASH downloader.

We do not compete with yt-dlp — we stand on it. siphon uses it twice:
  1. as a resolver: does a dedicated extractor know this site?
  2. as the manifest downloader for anything the sniffer finds (.m3u8/.mpd),
     because fragment retries, AES-128 HLS, live edges and ffmpeg merging are
     years of edge cases we have no reason to re-learn.

One YoutubeDL instance per run, so probe and download share cookies and the
already-extracted info (no second round-trip to the site).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.utils import DownloadError

from . import events, tools
from .media import safe_filename
from .models import Candidate

DEFAULT_TEMPLATE = "%(title).180B [%(id)s].%(ext)s"


@dataclass
class YtdlpConfig:
    outdir: Path = Path(".")
    template: str = DEFAULT_TEMPLATE
    format: str | None = None  # None = yt-dlp default (best video + best audio)
    connections: int = 8
    cookies_file: str | None = None
    cookies_from_browser: str | None = None  # "chrome" or "firefox:profile"
    proxy: str | None = None
    impersonate: str | None = None
    no_playlist: bool = False
    gallery_all: bool = False
    preset: str = "best"
    sponsorblock: list[str] = field(default_factory=list)  # categories to cut (e.g. ["sponsor"])
    section: str | None = None       # "START-END" in s or MM:SS / HH:MM:SS — download only that span
    rate_limit: str | None = None    # e.g. "2M" — cap download speed
    archive: str | None = None       # a download-archive file: skip anything already in it
    live_duration: float | None = None  # seconds to record a live stream; None = until it ends / Ctrl+C
    audio_only: bool = False
    subs: bool = False
    quiet: bool = False
    verbose: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


# Format presets: choose among the streams a platform already serves, never re-encode.
#   best     yt-dlp's default ranking: highest resolution, most efficient codec (AV1 > VP9 > H.264).
#   compat   H.264 + AAC in MP4: plays on every phone/TV/editor/messenger and is hardware-decoded
#            on pre-2021 GPUs (no AV1 decode before Intel 11th gen / NVIDIA RTX 30 / AMD RX 6000).
#   small    <=720p, most efficient codec, smallest file.
#   archive  best quality + every piece of context kept: metadata, chapters, thumbnail, subs,
#            info.json and description beside the file. MKV, since it holds all track types.
PRESETS: dict[str, dict[str, Any]] = {
    "best": {},
    "compat": {"format_sort": ["vcodec:h264", "acodec:aac", "res", "fps"], "merge_output_format": "mp4"},
    "small": {"format_sort": ["res:720", "vcodec:av01", "acodec:opus", "+size"]},
    "archive": {"merge_output_format": "mkv", "writeinfojson": True, "writedescription": True,
                "writethumbnail": True, "writesubtitles": True, "subtitleslangs": ["all", "-live_chat"]},
}
ARCHIVE_PP = [{"key": "FFmpegMetadata", "add_chapters": True, "add_metadata": True, "add_infojson": "if_exists"},
              {"key": "FFmpegEmbedSubtitle"}, {"key": "EmbedThumbnail", "already_have_thumbnail": False}]


def selected_tbr(info: dict[str, Any]) -> float:
    """Total bitrate of the formats yt-dlp selected in a processed info dict."""
    fmts = info.get("requested_formats") or [info]
    return sum(f.get("tbr") or 0 for f in fmts)


def pick_richest(infos: list[dict[str, Any]]) -> dict[str, Any]:
    """Measured 2026-10-08: YouTube serves different encodes under the SAME itag across
    requests (itag 398 AV1 720p: 374 kbps in 3 of 4 responses, 711 kbps in 1). Format
    IDs don't pin quality, so for archiving we sample a few responses and keep the richest."""
    return max(infos, key=selected_tbr)


class Unsupported(Exception):
    pass


class _Silent:
    """Probe-time logger: a 'no extractor' answer is expected, not an error to print."""

    def debug(self, msg: str) -> None: ...
    def info(self, msg: str) -> None: ...
    def warning(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...


def browser_spec(spec: str) -> tuple:
    """'chrome' | 'chrome:Profile 1' | 'firefox+gnomekeyring:default' -> yt-dlp tuple."""
    browser, _, profile = spec.partition(":")
    browser, _, keyring = browser.partition("+")
    return (browser.lower(), profile or None, keyring.upper() or None, None)


_DEDICATED: list | None = None


def dedicated_extractor(url: str) -> str | None:
    """Which non-generic extractor claims this URL? ~0.3s once (regex compile), then ~4ms.

    None means an unknown site: yt-dlp would only run its Generic extractor, which is
    slow (fetches + scans the page) and usually fails — the browser is the real answer.
    """
    global _DEDICATED
    if _DEDICATED is None:
        from yt_dlp.extractor import gen_extractor_classes
        from yt_dlp.plugins import load_all_plugins

        load_all_plugins()  # yt-dlp plugin packages count as dedicated extractors too
        _DEDICATED = [c for c in gen_extractor_classes() if c.ie_key() != "Generic"]
    return next((c.ie_key() for c in _DEDICATED if c.suitable(url)), None)


class YtdlpEngine:
    def __init__(self, cfg: YtdlpConfig):
        self.cfg = cfg
        self.files: list[Path] = []

    def params(self, **over: Any) -> dict[str, Any]:
        c = self.cfg
        p: dict[str, Any] = {
            "outtmpl": {"default": str(c.outdir / c.template)},
            "concurrent_fragment_downloads": c.connections,
            "retries": 10,
            "fragment_retries": 10,
            "merge_output_format": "mp4/mkv",
            "noplaylist": c.no_playlist,
            "quiet": c.quiet,
            "no_warnings": c.quiet,
            "verbose": c.verbose,
            "noprogress": c.quiet,
            "post_hooks": [lambda f: self.files.append(Path(f))],
            # YouTube's n/sig challenges need a JS runtime; node is enough when deno is absent.
            "js_runtimes": {"deno": {}, "node": {}},
            "windowsfilenames": False,
        }
        if events.enabled():
            p["progress_hooks"] = [events.ytdlp_hook]
        if tools.ffmpeg_dir():
            p["ffmpeg_location"] = tools.ffmpeg_dir()  # newest ffmpeg, not PATH's first (see tools.py)
        if c.live_duration:
            # Live HLS/DASH goes through yt-dlp's ffmpeg downloader; -t on the output bounds the recording.
            p["external_downloader_args"] = {"ffmpeg_o": ["-t", str(int(c.live_duration))]}
        p.update(PRESETS.get(c.preset, {}))
        if c.sponsorblock:
            # Cut sponsor/intro/outro/etc. segments crowdsourced via the SponsorBlock API.
            p["sponsorblock_remove"] = set(c.sponsorblock)
            p.setdefault("postprocessors", []).append(
                {"key": "SponsorBlock", "categories": set(c.sponsorblock), "when": "after_filter"})
            p["postprocessors"].append({"key": "ModifyChapters", "remove_sponsor_segments": set(c.sponsorblock)})
        if c.section:
            start, end = _parse_section(c.section)
            p["download_ranges"] = yt_dlp.utils.download_range_func(None, [(start, end)])
            p["force_keyframes_at_cuts"] = True  # clean cuts at the exact timestamps
        if c.rate_limit:
            p["ratelimit"] = yt_dlp.utils.parse_bytes(c.rate_limit)
        if c.archive:
            p["download_archive"] = c.archive
        if c.preset == "archive":
            p["postprocessors"] = list(ARCHIVE_PP)
        if c.format:
            p["format"] = c.format
        if c.cookies_file:
            p["cookiefile"] = c.cookies_file
        if c.cookies_from_browser:
            p["cookiesfrombrowser"] = browser_spec(c.cookies_from_browser)
        if c.proxy:
            p["proxy"] = c.proxy
        if c.impersonate:
            p["impersonate"] = ImpersonateTarget.from_str(c.impersonate)
        if c.audio_only:
            p["format"] = c.format or "bestaudio/best"
            p["postprocessors"] = p.get("postprocessors", []) + [{"key": "FFmpegExtractAudio", "preferredcodec": "best"}]
        if c.subs:
            p.update(writesubtitles=True, writeautomaticsub=True, subtitleslangs=["en.*", "-live_chat"],
                     postprocessors=p.get("postprocessors", []) + [{"key": "FFmpegEmbedSubtitle"}])
        p.update(c.extra)
        p.update(over)
        return p

    # ------------------------------------------------------------- resolve

    def probe(self, url: str) -> Candidate:
        """Ask yt-dlp's extractors. Raises Unsupported when only the generic one tried and failed."""
        with yt_dlp.YoutubeDL(self.params(quiet=True, no_warnings=True, noprogress=True, logger=_Silent())) as ydl:
            try:
                info = ydl.extract_info(url, download=False, process=False)
            except DownloadError as e:
                raise Unsupported(_short(e)) from e
            if info is None:
                raise Unsupported("no result")
            # An extractor can "succeed" by returning a pointer to another URL (a tweet's link
            # card, a site redirect, an embed). Follow it now: a dead end must count as
            # unsupported so the chain falls through to the browser sniffer.
            hop, seen = info, {url}
            while hop.get("_type") in ("url", "url_transparent") and len(seen) < 4:
                nxt = hop.get("url")
                if not nxt or nxt in seen:
                    break
                seen.add(nxt)
                try:
                    hop = ydl.extract_info(nxt, download=False, process=False, ie_key=hop.get("ie_key"))
                except DownloadError as e:
                    raise Unsupported(f"{info.get('extractor_key', '?')} pointed to {nxt[:80]}, "
                                      f"which has no video ({_short(e)[:60]})") from e
                if hop is None:
                    raise Unsupported(f"pointer to {nxt[:80]} resolved to nothing")
        n = None
        if info.get("_type") in ("playlist", "multi_video"):
            entries = info.get("entries")
            n = len(entries) if isinstance(entries, list) else info.get("playlist_count")
        note = f"{info.get('extractor_key', '?')} extractor"
        if n is not None:
            note += f" · playlist of {n}"
        elif info.get("_type") == "playlist":
            note += " · playlist"
        return Candidate(url=url, kind="site", source="ytdlp", page_url=url, title=info.get("title"),
                         score=200, note=note, info=info)

    def list_formats(self, cand: Candidate) -> None:
        with yt_dlp.YoutubeDL(self.params(listformats=True, quiet=False)) as ydl:
            ydl.process_ie_result(dict(cand.info or {}), download=False)

    # ------------------------------------------------------------ download

    def download_site(self, cand: Candidate) -> list[Path]:
        before = len(self.files)
        with yt_dlp.YoutubeDL(self.params()) as ydl:
            if self.cfg.preset == "archive" and (cand.info or {}).get("extractor_key") == "Youtube" \
                    and (cand.info or {}).get("_type", "video") == "video":
                samples = []
                for _ in range(4):
                    try:
                        samples.append(ydl.extract_info(cand.url, download=False))
                    except DownloadError:
                        continue
                if samples:
                    best = pick_richest(samples)
                    ydl.process_ie_result(best, download=True)
                    return self.files[before:]
            if cand.info is not None:
                ydl.process_ie_result(cand.info, download=True)
            else:
                ydl.download([cand.url])
        return self.files[before:]

    def download_stream(self, cand: Candidate, name: str) -> list[Path]:
        """HLS/DASH manifest (or a file our own downloader couldn't take) with the page's headers."""
        headers = {k.title(): v for k, v in cand.headers.items() if k.lower() not in ("range", "accept-encoding")}
        before = len(self.files)
        stem = safe_filename(name)
        with yt_dlp.YoutubeDL(self.params(
            http_headers=headers, outtmpl={"default": str(self.cfg.outdir / f"{stem}.%(ext)s")},
            noplaylist=True,
        )) as ydl:
            ydl.download([cand.url])
        return self.files[before:]


def _parse_section(spec: str) -> tuple[float, float | None]:
    """'START-END' where each side is seconds or MM:SS / HH:MM:SS; END may be empty (to the end)."""
    def t(x: str) -> float:
        x = x.strip()
        if not x:
            return 0.0
        parts = [float(p) for p in x.split(":")]
        sec = 0.0
        for p in parts:
            sec = sec * 60 + p
        return sec
    lo, _, hi = spec.partition("-")
    return t(lo), (t(hi) if hi.strip() else None)


def _short(e: Exception) -> str:
    msg = str(e).replace("ERROR: ", "")
    return msg.splitlines()[0][:200] if msg else type(e).__name__
