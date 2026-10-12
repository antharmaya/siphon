"""Download dispatch: route each Candidate kind to the tool that is best at it.

  site     -> yt-dlp (extractor result already in hand)
  hls/dash -> yt-dlp fragment downloader, replaying the page's headers
  file     -> our segmented downloader; on failure, yt-dlp (it can impersonate TLS)
  capture  -> ffmpeg remux of the recorded tracks (no network at all)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .. import netcheck, tools
from ..media import ext_of, safe_filename
from ..models import Candidate
from ..ytdlp import YtdlpEngine
from .segmented import SegmentedDownloader


class LocalNetworkRefused(Exception):
    pass


class DrmProtected(Exception):
    pass


def unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 10_000):
        p = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not p.exists():
            return p
    raise FileExistsError(path)


def name_for(c: Candidate) -> str:
    if c.title:
        return c.title
    tail = c.url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return tail.rsplit(".", 1)[0] or "video"


BrowserFallback = Callable[[Candidate, Path, "Callable[[int, int | None], None] | None"], Path]


def download(c: Candidate, outdir: Path, ydl: YtdlpEngine, connections: int, proxy: str | None,
             progress: Callable[[int, int | None], None] | None = None, log: Callable[[str], None] = lambda m: None,
             name: str | None = None, browser_fallback: BrowserFallback | None = None) -> list[Path]:
    if c.drm:
        raise DrmProtected(c.drm)
    # The page is the URL someone asked for (and `siphon serve` vetted); the media URL came from the
    # page or a redirect, so it is the attacker's choice. See netcheck.inward.
    if c.kind in ("file", "hls", "dash") and c.page_url and netcheck.inward(c.url, c.page_url):
        raise LocalNetworkRefused(f"{urlsplit(c.url).hostname} is on a more local network than {c.page_url[:80]}")
    outdir.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(name or name_for(c))

    if c.kind == "gallery":
        from ..resolve import gallery
        return gallery.download(c, outdir, cookies_browser=ydl.cfg.cookies_from_browser,
                                cookies_file=ydl.cfg.cookies_file, proxy=proxy,
                                gallery_all=getattr(ydl.cfg, "gallery_all", False), log=log)
    if c.kind == "site":
        return ydl.download_site(c)
    # Found by the sniffer => we know the page, so Chrome itself can make the requests if a
    # browser-bound CDN refuses Python (TLS fingerprint, client hints, bound cookies).
    can_fallback = browser_fallback is not None and c.source in ("sniff", "dom") and c.kind in ("file", "hls")

    ext = ext_of(c.url) or ((c.mime or "video/mp4").split("/")[-1].split(";")[0]) or "mp4"

    def via_browser(err: Exception) -> list[Path]:
        log(f"external download refused ({str(err)[:90]}); letting Chrome make the requests")
        return [browser_fallback(c, unique(outdir / f"{stem}.{'mp4' if c.kind == 'hls' else ext}"), progress)]

    if c.kind in ("hls", "dash"):
        try:
            return ydl.download_stream(c, stem)
        except Exception as e:
            if can_fallback:
                return via_browser(e)
            raise
    if c.kind == "capture":
        return [mux_capture(c, unique(outdir / f"{stem}.{_capture_ext(c)}"))]

    dest = unique(outdir / f"{stem}.{ext}")
    try:
        SegmentedDownloader(connections=connections, proxy=proxy, progress=progress).download(c.url, dest, c.headers)
        return [dest]
    except Exception as e:
        log(f"segmented download failed ({type(e).__name__}: {str(e)[:120]}); retrying through yt-dlp")
        try:
            return ydl.download_stream(c, stem)
        except Exception as e2:
            if can_fallback:
                return via_browser(e2)
            raise


def _capture_ext(c: Candidate) -> str:
    mimes = " ".join(m for _, m in c.tracks).lower()
    return "webm" if "webm" in mimes and "mp4" not in mimes else "mp4"


def mux_capture(c: Candidate, dest: Path) -> Path:
    """Remux recorded tracks into one file. Stream copy only: no re-encode, no quality loss."""
    ffmpeg = tools.ffmpeg()
    paths = [p for p, _ in c.tracks]
    if not ffmpeg:
        if len(paths) == 1:
            shutil.copyfile(paths[0], dest)
            return dest
        raise RuntimeError("ffmpeg is required to merge captured video+audio tracks")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+genpts+igndts"]
    for p in paths:
        cmd += ["-i", p]
    for i in range(len(paths)):
        cmd += ["-map", f"{i}"]
    cmd += ["-c", "copy"]
    if dest.suffix == ".mp4":
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(dest))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        # Mixed timelines (seek/quality switch) can trip the muxer; mkv is far more forgiving.
        alt = dest.with_suffix(".mkv")
        r2 = subprocess.run(cmd[:-1 - (2 if dest.suffix == ".mp4" else 0)] + [str(alt)], capture_output=True, text=True)
        if r2.returncode != 0:
            raise RuntimeError(f"ffmpeg mux failed: {r.stderr.strip()[-300:]}")
        return alt
    return dest
