"""Reach `siphon serve` from anywhere, without opening a port.

    --tailscale   private: only devices on your tailnet (https://<machine>.<tailnet>.ts.net)
    --funnel      public:  Tailscale Funnel, an https URL anyone can open (pairing still required)
    --cloudflare  public:  a Cloudflare quick tunnel (https://<random>.trycloudflare.com), no account

All three are outbound connections, so they work behind CGNAT (Jio). siphon runs the tool in
the *foreground* as a child process: when siphon stops, the exposure stops with it. A
`tailscale serve --bg` config would outlive siphon, leaving the machine reachable after you quit.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

TS_SETUP = "install: curl -fsSL https://tailscale.com/install.sh | sh, then: sudo tailscale up"


class ReachError(RuntimeError):
    pass


@dataclass
class Exposure:
    kind: str  # tailscale | funnel | cloudflare
    url: str
    host: str
    proc: subprocess.Popen
    public: bool

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def tailscale_name() -> str:
    """This machine's MagicDNS name, or a ReachError that says exactly what to do."""
    exe = shutil.which("tailscale")
    if not exe:
        raise ReachError(f"tailscale isn't installed ({TS_SETUP})")
    try:
        r = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=10)
        st = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        st = {}
    state = st.get("BackendState")
    if state != "Running":
        if not st:
            raise ReachError("the tailscale daemon isn't running: sudo systemctl enable --now tailscaled && sudo tailscale up")
        raise ReachError(f"tailscale is {state or 'not connected'}: run sudo tailscale up")
    name = (st.get("Self") or {}).get("DNSName", "").rstrip(".")
    if not name:
        raise ReachError("tailscale has no MagicDNS name for this machine; enable MagicDNS and HTTPS in the admin console")
    return name


def _watch(proc: subprocess.Popen, pattern: str, timeout: float) -> str | None:
    """Read the child's output until `pattern` appears; keep draining afterwards so it never blocks."""
    found: list[str] = []
    seen: list[str] = []
    done = threading.Event()

    def pump():
        for line in proc.stdout:
            seen.append(line)
            if not found and (m := re.search(pattern, line)):
                found.append(m.group(0))
                done.set()
        done.set()

    threading.Thread(target=pump, daemon=True).start()
    done.wait(timeout)
    if not found:
        tail = "".join(seen[-6:]).strip()
        if proc.poll() is None:
            proc.terminate()
        raise ReachError(_explain(tail) or f"no URL appeared within {timeout:.0f}s")
    return found[0]


def _explain(output: str) -> str:
    low = output.lower()
    if "access denied" in low or "permission" in low or "operator" in low:
        return "tailscale needs permission to serve as your user: sudo tailscale set --operator=$USER"
    if "https" in low and ("not enabled" in low or "certificate" in low):
        return "enable HTTPS certificates for your tailnet: https://login.tailscale.com/admin/dns"
    if "funnel" in low and ("not enabled" in low or "attribute" in low):
        return "Funnel isn't allowed for this node; enable it in your tailnet policy (nodeAttrs: funnel)"
    return output[-300:]


def tailscale(port: int, public: bool = False, timeout: float = 20) -> Exposure:
    name = tailscale_name()
    cmd = [shutil.which("tailscale"), "funnel" if public else "serve", "--yes", str(port)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    url = _watch(proc, r"https://[A-Za-z0-9.-]+\.ts\.net/?", timeout).rstrip("/")
    host = url.split("://", 1)[1]
    if host != name:  # trust the daemon's name for the allowlist; the printed URL is what we show
        host = name
    return Exposure("funnel" if public else "tailscale", url, host, proc, public)


def cloudflare(port: int, timeout: float = 40) -> Exposure:
    exe = shutil.which("cloudflared")
    if not exe:
        raise ReachError("cloudflared isn't installed: siphon setup cloudflared")
    cmd = [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    url = _watch(proc, r"https://[a-z0-9-]+\.trycloudflare\.com", timeout)
    return Exposure("cloudflare", url, url.split("://", 1)[1], proc, True)


def lan_ip() -> str | None:
    """The address this machine uses on the local network (no packet is sent)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed, nothing goes out on UDP connect
            return s.getsockname()[0]
    except OSError:
        return None


def qr(text: str) -> str | None:
    """A terminal QR code for `text`, or None when segno isn't available."""
    try:
        import io

        import segno
    except ImportError:
        return None
    buf = io.StringIO()
    segno.make(text, error="m").terminal(out=buf, compact=True)
    return buf.getvalue()


def wait_alive(exp: Exposure, seconds: float = .5) -> bool:
    time.sleep(seconds)
    return exp.proc.poll() is None
