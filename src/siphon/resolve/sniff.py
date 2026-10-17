"""Network sniffer + in-page recorder: load the page in a real browser and watch.

This is what IDM's browser extension and Soul Browser do. It works on pages no
extractor knows about, because every player — however obfuscated its JS — must
eventually fetch a manifest or a file, or feed bytes to a <video> through a blob:.

Three sources feed one candidate pool, best first:
  network  every request/response in every frame (iframes included), classified by
           URL + Content-Type. Manifests are read to tell master from rendition and
           to spot DRM. Cleanest result: the original, full-quality stream.
  capture  blob: videos. MediaSource bytes recorded at SourceBuffer.appendBuffer,
           plus media Blobs grabbed at URL.createObjectURL. Used when the network
           pass found nothing usable; needs a play-through (see capture.py).
  dom      <video>/<source> src, og:video, media URLs embedded in HTML/JSON.
           Lowest confidence: nothing proves the browser could fetch them.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from ..media import canonical, classify, is_ad, is_segment, manifest_drm
from ..models import Candidate
from .capture import CaptureSink
from .cdp import Browser, Session, find_chrome, setup_target, ua_override

PROFILE_DIR = Path(os.environ.get("SIPHON_PROFILE", Path.home() / ".local/share/siphon/profile"))

KEEP_HEADERS = {"referer", "origin", "user-agent", "cookie", "authorization", "x-requested-with"}

# Runs in every frame before page scripts.
INIT_JS = r"""
(() => {
  if (window.__siphonInstalled) return;
  window.__siphonInstalled = true;
  try { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => false }); } catch (e) {}
  // Chrome 141+ plays HLS natively (Chrome 152 here answers "maybe"). Players that see that
  // hand the .m3u8 to <video> directly and skip MediaSource — which would blind the capture
  // fallback. Deny native HLS so they take the hls.js/MSE path. The network pass is unaffected.
  try {
    const cpt = HTMLMediaElement.prototype.canPlayType;
    HTMLMediaElement.prototype.canPlayType = function (t) {
      return /mpegurl/i.test(String(t)) ? '' : cpt.call(this, t);
    };
  } catch (e) {}
  const report = (k, v) => { try { window.__siphonReport && window.__siphonReport(JSON.stringify([k, String(v)])); } catch (e) {} };
  const b64 = (u8) => {
    let s = '';
    for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
    return btoa(s);
  };
  const send = (id, mime, kind, seq, u8) => { try { window.__siphonChunk(JSON.stringify([id, mime, kind, seq, b64(u8)])); } catch (e) {} };
  const rid = () => Math.random().toString(36).slice(2, 10);

  // DRM: report only when a license is actually requested. Players probe key systems
  // at startup even for clear content, so requestMediaKeySystemAccess alone proves nothing.
  if (navigator.requestMediaKeySystemAccess) {
    const orig = navigator.requestMediaKeySystemAccess.bind(navigator);
    navigator.requestMediaKeySystemAccess = async (ks, cfg) => {
      const access = await orig(ks, cfg);
      const create = access.createMediaKeys.bind(access);
      access.createMediaKeys = async () => {
        const mk = await create();
        const cs = mk.createSession.bind(mk);
        mk.createSession = (...a) => {
          const s = cs(...a);
          const gr = s.generateRequest.bind(s);
          s.generateRequest = (...b) => { report('drm', ks); return gr(...b); };
          return s;
        };
        return mk;
      };
      return access;
    };
  }

  // blob: case 1 — MediaSource. Record every byte the player appends.
  if (window.MediaSource && window.SourceBuffer) {
    const addSB = MediaSource.prototype.addSourceBuffer;
    MediaSource.prototype.addSourceBuffer = function (mime) {
      const sb = addSB.call(this, mime);
      try { sb.__sid = rid(); sb.__mime = mime; sb.__seq = 0; report('mse', mime); } catch (e) {}
      return sb;
    };
    const append = SourceBuffer.prototype.appendBuffer;
    SourceBuffer.prototype.appendBuffer = function (data) {
      try {
        if (this.__sid && window.__siphonRecord !== false) {
          const u8 = data instanceof ArrayBuffer ? new Uint8Array(data)
                   : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
          send(this.__sid, this.__mime, 'mse', this.__seq++, u8);
        }
      } catch (e) {}
      return append.call(this, data);
    };
  }

  // blob: case 2 — a whole file held as a Blob. Grab it at creation, before the
  // page can revokeObjectURL() it; after that the bytes are unreachable.
  const createURL = URL.createObjectURL;
  URL.createObjectURL = function (obj) {
    const url = createURL.call(this, obj);
    try {
      if (obj instanceof Blob && !(obj instanceof File && obj.size < 1)) {
        const t = (obj.type || '').toLowerCase();
        if (t.startsWith('video/') || t.startsWith('audio/') || (!t && obj.size > (512 << 10))) {
          const id = rid();
          report('blob', url + ' ' + obj.size + ' ' + t);
          (async () => {
            const step = 4 << 20;
            let seq = 0;
            for (let o = 0; o < obj.size; o += step) {
              const u8 = new Uint8Array(await obj.slice(o, o + step).arrayBuffer());
              send(id, t || 'application/octet-stream', 'blob', seq++, u8);
            }
            try { window.__siphonBlobDone(String(id)); } catch (e) {}
          })();
        }
      }
    } catch (e) {}
    return url;
  };
})();
"""

PLAY_JS = r"""
async () => {
  let n = 0;
  const deadline = Date.now() + 2500;  // bounded here, not with asyncio.wait_for (see _drive)
  for (const v of document.querySelectorAll('video, audio')) {
    if (Date.now() > deadline) break;
    try { v.muted = true; v.preload = 'auto'; await Promise.race([v.play(), new Promise(r => setTimeout(r, 400))]); n++; } catch (e) {}
  }
  const sels = ['.vjs-big-play-button', '.jw-icon-display', '.jw-display-icon-container', '.plyr__control--overlaid',
    '.ytp-large-play-button', '.fp-play', '.mejs__overlay-button', '.shaka-play-button', '.bmpui-ui-hugeplaybacktogglebutton',
    '[class*="play-button" i]', '[class*="playbutton" i]', '[class*="btn-play" i]', '[class*="play-btn" i]',
    'button[aria-label*="play" i]', '[role="button"][aria-label*="play" i]'];
  let clicked = 0;
  for (const s of sels) {
    for (const el of document.querySelectorAll(s)) {
      const r = el.getBoundingClientRect();
      if (r.width > 4 && r.height > 4 && clicked < 3) { try { el.click(); clicked++; } catch (e) {} }
    }
  }
  return n + clicked;
}
"""

# Play every video to the end as fast as Chrome allows (16x). Re-applied each tick
# because players reset playbackRate on quality switches.
FASTPLAY_JS = r"""
(rate) => [...document.querySelectorAll('video, audio')].map(v => {
  try { v.muted = true; if (v.playbackRate !== rate) v.playbackRate = rate; if (v.paused && !v.ended) v.play().catch(() => {}); } catch (e) {}
  return { t: v.currentTime || 0, d: isFinite(v.duration) ? v.duration : null, ended: v.ended, rs: v.readyState };
})
"""

DOM_JS = r"""
() => {
  const out = new Set();
  const add = u => { if (u && typeof u === 'string' && !u.startsWith('blob:') && !u.startsWith('data:')) out.add(u); };
  document.querySelectorAll('video, audio, source').forEach(e => { add(e.src); add(e.currentSrc); });
  document.querySelectorAll('meta[property^="og:video"], meta[name="twitter:player:stream"], meta[itemprop="contentUrl"]')
    .forEach(m => add(m.content));
  const html = document.documentElement.outerHTML;
  const re = /https?:(?:\\?\/){2}[^"'\s<>()]+?\.(?:m3u8|mpd|mp4|webm|m4v|mov|mkv)(?:\?[^"'\s<>()]*)?/gi;
  for (const m of html.matchAll(re)) add(m[0].replace(/\\\//g, '/').replace(/&amp;/g, '&'));
  return [...out];
}
"""

LARGEST_PLAYER_JS = r"""
() => {
  let best = null, area = 0;
  for (const el of document.querySelectorAll('video, iframe, [class*="player" i]')) {
    const r = el.getBoundingClientRect();
    if (r.width * r.height > area && r.width > 120 && r.height > 80) { area = r.width * r.height; best = r; }
  }
  return best && { x: best.x + best.width / 2, y: best.y + best.height / 2 };
}
"""

CaptureMode = Literal["auto", "force", "off"]


@dataclass
class SniffOptions:
    timeout: float = 25.0
    headed: bool = False  # browse mode: you drive, siphon watches
    cookies: list[dict] = field(default_factory=list)  # Playwright cookie dicts
    profile: Path | None = PROFILE_DIR  # None = ephemeral
    proxy: str | None = None
    block_ads: bool = True
    capture: CaptureMode = "auto"  # auto: record blob: video only when the network pass finds nothing
    capture_timeout: float = 1800.0  # cap on a play-through (16x => a 1h video takes ~4 min)
    workdir: Path | None = None  # where captured tracks are written
    log: Callable[[str], None] = lambda msg: None


@dataclass
class SniffResult:
    page_url: str
    title: str | None
    candidates: list[Candidate]
    drm: set[str]
    segments_seen: int
    mse_seen: bool = False
    error: str | None = None
    workdir: Path | None = None



def chrome_path() -> str | None:
    return find_chrome()


def score(c: Candidate) -> float:
    if c.kind == "hls":
        s = 100 if c.is_master else 80
    elif c.kind == "dash":
        s = 95
    elif c.kind == "capture":
        s = 75 if "complete" in c.note else 35
    else:
        s = 50 + (5 * math.log10(c.size) if c.size else 0)
        if c.size is not None and c.size < 300_000:
            s -= 40  # thumbnails-as-video, preview loops, ad stubs
        if (c.mime or "").startswith("audio/"):
            s -= 15
    if c.source == "dom":
        s -= 10
    if c.drm:
        s -= 200
    return s


def _usable(cands: list[Candidate]) -> bool:
    return any(c.source == "sniff" and not c.drm and score(c) >= 45 for c in cands)


def _blocked_patterns() -> list[str]:
    """Ad hosts as Chrome URL-block wildcards. Blocking in Chrome (not via a per-request
    round-trip to Python) keeps ad-heavy pages fast; is_ad() still filters candidates."""
    from ..media import AD_HOSTS

    pats = [f"*{h.rstrip('.')}*" for h in AD_HOSTS]
    return pats + ["*/ads/creatives/*", "*/adserver/*", "*/vast/*", "*/vpaid/*"]


def _expr(fn_src: str, *args) -> str:
    return f"({fn_src.strip()})({', '.join(json.dumps(a) for a in args)})"


class _Tab:
    """The main page plus every frame target that belongs to it."""

    def __init__(self, browser: Browser):
        self.browser = browser
        self.main: Session | None = None

    @property
    def sessions(self) -> list[Session]:
        return [s for s in self.browser.sessions.values() if s.type in ("page", "iframe")]

    async def eval_frames(self, fn_src: str, *args, timeout: float = 3) -> list:
        expr = _expr(fn_src, *args)
        groups = await asyncio.gather(*(s.evaluate_all(expr, timeout) for s in self.sessions))
        return [v for g in groups for v in g]

    async def eval_main(self, fn_src: str, *args, timeout: float = 3):
        if not self.main:
            return None
        try:
            r = await self.main.send("Runtime.evaluate", {"expression": _expr(fn_src, *args), "awaitPromise": True,
                                                          "returnByValue": True}, timeout)
            return r.get("result", {}).get("value")
        except Exception:
            return None

    async def mouse(self, kind: str, x: float = 683, y: float = 400, dy: float = 0) -> None:
        if not self.main:
            return
        try:
            if kind == "wheel":
                await self.main.send("Input.dispatchMouseEvent",
                                     {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": dy}, 2)
            else:
                for t in ("mousePressed", "mouseReleased"):
                    await self.main.send("Input.dispatchMouseEvent",
                                         {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1}, 2)
        except Exception:
            pass

    async def title(self) -> str | None:
        t = await self.eval_main("() => document.title")
        return (t or "").strip() or None


async def sniff(url: str, opts: SniffOptions, on_found: Callable[[Candidate], None] | None = None) -> SniffResult:
    exe = find_chrome()
    if not exe:
        raise RuntimeError("no Chrome/Chromium found (install google-chrome, or set SIPHON_CHROME)")

    found: dict[str, Candidate] = {}
    drm: set[str] = set()
    state = {"segments": 0, "mse": False, "first_hit": None}
    workdir = opts.workdir or Path(tempfile.mkdtemp(prefix="siphon-capture-"))
    sink = CaptureSink(workdir)
    requests: dict[str, dict] = {}  # requestId -> {url, type, frame, headers}
    manifests: dict[str, Candidate] = {}  # requestId -> candidate awaiting its body

    def add(c: Candidate) -> None:
        key = canonical(c.url)
        old = found.get(key)
        if old and old.source != "dom" and c.source == "dom":
            return
        if old and old.size and not c.size:
            c.size = old.size
        c.score = score(c)
        found[key] = c
        if state["first_hit"] is None and c.source == "sniff" and c.score >= 45:
            state["first_hit"] = time.monotonic()
        if on_found and not old:
            on_found(c)

    # ---- page -> python bindings (CDP bindings carry one string) ----------------
    def on_binding(p: dict, s: Session | None) -> None:
        name, payload = p.get("name"), p.get("payload", "")
        try:
            if name == "__siphonChunk":
                tid, mime, kind, seq, b64 = json.loads(payload)
                sink.chunk(tid, mime, kind, seq, b64)
            elif name == "__siphonBlobDone":
                sink.finish(payload)
            elif name == "__siphonReport":
                kind, value = json.loads(payload)
                if kind == "drm":
                    drm.add(value)
                elif kind == "mse":
                    state["mse"] = True
                elif kind == "blob":
                    opts.log(f"blob object created in page: {value.split(' ', 1)[-1]}")
        except Exception:
            pass

    # ---- network ----------------------------------------------------------------
    def on_request(p: dict, s: Session | None) -> None:
        req = p.get("request", {})
        requests[p["requestId"]] = {"url": req.get("url", ""), "type": p.get("type", ""),
                                    "frame": p.get("documentURL") or url, "headers": dict(req.get("headers", {}))}

    def on_request_extra(p: dict, s: Session | None) -> None:
        # The headers Chrome really sent, including Cookie (absent from requestWillBeSent).
        r = requests.setdefault(p["requestId"], {"url": "", "type": "", "frame": url, "headers": {}})
        r["headers"].update(p.get("headers", {}))

    def on_response(p: dict, s: Session | None) -> None:
        resp = p.get("response", {})
        u = resp.get("url", "")
        status = resp.get("status", 0)
        if is_ad(u) or status >= 400 or u.startswith(("data:", "blob:")):
            return
        hdrs = {k.lower(): v for k, v in resp.get("headers", {}).items()}
        ct = hdrs.get("content-type") or resp.get("mimeType")
        kind = classify(u, ct)
        if kind is None and p.get("type") == "Media" and not is_segment(u, ct):
            kind = "file"
        if kind is None or (kind == "file" and is_segment(u, ct)):
            if is_segment(u, ct):
                state["segments"] += 1
            return
        info = requests.get(p["requestId"], {})
        sent = {k.lower(): v for k, v in info.get("headers", {}).items()}
        keep = {k: v for k, v in sent.items() if k in KEEP_HEADERS}
        keep.setdefault("referer", info.get("frame") or url)
        size = None
        cr = hdrs.get("content-range", "")
        if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
            size = int(cr.rsplit("/", 1)[1])
        elif hdrs.get("content-length", "").isdigit() and status == 200:
            size = int(hdrs["content-length"])
        c = Candidate(url=u, kind=kind, source="sniff", page_url=url, mime=(ct or "").split(";")[0] or None,
                      size=size, headers=keep)
        if kind in ("hls", "dash") and s is not None:
            manifests[p["requestId"]] = (c, s)  # judged once its body has arrived
        else:
            add(c)

    async def on_loaded(p: dict, s: Session | None) -> None:
        pending = manifests.pop(p.get("requestId"), None)
        if not pending:
            return
        c, sess = pending
        try:
            r = await sess.send("Network.getResponseBody", {"requestId": p["requestId"]}, 5)
            body = r.get("body", "")
            if r.get("base64Encoded"):
                import base64

                body = base64.b64decode(body).decode("utf-8", "replace")
            body = body[:400_000]
            if c.kind == "hls" and "#EXTM3U" not in body:
                return  # an error page served on a .m3u8 path
            c.is_master = c.kind == "hls" and "#EXT-X-STREAM-INF" in body
            c.drm = manifest_drm(body, c.kind)
        except Exception:
            pass
        add(c)

    title = None
    error = None
    played: dict = {}
    browser = await Browser.launch(
        exe, headless=not opts.headed, profile=opts.profile, proxy=opts.proxy,
        args=["--autoplay-policy=no-user-gesture-required", "--disable-blink-features=AutomationControlled"]
        + (["--mute-audio"] if not opts.headed else []),
    )
    tab = _Tab(browser)
    try:
        version = await browser.send("Browser.getVersion", timeout=10)
        ua_params = ua_override(version.get("product", ""))
        ua = ua_params["userAgent"]
        init = [INIT_JS] + (["window.__siphonRecord = false;"] if opts.capture == "off" else [])
        bindings = ["__siphonReport", "__siphonChunk", "__siphonBlobDone"]
        blocked = _blocked_patterns() if opts.block_ads else []
        main_ready = asyncio.Event()

        async def on_attached(p: dict, parent: Session | None) -> None:
            ti = p.get("targetInfo", {})
            sess = Session(browser, p["sessionId"], ti.get("targetId", ""), ti.get("type", ""), ti.get("url", ""),
                           ti.get("openerId"))
            browser.sessions[sess.id] = sess
            if sess.type not in ("page", "iframe"):
                try:  # workers etc.: nothing to hook, just let them run
                    await sess.send("Runtime.runIfWaitingForDebugger", timeout=5)
                except Exception:
                    pass
                return
            if sess.type == "page" and sess.opener and not opts.headed and tab.main is not None:
                # Headless: an ad popunder. Headed: leave it — login popups, the user's own tabs.
                browser.spawn(browser.send("Target.closeTarget", {"targetId": sess.target_id}, timeout=5))
                return
            await setup_target(sess, init_scripts=init, bindings=bindings, blocked=blocked,
                               user_agent=None if opts.headed else ua_params)
            if sess.type == "page" and tab.main is None:
                tab.main = sess
                main_ready.set()

        def on_detached(p: dict, s: Session | None) -> None:
            browser.sessions.pop(p.get("sessionId", ""), None)

        browser.on("Target.attachedToTarget", on_attached)
        browser.on("Target.detachedFromTarget", on_detached)
        browser.on("Runtime.bindingCalled", on_binding)
        browser.on("Network.requestWillBeSent", on_request)
        browser.on("Network.requestWillBeSentExtraInfo", on_request_extra)
        browser.on("Network.responseReceived", on_response)
        browser.on("Network.loadingFinished", on_loaded)

        if opts.cookies:
            try:
                await browser.send("Storage.setCookies", {"cookies": opts.cookies}, timeout=10)
            except Exception as e:
                opts.log(f"could not set cookies: {e}")
        await browser.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True,
                                                    "flatten": True}, timeout=10)
        # A fresh tab, created after auto-attach is on, so it is hooked before it runs anything.
        await browser.send("Target.createTarget", {"url": "about:blank"}, timeout=10)
        await asyncio.wait_for(main_ready.wait(), 15)
        for s in list(browser.sessions.values()):  # the launch tab: not needed
            if s.type == "page" and s is not tab.main:
                browser.spawn(browser.send("Target.closeTarget", {"targetId": s.target_id}, timeout=5))

        loaded = asyncio.Event()
        browser.on("Page.domContentEventFired", lambda p, s: loaded.set() if s is tab.main else None)
        try:
            nav = await tab.main.send("Page.navigate", {"url": url}, timeout=45)
            if nav.get("errorText"):
                error = f"navigation: {nav['errorText']}"
            else:
                await asyncio.wait_for(loaded.wait(), 45)
        except (asyncio.TimeoutError, Exception) as e:  # slow page: keep whatever loaded
            error = error or f"navigation: {type(e).__name__}"

        if opts.headed:
            opts.log("browse mode: play the video in the window, then close the window when done")
            await browser.wait_closed_by_user()
        else:
            settle = lambda: 1.5 if any(c.is_master or c.kind == "dash"  # noqa: E731
                                        for c in found.values() if c.source == "sniff") else 3.0
            await _drive(tab, opts.timeout, lambda: state["first_hit"], settle)
            title = await tab.title()
            want_capture = opts.capture == "force" or (
                opts.capture == "auto" and not _usable(list(found.values())) and (state["mse"] or sink.total)
            )
            if want_capture and not drm:
                opts.log("no fetchable stream on the network; recording the player's blob: stream (16x play-through)")
                played = await _play_through(tab, sink, opts)
            # DOM URLs are a fallback that gets discarded when the network pass won, so only
            # pay for it when needed — and then across all frames at once.
            if not _usable(list(found.values())):
                for urls in await tab.eval_frames(DOM_JS):
                    for u in urls or []:
                        k = classify(u)
                        if k and not is_ad(u) and not is_segment(u):
                            add(Candidate(url=u, kind=k, source="dom", page_url=url,
                                          headers={"referer": url, "user-agent": ua}))
            for _ in range(8):  # manifest bodies still in flight
                if not manifests:
                    break
                await asyncio.sleep(0.25)
            for _ in range(20):  # in-flight blob streams (async in the page)
                if all(t.done for t in sink.tracks.values() if t.kind == "blob"):
                    break
                await asyncio.sleep(0.25)
    finally:
        await browser.close()

    network_won = _usable(list(found.values()))
    for c in _capture_candidates(sink, url, title, played, drm):
        # A partial MSE recording is noise when the network pass found the real stream.
        if network_won and c.url.startswith("mse-capture://") and "complete" not in c.note:
            continue
        add(c)
    if network_won:
        for k in [k for k, c in found.items() if c.source == "dom"]:
            del found[k]
    cands = list(found.values())
    for c in cands:
        c.title = c.title or title
        if drm and not c.drm and c.kind in ("hls", "dash", "capture"):
            c.drm = ",".join(sorted(drm))
        c.score = score(c)
    cands.sort(key=lambda c: c.score, reverse=True)
    keep = any(c.kind == "capture" for c in cands)
    if not keep:
        shutil.rmtree(workdir, ignore_errors=True)
    return SniffResult(url, title, cands, drm, state["segments"], state["mse"] or bool(sink.mse_tracks()),
                       error, workdir if keep else None)


def _capture_candidates(sink: CaptureSink, url: str, title: str | None, played: dict, drm: set[str]) -> list[Candidate]:
    out = []
    for t in sink.blob_tracks():
        out.append(Candidate(url=f"blob-capture://{t.id}", kind="capture", source="capture", page_url=url,
                             title=title, mime=t.mime, size=t.bytes, tracks=[(str(t.path), t.mime)],
                             note="complete blob object"))
    mse = sink.mse_tracks()
    if mse:
        dur, pos = played.get("duration"), played.get("position")
        if played.get("ended") or (dur and pos and pos >= dur - 1.5):
            note = "complete play-through"
        elif dur and pos:
            note = f"partial: {pos:.0f}s of {dur:.0f}s recorded"
        else:
            note = "partial: only what the player buffered (use --capture force for a full play-through)"
        out.append(Candidate(url=f"mse-capture://{mse[0].id}", kind="capture", source="capture", page_url=url,
                             title=title, mime=" + ".join(t.mime.split(";")[0] for t in mse),
                             size=sum(t.bytes for t in mse), tracks=[(str(t.path), t.mime) for t in mse], note=note,
                             drm=",".join(sorted(drm)) or None))
    return out


async def _drive(tab: _Tab, timeout: float, first_hit: Callable[[], float | None],
                 settle: Callable[[], float] = lambda: 3.0) -> None:
    """Coax the player into requesting media, then stop as soon as results settle."""
    start = time.monotonic()
    nudges = [1.5, 5.0, 10.0]
    clicked_center = False
    while (elapsed := time.monotonic() - start) < timeout:
        hit = first_hit()
        if hit is not None and time.monotonic() - hit > settle():
            return  # media found + a quiet spell to collect the rest (variants, audio track)
        if nudges and elapsed >= nudges[0]:
            nudges.pop(0)
            await tab.mouse("wheel", dy=400)
            await tab.eval_frames(PLAY_JS)  # every frame at once, each bounded
            if not clicked_center and first_hit() is None:
                clicked_center = True
                pos = await tab.eval_main(LARGEST_PLAYER_JS)
                if pos:
                    await tab.mouse("click", pos["x"], pos["y"])  # a trusted gesture, unlike el.click()
        await asyncio.sleep(0.25)


async def _play_through(tab: _Tab, sink: CaptureSink, opts: SniffOptions) -> dict:
    """Play the main video to its end at 16x while the appendBuffer hook records."""
    start = time.monotonic()
    last_bytes, last_change = sink.total, time.monotonic()
    best: dict = {}
    last_log = 0.0
    while time.monotonic() - start < opts.capture_timeout:
        states = [st for group in await tab.eval_frames(FASTPLAY_JS, 16) for st in (group or [])]
        # The "main" video is the longest one; ads and previews are shorter.
        main = max((st for st in states if st.get("d")), key=lambda st: st["d"], default=None)
        if main:
            best = {"duration": main["d"], "position": main["t"], "ended": main["ended"]}
            if main["ended"] or main["t"] >= main["d"] - 0.5:
                best["ended"] = True
                break
        if sink.total != last_bytes:
            last_bytes, last_change = sink.total, time.monotonic()
        elif time.monotonic() - last_change > 45:
            break  # stalled: nothing appended for 45s
        now = time.monotonic()
        if now - last_log > 5 and main:
            last_log = now
            opts.log(f"recording {main['t']:.0f}/{main['d']:.0f}s  ({sink.total / 1e6:.1f} MB)")
        await asyncio.sleep(1.0)
    await asyncio.sleep(1.5)  # last appends in flight
    return best
