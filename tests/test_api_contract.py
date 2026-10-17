"""The engine against docs/api-v1.md: versioned routes, features, job labels, and an event stream
that resumes from Last-Event-ID, including events written by another siphon process."""

import json
import sqlite3


from siphon import jobs, queue
from siphon.store import Store
from test_serve import W, paired


def read_events(c, headers=None, until=lambda evs: len(evs) >= 1, limit=20):
    """Parse SSE frames until `until(events)` is true. Each event: {"id", "event", "data"}."""
    out, cur = [], {}
    with c.stream("GET", "/api/v1/events", headers=headers or {}, timeout=10) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("id: "):
                cur["id"] = int(line[4:])
            elif line.startswith("event: "):
                cur["event"] = line[7:]
            elif line.startswith("data: "):
                cur["data"] = json.loads(line[6:])
            elif line == "" and cur:
                out.append(cur)
                cur = {}
                if until(out) or len(out) >= limit:
                    break
    return out


def test_status_names_the_api_and_its_features(box):
    _, base, _ = box
    c = paired(base)
    s = c.get("/api/v1/status").json()
    assert s["api"] == 1 and {"jobs", "events.resume", "jobs.priority"} <= set(s["features"])
    assert c.get("/api/status").json()["api"] == 1  # the unversioned alias answers the same


def test_jobs_carry_priority_category_and_tags(box):
    app, base, _ = box
    app.runner.stop()  # keep jobs pending so the order is observable
    c = paired(base)
    low = c.post("/api/v1/jobs", json={"url": "https://a.example/low"}, headers=W).json()
    high = c.post("/api/v1/jobs", json={"url": "https://a.example/high", "priority": 5, "category": "movies",
                                        "tags": ["b", "a", "a"]}, headers=W).json()
    assert (high["priority"], high["category"], high["tags"]) == (5, "movies", ["a", "b"])
    assert low["priority"] == 0 and low["category"] is None
    assert queue.db().claim()["id"] == high["id"]  # higher priority runs first, though added later
    for bad in ({"priority": "high"}, {"priority": 1000}, {"category": ""}, {"tags": "x"}):
        r = c.post("/api/v1/jobs", json={"url": "https://a.example/x", **bad}, headers=W)
        assert r.status_code == 400, bad


def test_event_stream_resumes_after_a_gap_and_sees_other_processes(box):
    app, base, _ = box
    app.runner.stop()
    c = paired(base)
    first = read_events(c)
    assert first[0]["event"] == "snapshot" and "id" in first[0]
    seen = first[0]["id"]
    # Changes made while the client was away: one through the API, one by another process
    # (a separate store object on the same database file, as `siphon queue add` would be).
    a = c.post("/api/v1/jobs", json={"url": "https://a.example/1"}, headers=W).json()
    other = Store(queue.QUEUE.with_name("queue.db")).add(["https://a.example/2"], [])[0]
    queue.db().remove([a["id"]])
    evs = read_events(c, {"Last-Event-ID": str(seen)}, until=lambda e: len(e) >= 3)
    assert [(e["event"], e.get("data", {}).get("id")) for e in evs] == [
        ("job", a["id"]), ("job", other["id"]), ("removed", a["id"])]
    assert [e["id"] for e in evs] == sorted(e["id"] for e in evs) and evs[0]["id"] > seen


def test_a_client_too_far_behind_gets_a_fresh_snapshot(box):
    _, base, _ = box
    c = paired(base)
    c.post("/api/v1/jobs", json={"url": "https://a.example/1"}, headers=W)
    for bogus in ("999999", "-5"):  # a future id (another database) or one pruned long ago
        db = sqlite3.connect(queue.QUEUE.with_name("queue.db"))
        db.execute("DELETE FROM events WHERE seq < (SELECT MAX(seq) FROM events)")
        db.commit()
        db.close()
        evs = read_events(c, {"Last-Event-ID": bogus})
        assert evs[0]["event"] == "snapshot", bogus


def test_old_store_upgrades_in_place(tmp_path):
    """A version-1 database (the first SQLite store) gains the new columns and keeps its jobs."""
    from siphon.store import MIGRATIONS
    path = tmp_path / "queue.db"
    db = sqlite3.connect(path)
    db.executescript(MIGRATIONS[0])
    db.execute("INSERT INTO jobs(id, status, owner, data) VALUES (?,?,?,?)",
               ("abcd1234", "done", None, json.dumps({"url": "u", "args": [], "added": "2026-10-01T10:00:00",
                                                      "category": "disc", "file": "/x.mp4"})))
    db.execute("PRAGMA user_version=1")
    db.commit()
    db.close()
    job = Store(path).get("abcd1234")
    assert (job["status"], job["category"], job["added"], job["priority"], job["file"]) == \
        ("done", "disc", "2026-10-01T10:00:00", 0, "/x.mp4")
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 2


def test_organise_rejects_bad_labels():
    import pytest
    for bad in ({"priority": True}, {"category": "x" * 65}, {"tags": [""]}):
        with pytest.raises(jobs.BadRequest):
            jobs.organise(**bad)


