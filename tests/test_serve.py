"""siphon serve: the security layers, intent validation, and a real job end to end
(API → siphon subprocess → file streamed back with Range), against the local fixture site."""

import json
import sys
import threading
import time

import httpx
import pytest

from siphon import jobs, paths, queue, serve


@pytest.fixture
def box(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(paths, "STATE", state)
    monkeypatch.setattr(jobs, "LOGS", state / "logs")
    monkeypatch.setattr(queue, "QUEUE", state / "queue.json")
    out = tmp_path / "dl"
    out.mkdir()
    app = serve.App(out, 2, allow_loopback_urls=True,
                    runner_kwargs={"siphon_cmd": [sys.executable, "-m", "siphon"], "extra_args": ["--no-netcheck"]})
    srv = serve.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app.runner.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield app, base, out
    app.runner.stop()
    srv.shutdown()
    srv.server_close()


def paired(base: str) -> httpx.Client:
    c = httpx.Client(base_url=base, timeout=10)
    r = c.post("/api/pair", json={"code": serve.new_pair_code()})
    assert r.status_code == 200, r.text
    return c


W = {"X-Siphon": "1"}


# ------------------------------------------------------------------ the guards

def test_static_page_and_host_allowlist(box):
    _, base, _ = box
    r = httpx.get(base + "/")
    assert r.status_code == 200 and "Content-Security-Policy" in r.headers and b"siphon" in r.content
    assert httpx.get(base + "/states/downloading.svg").headers["content-type"].startswith("image/svg")
    # DNS rebinding: a hostname we were never told about is refused
    assert httpx.get(base + "/", headers={"Host": "evil.example"}).status_code == 403
    assert httpx.get(base + "/", headers={"Host": "localhost"}).status_code == 200


def test_api_requires_pairing(box):
    _, base, _ = box
    assert httpx.get(base + "/api/status").status_code == 401
    assert httpx.post(base + "/api/jobs", json={"url": "https://x.example/v"}, headers=W).status_code == 401


def test_pairing_code_is_single_use(box):
    _, base, _ = box
    code = serve.new_pair_code()
    c = httpx.Client(base_url=base)
    assert c.post("/api/pair", json={"code": "0000-0000"}).status_code == 401
    r = c.post("/api/pair", json={"code": code})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert c.get("/api/status").status_code == 200
    assert httpx.post(base + "/api/pair", json={"code": code}).status_code == 401  # used up


def test_wrong_guesses_burn_the_code(box):
    _, base, _ = box
    code = serve.new_pair_code()
    for _ in range(serve.PAIR_TRIES):
        httpx.post(base + "/api/pair", json={"code": "1111-1111"})
    assert httpx.post(base + "/api/pair", json={"code": code}).status_code == 401


def test_pair_link_sets_cookie_and_redirects(box):
    _, base, _ = box
    code = serve.new_pair_code().replace("-", "")
    r = httpx.get(f"{base}/pair?code={code}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/" and "siphon_session=" in r.headers["set-cookie"]
    r = httpx.get(f"{base}/pair?code={code}", follow_redirects=False)
    assert r.headers["location"] == "/?pair=failed"


def test_cookie_writes_need_the_header_but_bearer_does_not(box):
    _, base, _ = box
    c = paired(base)
    assert c.post("/api/jobs", json={"url": "https://x.example/v"}).status_code == 403  # no X-Siphon
    assert c.post("/api/jobs", json={"url": "https://x.example/v"},
                  headers={**W, "Origin": "https://evil.example"}).status_code == 403  # foreign origin
    tok = {"Authorization": f"Bearer {serve.api_token()}"}
    r = httpx.post(base + "/api/jobs", json={"url": "https://x.example/v", "dest": "box"}, headers=tok)
    assert r.status_code == 201 and r.json()["dest"] == "box"
    assert httpx.get(base + "/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_logout_drops_the_session(box):
    _, base, _ = box
    c = paired(base)
    assert c.post("/api/logout", headers=W).status_code == 200
    assert c.get("/api/status").status_code == 401


# ------------------------------------------------------------------ intent, not flags

@pytest.mark.parametrize("body,msg", [
    ({"url": "file:///etc/passwd"}, "http"),
    ({"url": "https://x.example/v", "args": ["-o", "/"]}, "unknown option"),
    ({"url": "https://x.example/v", "preset": "evil"}, "preset"),
    ({"url": "https://x.example/v", "format": "-o /tmp"}, "format"),
    ({"url": "https://x.example/v", "section": "1:00; rm"}, "section"),
    ({"url": "https://x.example/v", "dest": "../../etc"}, "dest"),
    ({"url": "https://x.example/v", "sponsorblock": ["everything"]}, "sponsorblock"),
])
def test_bad_intent_is_refused(box, body, msg):
    _, base, _ = box
    r = httpx.post(base + "/api/jobs", json=body, headers={"Authorization": f"Bearer {serve.api_token()}"})
    assert r.status_code == 400 and msg in r.json()["error"]


def test_intent_maps_to_an_allowlisted_command():
    assert jobs.build_args({"url": "u", "preset": "compat", "audio": True, "section": "1:30-2:45",
                            "sponsorblock": True, "playlist": False}) == \
        ["-P", "compat", "-x", "--section", "1:30-2:45", "--sponsorblock", "sponsor", "--no-playlist"]
    assert jobs.build_args({"url": "u"}) == []


def test_ssrf_guard_refuses_this_machine_and_cloud_metadata():
    for url in ("http://127.0.0.1:8080/x", "http://localhost/x", "http://[::1]/x", "http://169.254.169.254/latest/"):
        with pytest.raises(jobs.BadRequest):
            jobs.validate_url(url)
    assert jobs.validate_url("http://127.0.0.1/x", allow_loopback=True)


# ------------------------------------------------------------------ the real thing

def _wait(c: httpx.Client, jid: str, timeout=120) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        job = next(j for j in c.get("/api/jobs").json() if j["id"] == jid)
        if job["status"] in ("done", "error"):
            return job
        time.sleep(.5)
    raise AssertionError(f"job {jid} didn't finish")


def test_job_runs_and_the_file_streams_back(box, site):
    _, base, out = box
    c = paired(base)
    r = c.post("/api/jobs", json={"url": site["base"] + "/media/clip.mp4", "dest": "device"}, headers=W)
    assert r.status_code == 201, r.text
    job = _wait(c, r.json()["id"])
    assert job["status"] == "done", job
    assert len(job["files"]) == 1
    on_disk = open(job["files"][0], "rb").read()
    part = c.get(f"/api/jobs/{job['id']}/files/0", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.content == on_disk[:100]
    assert part.headers["content-range"] == f"bytes 0-99/{len(on_disk)}"
    whole = c.get(f"/api/jobs/{job['id']}/files/0")
    assert whole.status_code == 200 and whole.content == on_disk
    assert "attachment" in whole.headers["content-disposition"]
    assert c.get(f"/api/jobs/{job['id']}/files/1").status_code == 404


def test_files_outside_the_download_folder_are_never_served(box, tmp_path):
    app, base, out = box
    c = paired(base)
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    job = queue.add(["https://x.example/v"], [])[0]
    app.runner.update(job["id"], status="done", files=[str(secret)])
    assert c.get(f"/api/jobs/{job['id']}/files/0").status_code == 404


def test_events_stream_starts_with_a_snapshot(box):
    _, base, _ = box
    c = paired(base)
    with c.stream("GET", "/api/events", timeout=10) as r:
        assert r.headers["content-type"] == "text/event-stream"
        lines = []
        for line in r.iter_lines():
            lines.append(line)
            if line.startswith("data:"):
                break
    assert lines[0] == "event: snapshot" and isinstance(json.loads(lines[-1][5:]), list)


def test_progress_json_stdout_is_only_events(site, tmp_path):
    """The machine channel: every stdout line is a JSON event, even though yt-dlp, gallery-dl and
    ffmpeg all print to stdout themselves (events.enable() moves fd 1 to stderr)."""
    import subprocess

    r = subprocess.run([sys.executable, "-m", "siphon", "--progress-json", "--no-netcheck", "-o", str(tmp_path),
                        site["base"] + "/hls/master.m3u8"], capture_output=True, text=True, timeout=120)
    events = [json.loads(line) for line in r.stdout.splitlines()]  # raises if anything else got in
    kinds = [e["event"] for e in events]
    assert kinds[0] == "resolving" and kinds[-1] == "done" and "saved" in kinds, (kinds, r.stderr[-800:])
    assert events[-1]["ok"] is True


def test_installable_share_target_and_the_shortcut_token(box):
    _, base, _ = box
    m = httpx.get(base + "/manifest.webmanifest").json()
    assert m["share_target"]["params"]["url"] == "url" and m["display"] == "standalone"
    sw = httpx.get(base + "/sw.js")
    assert sw.status_code == 200 and sw.headers["content-type"].startswith("text/javascript")
    assert "/api/" in sw.text  # the worker must never cache the API
    assert httpx.get(base + "/api/token").status_code == 401  # never to an unpaired caller
    c = paired(base)
    assert c.get("/api/token").json()["token"] == serve.api_token()
