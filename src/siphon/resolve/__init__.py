"""Resolver chain: cheapest and most precise first, the browser last.

  direct  ~0.1s  the URL is itself a media file/manifest (not asked of pages yt-dlp knows)
  ytdlp   ~1-5s  a dedicated extractor knows the site (best metadata, all formats)
  sniff   ~5-30s load it in Chrome and watch the network / record the blob: stream

`auto` stops at the first that answers. The browser is the most general resolver
and also the slowest and the most fragile, so it is the fallback, never the default.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Literal

from ..media import classify
from ..models import Candidate
from ..ytdlp import Unsupported, YtdlpEngine, dedicated_extractor
from . import direct, gallery
from .sniff import SniffOptions, SniffResult, sniff

Engine = Literal["auto", "direct", "ytdlp", "sniff"]

# A site's own extractor saying the post has no video is an answer, not a failure: falling back to the
# browser then saves whatever video the page's own interface plays (on X: its UI animations).
NO_VIDEO = re.compile(r"no video could be found|there'?s no video|does not (contain|have) (a |any )?video|"
                      r"no (video|media) (formats? )?(was |were )?found", re.I)


@dataclass
class Resolution:
    url: str
    candidates: list[Candidate] = field(default_factory=list)
    tried: list[tuple[str, str]] = field(default_factory=list)  # (resolver, outcome)
    sniffed: SniffResult | None = None

    @property
    def best(self) -> Candidate | None:
        usable = [c for c in self.candidates if not c.drm]
        return max(usable, key=lambda c: c.score) if usable else None


def resolve(url: str, engine: Engine, ydl: YtdlpEngine, sopts: SniffOptions, proxy: str | None = None,
            on_found: Callable[[Candidate], None] | None = None, log: Callable[[str], None] = lambda m: None) -> Resolution:
    res = Resolution(url)
    t = time.monotonic()

    def took() -> str:
        nonlocal t
        now = time.monotonic()
        d, t = now - t, now
        return f" ({d:.1f}s)"

    # yt-dlp's extractor for this site, looked up once (~20ms on a known site). A page on a site
    # yt-dlp knows, whose URL doesn't look like a media file, isn't one: skip direct's GET of it
    # (0.4-2.5s measured on YouTube, the page fetched only to be thrown away). A URL that looks
    # like media keeps the old order, so a pasted .mp4 never waits for the extractor lookup.
    looks_media = classify(url) is not None
    ded = dedicated_extractor(url) if engine == "auto" and not looks_media else None
    if engine == "direct" or (engine == "auto" and ded is None):
        c = direct.probe(url, proxy=proxy)
        res.tried.append(("direct", ("media URL" if c else "not media") + took()))
        if c:
            res.candidates.append(c)
            return res
        if engine == "direct":
            return res
        if looks_media:
            ded = dedicated_extractor(url)

    # Unknown site (no dedicated extractor): don't make the browser wait for yt-dlp's
    # Generic probe (~5s, usually fruitless). Run both at once; best result wins on score.
    if engine == "auto" and not sopts.headed and ded is None:
        log("unknown site: racing yt-dlp's generic extractor against Chrome…")
        site, r, gen_note = asyncio.run(_race(url, ydl, sopts, on_found))
        res.tried.append(("ytdlp", gen_note))
        if site:
            res.candidates.append(site)
        _absorb(res, r, took())
        return res

    # Image posts (tweets, IG posts): gallery-dl gets every image AND any video. Prefer it over
    # yt-dlp whenever the post actually has images, or when yt-dlp wouldn't handle the URL at all —
    # so video-only posts on sites yt-dlp knows keep going through yt-dlp (better video).
    # Passing `ded` checks only gallery-dl's modules for that site: none for YouTube or Vimeo, so
    # no ~1s scan of all of gallery-dl to learn that it has nothing.
    if engine == "auto" and not getattr(sopts, "no_gallery", False) and gallery.supported(url, ded):
        gc = gallery.probe(url, cookies_browser=ydl.cfg.cookies_from_browser,
                           cookies_file=ydl.cfg.cookies_file, proxy=proxy, site=ded)
        if gc is not None:
            info = gc.info or {}
            has_images = info.get("videos", 0) < info.get("count", 0)
            if has_images or ded is None:
                res.tried.append(("gallery", gc.note + took()))
                res.candidates.append(gc)
                return res
        res.tried.append(("gallery", ("no media" if gc is None else "video-only, deferring to yt-dlp") + took()))

    if engine in ("auto", "ytdlp"):
        log("asking yt-dlp extractors…")
        try:
            c = ydl.probe(url)
            res.tried.append(("ytdlp", c.note + took()))
            res.candidates.append(c)
            return res
        except Unsupported as e:
            res.tried.append(("ytdlp", f"no: {e}"[:110] + took()))
            if engine == "ytdlp" or (ded and NO_VIDEO.search(str(e))):
                return res

    log("opening the page in Chrome and watching the network…" if not sopts.headed
        else "opening Chrome — browse to the video and play it")
    r = asyncio.run(sniff(url, sopts, on_found))
    _absorb(res, r, took())
    return res


async def _race(url: str, ydl: YtdlpEngine, sopts: SniffOptions, on_found):
    t0 = time.monotonic()

    def probe():
        try:
            c = ydl.probe(url)
            return c, f"{c.note} ({time.monotonic() - t0:.1f}s, in parallel)"
        except Unsupported as e:
            return None, f"no: {e}"[:100] + f" ({time.monotonic() - t0:.1f}s, in parallel)"

    (site, note), r = await asyncio.gather(asyncio.to_thread(probe), sniff(url, sopts, on_found))
    return site, r, note


def _absorb(res: Resolution, r: SniffResult, took: str) -> None:
    res.sniffed = r
    res.candidates.extend(r.candidates)
    outcome = f"{len(r.candidates)} candidate(s)"
    if r.segments_seen and not any(c.kind in ("hls", "dash") for c in r.candidates):
        outcome += f", {r.segments_seen} bare segments without a manifest"
    if r.drm:
        outcome += f", DRM requested: {', '.join(sorted(r.drm))}"
    if r.error:
        outcome += f" ({r.error})"
    res.tried.append(("sniff", outcome + took))
