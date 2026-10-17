"""Queue persistence + logic, no network (the runner spawns real siphon subprocesses,
covered by live testing)."""

import importlib
import json

import pytest


@pytest.fixture
def q(tmp_path, monkeypatch):
    monkeypatch.setenv("SIPHON_QUEUE", str(tmp_path / "queue.json"))
    import siphon.queue as queue

    importlib.reload(queue)  # pick up the env-driven QUEUE path
    return queue


def test_add_list_and_persist(q):
    added = q.add(["https://a/1", "https://b/2"], ["-P", "compat"])
    assert len(added) == 2 and all(j["status"] == "pending" for j in added)
    jobs = q.load()
    assert [j["url"] for j in jobs] == ["https://a/1", "https://b/2"]
    assert jobs[0]["args"] == ["-P", "compat"] and len(jobs[0]["id"]) == 8
    # Survives a reload from disk: a fresh store on the same database sees both.
    from siphon.store import Store
    assert len(Store(q.QUEUE.with_name("queue.db")).all()) == 2


def test_remove_by_id_prefix(q):
    jobs = q.add(["u1", "u2", "u3"], [])
    assert q.remove([jobs[1]["id"][:4]]) == 1
    assert [j["url"] for j in q.load()] == ["u1", "u3"]
    assert q.remove(["nope"]) == 0


def test_clear_variants(q):
    q.add(["u1", "u2", "u3", "u4"], [])
    jobs = q.load()
    jobs[0]["status"] = "done"
    jobs[1]["status"] = "error"
    jobs[2]["status"] = "active"
    q.save(jobs)
    assert q.clear("done") == 1  # only the completed one
    assert q.clear("finished") == 1  # the errored one (pending/active kept)
    assert {j["status"] for j in q.load()} == {"pending", "active"}
    assert q.clear("all") == 2


def test_new_files_reads_only_new_records(q, tmp_path):
    prov = tmp_path / "siphon.jsonl"
    prov.write_text('{"file": "old.mp4"}\n')
    since = prov.stat().st_size
    with open(prov, "a") as f:
        f.write('{"file": "a.mkv"}\n{"file": "b.jpg"}\n')
    assert q._new_files(prov, since, tmp_path) == [str(tmp_path / "a.mkv"), str(tmp_path / "b.jpg")]
    assert q._new_files(tmp_path / "none.jsonl", 0, tmp_path) == []


def test_has_output_detection(q):
    assert q._has_output(["-o", "/tmp"]) and q._has_output(["--output", "/x"])
    assert not q._has_output(["-P", "best", "--section", "1-2"])


def test_old_queue_json_is_imported_once(q):
    q.QUEUE.parent.mkdir(parents=True, exist_ok=True)
    q.QUEUE.write_text(json.dumps([{"id": "abcd1234", "url": "https://a/1", "args": [], "status": "error",
                                    "error": "boom", "file": None}]))
    jobs = q.load()
    assert [(j["id"], j["status"], j["error"]) for j in jobs] == [("abcd1234", "error", "boom")]
    assert not q.QUEUE.exists() and q.QUEUE.with_name("queue.json.migrated").exists()
    assert len(q.load()) == 1  # not imported twice


def test_recover_leaves_a_live_runners_job_alone(q):
    import os

    from siphon.store import me
    a, b = q.add(["u1", "u2"], [])
    q.db().update(a["id"], status="active", owner=me())               # this process: alive
    q.db().update(b["id"], status="active", owner=f"{2**22 + 7}:1")   # no such pid: dead
    assert q.db().recover() == 1
    got = {j["url"]: j["status"] for j in q.load()}
    assert got == {"u1": "active", "u2": "pending"}
    assert os.getpid()  # (me() is pid:start-time of this test process)


def test_two_runner_processes_never_run_a_job_twice(tmp_path):
    """Two `siphon queue run` processes on one queue: each job runs exactly once.

    The fake siphon appends the URL it was given to a shared ledger and sleeps a little, so
    overlapping runners show up as duplicate lines."""
    import subprocess
    import sys
    import textwrap

    state = tmp_path / "state"
    ledger = tmp_path / "ledger.txt"
    fake = tmp_path / "bin" / "siphon"
    fake.parent.mkdir()
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys, time
        with open({str(ledger)!r}, "a") as f:
            f.write(sys.argv[1] + "\\n")
        time.sleep(0.3)
        """))
    fake.chmod(0o755)
    env = {**__import__("os").environ, "SIPHON_STATE": str(state), "SIPHON_QUEUE": str(state / "queue.json"),
           "PATH": f"{fake.parent}:{__import__('os').environ['PATH']}"}
    urls = [f"https://example.com/{i}" for i in range(12)]
    subprocess.run([sys.executable, "-m", "siphon", "queue", "add", *urls], env=env, check=True, capture_output=True)
    runners = [subprocess.Popen([sys.executable, "-m", "siphon", "queue", "run", "-N", "2", "-o", str(tmp_path / "dl")],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(2)]
    for r in runners:
        r.wait(timeout=120)
    ran = ledger.read_text().split()
    assert sorted(ran) == sorted(urls), f"ran {len(ran)} times for {len(urls)} jobs"
