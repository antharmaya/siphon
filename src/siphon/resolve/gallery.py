"""gallery-dl as a resolver+downloader for image posts and mixed media.

yt-dlp is video-only: a photo tweet, or the stills in a mixed post, are invisible to
it. gallery-dl is the specialist — it has dedicated extractors for Twitter/X, Instagram,
Reddit, Tumblr, Pixiv and ~300 more, and for a single post it returns every image AND
any video (Twitter serves mp4 variants that gallery-dl grabs directly).

Siphon uses it two ways, mirroring the yt-dlp integration:
  resolve   does gallery-dl have a dedicated extractor, and how many items does the
            post hold? (a simulate pass — no files written)
  download  run it for real into the output dir and collect what it wrote.

Auth: X/Instagram killed guest access, so most posts need the user's own login. That
flows in through the same --cookies-from-browser / --cookies the rest of siphon uses.
"""

from __future__ import annotations

import io
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from ..models import Candidate

# Whole-account / feed extractors: a URL like x.com/NASA or /media is thousands of posts.
# siphon won't auto-download those without --gallery-all; a single post is bounded and safe.
BULK_SUBCATEGORIES = {"user", "timeline", "media", "tweets", "likes", "search", "hashtag",
                      "board", "followers", "following", "feed", "channel", "subreddit", "tag"}

# Query params Twitter/X appends to pick a rendition; strip so one image dedupes to one URL.
_IMG_EXT = {"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "tiff", "heic"}


def available() -> bool:
    try:
        import gallery_dl  # noqa: F401

        return True
    except ImportError:
        return False


def _apply_auth(cookies_browser: str | None, cookies_file: str | None, proxy: str | None) -> None:
    from gallery_dl import config

    config.set((), "quiet", True)
    config.set((), "verbose", False)
    config.set(("output",), "mode", "null")
    if cookies_browser:
        from ..ytdlp import browser_spec

        browser, profile, keyring, _ = browser_spec(cookies_browser)
        spec: list[Any] = [browser]
        if profile:
            spec.append(profile)
        if keyring:
            spec += [None, keyring]
        config.set(("extractor",), "cookies", spec)
    elif cookies_file:
        config.set(("extractor",), "cookies", cookies_file)
    if proxy:
        config.set(("extractor",), "proxy", proxy)


def supported(url: str) -> str | None:
    """gallery-dl's subcategory for this URL (e.g. 'tweet', 'user'), or None if only the
    generic/direct-link extractor matches — meaning no real extractor, leave it to the chain."""
    if not available():
        return None
    from gallery_dl import extractor

    try:
        ie = extractor.find(url)
    except Exception:
        return None
    if ie is None or ie.category in ("", "directlink", "generic", "recursive", "oauth"):
        return None
    return ie.subcategory or ie.category


def probe(url: str, cookies_browser: str | None = None, cookies_file: str | None = None,
          proxy: str | None = None, cap: int = 80) -> Candidate | None:
    """Simulate the extraction: list up to `cap` media items without downloading."""
    sub = supported(url)
    if sub is None:
        return None
    from gallery_dl import config, job

    config.clear()
    _apply_auth(cookies_browser, cookies_file, proxy)
    config.set(("extractor",), "image-range", f"1-{cap + 1}")  # +1 so we can tell "more than cap"

    j = job.DataJob(url, file=io.StringIO())  # DataJob prints its data to stdout otherwise
    try:
        j.run()
    except Exception:
        return None

    items, videos, title, err = [], 0, None, None
    for msg in j.data:
        if not isinstance(msg, (list, tuple)) or not msg:
            continue
        if msg[0] == job.Message.Url and len(msg) >= 2:
            kw = msg[2] if len(msg) > 2 else {}
            items.append(msg[1])
            ext = str(kw.get("extension", "")).lower()
            if ext and ext not in _IMG_EXT:
                videos += 1
            title = title or _title(kw)
        elif msg[0] == -1:  # gallery-dl error record
            err = (msg[1] or {}).get("message") if len(msg) > 1 and isinstance(msg[1], dict) else "extraction failed"

    if not items:
        return None if err else None
    unbounded = (sub in BULK_SUBCATEGORIES) or len(items) > cap
    count = len(items) if not unbounded else cap  # don't claim an exact count for a feed
    note = f"{count}{'+' if unbounded else ''} item(s)"
    if videos:
        note += f" ({videos} video)"
    if unbounded:
        note += " — feed/profile, use --gallery-all"
    return Candidate(url=url, kind="gallery", source="gallery", page_url=url, title=title, score=150,
                     note=note, info={"subcategory": sub, "count": count, "videos": videos,
                                      "unbounded": unbounded})


def _title(kw: dict) -> str | None:
    for k in ("content", "title", "description", "tweet_id", "post_id", "id"):
        v = kw.get(k)
        if v:
            s = str(v).strip().replace("\n", " ")
            return (s[:80] + "…") if len(s) > 80 else s
    author = kw.get("author") or {}
    return author.get("name") if isinstance(author, dict) else None


def download(cand: Candidate, outdir: Path, cookies_browser: str | None = None, cookies_file: str | None = None,
             proxy: str | None = None, gallery_all: bool = False, cap: int = 80,
             log: Callable[[str], None] = lambda m: None) -> list[Path]:
    """Download the post's media into outdir. gallery-dl writes to a fresh temp dir with its
    own naming; we then move everything out, so siphon controls the final location + dedup."""
    from gallery_dl import config, job

    info = cand.info or {}
    if info.get("unbounded") and not gallery_all:
        raise RuntimeError(f"{cand.note}: this is a feed/profile; re-run with --gallery-all to fetch it")

    work = Path(tempfile.mkdtemp(prefix=".siphon-gdl-", dir=outdir))
    config.clear()
    _apply_auth(cookies_browser, cookies_file, proxy)
    config.set(("extractor",), "base-directory", str(work))
    config.set(("extractor",), "directory", [])  # flat: no per-site subfolders
    if not gallery_all:
        config.set(("extractor",), "image-range", f"1-{cap}")
    try:
        rc = job.DownloadJob(cand.url).run()
        if rc not in (0, None):
            log(f"gallery-dl exited with code {rc}")
        written = sorted(p for p in work.rglob("*") if p.is_file() and not p.name.endswith(".part"))
        out = []
        for src in written:
            dest = _unique(outdir / src.name)
            shutil.move(str(src), dest)
            out.append(dest)
        if not out:
            raise RuntimeError("gallery-dl wrote no files (auth needed? try --cookies-from-browser)")
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 10000):
        p = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not p.exists():
            return p
    raise FileExistsError(path)
