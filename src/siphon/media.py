"""Pure functions: is this URL/response media, what kind, is it an ad, a segment?

Kept free of I/O so the sniffer's judgement calls are unit-testable without a browser.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Kind

FILE_EXTS = {
    "mp4", "m4v", "webm", "mkv", "mov", "flv", "avi", "wmv", "3gp", "ogv",
    "mp3", "m4a", "aac", "ogg", "opus", "wav", "flac",
}
SEGMENT_EXTS = {"ts", "m4s", "m4f", "cmfv", "cmfa", "aac", "vtt", "webvtt"}
SEGMENT_HINTS = re.compile(r"(seg(ment)?[-_]?\d+|frag(ment)?[-_\(]?\d+|chunk[-_]?\d+|/range/|init\.mp4|\binit[-_.])", re.I)
# Query params that slice one progressive file into byte ranges (FB/IG, Vimeo CDN, etc).
RANGE_PARAMS = {"bytestart", "byteend", "range", "_nc_rid"}

AD_HOSTS = (
    "doubleclick.net", "googlesyndication.com", "googleadservices.com", "imasdk.googleapis.com",
    "adservice.google.", "moatads.com", "adnxs.com", "criteo.", "taboola.com", "outbrain.com",
    "springserve.com", "spotxchange.com", "spotx.tv", "teads.tv", "amazon-adsystem.com",
    "adsrvr.org", "pubmatic.com", "rubiconproject.com", "openx.net", "smartadserver.com",
    "innovid.com", "serving-sys.com", "flashtalking.com", "aniview.com", "vidoomy.com",
    "exoclick.com", "trafficjunky.", "juicyads.com", "popads.net", "propellerads.", "a-ads.com",
    "adsterra.", "hilltopads.", "clickadu.", "tsyndicate.com", "magsrv.com", "realsrv.com",
)
# IAB display sizes in a path (…/300x250, banner_728x90.mp4) are video *ad* creatives.
# Ad-serving path shapes (…/ads/creatives/x.mp4, /vast/, /preroll/): a stronger, more general
# tell than any host list, which can never be complete.
AD_PATH = re.compile(r"/(ads?|adserver|adserve|creatives?|preroll|midroll|vast|vpaid)/", re.I)
IAB_SIZE = re.compile(r"(?<!\d)(300x250|728x90|970x250|160x600|300x600|320x50|468x60|970x90|336x280)(?!\d)")


def host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_ad(url: str) -> bool:
    h = host(url)
    path = urlsplit(url).path
    return any(a in h for a in AD_HOSTS) or bool(IAB_SIZE.search(path) or AD_PATH.search(path))


def ext_of(url: str) -> str:
    path = urlsplit(url).path.lower()
    last = path.rsplit("/", 1)[-1]
    return last.rsplit(".", 1)[-1] if "." in last else ""


def classify(url: str, content_type: str | None = None) -> Kind | None:
    """Return the media kind for a URL (+ optional Content-Type), or None if not media."""
    ct = (content_type or "").split(";")[0].strip().lower()
    ext = ext_of(url)
    low = url.lower()
    if "mpegurl" in ct or ext == "m3u8" or ".m3u8?" in low:
        return "hls"
    if "dash+xml" in ct or ext == "mpd" or ".mpd?" in low:
        return "dash"
    if url.startswith(("blob:", "data:")):
        return None
    if ct.startswith(("video/", "audio/")) or ext in FILE_EXTS:
        # A video/mp2t or .m4s response is a piece of a stream, not a file.
        if ct in ("video/mp2t", "video/iso.segment") or ext in SEGMENT_EXTS:
            return None
        return "file"
    if ct == "application/octet-stream" and ext in FILE_EXTS:
        return "file"
    return None


def is_segment(url: str, content_type: str | None = None) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return (
        ext_of(url) in SEGMENT_EXTS
        or ct in ("video/mp2t", "video/iso.segment")
        or bool(SEGMENT_HINTS.search(urlsplit(url).path))
    )


def canonical(url: str) -> str:
    """Strip byte-range query params so 40 range requests for one mp4 dedupe to one URL."""
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in RANGE_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))


_BAD = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')


def safe_filename(name: str, max_bytes: int = 180) -> str:
    name = _BAD.sub(" ", name).strip(" .")
    name = re.sub(r"\s+", " ", name) or "video"
    b = name.encode()[:max_bytes]
    return b.decode(errors="ignore").strip() or "video"


HLS_DRM = re.compile(r"#EXT-X-(SESSION-)?KEY:[^\n]*METHOD=SAMPLE-AES[^\n]*KEYFORMAT=\"(?!identity)", re.I)
HLS_FAIRPLAY = re.compile(r"KEYFORMAT=\"com\.apple\.streamingkeydelivery\"|skd://", re.I)
DASH_DRM = re.compile(r"edef8ba9-79d6-4ace-a3c8-27dcd51d21ed|9a04f079-9840-4286-ab92-e65be0885f95|<cenc:pssh", re.I)


def manifest_drm(text: str, kind: Kind) -> str | None:
    """Detect DRM in a manifest body. Plain AES-128 HLS is NOT DRM and returns None."""
    if kind == "hls":
        if HLS_FAIRPLAY.search(text):
            return "fairplay"
        if HLS_DRM.search(text):
            return "widevine/playready"
    elif kind == "dash" and DASH_DRM.search(text):
        if "edef8ba9" in text.lower():
            return "widevine"
        return "playready"
    return None
