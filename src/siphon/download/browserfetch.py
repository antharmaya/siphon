"""Last-resort downloader: let Chrome itself make the requests.

For CDNs that serve only a real browser — TLS fingerprint, client hints (sec-ch-ua),
cf_clearance cookies bound to both. Replaying URL + headers from Python cannot pass
those checks; Chrome passes them by being Chrome. Same machine and IP as the sniffer,
so "IP-locked" URLs are a non-issue here; browser-locked ones are the point.

Each request is replayed the way the browser originally made it:
  hls   the player fetched manifests/segments with XHR/fetch, so CORS already allows
        that frame's origin. siphon fetches the same URLs from inside that frame,
        decrypts AES-128 if present, and remuxes. (Encrypted-by-DRM HLS is refused upstream.)
  file  a <video src> load is exempt from CORS but a script fetch is not, so instead the
        URL is opened in a hidden tab with the page as Referer, and Chrome's response body
        is streamed out over CDP (Fetch.takeResponseBodyAsStream).

No URL on a more local network than the page is fetched (netcheck.inward), redirects included.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

from .. import netcheck, tools
from ..models import Candidate
from ..resolve.cdp import Browser, Session, find_chrome, setup_target, ua_override

FETCH_B64_JS = r"""
async (u, range) => {
  // Mirror the player: hls.js fetches cross-origin segments WITHOUT credentials, and CDNs
  // answer "Access-Control-Allow-Origin: *", which CORS rejects for credentialed requests.
  // So default first; credentialed only as a retry (CDNs that need cookies cross-origin).
  const opts = range ? { headers: { Range: range } } : {};
  let r;
  try { r = await fetch(u, opts); } catch (e) { r = null; }
  if (!r || r.status === 401 || r.status === 403) {
    try { r = await fetch(u, { ...opts, credentials: 'include' }); } catch (e) { return { err: String(e) }; }
  }
  if (!r.ok) return { err: 'HTTP ' + r.status };
  const b = new Uint8Array(await r.arrayBuffer());
  let s = '';
  for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return { url: r.url, data: btoa(s) };
}
"""

Progress = Callable[[int, "int | None"], None]


class BrowserFetchError(Exception):
    pass


def _guard(url: str, page: str) -> None:
    if netcheck.inward(url, page):
        raise BrowserFetchError(f"refused {urlsplit(url).hostname}: it is on a more local network than the page")


# ---------------------------------------------------------------- HLS parsing

def _attrs(line: str) -> dict[str, str]:
    return {k.upper(): v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(":", 1)[1])}


def pick_variant(master: str, base: str) -> tuple[str, str | None]:
    """Highest-bandwidth variant, plus the URI of its default audio rendition if separate."""
    best, best_bw, audio_group = None, -1, None
    lines = master.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF"):
            a = _attrs(ln)
            bw = int(a.get("BANDWIDTH", "0") or 0)
            uri = next((x.strip() for x in lines[i + 1:] if x.strip() and not x.startswith("#")), None)
            if uri and bw > best_bw:
                best, best_bw, audio_group = urljoin(base, uri), bw, a.get("AUDIO")
    audio = None
    if audio_group:
        for ln in lines:
            if ln.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in ln:
                a = _attrs(ln)
                if a.get("GROUP-ID") == audio_group and a.get("URI") and (a.get("DEFAULT") == "YES" or audio is None):
                    audio = urljoin(base, a["URI"])
    if not best:
        raise BrowserFetchError("master playlist has no variants")
    return best, audio


def parse_media(playlist: str, base: str) -> dict:
    """Segments with their AES-128 key URI + IV and byte range, the init segment, and whether it's live."""
    seq = 0
    key = None  # (uri, iv_hex | None) or None
    init = None
    segs = []
    brange, ends = None, {}  # a BYTERANGE without @offset starts where that URI's previous range ended

    def span(url: str, spec: str) -> tuple[int, int]:
        n, _, o = spec.partition("@")
        start = int(o) if o else ends.get(url, 0)
        ends[url] = start + int(n)
        return start, start + int(n) - 1

    for ln in playlist.splitlines():
        ln = ln.strip()
        if ln.startswith("#EXT-X-BYTERANGE"):
            brange = ln.split(":", 1)[1]
        elif ln.startswith("#EXT-X-MEDIA-SEQUENCE"):
            seq = int(ln.split(":", 1)[1])
        elif ln.startswith("#EXT-X-KEY"):
            a = _attrs(ln)
            method = a.get("METHOD", "NONE")
            if method == "NONE":
                key = None
            elif method == "AES-128":
                key = (urljoin(base, a["URI"]), a.get("IV"))
            else:
                raise BrowserFetchError(f"HLS encryption {method} is DRM; not decrypted")
        elif ln.startswith("#EXT-X-MAP"):
            a = _attrs(ln)
            u = urljoin(base, a["URI"])
            init = {"url": u, "range": span(u, a["BYTERANGE"]) if a.get("BYTERANGE") else None}
        elif ln and not ln.startswith("#"):
            u = urljoin(base, ln)
            segs.append({"url": u, "seq": seq, "key": key, "range": span(u, brange) if brange else None})
            brange = None
            seq += 1
    return {"segments": segs, "init": init, "live": "#EXT-X-ENDLIST" not in playlist}


