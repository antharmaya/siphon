#!/usr/bin/env python3
"""Verify the store build in a real browser, then capture and compose the store graphics.

    python3 scripts/build-extension.py --unpacked      # first: dist/extension-{store,github}/
    .venv/bin/python store/src/capture.py              # needs: brave-browser, ffmpeg, siphon, websockets

What it does, all on this machine, nothing published:
  1. Generates the demo media (an abstract 8 s gradient loop, plus MP4 and HLS copies) and serves
     store/src/site/ as http://field-notes.example (a reserved .example name, mapped to 127.0.0.1).
  2. Runs a throwaway `siphon serve` in its own state dir and UTS namespace (hostname "box"),
     reachable as http://box.tailnet.example, so the "send to your box" test is a real round trip.
  3. Launches headless Brave with the STORE build and a temporary profile whose native-messaging
     dir points at extension/host/siphon_host.py, and checks:
       - the store policy is active, and YouTube URLs are refused with the store message
       - no siphon button is injected on youtube.com, and one is on the demo page
       - the popup reads the tab URL without the `tabs` permission
       - a real button click saves the video through chrome.downloads
       - the popup reaches the native host, and the box test passes
     Then launches the GITHUB build and checks the opposite: YouTube is not refused, the button
     appears there.
  4. Captures the page and popup, then renders store/src/frames.html into the final PNGs in store/.

The downloading screenshot is the one staged frame: after a real click, a progress event at 42 %
is sent through the extension's own relay() so the button shows that state; everything else on
screen is the extension's real UI in its real state.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STORE = HERE.parent
CAPS = HERE / "captures"
BRAVE = shutil.which("brave-browser") or shutil.which("brave")
SITE_HOST, BOX_HOST = "field-notes.example", "box.tailnet.example"
YOUTUBE = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo", the first upload
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---- media + servers -----------------------------------------------------------------------

def make_media(dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    webm = dst / "study-in-blue.webm"
    if not webm.exists():
        # Rendered small and scaled up: the blur is the slow part and it hides the scaling.
        subprocess.run([*ff, "-f", "lavfi", "-i",
                        "gradients=s=640x360:r=30:d=8:c0=0x0c172a:c1=0x1256b2:c2=0x6da5f8:c3=0x241c17:"
                        "c4=0xf9f7f4:nb_colors=5:speed=0.012:type=spiral:seed=7",
                        "-vf", "gblur=sigma=9,scale=1280:720,noise=alls=6:allf=t,format=yuv420p",
                        "-c:v", "libvpx-vp9", "-b:v", "1.5M", "-row-mt", "1", "-deadline", "realtime",
                        "-cpu-used", "8", str(webm)], check=True)
    subprocess.run([*ff, "-ss", "2.5", "-i", str(webm), "-frames:v", "1", "-q:v", "3", str(dst / "poster.jpg")], check=True)
    subprocess.run([*ff, "-i", str(webm), "-t", "4", "-vf", "scale=960:540", "-c:v", "libx264", "-preset",
                    "ultrafast", "-pix_fmt", "yuv420p", str(dst / "study-in-blue.mp4")], check=True)
    (dst / "hls").mkdir(exist_ok=True)
    subprocess.run([*ff, "-i", str(webm), "-t", "4", "-vf", "scale=640:360", "-c:v", "libx264", "-preset",
                    "ultrafast", "-pix_fmt", "yuv420p", "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod",
                    str(dst / "hls/720p.m3u8")], check=True)
    (dst / "hls/master.m3u8").write_text("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720\n720p.m3u8\n")


def start_site(root: Path, port: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
                             "--directory", str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_box(state: Path, dl: Path, port: int) -> tuple[subprocess.Popen, str]:
    env = {**os.environ, "SIPHON_STATE": str(state), "SIPHON_DOWNLOAD_DIR": str(dl)}
    token = subprocess.run(["siphon", "serve", "token"], env=env, capture_output=True, text=True, check=True).stdout.strip()
    # Own UTS namespace so the box reports the hostname "box", not this machine's name.
    cmd = ["unshare", "-ru", "sh", "-c",
           f'hostname box && exec siphon serve --port {port} --allow-host {BOX_HOST} --no-qr -o "{dl}"']
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    return p, token


# ---- a small CDP client (flatten sessions over one socket) ---------------------------------

class CDP:
    def __init__(self, ws):
        self.ws, self.n, self.pending, self.queues = ws, 0, {}, []

    async def run(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self.pending:
                self.pending.pop(m["id"]).set_result(m)
            else:
                for q in self.queues:
                    q.put_nowait(m)

    async def send(self, method, params=None, session=None, timeout=30):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.n] = fut
        msg = {"id": self.n, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        await self.ws.send(json.dumps(msg))
        r = await asyncio.wait_for(fut, timeout)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r["result"]

    async def eval(self, session, expr, context=None, timeout=30):
        p = {"expression": expr, "awaitPromise": True, "returnByValue": True}
        if context:
            p["contextId"] = context
        r = await self.send("Runtime.evaluate", p, session, timeout)
        if "exceptionDetails" in r:
            raise RuntimeError(json.dumps(r["exceptionDetails"])[:800])
        return r["result"].get("value")

    async def attach(self, target_id):
        return (await self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True}))["sessionId"]

    async def open(self, url, width, height, scale=2):
        tid = (await self.send("Target.createTarget", {"url": "about:blank"}))["targetId"]
        s = await self.attach(tid)
        await self.send("Emulation.setDeviceMetricsOverride",
                        {"width": width, "height": height, "deviceScaleFactor": scale, "mobile": False}, s)
        await self.send("Page.enable", {}, s)
        await self.send("Runtime.enable", {}, s)
        await self.send("Page.navigate", {"url": url}, s)
        return tid, s

    async def wait(self, s, expr, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if await self.eval(s, expr):
                    return True
            except RuntimeError:
                pass
            await asyncio.sleep(0.25)
        return False

    async def shot(self, s, path: Path, clip=None):
        p = {"format": "png", "captureBeyondViewport": bool(clip)}
        if clip:
            p["clip"] = {**clip, "scale": 1}
        data = (await self.send("Page.captureScreenshot", p, s, 60))["data"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))

    async def mouse(self, s, x, y, click=False):
        await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, s)
        if click:
            for t in ("mousePressed", "mouseReleased"):
                await self.send("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1}, s)


async def launch(ext: Path, work: Path, env_extra: dict, site_port: int, box_port: int):
    udd = work / "profile"
    (udd / "Default").mkdir(parents=True, exist_ok=True)
    dl = work / "downloads"
    dl.mkdir(exist_ok=True)
    (udd / "Default/Preferences").write_text(json.dumps(
        {"download": {"default_directory": str(dl), "prompt_for_download": False, "directory_upgrade": True}}))
    args = [BRAVE, "--headless=new", f"--user-data-dir={udd}", f"--load-extension={ext}",
            f"--disable-extensions-except={ext}", "--disable-features=DisableLoadExtensionCommandLineSwitch",
            "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
            "--autoplay-policy=no-user-gesture-required", "--hide-scrollbars", "--window-size=1400,900",
            f"--host-resolver-rules=MAP {SITE_HOST} 127.0.0.1:{site_port}, MAP {BOX_HOST} 127.0.0.1:{box_port}"]
    proc = subprocess.Popen(args, env={**os.environ, **env_extra}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = udd / "DevToolsActivePort"
    for _ in range(200):
        if port_file.exists() and len(port_file.read_text().split()) >= 2:
            break
        await asyncio.sleep(0.1)
    port, path = port_file.read_text().split()[:2]
    ws = await websockets.connect(f"ws://127.0.0.1:{port}{path}", max_size=200_000_000)
    cdp = CDP(ws)
    asyncio.create_task(cdp.run())
    ext_id = None
    for _ in range(100):
        for t in (await cdp.send("Target.getTargets"))["targetInfos"]:
            if t["type"] == "service_worker" and t["url"].startswith("chrome-extension://"):
                ext_id, sw_target = t["url"].split("/")[2], t["targetId"]
        if ext_id:
            break
        await asyncio.sleep(0.1)
    if not ext_id:
        raise SystemExit("extension service worker never started (is --load-extension honoured?)")
    sw = await cdp.attach(sw_target)
    # Native messaging: Chromium reads <user-data-dir>/NativeMessagingHosts for user-level hosts.
    nm = udd / "NativeMessagingHosts"
    nm.mkdir(exist_ok=True)
    (nm / "com.antharmaya.siphon.json").write_text(json.dumps({
        "name": "com.antharmaya.siphon", "description": "siphon (store screenshot run)",
        "path": str(ROOT / "extension/host/siphon_host.py"), "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"]}))
    return proc, cdp, ext_id, sw, dl


async def isolated_context(cdp, s, ext_id, q):
    """The content script's isolated world in the top frame, from Runtime.executionContextCreated."""
    for m in list(q._queue):
        if m.get("sessionId") == s and m.get("method") == "Runtime.executionContextCreated":
            c = m["params"]["context"]
            if c["auxData"].get("isDefault") is False and (
                    "siphon" in c.get("name", "") or c.get("origin", "").startswith(f"chrome-extension://{ext_id}")):
                return c["id"]
    return None