def test_pause_resume_and_edit_a_queued_job(box):
    app, base, _ = box
    app.runner.stop()
    c = paired(base)
    j = c.post("/api/v1/jobs", json={"url": "https://a.example/1"}, headers=W).json()
    assert c.post(f"/api/v1/jobs/{j['id']}/pause", headers=W).json()["status"] == "paused"
    assert queue.db().claim() is None  # a paused job is not picked up
    assert c.post(f"/api/v1/jobs/{j['id']}/pause", headers=W).status_code == 409
    assert c.post(f"/api/v1/jobs/{j['id']}/resume", headers=W).json()["status"] == "pending"
    assert c.post(f"/api/v1/jobs/{j['id']}/resume", headers=W).status_code == 409
    e = c.patch(f"/api/v1/jobs/{j['id']}", json={"priority": 3, "category": "talks", "tags": ["x"]}, headers=W).json()
    assert (e["priority"], e["category"], e["tags"]) == (3, "talks", ["x"])
    assert c.patch(f"/api/v1/jobs/{j['id']}", json={"category": None}, headers=W).json()["category"] is None
    assert c.patch(f"/api/v1/jobs/{j['id']}", json={"url": "https://evil.example/"}, headers=W).status_code == 400
    assert c.get(f"/api/v1/jobs/{j['id']}").json()["id"] == j["id"]
    assert c.get("/api/v1/jobs/00000000").status_code == 404


def test_pausing_a_running_download_stops_it_and_keeps_it_paused(tmp_path, monkeypatch):
    import sys
    import time

    from siphon import paths
    monkeypatch.setattr(paths, "STATE", tmp_path / "state")
    monkeypatch.setattr(jobs, "LOGS", tmp_path / "state" / "logs")
    monkeypatch.setattr(queue, "QUEUE", tmp_path / "state" / "queue.json")
    slow = [sys.executable, "-c", "import time; time.sleep(60)"]  # stands in for a long download
    r = jobs.Runner(tmp_path, 1, siphon_cmd=slow)
    r.start()
    try:
        j = r.submit({"url": "https://a.example/big"}, allow_loopback=True)
        deadline = time.monotonic() + 10
        while not (r.live.get(j["id"]) or {}).get("proc") and time.monotonic() < deadline:
            time.sleep(0.05)
        proc = r.live[j["id"]]["proc"]
        assert r.pause(j["id"])["status"] == "paused"
        proc.wait(timeout=10)  # the download process really stopped
        time.sleep(0.3)
        assert queue.db().get(j["id"])["status"] == "paused"  # not recorded as an error
    finally:
        r.stop()


def test_cli_queue_hands_work_to_a_running_engine(tmp_path):
    """Real processes: `siphon serve` running, then the CLI. `queue run` leaves the jobs to the engine,
    and `queue pause` goes through the engine, which stops the running download."""
    import os
    import socket
    import subprocess
    import sys
    import time

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "SIPHON_STATE": str(tmp_path / "state"), "SIPHON_QUEUE": str(tmp_path / "state" / "queue.json")}

    def cli(*a):
        return subprocess.run([sys.executable, "-m", "siphon", "queue", *a], env=env, capture_output=True, text=True, timeout=60)

    eng = subprocess.Popen([sys.executable, "-m", "siphon", "serve", "--port", str(port), "--no-qr", "-o", str(tmp_path)],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        serve_json = tmp_path / "state" / "serve.json"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and '"engine"' not in (serve_json.read_text() if serve_json.exists() else ""):
            time.sleep(0.2)
        second = subprocess.run([sys.executable, "-m", "siphon", "serve", "--port", "0", "--no-qr"], env=env,
                                capture_output=True, text=True, timeout=60)
        assert second.returncode == 1 and "already running" in second.stderr  # one engine per user
        # A non-routable address: the download hangs on connect, so the job stays running.
        assert cli("add", "http://10.255.255.1/big.mp4").returncode == 0
        r = cli("run")
        assert r.returncode == 0 and "engine" in r.stderr and "is running the queue" in r.stderr
        from siphon.store import Store
        store = Store(tmp_path / "state" / "queue.db")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and store.all()[0]["status"] != "active":
            time.sleep(0.2)
        jid = store.all()[0]["id"]
        r = cli("pause", jid[:4])
        assert r.returncode == 0, r.stderr
        time.sleep(1)
        assert store.get(jid)["status"] == "paused"
        assert cli("resume", jid).returncode == 0 and store.get(jid)["status"] in ("pending", "active")
    finally:
        eng.terminate()
        eng.wait(timeout=10)
    assert '"engine"' not in serve_json.read_text()  # a stopped engine removes its record


def test_engine_service_uses_a_sandboxed_browsers_login(tmp_path, monkeypatch):
    from siphon import engine
    monkeypatch.setenv("HOME", str(tmp_path))
    assert engine.default_cookies() is None
    (tmp_path / ".var/app/app.zen_browser.zen/.zen").mkdir(parents=True)
    name, src = engine.default_cookies()
    assert name == "Zen (Flatpak)" and src == f"firefox:{tmp_path}/.var/app/app.zen_browser.zen/.zen"
    unit = engine.unit_text("/opt/my tools/siphon", src)
    assert f"ExecStart='/opt/my tools/siphon' serve --no-qr --cookies-from-browser firefox:{tmp_path}" in unit
    assert "WantedBy=default.target" in unit and "Restart=on-failure" in unit
    assert "--cookies" not in engine.unit_text("/usr/bin/siphon", None)
