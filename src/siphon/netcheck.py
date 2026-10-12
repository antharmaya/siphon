"""Tell "this site is blocked for you" apart from "siphon is broken" — in seconds.

Measured on Jio, 2026-10-08, for TikTok (banned in India since 2020):
  layer 1  ISP DNS answers with a sinkhole address (2405:200:… — Jio's own range)
           that silently drops packets. Symptom: a 45s page-load timeout, which
           looks exactly like a slow or broken tool.
  layer 2  Even with the real IP (via DNS-over-HTTPS), TikTok itself 302s every
           Indian IP to /in/about. Only a non-Indian exit gets past this.

Layer 1 is detected by comparing the system resolver with Cloudflare DoH and
trying a 4s TCP connect. Layer 2 is detected from known geofence redirects.
Neither check runs when a proxy is in use: the proxy does its own DNS and exit.
"""

from __future__ import annotations

import functools
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

DOH = "https://cloudflare-dns.com/dns-query"
# Where platforms send visitors from a region they refuse. Extend as new ones show up.
# Hosts known to geofence whole countries: worth one extra request even when DNS is fine.
GEOFENCING_HOSTS = ("tiktok.com",)
GEOFENCE = [
    (re.compile(r"tiktok\.com/[a-z]{2}/about/?$"), "TikTok refuses this country (redirects to its regional 'about' page)"),
    (re.compile(r"/(geo-?block(ed)?|region-?block(ed)?|not-?available-?in-?your-?(country|region))", re.I),
     "the site redirects visitors from your region to a block page"),
]


@dataclass
class Reach:
    host: str
    ok: bool
    layer: str = ""  # "dns" | "geofence" | "unreachable"
    detail: str = ""
    system_ips: tuple[str, ...] = ()
    doh_ips: tuple[str, ...] = ()


def system_resolve(host: str, port: int = 443) -> list[str]:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})
    except socket.gaierror:
        return []


def doh_resolve(host: str, timeout: float = 5) -> list[str]:
    ips: list[str] = []
    try:
        with httpx.Client(timeout=timeout) as c:
            for rtype in ("A", "AAAA"):
                r = c.get(DOH, params={"name": host, "type": rtype}, headers={"accept": "application/dns-json"})
                ips += [a["data"] for a in r.json().get("Answer", []) if a.get("type") in (1, 28)]
    except Exception:
        pass
    return ips


def tcp_ok(ip: str, port: int = 443, timeout: float = 3) -> bool:
    try:
        fam = socket.AF_INET6 if ipaddress.ip_address(ip).version == 6 else socket.AF_INET
        with socket.socket(fam, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((ip, port))
        return True
    except OSError:
        return False


def geofence_reason(location: str) -> str | None:
    for pat, why in GEOFENCE:
        if pat.search(location):
            return why
    return None


def _first_redirect(url: str, ip: str, host: str) -> str:
    """GET via a specific IP with correct SNI/Host, so a poisoned resolver can't interfere."""
    parts = urlsplit(url)
    netloc = f"[{ip}]" if ":" in ip else ip
    if parts.port:
        netloc += f":{parts.port}"
    target = f"{parts.scheme}://{netloc}" + (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    with httpx.Client(timeout=8, follow_redirects=False) as c:
        r = c.get(target, headers={"Host": host, "User-Agent": "Mozilla/5.0"}, extensions={"sni_hostname": host})
        return r.headers.get("location", "") if r.is_redirect else ""


def _is_local(host: str) -> bool:
    if host in ("localhost",) or host.endswith((".local", ".lan", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


@functools.lru_cache(maxsize=256)
def scope(host: str) -> int:
    """0 public, 1 a private network (LAN, CGNAT/Tailscale, ULA), 2 this machine or its link
    (loopback, 0.0.0.0, link-local: cloud metadata lives at 169.254.169.254). A name counts as its
    most local address; one that doesn't resolve here (proxy-only, typo) as public."""
    try:
        ips = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        try:
            ips = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in socket.getaddrinfo(host, None)]
        except (OSError, UnicodeError, ValueError):
            return 0
    out = 0
    for ip in ips:
        ip = getattr(ip, "ipv4_mapped", None) or ip  # ::ffff:127.0.0.1 isn't loopback to Python < 3.13
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            return 2
        if not ip.is_global:
            out = 1
    return out


def inward(url: str, page: str) -> bool:
    """`url` sits on a more local network than the page that named it. A public page pointing at
    127.0.0.1, a router or 169.254.169.254 must not get siphon to fetch it and save the reply where
    `siphon serve` hands it to a phone; Chrome's Local Network Access draws the same line."""
    return scope(urlsplit(url).hostname or "") > scope(urlsplit(page).hostname or "")


def check(url: str) -> Reach:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host or parts.scheme not in ("http", "https") or _is_local(host):
        return Reach(host, True)  # LAN/loopback: public DoH can't speak for it, and ISPs don't block it
    port = parts.port or (443 if parts.scheme == "https" else 80)
    sys_ips = system_resolve(host, port)
    if sys_ips and tcp_ok(sys_ips[0], port):
        working = sys_ips[0]
        reach = Reach(host, True, system_ips=tuple(sys_ips))
    else:
        doh_ips = doh_resolve(host)
        good = next((ip for ip in doh_ips[:3] if tcp_ok(ip, port)), None)
        if not good:
            return Reach(host, False, "unreachable", f"{host} doesn't answer from this network, even with "
                         "independent DNS (outage, or blocked by IP)", tuple(sys_ips), tuple(doh_ips))
        reach = Reach(host, False, "dns", f"your ISP's DNS sends {host} to a dead address "
                      f"({sys_ips[0] if sys_ips else 'no answer'}); its real address ({good}) is reachable",
                      tuple(sys_ips), tuple(doh_ips))
        working = good
    if reach.ok and not any(host == h or host.endswith("." + h) for h in GEOFENCING_HOSTS):
        return reach  # healthy path: one DNS lookup + one TCP connect, no HTTP
    try:
        loc = _first_redirect(url, working, host)
    except Exception:
        return reach
    why = geofence_reason(loc) if loc else None
    if why:
        return Reach(host, False, "geofence", f"{why}: {loc}", reach.system_ips, reach.doh_ips)
    return reach


def proxy_alive(proxy: str, timeout: float = 3) -> bool:
    """A dead tunnel otherwise surfaces as "no media found", which points everywhere but the cause."""
    parts = urlsplit(proxy)
    if not parts.hostname:
        return False
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme, 1080)
    try:
        with socket.create_connection((parts.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def advice(r: Reach) -> str:
    exit_hint = ("it needs a connection that EXITS outside your country: e.g. a free cloud VM abroad + "
                 "`ssh -D 1080 user@vm`, then `--proxy socks5h://127.0.0.1:1080` "
                 "(or set SIPHON_PROXY, with SIPHON_PROXY_DOMAINS to route only blocked sites)")
    if r.layer == "geofence":
        return f"blocked by the platform for your region. Fixing DNS won't help; {exit_hint}."
    if r.layer == "dns":
        return ("blocked by your ISP at the DNS layer. Any proxy/VPN fixes this; if the platform also geofences "
                f"your country, {exit_hint}.")
    return "check the URL, or try again later / through --proxy."