# ---- the runs -------------------------------------------------------------------------------

async def open_popup(cdp, ext_id, sw):
    """popup.html in a tab, showing the demo tab's streams. A tab-hosted popup has its own
    sender.tab, so the background would list the popup's tab; hand render() the demo tab's list
    straight from the background's per-tab map instead (the same data a real popup receives)."""
    ptid, pop = await cdp.open(f"chrome-extension://{ext_id}/popup.html", 340, 640)
    await cdp.wait(pop, "document.readyState === 'complete'")
    await asyncio.sleep(0.8)  # let init() finish first, so it can't re-render over this
    t = await cdp.eval(pop, f"chrome.tabs.query({{url: '*://{SITE_HOST}/*'}}).then(([t]) => ({{id: t.id, url: t.url}}))")
    items = await cdp.eval(sw, f"[...(perTab.get({t['id']})?.values() || [])]")
    await cdp.eval(pop, f"tabId = {t['id']}; pageUrl = {json.dumps(t['url'])}; render({json.dumps(items)}); true")
    return pop, {"url": t["url"], "kinds": [i["kind"] for i in items]}


async def store_run(ext, work, ports, box_token):
    proc, cdp, ext_id, sw, dl = await launch(ext, work, {"SIPHON_DOWNLOAD_DIR": str(work / "downloads"),
                                                          "SIPHON_HOST_LOG": str(work / "host.log")}, *ports)
    print(f"store build loaded as {ext_id}")
    q: asyncio.Queue = asyncio.Queue()
    cdp.queues.append(q)
    try:
        ready = await cdp.wait(sw, "typeof POLICY !== 'undefined' && typeof relay === 'function'", 10)
        if not ready:
            print("  sw globals:", await cdp.eval(sw, "[typeof HOST, typeof self.SIPHON_POLICY, typeof importScripts, location.href]"))
        pol = await cdp.eval(sw, "({edition: POLICY.edition, yt: isBlocked('https://music.youtube.com/watch?v=1'), "
                                 "gv: isBlocked('https://rr1---sn-x.googlevideo.com/videoplayback'), "
                                 "site: isBlocked('http://field-notes.example/')})")
        check("store policy active in the service worker", pol == {"edition": "store", "yt": True, "gv": True, "site": False}, str(pol))

        # The demo page: button present, popup data, a real click.
        tid, page = await cdp.open(f"http://{SITE_HOST}/", 1120, 620)
        await cdp.wait(page, "document.readyState === 'complete' && document.fonts.status === 'loaded'")
        await cdp.eval(page, "(async () => { const v = document.getElementById('film'); await v.play(); "
                             "await new Promise(r => setTimeout(r, 2500)); v.pause(); })()")
        ok = await cdp.wait(page, "document.querySelectorAll('.siphon-dl-holder').length > 0", 15)
        check("siphon button injected on the demo page", ok)
        r = await cdp.eval(page, "(() => { const b = document.querySelector('.siphon-dl-btn').getBoundingClientRect(); "
                                 "return [b.x + b.width / 2, b.y + b.height / 2]; })()")
        await cdp.mouse(page, *r)
        await asyncio.sleep(0.4)
        await cdp.shot(page, CAPS / "page-hover.png")

        # Popup: the tab URL must be readable without the `tabs` permission.
        pop, info = await open_popup(cdp, ext_id, sw)
        check("popup reads the tab URL without the `tabs` permission", info["url"] == f"http://{SITE_HOST}/", info["url"])
        check("background recorded the page's HLS and file streams", "hls" in info["kinds"] and "file" in info["kinds"], str(info["kinds"]))
        host = await cdp.eval(pop, "document.getElementById('host').textContent")
        # Expected "host missing": Chromium reads user-level native hosts from the DEFAULT profile
        # dir, whose manifest (host/install.sh) allows only the GitHub ID. The store ID is new.
        print(f"  NOTE  store build vs the installed native host: {host!r} (store ID {ext_id} is not in allowed_origins)")
        hint = await cdp.eval(pop, "getComputedStyle(document.getElementById('install')).display + ' | ' + document.getElementById('install').textContent")
        check("store popup points a missing host at the GitHub install", hint.startswith("block") and "github.com/antharmaya/siphon" in hint, hint[:90])
        miss = await cdp.eval(pop, f"""new Promise((res) => {{
            chrome.runtime.onMessage.addListener((m) => {{ if (m.jobId === 't-host' && m.event === 'error') res(m.message); }});
            chrome.runtime.sendMessage({{type: 'download', jobId: 't-host', tabId: -1, mode: 'siphon', page: 'http://{SITE_HOST}/'}});
            setTimeout(() => res(null), 8000);
        }})""")
        check("store build's host-missing error names the GitHub install", bool(miss) and "github.com/antharmaya/siphon" in miss, str(miss)[:120])

        # A real click on the button: a plain file, so chrome.downloads saves it.
        await cdp.send("Target.activateTarget", {"targetId": tid})
        await cdp.mouse(page, *r, click=True)
        ok = await cdp.wait(page, "document.querySelector('.siphon-dl-btn').classList.contains('siphon-done')", 20)
        saved = sorted(p.name for p in dl.iterdir())
        check("button click saves the file via chrome.downloads", ok and "study-in-blue.webm" in saved, str(saved))
        label = await cdp.eval(page, "document.querySelector('.siphon-dl-label').textContent")
        check("button shows the saved state", label.startswith("Saved"), label)
        await cdp.mouse(page, 4, 4)
        await asyncio.sleep(0.3)
        await cdp.shot(page, CAPS / "page-saved.png")

        # The one staged frame: progress at 42 % through the extension's own relay().
        ctx = await isolated_context(cdp, page, ext_id, q)
        job = await cdp.eval(page, "document.querySelector('.siphon-dl-btn')._jobId", context=ctx)
        await cdp.eval(sw, f"""(async () => {{
            const [t] = await chrome.tabs.query({{url: '*://{SITE_HOST}/*'}});
            relay(t.id, {json.dumps(job)}, {{event: 'progress', percent: 42}});
        }})()""")
        await cdp.wait(page, "document.querySelector('.siphon-dl-label').textContent === '42%'", 5)
        await asyncio.sleep(1.1)  # a frame into the swallow animation
        await cdp.shot(page, CAPS / "page-progress.png")

        # Refusal: every path into a download goes through the background's check.
        refusal = await cdp.eval(pop, """new Promise((res) => {
            const got = {};
            chrome.runtime.onMessage.addListener((m) => { if (m.jobId === 't-yt') { got.relay = m.message; res(got); } });
            chrome.runtime.sendMessage({type: 'download', jobId: 't-yt', tabId: -1, mode: 'siphon',
                                        page: 'https://music.youtube.com/watch?v=abc'}, (r) => { got.resp = r; });
            setTimeout(() => res(got), 4000);
        })""")
        check("store build refuses a YouTube download with the store message",
              (refusal.get("resp") or {}).get("refused") and "Chrome Web Store edition" in (refusal.get("relay") or ""),
              str(refusal))

        # youtube.com itself: the content script must not be there.
        ytid, yt = await cdp.open(YOUTUBE, 1280, 800, 1)
        await cdp.wait(yt, "!!document.querySelector('video')", 25)
        await asyncio.sleep(5)
        n = await cdp.eval(yt, "document.querySelectorAll('.siphon-dl-holder').length")
        has_video = await cdp.eval(yt, "!!document.querySelector('video')")
        ctx = await isolated_context(cdp, yt, ext_id, q)
        check("store build: no button and no content script on youtube.com", has_video and n == 0 and ctx is None,
              f"video={has_video} buttons={n} isolated_world={ctx}")
    finally:
        proc.terminate()