def aes128_decrypt(data: bytes, key: bytes, iv_hex: str | None, seq: int) -> bytes:
    from Cryptodome.Cipher import AES

    iv = bytes.fromhex(iv_hex[2:] if iv_hex and iv_hex.lower().startswith("0x") else (iv_hex or "")) \
        if iv_hex else seq.to_bytes(16, "big")
    out = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    pad = out[-1] if out else 0
    return out[:-pad] if 0 < pad <= 16 else out


# ------------------------------------------------------------- the browser

class _Fetcher:
    def __init__(self, browser: Browser, page: Session, page_url: str):
        self.browser = browser
        self.page = page
        self.page_url = page_url

    def _context_for(self, origin: str | None) -> list[Session | int]:
        """The frame whose origin matches the original requester: CORS was granted to it."""
        sessions = [s for s in self.browser.sessions.values() if s.type in ("page", "iframe")]
        if origin:
            for s in sessions:
                for ctx in s.contexts.values():
                    if s.origins.get(ctx) == origin:
                        return [s, ctx]
        return [self.page, None]

    async def get(self, url: str, origin: str | None, rng: tuple[int, int] | None = None) -> bytes:
        _guard(url, self.page_url)
        sess, ctx = self._context_for(origin)
        hdr = f"bytes={rng[0]}-{rng[1]}" if rng else None
        params = {"expression": f"({FETCH_B64_JS.strip()})({json.dumps(url)}, {json.dumps(hdr)})",
                  "awaitPromise": True, "returnByValue": True}
        if ctx:
            params["contextId"] = ctx
        r = await sess.send("Runtime.evaluate", params, 120)
        v = r.get("result", {}).get("value") or {}
        if "data" not in v:
            raise BrowserFetchError(f"in-page fetch of {url[:80]} failed: "
                                    f"{v.get('err') or r.get('exceptionDetails', {}).get('text')}")
        _guard(v.get("url") or url, self.page_url)  # fetch() follows redirects; vet where it ended up
        data = base64.b64decode(v["data"])
        if rng and len(data) != rng[1] - rng[0] + 1:
            if len(data) <= rng[1]:
                raise BrowserFetchError(f"short byte range from {url[:80]}: {len(data)} bytes")
            data = data[rng[0]:rng[1] + 1]  # the server ignored Range and sent the whole resource
        return data


async def _open(page_url: str, *, profile: Path | None, proxy: str | None, cookies: list[dict]) -> tuple[Browser, Session]:
    exe = find_chrome()
    if not exe:
        raise BrowserFetchError("no Chrome found")
    b = await Browser.launch(exe, headless=True, profile=profile, proxy=proxy,
                             args=["--autoplay-policy=no-user-gesture-required", "--mute-audio",
                                   "--disable-blink-features=AutomationControlled"])
    try:
        return b, await _visit(b, page_url, cookies)
    except BaseException:
        await b.close()  # the caller only gets a browser to close if this returns
        raise


async def _visit(b: Browser, page_url: str, cookies: list[dict]) -> Session:
    version = await b.send("Browser.getVersion", timeout=10)
    ua = ua_override(version.get("product", ""))
    ready = asyncio.Event()
    holder: dict = {}

    async def on_attached(p: dict, _parent) -> None:
        ti = p.get("targetInfo", {})
        s = Session(b, p["sessionId"], ti.get("targetId", ""), ti.get("type", ""), ti.get("url", ""))
        s.origins = {}
        b.sessions[s.id] = s
        if s.type not in ("page", "iframe"):
            try:
                await s.send("Runtime.runIfWaitingForDebugger", timeout=5)
            except Exception:
                pass
            return
        await setup_target(s, init_scripts=[], bindings=[], blocked=[], user_agent=ua)
        if s.type == "page" and "page" not in holder:
            holder["page"] = s
            ready.set()

    def on_ctx(p: dict, s: Session | None) -> None:
        c = p.get("context", {})
        if s is not None and c.get("auxData", {}).get("isDefault"):
            s.origins = getattr(s, "origins", {})
            s.origins[c["id"]] = c.get("origin", "")

    b.on("Target.attachedToTarget", on_attached)
    b.on("Runtime.executionContextCreated", on_ctx)
    if cookies:
        try:
            await b.send("Storage.setCookies", {"cookies": cookies}, timeout=10)
        except Exception:
            pass
    await b.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
    await b.send("Target.createTarget", {"url": "about:blank"})
    await asyncio.wait_for(ready.wait(), 15)
    page: Session = holder["page"]
    loaded = asyncio.Event()
    b.on("Page.domContentEventFired", lambda p, s: loaded.set() if s is page else None)
    # Visit the page first: that is what earns the cookies / challenge clearance the CDN checks.
    await page.send("Page.navigate", {"url": page_url}, 45)
    try:
        await asyncio.wait_for(loaded.wait(), 30)
    except asyncio.TimeoutError:
        pass
    await asyncio.sleep(1.5)
    return page


