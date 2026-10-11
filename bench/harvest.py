"""Harvest fresh, real video links from each platform's own public feeds.

Hand-typed "trending" URLs rot within days and are often guesses. This pulls live
ones so the platform matrix can be re-run against whatever is current.

    uv run python bench/harvest.py > bench/links.tsv
"""

from __future__ import annotations

import re
import sys

import httpx
import yt_dlp

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
http = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, follow_redirects=True, timeout=20)


def flat(url: str, n: int = 3) -> list[str]:
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": n,
                           "js_runtimes": {"deno": {}, "node": {}}}) as y:
        info = y.extract_info(url, download=False)
    return [e.get("url") or e.get("webpage_url") for e in (info.get("entries") or [])][:n]


def youtube():
    # /feed/trending was removed by YouTube (redirects home); a top channel's latest uploads instead.
    return flat("https://www.youtube.com/@MrBeast/videos")


def youtube_shorts():
    return flat("https://www.youtube.com/@MrBeast/shorts")


def dailymotion():
    j = http.get("https://api.dailymotion.com/videos?sort=trending&limit=3&fields=url").json()
    return [v["url"] for v in j["list"]]


def vimeo():
    return flat("https://vimeo.com/channels/staffpicks")


def twitch():
    return flat("https://www.twitch.tv/kaicenat/clips?filter=clips&range=7d")


def _rss(url: str) -> str:
    # Reddit's anonymous .json now returns HTML; the RSS feed is still public.
    return http.get(url).text.replace("&amp;", "&")


def reddit():
    xml = _rss("https://www.reddit.com/r/nextfuckinglevel/top/.rss?t=day&limit=25")
    out = []
    for m in re.finditer(r"<link href=\"(https://www\.reddit\.com/r/[^\"]+/comments/[^\"]+)\"", xml):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out[:3]


def streamable():
    out = []
    for sub in ("nba", "hockey", "soccer", "formula1", "MMA"):
        for m in re.finditer(r"https://streamable\.com/[a-z0-9]{4,}", _rss(f"https://www.reddit.com/r/{sub}/new/.rss?limit=100")):
            if m.group(0) not in out:
                out.append(m.group(0))
    return out[:3]


def generic():
    # Ordinary sites with their own players: the case the sniffer exists for.
    return ["https://www.nasa.gov/video-detail/jsc2026m000086-artemisiiflightday10highlights-nolowerthirdsmedium/",
            "https://www.nhl.com/video/t-317756370"]


def bilibili():
    j = http.get("https://api.bilibili.com/x/web-interface/popular?ps=3&pn=1").json()
    return [f"https://www.bilibili.com/video/{v['bvid']}" for v in j["data"]["list"]][:3]


def rumble():
    html = http.get("https://rumble.com/videos?sort=views&date=this-week").text
    seen = []
    for m in re.finditer(r'href="(/v[a-z0-9]+-[^"]+\.html)', html):
        u = "https://rumble.com" + m.group(1).split("?")[0]
        if "live" in u:
            continue  # 24/7 live streams never finish; live is tested separately
        if u not in seen:
            seen.append(u)
    return seen[:3]


def anchors(page_url: str, pattern: str, n: int = 3, wait: float = 6.0) -> list[str]:
    """Open the page in the system Chrome (siphon's CDP client) and collect links matching pattern."""
    import asyncio

    from siphon.resolve.cdp import Browser, Session, find_chrome

    async def run():
        b = await Browser.launch(find_chrome(), headless=True, profile=None,
                                 args=["--disable-blink-features=AutomationControlled"])
        try:
            t = await b.send("Target.createTarget", {"url": "about:blank"})
            a = await b.send("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True})
            page = Session(b, a["sessionId"], t["targetId"], "page")
            await page.send("Network.setUserAgentOverride", {"userAgent": UA})
            await page.send("Page.navigate", {"url": page_url}, timeout=45)
            await asyncio.sleep(wait)
            await page.send("Runtime.evaluate", {"expression": "window.scrollBy(0, 2000)"})
            await asyncio.sleep(1.5)
            r = await page.send("Runtime.evaluate", {"expression": "[...document.querySelectorAll('a[href]')].map(e => e.href)",
                                                     "returnByValue": True})
            return r.get("result", {}).get("value") or []
        finally:
            await b.close()

    out = []
    for h in asyncio.run(run()):
        h = h.split("?")[0]
        if re.search(pattern, h) and h not in out:
            out.append(h)
    return out[:n]


def tiktok():
    return anchors("https://www.tiktok.com/@khaby.lame", r"tiktok\.com/@[^/]+/video/\d+")


def kick():
    return anchors("https://kick.com/browse/clips", r"kick\.com/[^/]+/clips/clip_\w+") or \
        anchors("https://kick.com/xqc/clips", r"kick\.com/[^/]+/clips/clip_\w+")


def instagram():
    return anchors("https://www.instagram.com/natgeo/reels/", r"instagram\.com/(natgeo/)?reels?/[\w-]+/?$")


def facebook():
    return anchors("https://www.facebook.com/watch/", r"facebook\.com/(watch/\?v=|reel/|[^/]+/videos/)\d+")


def twitter():
    # /media needs a login; the profile timeline shows recent posts logged-out. Not every post has video.
    return anchors("https://x.com/NASA", r"x\.com/NASA/status/\d+$", n=5, wait=8)


def pinterest():
    return anchors("https://www.pinterest.com/search/videos/?q=cooking", r"pinterest\.[a-z.]+/pin/\d+")


SOURCES = {f.__name__: f for f in (youtube, youtube_shorts, tiktok, instagram, facebook, twitter, dailymotion, vimeo,
                                   twitch, reddit, streamable, bilibili, rumble, kick, pinterest, generic)}

if __name__ == "__main__":
    for name, fn in SOURCES.items():
        try:
            links = [u for u in fn() if u]
            for u in links:
                print(f"{name}\t{u}")
            print(f"# {name}: {len(links)}", file=sys.stderr)
        except Exception as e:
            print(f"# {name}: FAILED {type(e).__name__}: {str(e)[:140]}", file=sys.stderr)
