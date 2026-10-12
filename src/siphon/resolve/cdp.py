"""A minimal Chrome DevTools Protocol client: system Chrome over one WebSocket.

Replaces Playwright (136 MB, a bundled Node.js driver process, and an exact-version
pin) for the one job siphon needs: open pages in the Chrome already installed, watch
the network in every frame, inject scripts, receive bindings, run JS in frames.

Model (CDP "flatten" mode): one browser WebSocket; every target (page, out-of-process
iframe, popup) is attached as a session, and every command/event carries its
sessionId. Auto-attach with waitForDebuggerOnStart pauses each new target until we
have installed our hooks, then lets it run — so no frame can execute a script
before siphon's init script is in place.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

CHROMES = ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser",
           "microsoft-edge", "microsoft-edge-stable", "brave-browser", "brave")


class CDPError(Exception):
    pass


def find_chrome() -> str | None:
    """System Chrome first (it ships H.264/AAC), then any Chromium-family browser.
    All of them speak CDP. Override with SIPHON_CHROME."""
    if os.environ.get("SIPHON_CHROME"):
        return os.environ["SIPHON_CHROME"]
    for exe in CHROMES:
        if p := shutil.which(exe):
            return p
    # Last resort: a browser Playwright downloaded earlier, if any is lying around.
    cache = Path.home() / ".cache/ms-playwright"
    for cand in sorted(cache.glob("chromium-*/chrome-linux*/chrome"), reverse=True):
        return str(cand)
    return None


def has_proprietary_codecs(exe: str | None) -> bool:
    return bool(exe) and "chromium" not in Path(exe).name and "ms-playwright" not in exe


def profile_in_use(user_data_dir: Path) -> bool:
    """Chrome refuses two instances on one profile (the second hands off and exits)."""
    lock = user_data_dir / "SingletonLock"
    try:
        target = os.readlink(lock)
    except OSError:
        return False
    pid = target.rsplit("-", 1)[-1]
    if not pid.isdigit():
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def ua_override(product: str) -> dict:
    """Network.setUserAgentOverride params that hide "HeadlessChrome" WITHOUT losing client hints.

    Overriding only `userAgent` makes Chrome drop/blank the sec-ch-ua* headers — measured: a CDN
    that checks client hints then refused siphon's browser while accepting plain headless Chrome.
    Supplying userAgentMetadata keeps them consistent with the UA string.
    """
    full = product.split("/")[-1] or "150.0.0.0"
    major = full.split(".")[0]
    brands = [{"brand": "Google Chrome", "version": major}, {"brand": "Chromium", "version": major},
              {"brand": "Not=A?Brand", "version": "24"}]
    return {
        "userAgent": f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                     f"Chrome/{major}.0.0.0 Safari/537.36",
        "userAgentMetadata": {
            "brands": brands, "fullVersionList": [dict(b, version=full if b["version"] == major else b["version"])
                                                  for b in brands],
            "fullVersion": full, "platform": "Linux", "platformVersion": "", "architecture": "x86",
            "model": "", "mobile": False, "bitness": "64", "wow64": False,
        },
    }


@dataclass
class Session:
    browser: "Browser"
    id: str
    target_id: str
    type: str
    url: str = ""
    opener: str | None = None
    contexts: dict[str, int] = field(default_factory=dict)  # frameId -> default execution context id

    async def send(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        return await self.browser.send(method, params, session=self.id, timeout=timeout)

    async def evaluate_all(self, expression: str, timeout: float = 3) -> list[Any]:
        """Run an expression in every frame's main world in this target, concurrently."""
        async def one(ctx: int):
            try:
                r = await self.send("Runtime.evaluate", {"expression": expression, "contextId": ctx,
                                                         "awaitPromise": True, "returnByValue": True}, timeout)
                return r.get("result", {}).get("value")
            except (CDPError, asyncio.TimeoutError):
                return None
        return list(await asyncio.gather(*(one(c) for c in list(self.contexts.values()))))


Handler = Callable[[dict, "Session | None"], "Awaitable[None] | None"]