async def _hls(fetch: _Fetcher, c: Candidate, workdir: Path, log: Callable[[str], None],
               progress: Progress | None) -> list[tuple[Path, str]]:
    origin = c.headers.get("origin") or _origin(c.headers.get("referer"))
    text = (await fetch.get(c.url, origin)).decode("utf-8", "replace")
    tracks = [(c.url, text)]
    if "#EXT-X-STREAM-INF" in text:
        video_url, audio_url = pick_variant(text, c.url)
        tracks = [(video_url, (await fetch.get(video_url, origin)).decode("utf-8", "replace"))]
        if audio_url:
            tracks.append((audio_url, (await fetch.get(audio_url, origin)).decode("utf-8", "replace")))
    medias = [parse_media(ptext, purl) for purl, ptext in tracks]
    if any(m["live"] for m in medias):
        raise BrowserFetchError("live HLS isn't supported by the browser fallback")
    if not all(m["segments"] for m in medias):
        raise BrowserFetchError("playlist has no segments")
    total = sum(len(m["segments"]) for m in medias)
    keys: dict[str, asyncio.Task] = {}
    sem = asyncio.Semaphore(6)
    done = [0, 0]  # segments, bytes

    async def key(uri: str) -> bytes:
        # One request per key: segments race for it, and some key servers issue single-use tokens.
        if uri not in keys:
            keys[uri] = asyncio.ensure_future(fetch.get(uri, origin))
        return await keys[uri]

    async def grab(s: dict, path: Path) -> None:
        async with sem:
            data = await fetch.get(s["url"], origin, s["range"])
        if s["key"]:
            kuri, iv = s["key"]
            data = aes128_decrypt(data, await key(kuri), iv, s["seq"])
        path.write_bytes(data)  # to disk now: a feature-length video doesn't fit in memory twice
        done[0] += 1
        done[1] += len(data)
        if progress:
            progress(done[1], done[1] * total // done[0])

    pieces = [[workdir / f"t{n}-{i:06d}" for i in range(len(m["segments"]))] for n, m in enumerate(medias)]
    for n, m in enumerate(medias):
        log(f"browser fetch: {len(m['segments'])} segments (track {n + 1}/{len(medias)})")
    try:
        async with asyncio.TaskGroup() as tg:  # the first failure cancels the rest
            for m, ps in zip(medias, pieces):
                for s, p in zip(m["segments"], ps):
                    tg.create_task(grab(s, p))
    except ExceptionGroup as eg:
        raise eg.exceptions[0] from None
    out = []
    for n, (m, ps) in enumerate(zip(medias, pieces)):
        head = ps[0].read_bytes()[:8]
        ext = "mp4" if m["init"] or head[4:8] in (b"ftyp", b"styp", b"moof") else "ts"
        path = workdir / f"track{n}.{ext}"
        with open(path, "wb") as f:
            if m["init"]:
                f.write(await fetch.get(m["init"]["url"], origin, m["init"]["range"]))
            for p in ps:
                with open(p, "rb") as src:
                    shutil.copyfileobj(src, f)
                p.unlink()
        out.append((path, ext))
    return out


async def _file(b: Browser, c: Candidate, page_url: str, workdir: Path, log: Callable[[str], None],
                progress: Progress | None) -> list[tuple[Path, str]]:
    t = await b.send("Target.createTarget", {"url": "about:blank"})
    sess = next((s for s in b.sessions.values() if s.target_id == t["targetId"]), None)
    for _ in range(50):
        if sess:
            break
        await asyncio.sleep(0.1)
        sess = next((s for s in b.sessions.values() if s.target_id == t["targetId"]), None)
    if not sess:
        raise BrowserFetchError("couldn't open a tab")
    paused: asyncio.Queue = asyncio.Queue()
    b.on("Fetch.requestPaused", lambda p, s: paused.put_nowait(p) if s is sess else None)
    # Request stage: vet every hop of a redirect chain before Chrome sends it. Response stage: the body.
    await sess.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "resourceType": "Document", "requestStage": st}
                                                  for st in ("Request", "Response")]})
    # Don't await Page.navigate: it only returns once the navigation commits, which can't happen
    # while we hold its response paused. Fire it, then wait for the interception instead.
    b.spawn(sess.send("Page.navigate", {"url": c.url, "referrer": c.headers.get("referer") or c.page_url}, 60))
    for _ in range(20):
        p = await asyncio.wait_for(paused.get(), 45)
        rid = p["requestId"]
        if "responseStatusCode" not in p and "responseErrorReason" not in p:
            if netcheck.inward(p["request"]["url"], page_url):
                await sess.send("Fetch.failRequest", {"requestId": rid, "errorReason": "BlockedByClient"})
                _guard(p["request"]["url"], page_url)
            await sess.send("Fetch.continueRequest", {"requestId": rid})
            continue
        if "responseErrorReason" in p:
            raise BrowserFetchError(f"Chrome couldn't load it: {p['responseErrorReason']}")
        if 300 <= p["responseStatusCode"] < 400:
            await sess.send("Fetch.continueRequest", {"requestId": rid})  # Chrome follows; the next hop pauses above
            continue
        break
    else:
        raise BrowserFetchError("too many redirects")
    status = p["responseStatusCode"]
    hdrs = {h["name"].lower(): h["value"] for h in p.get("responseHeaders", [])}
    if status >= 400 or hdrs.get("content-type", "").lower().startswith("text/html"):
        await sess.send("Fetch.failRequest", {"requestId": rid, "errorReason": "Aborted"})
        raise BrowserFetchError(f"even Chrome got HTTP {status}" if status >= 400
                                else "got a web page, not media (a login or challenge page?)")
    cl = hdrs.get("content-length", "")
    total = int(cl) if cl.isdigit() and "content-encoding" not in hdrs else None  # the stream is decoded
    stream = (await sess.send("Fetch.takeResponseBodyAsStream", {"requestId": rid}))["stream"]
    path = workdir / "file.bin"
    got, last = 0, 0
    with open(path, "wb") as f:
        while True:
            chunk = await sess.send("IO.read", {"handle": stream, "size": 4 << 20}, timeout=120)
            data = base64.b64decode(chunk["data"]) if chunk.get("base64Encoded") else chunk["data"].encode()
            f.write(data)
            got += len(data)
            if progress:
                progress(got, total)
            if got - last > 32 << 20:
                last = got
                log(f"browser fetch: {got / 1e6:.0f} MB" + (f" of {total / 1e6:.0f} MB" if total else ""))
            if chunk.get("eof"):
                break
    await sess.send("IO.close", {"handle": stream})
    if total and got != total:
        raise BrowserFetchError(f"got {got} of {total} bytes")
    return [(path, "file")]


