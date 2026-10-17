"""siphon serve — your private download box, reachable from your phone.

    siphon serve                 run it (loopback only by default); prints a pairing code
    siphon serve pair            a fresh pairing code, from any terminal, while serve runs
    siphon serve token           the API token, for scripts and the iOS Shortcut

Security model (a download server is a juicy target, so every layer is deliberate):
- Binds to 127.0.0.1 unless told otherwise. Reach it from elsewhere through Tailscale or a
  Cloudflare tunnel, not by opening a port.
- Host-header allowlist: blocks DNS rebinding, where a web page you visit points a hostname
  at 127.0.0.1 and talks to a local daemon from your own browser.
- Devices pair with a one-time 8-digit code (10 minutes, 5 tries) and get an HttpOnly,
  SameSite=Strict session cookie. Scripts use a bearer token instead.
- Requests that change things need an `X-Siphon: 1` header when cookie-authenticated, which a
  cross-site page can't send without a CORS preflight this server never grants.
- The API takes intent (URL, preset, destination), never CLI flags; see jobs.build_args.
- Files are served only from a finished job's own recorded list, inside the download dir.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import queue as pyqueue
import re
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import __version__, jobs, paths, queue, reach

WEB = Path(__file__).parent / "web"
COOKIE = "siphon_session"
PAIR_TTL = 600
PAIR_TRIES = 5
MAX_BODY = 64 * 1024
_auth_lock = threading.Lock()


def _auth_file() -> Path:
    return paths.STATE / "serve.json"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _load_auth() -> dict:
    try:
        return json.loads(_auth_file().read_text())
    except (OSError, ValueError):
        return {}


def _save_auth(d: dict) -> None:
    f = _auth_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, f)


def api_token() -> str:
    with _auth_lock:
        d = _load_auth()
        if not d.get("token"):
            d["token"] = secrets.token_urlsafe(32)
            _save_auth(d)
        return d["token"]


def new_pair_code() -> str:
    """8 digits: easy to type on a phone keypad; 10^8 codes, 5 tries, 10 minutes."""
    code = f"{secrets.randbelow(10 ** 8):08d}"
    with _auth_lock:
        d = _load_auth()
        d["pair"] = {"hash": _sha(code), "expires": time.time() + PAIR_TTL, "tries": 0}
        _save_auth(d)
    return f"{code[:4]}-{code[4:]}"


def redeem(code: str) -> str | None:
    """Exchange a pairing code for a session id. One use; wrong guesses burn the code."""
    code = re.sub(r"\D", "", code or "")
    with _auth_lock:
        d = _load_auth()
        p = d.get("pair")
        if not p or time.time() > p["expires"]:
            return None
        if not hmac.compare_digest(p["hash"], _sha(code)):
            p["tries"] = p.get("tries", 0) + 1
            if p["tries"] >= PAIR_TRIES:
                d.pop("pair")
            _save_auth(d)
            return None
        d.pop("pair")
        sid = secrets.token_urlsafe(32)
        d.setdefault("sessions", {})[_sha(sid)] = {"created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _save_auth(d)
        return sid


def _session_ok(sid: str) -> bool:
    return bool(sid) and _sha(sid) in _load_auth().get("sessions", {})


def _drop_session(sid: str) -> None:
    with _auth_lock:
        d = _load_auth()
        d.get("sessions", {}).pop(_sha(sid), None)
        _save_auth(d)


# ------------------------------------------------------------------ the app

class App:
    def __init__(self, outdir: Path, concurrency: int = 2, allow_hosts: set[str] | None = None,
                 allow_loopback_urls: bool = False, runner_kwargs: dict | None = None):
        self.outdir = outdir
        self.allow_hosts = {"localhost", socket.gethostname().lower(), f"{socket.gethostname().lower()}.local"} | {
            h.lower() for h in (allow_hosts or set())}
        self.allow_loopback_urls = allow_loopback_urls
        self.https_hosts: set[str] = set()  # hostnames that only arrive through an https tunnel
        self.subs: list[pyqueue.Queue] = []
        self.subs_lock = threading.Lock()
        self.runner = jobs.Runner(outdir, concurrency, on_update=self.broadcast, **(runner_kwargs or {}))
        self._caps: tuple[float, list] = (0.0, [])

    def broadcast(self, job: dict) -> None:
        with self.subs_lock:
            for q in self.subs:
                q.put(job)

    def subscribe(self) -> pyqueue.Queue:
        q: pyqueue.Queue = pyqueue.Queue()
        with self.subs_lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q: pyqueue.Queue) -> None:
        with self.subs_lock:
            if q in self.subs:
                self.subs.remove(q)

    def host_ok(self, host_header: str) -> bool:
        host = (host_header or "").strip().lower()
        if host.startswith("["):  # [::1]:8765
            host = host[1:host.find("]")]
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        try:
            ipaddress.ip_address(host)
            return True  # an IP literal can't be DNS-rebound
        except ValueError:
            return host in self.allow_hosts

    def capabilities(self) -> list[dict]:
        at, caps = self._caps
        if time.time() - at > 60:
            from .setup import capabilities
            caps = [c.to_json() for c in capabilities()]
            self._caps = (time.time(), caps)
        return caps


STATIC = {"/": "index.html", "/live.js": "live.js", "/app.js": "app.js", "/app.css": "app.css", "/manifest.webmanifest": "manifest.webmanifest",
          "/mark.svg": "mark.svg", "/lockup.svg": "lockup.svg", "/icon-192.png": "icon-192.png", "/icon-512.png": "icon-512.png", "/sw.js": "sw.js",
          **{f"/states/{n}.svg": f"states/{n}.svg" for n in ("resolving", "downloading", "done", "error")}}
CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
# What this engine serves (docs/api-v1.md). Clients check these, never the version number.
FEATURES = ["jobs", "events.resume", "jobs.priority", "jobs.category", "jobs.tags", "jobs.pause", "jobs.edit"]
JOB_RE = re.compile(r"^/api/jobs/([0-9a-f]{4,8})(?:/(retry|pause|resume|files/(\d+)|link/(\d+)))?$")


class Handler(BaseHTTPRequestHandler):
    server_version = "siphon"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: App

    def log_message(self, fmt, *args):  # quiet; jobs have their own logs
        if os.environ.get("SIPHON_SERVE_DEBUG"):
            sys.stderr.write("serve: " + fmt % args + "\n")

    # -------------------------------------------------------------- plumbing
    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json", headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj, headers: dict | None = None) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json", headers)

    def _err(self, code: int, msg: str) -> None:
        self._json(code, {"error": msg})

    def _body(self) -> dict:
        raw = self._raw or b"{}"
        try:
            obj = json.loads(raw or b"{}")
        except ValueError as e:
            raise jobs.BadRequest("body must be JSON") from e
        if not isinstance(obj, dict):
            raise jobs.BadRequest("body must be a JSON object")
        return obj

    def _cookie(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    def _https(self) -> bool:
        # Tunnels (tailscale serve/funnel, cloudflared) terminate TLS and proxy from loopback.
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in self.app.https_hosts or self.headers.get("X-Forwarded-Proto") == "https"

    def _auth(self) -> str | None:
        """'token' (bearer, for scripts) or 'cookie' (a paired browser), else None."""
        h = self.headers.get("Authorization") or ""
        if h.startswith("Bearer ") and hmac.compare_digest(h[7:].strip(), api_token()):
            return "token"
        if _session_ok(self._cookie()):
            return "cookie"
        return None

    def _set_session(self, sid: str) -> str:
        secure = "; Secure" if self._https() else ""
        return f"{COOKIE}={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000{secure}"

    # -------------------------------------------------------------- dispatch
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def _dispatch(self, method: str) -> None:
        # Read the body before any decision. An early refusal that leaves body bytes unread on a
        # kept-alive connection gets them parsed as the next request (a desync).
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return self._err(400, "bad Content-Length")
        if n > MAX_BODY or n < 0:
            self.close_connection = True
            return self._err(413, "request too large")
        self._raw = self.rfile.read(n) if n else b""
        if not self.app.host_ok(self.headers.get("Host", "")):
            return self._err(403, "unknown host; add it with --allow-host")
        url = urlsplit(self.path)
        path = url.path
        try:
            if method == "GET" and path in STATIC:
                return self._static(STATIC[path])
            if method == "GET" and path == "/pair":
                return self._pair_link(parse_qs(url.query).get("code", [""])[0])
            if method == "POST" and path == "/api/pair":
                return self._pair_api()
            how = self._auth()
            if not how:
                return self._err(401, "not paired: run `siphon serve pair` on the box and enter the code")
            if method != "GET" and how == "cookie":
                origin = self.headers.get("Origin")
                if self.headers.get("X-Siphon") != "1" or (origin and urlsplit(origin).netloc != self.headers.get("Host")):
                    return self._err(403, "cross-site request refused")
            return self._api(method, path, url)
        except jobs.BadRequest as e:
            return self._err(400, str(e))
        except jobs.Conflict as e:
            return self._err(409, str(e))
        except (BrokenPipeError, ConnectionResetError):
            return None

    def _api(self, method: str, path: str, url) -> None:
        r = self.app.runner
        if method == "GET" and path == "/api/v1/events":
            return self._events_v1()
        if path.startswith("/api/v1/"):  # v1 is the same routes; the unversioned ones stay for shipped clients
            path = "/api/" + path[len("/api/v1/"):]
        if method == "GET" and path == "/api/status":
            from . import cloud
            clouds = [{"id": f"cloud:{n}", "name": n, "type": kind} for n, kind in cloud.remotes()]
            return self._json(200, {"version": __version__, "api": 1, "features": FEATURES, "host": socket.gethostname(),
                                    "downloads": str(self.app.outdir), "destinations": ["device", "box"],
                                    "clouds": clouds, "cloud_default": paths.load_config().get("cloud_default"),
                                    "capabilities": self.app.capabilities()})
        if method == "GET" and path == "/api/jobs":
            return self._json(200, r.jobs())
        if method == "POST" and path == "/api/jobs":
            job = r.submit(self._body(), allow_loopback=self.app.allow_loopback_urls)
            return self._json(201, job)
        if method == "GET" and path == "/api/events":
            return self._events()
        if method == "GET" and path == "/api/token":  # for the iPhone Shortcut; only a paired device gets it
            return self._json(200, {"token": api_token()})
        if method == "POST" and path == "/api/logout":
            _drop_session(self._cookie())
            return self._json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})
        if m := JOB_RE.match(path):
            jid, action, n, ln = m.group(1), m.group(2), m.group(3), m.group(4)
            job = r.get(jid)
            if not job:
                return self._err(404, "no such job")
            if method == "DELETE" and not action:
                r.cancel(jid)
                self.app.broadcast({"id": jid, "removed": True})
                return self._json(200, {"ok": True})
            if method == "GET" and not action:
                return self._json(200, job)
            if method == "PATCH" and not action:
                return self._json(200, r.edit(jid, self._body()))
            if method == "POST" and action == "retry":
                return self._json(200, r.retry(jid))
            if method == "POST" and action == "pause":
                return self._json(200, r.pause(jid))
            if method == "POST" and action == "resume":
                return self._json(200, r.resume(jid))
            if method == "GET" and n is not None:
                return self._file(job, int(n), parse_qs(url.query).get("inline") == ["1"])
            if method == "GET" and ln is not None:  # a private, expiring link straight from R2/S3/B2
                from . import cloud
                remote = job.get("remote") or []
                if not 0 <= int(ln) < len(remote):
                    return self._err(404, "no such cloud file")
                try:
                    return self._json(200, {"url": cloud.presign(remote[int(ln)])})
                except cloud.CloudError as e:
                    return self._err(409, str(e))
        return self._err(404, "not found")

    # -------------------------------------------------------------- pages
    def _static(self, name: str) -> None:
        f = WEB / name
        if not f.is_file():
            return self._err(404, "not found")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".webmanifest"):
            ctype = "application/manifest+json"
        if name.endswith(".js"):
            ctype = "text/javascript"  # mimetypes may say application/javascript; workers need a JS type
        headers = {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"}
        self._send(200, f.read_bytes(), ctype, headers)

    def _pair_link(self, code: str) -> None:
        sid = redeem(code)
        if not sid:
            return self._send(303, b"", "text/plain", {"Location": "/?pair=failed"})
        self._send(303, b"", "text/plain", {"Location": "/", "Set-Cookie": self._set_session(sid)})

    def _pair_api(self) -> None:
        sid = redeem(str(self._body().get("code", "")))
        if not sid:
            return self._err(401, "that code is wrong or expired; run `siphon serve pair` for a new one")
        self._json(200, {"ok": True}, {"Set-Cookie": self._set_session(sid)})

    def _events(self) -> None:
        """Server-sent events: a snapshot, then every job change, with a heartbeat."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = self.app.subscribe()
        try:
            self._sse("snapshot", self.app.runner.jobs())
            while True:
                try:
                    self._sse("job", q.get(timeout=15))
                except pyqueue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.unsubscribe(q)
            self.close_connection = True

    def _events_v1(self) -> None:
        """v1 stream: stored changes carry the event id from the job database, so a reconnecting
        client (Last-Event-ID) gets exactly what it missed, even across an engine restart or from
        another siphon process. Live progress has no id; it is never replayed."""
        store = queue.db()
        try:
            since = int(self.headers.get("Last-Event-ID", ""))
        except ValueError:
            since = None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = self.app.subscribe()
        try:
            missed = store.events_since(since) if since is not None else None
            if missed is None:
                sent = store.last_event()
                self._sse("snapshot", self.app.runner.jobs(), sent)
            else:
                sent = since
            quiet = 0.0
            while True:
                for e in missed if missed is not None else store.events_since(sent) or []:
                    if e["kind"] == "removed":
                        self._sse("removed", {"id": e["job_id"]}, e["seq"])
                    else:
                        self._sse("job", self.app.runner.view(e["data"]), e["seq"])
                    sent = e["seq"]
                missed = None
                live = []
                try:
                    live.append(q.get(timeout=1))  # woken by this engine; the 1s poll catches other processes
                    while True:
                        live.append(q.get_nowait())
                except pyqueue.Empty:
                    pass
                for job in {j["id"]: j for j in live if j.get("progress") and j.get("status") == "active"}.values():
                    self._sse("progress", job)
                quiet = 0.0 if live else quiet + 1
                if quiet >= 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    quiet = 0.0
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.unsubscribe(q)
            self.close_connection = True

    def _sse(self, event: str, data, event_id: int | None = None) -> None:
        head = f"id: {event_id}\n" if event_id is not None else ""
        self.wfile.write(f"{head}event: {event}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    def _file(self, job: dict, n: int, inline: bool) -> None:
        files = job.get("files") or []
        if not 0 <= n < len(files):
            return self._err(404, "no such file")
        f = Path(files[n]).resolve()
        root = self.app.outdir.resolve()
        if root not in f.parents or not f.is_file():
            return self._err(404, "file is gone or outside the download folder")
        size = f.stat().st_size
        start, end = 0, size - 1
        code = 200
        if m := re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or ""):
            a, b = m.groups()
            if a == "" and b:  # last N bytes
                start, end = max(0, size - int(b)), size - 1
            else:
                start, end = int(a or 0), min(int(b), size - 1) if b else size - 1
            if start > end or start >= size:
                return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            code = 206
        length = end - start + 1
        disp = "inline" if inline else "attachment"
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"{disp}; filename*=UTF-8''{quote(f.name)}")
        self.send_header("X-Content-Type-Options", "nosniff")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(f, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(app: App, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = type("SiphonHandler", (Handler,), {"app": app})
    cls = _Server
    if ":" in host:
        cls = type("_Server6", (_Server,), {"address_family": socket.AF_INET6})
    return cls((host, port), handler)


# ------------------------------------------------------------------ CLI

def main(argv: list[str]) -> int:
    from rich.console import Console

    err = Console(stderr=True, highlight=False)
    if argv[:1] == ["pair"]:
        code = new_pair_code()
        base = _load_auth().get("base")
        err.print(f"pairing code [bold]{code}[/] (valid 10 minutes, one device)", soft_wrap=True)
        if base:
            link = f"{base}/pair?code={code.replace('-', '')}"
            err.print(f"or open [bold]{link}[/]", soft_wrap=True)
            if q := reach.qr(link):
                err.print(q, highlight=False, markup=False)
        print(code)
        return 0
    if argv[:1] == ["token"]:
        print(api_token())
        return 0

    p = argparse.ArgumentParser(prog="siphon serve", description="Your private download box, reachable from your phone.")
    p.add_argument("--listen", default="127.0.0.1", help="address to bind (default 127.0.0.1; reach it via Tailscale)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("-N", "--jobs", type=int, default=2, help="downloads at a time (default 2)")
    p.add_argument("-o", "--downloads", help="download folder (default ~/Downloads or $SIPHON_DOWNLOAD_DIR)")
    p.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                   help="a hostname this server answers to, e.g. box.tailnet.ts.net (repeatable)")
    way = p.add_mutually_exclusive_group()
    way.add_argument("--tailscale", action="store_true", help="reach it privately from your tailnet devices (recommended)")
    way.add_argument("--funnel", action="store_true", help="a public https URL via Tailscale Funnel (pairing still required)")
    way.add_argument("--cloudflare", action="store_true", help="a public https URL via a Cloudflare quick tunnel, no account")
    p.add_argument("--no-qr", action="store_true", help="don't print the pairing QR code")
    p.add_argument("--cookies", metavar="FILE", help="cookies.txt used for every job (a server has no browser to borrow a login from)")
    p.add_argument("--cookies-from-browser", metavar="BROWSER", help="use this machine's browser login for every job")
    p.add_argument("--allow-loopback-urls", action="store_true", help=argparse.SUPPRESS)  # tests / local dev
    a = p.parse_args(argv)

    paths.use_managed_bin()
    outdir = Path(a.downloads).expanduser() if a.downloads else queue.default_outdir()
    outdir.mkdir(parents=True, exist_ok=True)
    extra = []
    if a.cookies:
        extra += ["--cookies", str(Path(a.cookies).expanduser())]
    if a.cookies_from_browser:
        extra += ["--cookies-from-browser", a.cookies_from_browser]
    app = App(outdir, a.jobs, set(a.allow_host), a.allow_loopback_urls, runner_kwargs={"extra_args": extra} if extra else None)
    try:
        srv = make_server(app, a.listen, a.port)
    except OSError as e:
        err.print(f"[red]✗ can't listen on {a.listen}:{a.port}:[/] {e}. Is siphon serve already running? Try --port.")
        return 1
    app.runner.start()
    exposure = None
    if a.tailscale or a.funnel or a.cloudflare:
        try:
            exposure = reach.cloudflare(a.port) if a.cloudflare else reach.tailscale(a.port, public=a.funnel)
        except reach.ReachError as e:
            err.print(f"[red]✗ couldn't open the way in:[/] {e}", soft_wrap=True)
            app.runner.stop()
            srv.server_close()
            return 1
        app.allow_hosts.add(exposure.host)
        app.https_hosts.add(exposure.host)
        base = exposure.url
    elif a.listen in ("0.0.0.0", "::"):
        base = f"http://{reach.lan_ip() or socket.gethostname()}:{a.port}"
    else:
        base = f"http://{'[' + a.listen + ']' if ':' in a.listen else a.listen}:{a.port}"
    with _auth_lock:
        d = _load_auth()
        d["base"] = base
        _save_auth(d)
    code = new_pair_code()
    api_token()
    link = f"{base}/pair?code={code.replace('-', '')}"
    err.print(f"[bold]siphon serve[/] {__version__} → [bold]{base}[/]   downloads: {outdir}", soft_wrap=True)
    if exposure:
        scope = "[yellow]public internet[/]" if exposure.public else "your tailnet only"
        err.print(f"  reachable from {scope} via {exposure.kind}; stops when siphon stops", soft_wrap=True)
    err.print(f"  pair a device: open [bold]{link}[/]  or enter [bold]{code}[/]"
              f"  [dim](10 min, one device; `siphon serve pair` for another)[/]", soft_wrap=True)
    if not a.no_qr and (q := reach.qr(link)):
        err.print(q, highlight=False, markup=False)
    if a.listen not in ("127.0.0.1", "::1", "localhost") and not exposure:
        err.print("  [yellow]![/] listening beyond this machine: anyone on the network can reach the pairing page. "
                  "Codes are one-use and expire, but prefer --tailscale.", soft_wrap=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        err.print("stopping; unfinished jobs resume next time")
    finally:
        if exposure:
            exposure.stop()
        app.runner.stop()
        srv.server_close()
    return 0
