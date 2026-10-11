"""Cheapest resolver: is the URL itself already a media file or manifest?

One request, no browser, no extractor. Runs first because it costs ~100ms and
short-circuits every paste of a raw .mp4/.m3u8 link.
"""

from __future__ import annotations

import re

import httpx

from ..media import classify
from ..models import Candidate

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"


def probe(url: str, headers: dict[str, str] | None = None, proxy: str | None = None, timeout: float = 15) -> Candidate | None:
    hdrs = {"User-Agent": UA, "Range": "bytes=0-1023", **(headers or {})}
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, proxy=proxy) as c:
            with c.stream("GET", url, headers=hdrs) as r:
                if r.status_code >= 400:
                    return None
                ct = r.headers.get("content-type", "")
                kind = classify(str(r.url), ct)
                if kind is None:
                    return None
                head = b""
                for chunk in r.iter_bytes(1024):
                    head += chunk
                    if len(head) >= 1024:
                        break
                size = None
                m = re.search(r"/(\d+)$", r.headers.get("content-range", ""))
                if m:
                    size = int(m.group(1))
                elif r.status_code == 200 and r.headers.get("content-length", "").isdigit():
                    size = int(r.headers["content-length"])
                if kind == "file" and head.lstrip()[:1] == b"<":
                    return None  # an HTML error page with a .mp4 name
                is_master = kind == "hls" and b"#EXT-X-STREAM-INF" in head
                return Candidate(url=str(r.url), kind=kind, source="direct", page_url=url,
                                 mime=ct.split(";")[0] or None, size=size, is_master=is_master,
                                 headers={k: v for k, v in (headers or {}).items()})
    except (httpx.HTTPError, ImportError, ValueError):
        return None
