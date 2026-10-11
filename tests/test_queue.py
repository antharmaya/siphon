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
    # Survives a reload from disk.
    assert len(json.loads((q.QUEUE).read_text())) == 2


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
