#!/usr/bin/env python3
"""Verify the Firefox build in a real Firefox, driven over WebDriver BiDi (plus Marionette to open the
extension's own page, which WebDriver navigation refuses). Nothing is published.

    python3 scripts/build-extension.py --unpacked           # first: dist/extension-firefox/
    .venv/bin/python store/src/firefox_check.py /path/to/firefox [--hosts legacy|xdg]

Needs: a Firefox binary (not a Flatpak or Snap: those can't start a native host), ffmpeg, websockets.

It runs Firefox headless with a throwaway profile and a throwaway HOME, registers the native host
there with the real `siphon setup browser`, serves store/src/site/ locally, and checks:
  - the build installs, its policy says "firefox", and nothing is blocked (YouTube included)
  - site access (<all_urls>) is granted at install, so the popup shows no "allow" banner
  - the download button is injected on the demo page
  - the popup side reads the tab's URL without the `tabs` permission
  - the background recorded the page's HLS and file streams (webRequest in an event page)
  - the popup reaches the native host (the manifest keyed by the add-on ID)
  - a click on the button saves the file through the browser's own downloader
  - an HLS stream goes through the native host to the siphon CLI and lands as a file
  - the extension reaches a local `siphon serve` with its token (the route for sandboxed browsers)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture import ROOT, check, free_port, make_media, start_site  # noqa: E402

EXT = ROOT / "dist" / "extension-firefox"
ADDON_ID = "siphon@antharmaya.com"
FAILED: list[str] = []


def verdict(name: str, ok: bool, detail: str = "") -> None:
    check(name, ok, detail)
    if not ok:
        FAILED.append(name)


class BiDi:
    def __init__(self, ws):
        self.ws, self.n, self.pending = ws, 0, {}

    async def run(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if m.get("id") in self.pending:
                self.pending.pop(m["id"]).set_result(m)

    async def send(self, method, params=None, timeout=60):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.n] = fut
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        r = await asyncio.wait_for(fut, timeout)
        if r.get("type") == "error":
            raise RuntimeError(f"{method}: {r.get('error')}: {r.get('message')}")
        return r["result"]

    async def js(self, ctx, expr, timeout=60):
        """Evaluate an expression (a promise is awaited) and return its JSON-serialisable value."""
        r = await self.send("script.evaluate", {
            "expression": f"(async () => JSON.stringify(await ({expr})))()", "target": {"context": ctx},
            "awaitPromise": True, "userActivation": True}, timeout)
        if r.get("type") != "success":
            raise RuntimeError(f"js failed: {json.dumps(r.get('exceptionDetails', r))[:400]}")
        v = r["result"]
        return json.loads(v["value"]) if v.get("type") == "string" else None

    async def wait(self, ctx, expr, seconds=15):
        for _ in range(int(seconds * 4)):
            try:
                if await self.js(ctx, expr):
                    return True
            except RuntimeError:
                pass
            await asyncio.sleep(0.25)
        return False

    async def find(self, prefix):
        tree = await self.send("browsingContext.getTree", {})
        return next((c["context"] for c in tree["contexts"] if c["url"].startswith(prefix)), None)

    async def tab(self, url):
        ctx = (await self.send("browsingContext.create", {"type": "tab"}))["context"]
        await self.send("browsingContext.navigate", {"context": ctx, "url": url, "wait": "complete"})
        return ctx


class Marionette:
    """The older remote protocol: `len:[0, id, command, params]` on a plain socket. It owns the session
    (Firefox allows one), which BiDi then joins through the webSocketUrl it hands back."""

    async def start(self, port: int) -> str:
        for _ in range(120):
            try:
                self.r, self.w = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.25)
        else:
            sys.exit("Firefox never opened its Marionette port")
        self.n = 0
        await self._packet()  # greeting
        got = await self.call("WebDriver:NewSession", {"webSocketUrl": True})
        url = got["capabilities"].get("webSocketUrl")
        if not url:
            sys.exit(f"no BiDi socket in the session: {sorted(got['capabilities'])}")
        return url

    async def _packet(self):
        n = int((await self.r.readuntil(b":"))[:-1])
        return json.loads(await self.r.readexactly(n))

    async def call(self, cmd, params):
        self.n += 1
        body = json.dumps([0, self.n, cmd, params])
        self.w.write(f"{len(body)}:{body}".encode())
        await self.w.drain()
        _, _, error, result = await self._packet()
        if error:
            raise RuntimeError(f"{cmd}: {error.get('error')}: {error.get('message')}")
        return result

    async def open(self, url: str) -> None:
        """Open an extension page in a tab. WebDriver navigation to moz-extension:// is refused, so the
        browser's own UI code opens it (needs --remote-allow-system-access)."""
        await self.call("Marionette:SetContext", {"value": "chrome"})
        await self.call("WebDriver:ExecuteScript", {"args": [url], "script":
                        "gBrowser.addTab(arguments[0], {triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal()});"})
        await self.call("Marionette:SetContext", {"value": "content"})


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("firefox", help="path to a firefox binary")
    ap.add_argument("--hosts", choices=["setup", "legacy", "xdg"], default="setup",
                    help="where the native host manifest goes: wherever `siphon setup browser` puts it (default), "
                         "or forced to ~/.mozilla (legacy) or ~/.config/mozilla (xdg) to probe what Firefox reads")
    ap.add_argument("--keep", action="store_true", help="keep the work dir")
    a = ap.parse_args()
    if not (EXT / "manifest.json").is_file():
        sys.exit("build first: python3 scripts/build-extension.py --unpacked")

    work = Path(tempfile.mkdtemp(prefix="siphon-ffcheck-"))
    home, profile, dl = work / "home", work / "profile", work / "downloads"
    for d in (home / ".mozilla", profile, dl):
        d.mkdir(parents=True)
    site = work / "site"
    shutil.copytree(ROOT / "store/src/site", site)
    make_media(site / "media")
    site_port, bidi_port, mar_port = free_port(), free_port(), free_port()
    base = f"http://127.0.0.1:{site_port}/"
    ext_uuid = str(uuid.uuid4())
    env = {**os.environ, "HOME": str(home), "SIPHON_STATE": str(work / "state"), "SIPHON_DOWNLOAD_DIR": str(dl),
           "SIPHON_HOST_LOG": str(work / "host.log"), "MOZ_HEADLESS": "1"}
    env.pop("XDG_CONFIG_HOME", None)

    r = subprocess.run([sys.executable, "-m", "siphon", "setup", "browser"], env=env, capture_output=True, text=True)
    manifest = home / ".mozilla/native-messaging-hosts/com.antharmaya.siphon.json"
    verdict("`siphon setup browser` registers the Firefox host", r.returncode == 0 and manifest.is_file(), r.stderr[-200:])
    if a.hosts == "xdg":
        target = home / ".config/mozilla/native-messaging-hosts"
        target.mkdir(parents=True)
        shutil.move(str(manifest), target / manifest.name)
        shutil.rmtree(home / ".mozilla")
    print(f"  host manifest: {a.hosts}")

    prefs = {
        "remote.active-protocols": 1, "marionette.port": mar_port, "browser.shell.checkDefaultBrowser": False, "datareporting.policy.dataSubmissionEnabled": False,
        "browser.download.dir": str(dl), "browser.download.folderList": 2, "browser.download.useDownloadDir": True,
        "browser.download.always_ask_before_handling_new_types": False, "browser.download.manager.showWhenStarting": False,
        "extensions.webextensions.uuids": json.dumps({ADDON_ID: ext_uuid}), "app.update.enabled": False,
        "browser.aboutwelcome.enabled": False, "toolkit.telemetry.reportingpolicy.firstRun": False,
    }
    (profile / "user.js").write_text("".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n" for k, v in prefs.items()))

    web = start_site(site, site_port)
    ff = subprocess.Popen([a.firefox, "--headless", "--no-remote", "--profile", str(profile),
                           "--marionette", "--remote-allow-system-access", "--remote-debugging-port", str(bidi_port)], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        mar = Marionette()
        ws = await websockets.connect(await mar.start(mar_port), max_size=50_000_000)
        b = BiDi(ws)
        pump = asyncio.create_task(b.run())
        version = subprocess.run([a.firefox, "--version"], capture_output=True, text=True, env=env).stdout.strip()
        got = await b.send("webExtension.install", {"extensionData": {"type": "path", "path": str(EXT)}})
        verdict(f"the Firefox build installs in {version}", got.get("extension") == ADDON_ID, str(got))

        await mar.open(f"moz-extension://{ext_uuid}/popup.html")
        ext = None
        for _ in range(40):
            ext = await b.find(f"moz-extension://{ext_uuid}/")
            if ext:
                break
            await asyncio.sleep(0.25)
        await b.wait(ext, "document.readyState === 'complete' && typeof browser !== 'undefined'", 10) if ext else None
        if not ext:
            sys.exit("the popup page didn't open")
        pol = await b.js(ext, "(async () => { const bg = await browser.runtime.getBackgroundPage(); return bg ? "
                              "{edition: bg.SIPHON_POLICY.edition, yt: bg.isBlocked('https://www.youtube.com/watch?v=1')} : null; })()")
        verdict("policy says firefox and blocks nothing", pol == {"edition": "firefox", "yt": False}, str(pol))
        granted = await b.js(ext, "browser.permissions.contains({origins: ['<all_urls>']})")
        banner = await b.js(ext, "getComputedStyle(document.getElementById('grant')).display")
        verdict("site access is granted at install, no 'allow' banner", granted is True and banner == "none", f"granted={granted} banner={banner}")

        page = await b.tab(base)
        await b.js(page, "(async () => { const v = document.getElementById('film'); try { await v.play(); } catch {} "
                         "await new Promise(r => setTimeout(r, 2000)); v.pause(); return 1; })()")
        ok = await b.wait(page, "document.querySelectorAll('.siphon-dl-holder').length > 0", 15)
        verdict("download button injected on the demo page", ok)

        tabs = await b.js(ext, "browser.tabs.query({}).then(ts => ts.map(t => ({id: t.id, url: t.url || null})))")
        mine = [t for t in tabs if t["url"] == base]
        verdict("the tab's URL is readable without the `tabs` permission", len(mine) == 1, str(tabs)[:200])
        kinds = []
        if mine:
            # Read what the background persisted: a message from this tab would be answered for this tab.
            key = f"media_{mine[0]['id']}"
            items = (await b.js(ext, f"browser.storage.session.get({json.dumps(key)})") or {}).get(key, [])
            kinds = sorted({i["kind"] for i in items})
        verdict("background recorded the page's HLS and file streams", "hls" in kinds and "file" in kinds, str(kinds))

        ping = await b.js(ext, "browser.runtime.sendMessage({type: 'ping'})")
        verdict("the popup reaches the native host", bool(ping and ping.get("ok")), str(ping))
        shown = await b.js(ext, "document.getElementById('host').textContent")
        print(f"  popup header: {shown!r}")

        await b.send("browsingContext.activate", {"context": page})
        await b.js(page, "(document.querySelector('.siphon-dl-btn').click(), 1)")
        ok = await b.wait(page, "document.querySelector('.siphon-dl-btn').classList.contains('siphon-done')", 30)
        saved = sorted(p.name for p in dl.iterdir())
        label = await b.js(page, "document.querySelector('.siphon-dl-label').textContent")
        verdict("a click saves the file through Firefox's downloader", ok and "study-in-blue.webm" in saved, f"{saved} label={label!r}")

        res = await b.js(ext, f"""new Promise((res) => {{
            const seen = [];
            browser.runtime.onMessage.addListener((m) => {{
                if (m.jobId !== 't-hls') return;
                seen.push(m.event);
                if (m.event === 'done' || m.event === 'error') res({{last: m, seen}});
            }});
            browser.runtime.sendMessage({{type: 'download', jobId: 't-hls', tabId: -1, mode: 'siphon',
                                         page: {json.dumps(base)}, media: {json.dumps(base + 'media/hls/master.m3u8')}, kind: 'hls'}});
            setTimeout(() => res({{last: null, seen}}), 150000);
        }})""", timeout=170)
        last = res.get("last") or {}
        landed = [p.name for p in dl.iterdir() if p.suffix in (".mp4", ".mkv", ".ts") and p.name != "study-in-blue.mp4"]
        verdict("an HLS stream goes through the native host and lands as a file",
                last.get("event") == "done" and bool(landed), f"{last} seen={res.get('seen')} files={sorted(p.name for p in dl.iterdir())}")
        if last.get("event") != "done" and (work / "host.log").exists():
            print("  host.log tail:\n    " + "\n    ".join((work / "host.log").read_text().splitlines()[-12:]))
        # The route a Flatpak or Snap browser uses instead of a native host: siphon serve on localhost.
        box_port = free_port()
        token = subprocess.run([sys.executable, "-m", "siphon", "serve", "token"], env=env, capture_output=True, text=True).stdout.strip()
        box = subprocess.Popen([sys.executable, "-m", "siphon", "serve", "--port", str(box_port), "--no-qr", "-o", str(dl)],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", box_port), 0.2).close()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            st = await b.js(ext, f"browser.runtime.sendMessage({{type: 'boxTest', boxUrl: 'http://127.0.0.1:{box_port}', boxToken: {json.dumps(token)}}})")
            verdict("the extension reaches `siphon serve` on localhost with its token", bool(st and st.get("ok") and st.get("version")), str(st)[:160])
            bad = await b.js(ext, f"browser.runtime.sendMessage({{type: 'boxTest', boxUrl: 'http://127.0.0.1:{box_port}', boxToken: 'wrong'}})")
            verdict("and a wrong token is refused", bool(bad) and bad.get("ok") is False and bad.get("error") == "token rejected", str(bad))
        finally:
            box.terminate()
        pump.cancel()
        await ws.close()
    finally:
        ff.terminate()
        web.terminate()
        try:
            ff.wait(10)
        except subprocess.TimeoutExpired:
            ff.kill()
        if a.keep:
            print(f"work dir kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    print(f"\n{'ALL PASS' if not FAILED else 'FAILED: ' + '; '.join(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