async def github_run(ext, work, ports, box_token):
    proc, cdp, ext_id, sw, dl = await launch(ext, work, {"SIPHON_DOWNLOAD_DIR": str(work / "downloads"),
                                                          "SIPHON_HOST_LOG": str(work / "host.log")}, *ports)
    print(f"github build loaded as {ext_id}")
    try:
        await cdp.wait(sw, "typeof POLICY !== 'undefined'", 10)
        pol = await cdp.eval(sw, "({edition: POLICY.edition, yt: isBlocked('https://www.youtube.com/watch?v=1')})")
        check("github policy blocks nothing", pol == {"edition": "github", "yt": False}, str(pol))

        # Popup captures come from this build: popup.html/popup.js are byte-identical in both
        # editions, and this ID is the one the installed native host allows.
        tid, page = await cdp.open(f"http://{SITE_HOST}/", 1120, 620)
        await cdp.wait(page, "document.readyState === 'complete'")
        await asyncio.sleep(1.5)
        pop, info = await open_popup(cdp, ext_id, sw)
        await cdp.wait(pop, "document.getElementById('host').textContent.startsWith('host v')", 10)
        host = await cdp.eval(pop, "document.getElementById('host').textContent")
        check("github build reaches the installed native host", host.startswith("host v"), host)
        h = await cdp.eval(pop, "Math.ceil(document.body.getBoundingClientRect().height)")
        await cdp.shot(pop, CAPS / "popup-streams.png", {"x": 0, "y": 0, "width": 340, "height": h})

        # Send-to-box: a real round trip to the throwaway `siphon serve`.
        await cdp.eval(pop, f"""(() => {{
            document.querySelector('input[name=target][value=box]').click();
            for (const [id, v] of [['boxUrl', 'http://{BOX_HOST}'], ['boxToken', {json.dumps(box_token)}]]) {{
              const el = document.getElementById(id); el.value = v; el.dispatchEvent(new Event('change'));
            }}
            document.getElementById('boxTest').click();
        }})()""")
        ok = await cdp.wait(pop, "document.getElementById('boxResult').textContent.startsWith('✓')", 15)
        res = await cdp.eval(pop, "document.getElementById('boxResult').textContent")
        check("box test round trip", ok, res)
        h = await cdp.eval(pop, "Math.ceil(document.body.getBoundingClientRect().height)")
        await cdp.shot(pop, CAPS / "popup-box.png", {"x": 0, "y": 0, "width": 340, "height": h})
        await cdp.eval(pop, "chrome.storage.local.set({target: 'local'})")
        ytid, yt = await cdp.open(YOUTUBE, 1280, 800, 1)
        await cdp.wait(yt, "!!document.querySelector('video')", 25)
        ok = await cdp.wait(yt, "document.querySelectorAll('.siphon-dl-holder').length > 0", 20)
        check("github build: button present on youtube.com", ok)
    finally:
        proc.terminate()


