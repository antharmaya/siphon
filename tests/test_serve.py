"""siphon serve: the security layers, intent validation, and a real job end to end
(API → siphon subprocess → file streamed back with Range), against the local fixture site."""

import json
import sys
import time

import httpx
import pytest

from siphon import jobs, queue, serve


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


# ------------------------------------------------------------------ live updates in the PWA
# Cloudflare quick tunnels buffer SSE until the stream closes, so the page must notice the silence
# and poll. live.js has no DOM, so node drives it here with a fake clock and a fake EventSource.
LIVE_HARNESS = r"""
const vm = require("vm"), fs = require("fs");
let now = 0, nextId = 1, timers = [];
const clock = {
  setTimeout(fn, ms) { const id = nextId++; timers.push({ id, at: now + ms, fn }); return id; },
  clearTimeout(id) { timers = timers.filter((t) => t.id !== id); },
};
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
async function advance(to) {
  for (;;) {
    timers.sort((a, b) => a.at - b.at || a.id - b.id);
    const t = timers[0];
    if (!t || t.at > to) break;
    timers.shift(); now = t.at; t.fn(); await flush();
  }
  now = to;
}
class FakeES {  // plays a script of [ms after open, "snapshot" | "job" | "error"]
  constructor(script) {
    this.closed = false; this.on = {}; this.onerror = null;
    for (const [at, what] of script) clock.setTimeout(() => this.closed || this.fire(what), at);
  }
  addEventListener(name, fn) { this.on[name] = fn; }
  fire(what) {
    if (what === "error") return this.onerror && this.onerror();
    this.on[what] && this.on[what]({ data: what === "snapshot" ? "[]" : '{"id":"ab12"}' });
  }
  close() { this.closed = true; }
}
const ctx = vm.createContext({});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
async function scenario(scripts, busy, until, marks) {
  timers = []; now = 0;
  const out = { polls: [], snapshots: 0, connects: 0, modes: {} }, made = [];
  const live = ctx.liveUpdates({
    connect: () => { const es = new FakeES(scripts[made.length] || []); made.push(es); out.connects++; return es; },
    fetchJobs: async () => { out.polls.push(now); return []; },
    onSnapshot: () => out.snapshots++, onJob: () => {}, busy: () => busy, timers: clock,
  });
  live.start();
  for (const m of marks) { await advance(m); out.modes[m] = live.mode; }
  await advance(until);
  out.closed = made.map((e) => e.closed);
  out.mode = live.mode;
  return out;
}
(async () => console.log(JSON.stringify({
  buffered: await scenario([], true, 7600, [2999, 3000]),
  idle: await scenario([], false, 23100, []),
  working: await scenario([[[100, "snapshot"], [5000, "job"]]], true, 30000, []),
  refused: await scenario([[[50, "error"]]], true, 100, []),
  recovers: await scenario([[], [[200, "snapshot"]]], true, 70000, [59999]),
})))();
"""


@pytest.fixture(scope="module")
def live_runs(tmp_path_factory):
    import shutil
    import subprocess

    if not shutil.which("node"):
        pytest.skip("node not installed")
    h = tmp_path_factory.mktemp("live") / "harness.js"
    h.write_text(LIVE_HARNESS)
    r = subprocess.run(["node", str(h), str(serve.WEB / "live.js")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_silent_event_stream_falls_back_to_polling(live_runs):
    b = live_runs["buffered"]
    assert b["modes"] == {"2999": "sse", "3000": "polling"}  # waits ~3 s for the snapshot, no longer
    assert b["polls"] == [3000, 4500, 6000, 7500]  # every 1.5 s while a job is active
    assert b["closed"] == [True]  # a buffered stream must not flush a stale snapshot later
    assert live_runs["idle"]["polls"] == [3000, 13000, 23000]  # slower when nothing is running


def test_working_event_stream_never_polls(live_runs):
    w = live_runs["working"]
    assert w["mode"] == "sse" and w["polls"] == [] and w["snapshots"] == 1 and w["closed"] == [False]
    r = live_runs["refused"]
    assert r["mode"] == "polling" and r["polls"] == [50]  # an error doesn't wait out the 3 s


def test_polling_hands_back_to_sse_when_it_gets_through(live_runs):
    r = live_runs["recovers"]  # SSE retried at 63 s; that stream's snapshot arrives at 63.2 s
    assert r["modes"]["59999"] == "polling" and r["connects"] == 2 and r["mode"] == "sse"
    assert r["closed"] == [True, False] and max(r["polls"]) < 63200  # polling stopped once it spoke


def test_live_updates_script_is_served_and_cached(box):
    _, base, _ = box
    r = httpx.get(base + "/live.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    page = httpx.get(base + "/").text
    assert page.index("/live.js") < page.index("/app.js")  # app.js calls liveUpdates at boot
    assert "liveUpdates({" in httpx.get(base + "/app.js").text  # not a bare EventSource
    assert '"/live.js"' in httpx.get(base + "/sw.js").text  # offline shell needs it too
