"""Cloud destinations with real rclone against a `local` remote in an isolated config: the real
upload path runs, and no test can touch anyone's actual Google Drive."""

import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time

import httpx
import pytest

from siphon import cloud, jobs, paths, queue, serve

RCLONE = shutil.which("rclone") or (str(paths.BIN / "rclone") if (paths.BIN / "rclone").exists() else None)
needs_rclone = pytest.mark.skipif(not RCLONE, reason="rclone not installed (siphon setup rclone)")


@pytest.fixture
def fakecloud(tmp_path, monkeypatch):
    """A connected remote called `fakecloud` (rclone's local backend), cwd = its root."""
    monkeypatch.setenv("RCLONE_CONFIG", str(tmp_path / "rclone.conf"))
    monkeypatch.setenv("PATH", f"{shutil.os.path.dirname(RCLONE)}{shutil.os.pathsep}{shutil.os.environ['PATH']}")
    monkeypatch.setattr(paths, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(paths, "STATE", tmp_path / "state")
    subprocess.run([RCLONE, "config", "create", "fakecloud", "local"], check=True, capture_output=True)
    root = tmp_path / "cloudroot"
    root.mkdir()
    monkeypatch.chdir(root)  # local-backend folders are relative to the working directory
    return root


@needs_rclone
def test_targets_resolve_and_unknown_remotes_say_how_to_fix(fakecloud):
    assert cloud.remotes() == [("fakecloud", "local")]
    assert cloud.split_target("fakecloud:siphon/clips/") == ("fakecloud", "siphon/clips")
    with pytest.raises(cloud.CloudError, match="siphon cloud add gdrive"):
        cloud.split_target("gdrive:x")
    with pytest.raises(cloud.CloudError, match="no default cloud"):
        cloud.split_target("cloud")
    paths.save_config({"cloud_default": "fakecloud:inbox"})
    assert cloud.split_target("cloud") == ("fakecloud", "inbox")


@needs_rclone
def test_upload_moves_the_file_and_reports_progress(fakecloud, tmp_path):
    f = tmp_path / "clip.bin"
    f.write_bytes(b"x" * 300_000)
    ticks = []
    dest = cloud.upload(f, "fakecloud:up", progress=lambda d, t, s: ticks.append((d, t)))
    assert dest == "fakecloud:up/clip.bin"
    assert (fakecloud / "up" / "clip.bin").read_bytes() == b"x" * 300_000 and not f.exists()
    assert ticks and ticks[-1] == (300_000, 300_000)
    g = tmp_path / "keep.bin"
    g.write_bytes(b"y")
    cloud.upload(g, "fakecloud:up", keep=True)
    assert g.exists() and (fakecloud / "up" / "keep.bin").exists()


@needs_rclone
def test_cli_downloads_uploads_and_keeps_provenance(fakecloud, site, tmp_path):
    out = tmp_path / "dl"
    r = subprocess.run([sys.executable, "-m", "siphon", "--progress-json", "--no-netcheck", "-o", str(out),
                        "--to", "fakecloud:siphon", site["base"] + "/media/clip.mp4"],
                       capture_output=True, text=True, timeout=120)
    events = [json.loads(line) for line in r.stdout.splitlines()]
    kinds = [e["event"] for e in events]
    assert "uploading" in kinds and "uploaded" in kinds and events[-1] == {"event": "done", "ok": True}, (kinds, r.stderr[-600:])
    up = [e for e in events if e["event"] == "uploaded"][0]
    landed = fakecloud / "siphon" / up["remote"].split("/")[-1]
    assert landed.exists() and not list(out.glob("*.mp4"))  # moved, not copied
    rec = json.loads((out / "siphon.jsonl").read_text().splitlines()[-1])
    assert rec["uploaded_to"] == up["remote"]
    assert rec["sha256"] == hashlib.sha256(landed.read_bytes()).hexdigest()


@needs_rclone
def test_cli_refuses_an_unknown_remote_before_downloading(fakecloud, tmp_path):
    out = tmp_path / "dl"
    r = subprocess.run([sys.executable, "-m", "siphon", "--to", "nope:x", "-o", str(out), "https://example.com/v.mp4"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "siphon cloud add nope" in r.stderr and not out.exists()


@needs_rclone
def test_phone_job_can_go_straight_to_the_cloud(fakecloud, site, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "LOGS", tmp_path / "state" / "logs")
    monkeypatch.setattr(queue, "QUEUE", tmp_path / "state" / "queue.json")
    out = tmp_path / "dl"
    out.mkdir()
    app = serve.App(out, 1, allow_loopback_urls=True,
                    runner_kwargs={"siphon_cmd": [sys.executable, "-m", "siphon"], "extra_args": ["--no-netcheck"]})
    srv = serve.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app.runner.start()
    tok = {"Authorization": f"Bearer {serve.api_token()}"}
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        st = httpx.get(base + "/api/status", headers=tok).json()
        assert st["clouds"] == [{"id": "cloud:fakecloud", "name": "fakecloud", "type": "local"}]
        assert httpx.post(base + "/api/jobs", headers=tok, json={"url": "https://x.example/v", "dest": "cloud:gdrive"}).status_code == 400
        jid = httpx.post(base + "/api/jobs", headers=tok,
                         json={"url": site["base"] + "/media/clip.mp4", "dest": "cloud:fakecloud:phone"}).json()["id"]
        end = time.time() + 120
        while time.time() < end:
            job = next(j for j in httpx.get(base + "/api/jobs", headers=tok).json() if j["id"] == jid)
            if job["status"] in ("done", "error"):
                break
            time.sleep(.5)
        assert job["status"] == "done", job
        assert job["files"] == [] and job["remote"] and job["remote"][0].startswith("fakecloud:phone/")
        assert (fakecloud / "phone").exists() and list((fakecloud / "phone").iterdir())
    finally:
        app.runner.stop()
        srv.shutdown()
        srv.server_close()


@needs_rclone
def test_cloud_cli_list_and_default(fakecloud, capsys):
    assert cloud.main(["default", "fakecloud:inbox"]) == 0
    assert paths.load_config()["cloud_default"] == "fakecloud:inbox"
    assert cloud.main(["default", "missing:x"]) == 1
    assert cloud.main(["add", "weird name"]) == 1


@needs_rclone
def test_cloud_add_never_prints_the_stored_config(fakecloud, capfd):
    # rclone prints the finished config (a Drive refresh token, for one) on stdout
    assert cloud.main(["add", "disk", "--type", "sftp", "host=example.invalid", "user=me"]) == 0
    out, err = capfd.readouterr()
    assert "host = example.invalid" not in out + err and "connected" in err
    assert ("disk", "sftp") in cloud.remotes()


@pytest.fixture
def fake_rclone(tmp_path, monkeypatch):
    """A stand-in rclone with an R2 remote and a Drive remote; `link` prints a presigned URL."""
    d = tmp_path / "fakebin"
    d.mkdir()
    exe = d / "rclone"
    exe.write_text('''#!/bin/sh
case "$1" in
  listremotes) printf 'r2:          s3\\ngdrive:      drive\\n';;
  link) echo "https://acct.r2.cloudflarestorage.com/siphon/clip.mp4?X-Amz-Expires=86400&X-Amz-Signature=abc";;
esac
''')
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}{shutil.os.pathsep}{shutil.os.environ['PATH']}")


def test_presign_only_for_private_expiring_links(fake_rclone):
    url = cloud.presign("r2:siphon/clip.mp4")
    assert url.startswith("https://") and "Expires" in url
    with pytest.raises(cloud.CloudError, match="can't make private expiring links"):
        cloud.presign("gdrive:siphon/clip.mp4")  # rclone link would make a Drive file public


def test_phone_gets_a_link_straight_from_r2(fake_rclone, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "STATE", tmp_path / "state")
    monkeypatch.setattr(jobs, "LOGS", tmp_path / "state" / "logs")
    monkeypatch.setattr(queue, "QUEUE", tmp_path / "state" / "queue.json")
    app = serve.App(tmp_path, 1)
    srv = serve.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    tok = {"Authorization": f"Bearer {serve.api_token()}"}
    try:
        j1 = queue.add(["https://x.example/a"], [])[0]
        app.runner.update(j1["id"], status="done", remote=["r2:siphon/clip.mp4"], files=[])
        j2 = queue.add(["https://x.example/b"], [])[0]
        app.runner.update(j2["id"], status="done", remote=["gdrive:siphon/clip.mp4"], files=[])
        r = httpx.get(f"{base}/api/jobs/{j1['id']}/link/0", headers=tok)
        assert r.status_code == 200 and "X-Amz-Signature" in r.json()["url"]
        assert httpx.get(f"{base}/api/jobs/{j2['id']}/link/0", headers=tok).status_code == 409
        assert httpx.get(f"{base}/api/jobs/{j1['id']}/link/3", headers=tok).status_code == 404
        assert httpx.get(f"{base}/api/jobs/{j1['id']}/link/0").status_code == 401
    finally:
        srv.shutdown()
        srv.server_close()