async def render_frames(ports):
    """Render frames.html (file://) into the final PNGs, each at its exact size."""
    work = Path(tempfile.mkdtemp(prefix="siphon-frames-"))
    udd = work / "profile"
    args = [BRAVE, "--headless=new", f"--user-data-dir={udd}", "--remote-debugging-port=0", "--no-first-run",
            "--hide-scrollbars", "--allow-file-access-from-files", "--window-size=1400,900"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        pf = udd / "DevToolsActivePort"
        for _ in range(200):
            if pf.exists() and len(pf.read_text().split()) >= 2:
                break
            await asyncio.sleep(0.1)
        port, path = pf.read_text().split()[:2]
        ws = await websockets.connect(f"ws://127.0.0.1:{port}{path}", max_size=200_000_000)
        cdp = CDP(ws)
        asyncio.create_task(cdp.run())
        tid, s = await cdp.open((HERE / "frames.html").as_uri(), 1400, 900, 1)
        await cdp.wait(s, "document.readyState === 'complete' && document.fonts.status === 'loaded' && "
                          "[...document.images].every(i => i.complete && i.naturalWidth)", 30)
        await asyncio.sleep(0.5)
        frames = await cdp.eval(s, "[...document.querySelectorAll('[data-out]')].map(e => { const r = e.getBoundingClientRect(); "
                                   "return {out: e.dataset.out, x: r.x + scrollX, y: r.y + scrollY, width: r.width, height: r.height}; })")
        for f in frames:
            out = STORE / f.pop("out")
            await cdp.shot(s, out, f)
            print(f"  wrote {out.relative_to(ROOT)}  {int(f['width'])}x{int(f['height'])}")
    finally:
        proc.terminate()
        shutil.rmtree(work, ignore_errors=True)


async def main():
    if not BRAVE:
        raise SystemExit("brave-browser not found")
    for ed in ("store", "github"):
        if not (ROOT / f"dist/extension-{ed}/manifest.json").exists():
            raise SystemExit("run: python3 scripts/build-extension.py --unpacked")
    work = Path(tempfile.mkdtemp(prefix="siphon-store-"))
    site = work / "site"
    shutil.copytree(HERE / "site", site)
    make_media(site / "media")
    site_port, box_port = free_port(), free_port()
    srv = start_site(site, site_port)
    box, token = start_box(work / "box-state", work / "box-dl", box_port)
    try:
        print("== store build")
        await store_run(ROOT / "dist/extension-store", work / "store", (site_port, box_port), token)
        print("== github build")
        await github_run(ROOT / "dist/extension-github", work / "github", (site_port, box_port), token)
        if "--no-frames" not in sys.argv:
            print("== frames")
            await render_frames((site_port, box_port))
    finally:
        srv.terminate()
        box.terminate()
        shutil.rmtree(work, ignore_errors=True)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
