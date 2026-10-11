"""Reaching siphon serve via tailscale / cloudflared — with fake binaries, so tests never touch
a real tailnet or open a real public tunnel."""

import os
import stat
import threading

import httpx
import pytest

from siphon import jobs, paths, queue, reach, serve


def _script(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def fakebin(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    return d


def healthy_tailscale(d):
    _script(d / "tailscale", '''case "$1" in
  status) echo '{"BackendState":"Running","Self":{"DNSName":"box.tail9.ts.net."}}';;
  serve|funnel) echo "Available within your tailnet:"; echo; echo "https://box.tail9.ts.net/"; echo "|-- / proxy http://127.0.0.1:$3"; exec sleep 60;;
esac
''')


def test_tailscale_private_exposure(fakebin):
    healthy_tailscale(fakebin)
    exp = reach.tailscale(8765)
    try:
        assert exp.url == "https://box.tail9.ts.net" and exp.host == "box.tail9.ts.net"
        assert exp.kind == "tailscale" and not exp.public and exp.proc.poll() is None
    finally:
        exp.stop()
    assert exp.proc.poll() is not None  # stopping siphon stops the exposure


def test_funnel_is_marked_public(fakebin):
    healthy_tailscale(fakebin)
    exp = reach.tailscale(8765, public=True)
    try:
        assert exp.kind == "funnel" and exp.public
    finally:
        exp.stop()


@pytest.mark.parametrize("status,hint", [
    ("", "daemon isn't running"),
    ('{"BackendState":"Stopped"}', "sudo tailscale up"),
    ('{"BackendState":"NeedsLogin"}', "NeedsLogin"),
])
def test_tailscale_not_ready_says_what_to_do(fakebin, status, hint):
    _script(fakebin / "tailscale", f"echo '{status}'\n" if status else "exit 1\n")
    with pytest.raises(reach.ReachError, match=hint):
        reach.tailscale(8765)


def test_tailscale_permission_problem_is_explained(fakebin):
    _script(fakebin / "tailscale", '''case "$1" in
  status) echo '{"BackendState":"Running","Self":{"DNSName":"box.tail9.ts.net."}}';;
  serve) echo "Access denied: serve config denied"; exit 1;;
esac
''')
    with pytest.raises(reach.ReachError, match="--operator"):
        reach.tailscale(8765, timeout=5)


def test_tailscale_missing(fakebin, monkeypatch):
    monkeypatch.setenv("PATH", str(fakebin))
    with pytest.raises(reach.ReachError, match="isn't installed"):
        reach.tailscale_name()


def test_cloudflare_quick_tunnel(fakebin):
    _script(fakebin / "cloudflared", '''echo "INF Requesting new quick Tunnel on trycloudflare.com..." >&2
echo "INF |  https://quiet-river-1234.trycloudflare.com                   |" >&2
exec sleep 60
''')
    exp = reach.cloudflare(8765)
    try:
        assert exp.url == "https://quiet-river-1234.trycloudflare.com" and exp.public
    finally:
        exp.stop()


def test_requests_through_a_tunnel_get_secure_cookies(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STATE", tmp_path / "state")
    monkeypatch.setattr(jobs, "LOGS", tmp_path / "state" / "logs")
    monkeypatch.setattr(queue, "QUEUE", tmp_path / "state" / "queue.json")
    app = serve.App(tmp_path, 1)
    srv = serve.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        tunnel = {"Host": "box.tail9.ts.net"}
        assert httpx.get(base + "/", headers=tunnel).status_code == 403  # unknown until the tunnel is up
        app.allow_hosts.add("box.tail9.ts.net")
        app.https_hosts.add("box.tail9.ts.net")
        r = httpx.post(base + "/api/pair", json={"code": serve.new_pair_code()}, headers=tunnel)
        assert r.status_code == 200 and "Secure" in r.headers["set-cookie"]
        r = httpx.post(base + "/api/pair", json={"code": serve.new_pair_code()})  # plain local http
        assert "Secure" not in r.headers["set-cookie"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_qr_and_lan_ip():
    q = reach.qr("http://example.test/pair?code=12345678")
    assert q is None or ("█" in q or "▀" in q or "▄" in q)
    ip = reach.lan_ip()
    assert ip is None or ip.count(".") == 3