class Browser:
    def __init__(self, proc: asyncio.subprocess.Process, ws, user_data_dir: Path, ephemeral: bool):
        self.proc = proc
        self.ws = ws
        self.user_data_dir = user_data_dir
        self.ephemeral = ephemeral
        self.sessions: dict[str, Session] = {}
        self._next = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: dict[str, list[Handler]] = {}
        self._tasks: set[asyncio.Task] = set()
        self.closed = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    # ------------------------------------------------------------- launch

    @classmethod
    async def launch(cls, exe: str, *, headless: bool, profile: Path | None, args: list[str] = (),
                     proxy: str | None = None) -> "Browser":
        from websockets.asyncio.client import connect

        ephemeral = profile is None or profile_in_use(profile)
        udd = Path(tempfile.mkdtemp(prefix="siphon-chrome-")) if ephemeral else profile
        udd.mkdir(parents=True, exist_ok=True)
        (udd / "DevToolsActivePort").unlink(missing_ok=True)
        argv = [exe, f"--user-data-dir={udd}", "--remote-debugging-port=0", "--no-first-run",
                "--no-default-browser-check", "--password-store=basic", "--use-mock-keychain",
                "--disable-sync", "--disable-features=Translate,MediaRouter,OptimizationHints",
                *args]
        if headless:
            argv += ["--headless=new", "--window-size=1366,800"]
        if proxy:
            # Chrome resolves DNS through a socks5 proxy already; it rejects the "socks5h" spelling.
            argv.append("--proxy-server=" + proxy.replace("socks5h://", "socks5://", 1))
        argv.append("about:blank")
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        port_file = udd / "DevToolsActivePort"
        for _ in range(150):  # up to 15s for a cold start
            if port_file.exists() and len(lines := port_file.read_text().split()) >= 2:
                break
            if proc.returncode is not None:
                raise CDPError(f"{exe} exited with code {proc.returncode} before opening DevTools")
            await asyncio.sleep(0.1)
        else:
            proc.kill()
            raise CDPError("Chrome didn't open its DevTools port within 15s")
        ws = await connect(f"ws://127.0.0.1:{lines[0]}{lines[1]}", max_size=None, ping_interval=None,
                           open_timeout=10)
        return cls(proc, ws, udd, ephemeral)

    # ---------------------------------------------------------- messaging

    async def send(self, method: str, params: dict | None = None, *, session: str | None = None,
                   timeout: float = 30) -> dict:
        if self.closed.is_set():
            raise CDPError("browser closed")
        self._next += 1
        mid = self._next
        msg: dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self.ws.send(json.dumps(msg))
            # We own this future, so a timeout can simply forget it — no orphaned
            # library-side state (the trap Playwright + wait_for set earlier).
            return await asyncio.wait_for(fut, timeout)
        except Exception as e:
            if isinstance(e, (CDPError, asyncio.TimeoutError)):
                raise
            raise CDPError(str(e)) from e
        finally:
            self._pending.pop(mid, None)

    def on(self, method: str, handler: Handler) -> None:
        self._handlers.setdefault(method, []).append(handler)

    def spawn(self, coro) -> None:
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(lambda x: (self._tasks.discard(x), x.cancelled() or x.exception()))

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.get(msg["id"])
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(CDPError(msg["error"].get("message", "CDP error")))
                        else:
                            fut.set_result(msg.get("result", {}))
                    continue
                method = msg.get("method", "")
                sess = self.sessions.get(msg.get("sessionId", ""))
                for h in self._handlers.get(method, ()):
                    try:
                        r = h(msg.get("params", {}), sess)
                        if asyncio.iscoroutine(r):
                            self.spawn(r)
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CDPError("browser connection closed"))

    # ------------------------------------------------------------ shutdown

    async def close(self) -> None:
        try:
            await self.send("Browser.close", timeout=5)
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), 5)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                self.proc.kill()
                await asyncio.wait_for(self.proc.wait(), 5)
            except Exception:
                pass
        try:
            await self.ws.close()
        except Exception:
            pass
        self._reader.cancel()
        for t in list(self._tasks):
            t.cancel()
        if self.ephemeral:
            shutil.rmtree(self.user_data_dir, ignore_errors=True)

    async def wait_closed_by_user(self) -> None:
        """Browse mode: return when the user closes the last window (Chrome exits)."""
        await self.proc.wait()
        self.closed.set()


async def setup_target(sess: Session, *, init_scripts: list[str], bindings: list[str],
                       blocked: list[str], user_agent: dict | None) -> None:
    """Hooks every target gets before it runs a single script of its own."""
    b = sess.browser

    def ctx_created(p, s):
        if s is sess:
            c = p.get("context", {})
            aux = c.get("auxData", {})
            if aux.get("isDefault") and aux.get("frameId"):
                sess.contexts[aux["frameId"]] = c["id"]

    def ctx_destroyed(p, s):
        if s is sess:
            cid = p.get("executionContextId")
            for k in [k for k, v in sess.contexts.items() if v == cid]:
                del sess.contexts[k]

    def ctx_cleared(p, s):
        if s is sess:
            sess.contexts.clear()

    b.on("Runtime.executionContextCreated", ctx_created)
    b.on("Runtime.executionContextDestroyed", ctx_destroyed)
    b.on("Runtime.executionContextsCleared", ctx_cleared)

    cmds = [sess.send("Network.enable", {"maxPostDataSize": 0}), sess.send("Runtime.enable")]
    if sess.type == "page":
        cmds.append(sess.send("Page.enable"))
    cmds += [sess.send("Page.addScriptToEvaluateOnNewDocument", {"source": s}) for s in init_scripts]
    cmds += [sess.send("Runtime.addBinding", {"name": n}) for n in bindings]
    if blocked:
        cmds.append(sess.send("Network.setBlockedURLs", {"urls": blocked}))
    if user_agent:
        cmds.append(sess.send("Network.setUserAgentOverride", user_agent))  # see ua_override()
    # Children (out-of-process iframes, workers) attach to this session, paused, like us.
    cmds.append(sess.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True,
                                                   "flatten": True}))
    await asyncio.gather(*cmds, return_exceptions=True)
    try:
        await sess.send("Runtime.runIfWaitingForDebugger", timeout=5)
    except Exception:
        pass