def _origin(url: str | None) -> str | None:
    if not url:
        return None
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else None


async def _run(c: Candidate, dest: Path, *, profile, proxy, cookies, log, progress) -> Path:
    if c.kind not in ("file", "hls"):
        raise BrowserFetchError(f"browser fallback handles file/hls, not {c.kind}")
    page_url = c.page_url or c.url
    _guard(c.url, page_url)
    # Inside the download folder so the final rename is atomic; every exit path removes it.
    workdir = Path(tempfile.mkdtemp(prefix=".siphon-bf-", dir=dest.parent))
    try:
        b, page = await _open(page_url, profile=profile, proxy=proxy, cookies=cookies)
        try:
            parts = await (_hls(_Fetcher(b, page, page_url), c, workdir, log, progress) if c.kind == "hls"
                           else _file(b, c, page_url, workdir, log, progress))
        finally:
            await b.close()
        if c.kind == "file":
            parts[0][0].replace(dest)
            return dest
        cmd = [tools.ffmpeg() or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for p, _ in parts:
            cmd += ["-i", str(p)]
        for i in range(len(parts)):
            cmd += ["-map", str(i)]
        cmd += ["-c", "copy", "-movflags", "+faststart", str(dest)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            dest.unlink(missing_ok=True)
            raise BrowserFetchError(f"ffmpeg remux failed: {r.stderr.strip()[-200:]}")
        return dest
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def browser_fetch(c: Candidate, dest: Path, *, profile: Path | None = None, proxy: str | None = None,
                  cookies: list[dict] | None = None, log: Callable[[str], None] = lambda m: None,
                  progress: Progress | None = None) -> Path:
    return asyncio.run(_run(c, dest, profile=profile, proxy=proxy, cookies=cookies or [], log=log,
                            progress=progress))
