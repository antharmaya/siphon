"""Native-messaging host: framing + progress parsing (no browser needed)."""

def _load_host():
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "extension" / "host" / "siphon_host.py"
    spec = importlib.util.spec_from_file_location("siphon_host", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_host_framing_roundtrip(tmp_path, monkeypatch):
    import io
    import struct

    host = _load_host()
    msg = {"action": "hello"}
    data = __import__("json").dumps(msg).encode()
    monkeypatch.setattr(host.sys, "stdin", type("S", (), {"buffer": io.BytesIO(struct.pack("<I", len(data)) + data)})())
    assert host.read_message() == msg
    # send() must emit a length prefix + the JSON payload.
    out = io.BytesIO()
    monkeypatch.setattr(host.sys, "stdout", type("S", (), {"buffer": out})())
    host.send({"event": "hello", "version": "9.9"})
    buf = out.getvalue()
    (n,) = struct.unpack("<I", buf[:4])
    assert __import__("json").loads(buf[4:4 + n]) == {"event": "hello", "version": "9.9"}


def test_host_progress_parsing():
    host = _load_host()
    assert host.PCT.search("[download]  58.1% of 218KiB at 2.90MiB/s").group(1) == "58.1"
    assert host.SPEED.search("[download] 100% of 218KiB at 2.90MiB/s").group(1) == "2.90MiB/s"
    assert host.SAVED.search("✓ /home/u/Downloads/clip [ab12].mp4  28MB").group(1) == "/home/u/Downloads/clip [ab12].mp4"
    assert host.SAVED.search('[download] Destination: /tmp/My Video.webm').group(1) == "/tmp/My Video.webm"


def test_host_reads_saved_files_from_provenance(tmp_path):
    host = _load_host()
    prov = tmp_path / "siphon.jsonl"
    prov.write_text('{"file": "old.mp4"}\n')
    since = prov.stat().st_size
    # A run appends two records (e.g. a multi-image tweet); only the new ones count.
    with open(prov, "a") as f:
        f.write('{"file": "a [emoji \U0001f577️].mkv"}\n{"file": "b.jpg"}\n')
    got = host.read_saved(prov, since, tmp_path)
    assert got == [str(tmp_path / "a [emoji \U0001f577️].mkv"), str(tmp_path / "b.jpg")]
    assert host.read_saved(prov, prov.stat().st_size, tmp_path) == []  # nothing new
    assert host.read_saved(tmp_path / "nope.jsonl", 0, tmp_path) == []


def test_host_reads_a_firefox_forks_own_profiles(tmp_path, monkeypatch):
    host = _load_host()
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".zen").mkdir()
    assert host.cookie_source("firefox", "/app/zen/zen") == f"firefox:{tmp_path}/.zen"
    assert host.cookie_source("firefox", "/usr/lib64/firefox/firefox") == "firefox"
    assert host.cookie_source("firefox", "/usr/bin/librewolf") == "firefox"  # no ~/.librewolf here
    assert host.cookie_source("brave", "/app/zen/zen") == "brave"



def test_host_retries_when_the_installed_siphon_is_older_than_the_host(tmp_path, monkeypatch):
    # 0.3.0's CLI rejects --cookies-optional; a newer host in front of it failed every click (2026-10-17).
    import io
    import json
    import struct

    host = _load_host()
    old = tmp_path / "siphon"
    old.write_text("#!/bin/sh\nfor a in \"$@\"; do [ \"$a\" = --cookies-optional ] && "
                   "{ echo 'siphon: error: unrecognized arguments: --cookies-optional'; exit 2; }; done\n"
                   "echo \"✓ $3/clip.mp4  1MB\"\n")
    old.chmod(0o755)
    monkeypatch.setenv("SIPHON_BIN", str(old))
    monkeypatch.setenv("SIPHON_DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("SIPHON_HOST_LOG", str(tmp_path / "host.log"))
    monkeypatch.setattr(host, "LOG", tmp_path / "host.log")
    out = io.BytesIO()
    monkeypatch.setattr(host.sys, "stdout", type("S", (), {"buffer": out})())
    host.run_download({"page": "https://example.com/v", "browser": "chrome"})
    buf, events = out.getvalue(), []
    while buf:
        (n,) = struct.unpack("<I", buf[:4])
        events.append(json.loads(buf[4:4 + n]))
        buf = buf[4 + n:]
    assert events[-1] == {"event": "done", "file": f"{tmp_path}/clip.mp4"}
    assert "retrying without it" in (tmp_path / "host.log").read_text()


def test_a_click_on_a_video_skips_the_image_post_check(tmp_path, monkeypatch):
    import io

    host = _load_host()
    seen = tmp_path / "argv"
    fake = tmp_path / "siphon"
    fake.write_text(f"#!/bin/sh\necho \"$@\" > {seen}\n")
    fake.chmod(0o755)
    monkeypatch.setenv("SIPHON_BIN", str(fake))
    monkeypatch.setenv("SIPHON_DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(host, "LOG", tmp_path / "host.log")
    monkeypatch.setattr(host.sys, "stdout", type("S", (), {"buffer": io.BytesIO()})())
    host.run_download({"page": "https://x.com/a/status/1", "video": True})
    assert "--no-gallery" in seen.read_text().split()
    host.run_download({"page": "https://x.com/a/status/1"})  # "Download all images" keeps gallery-dl
    assert "--no-gallery" not in seen.read_text().split()

def _load_build():
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "scripts" / "build-extension.py"
    spec = importlib.util.spec_from_file_location("build_extension", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_firefox_build_is_an_event_page_with_an_addon_id_and_no_chrome_keys():
    import json

    b = _load_build()
    src = json.loads((b.SRC / "manifest.json").read_text())
    m = b.firefox_manifest(src)
    assert m["background"] == {"scripts": ["policy.js", "background.js"]}  # policy first: no importScripts there
    gecko = m["browser_specific_settings"]["gecko"]
    assert gecko["id"] == "siphon@antharmaya.com" and gecko["data_collection_permissions"]["required"]
    assert "key" not in m and "minimum_chrome_version" not in m
    assert not any(cs.get("exclude_matches") for cs in m["content_scripts"])  # full edition: nothing excluded
    assert '"blockedHosts": []' in b.firefox_policy()
    assert "key" in src and "service_worker" in src["background"]  # the source manifest is untouched


def test_store_build_still_keeps_youtube_out():
    import json

    b = _load_build()
    m = b.store_manifest(json.loads((b.SRC / "manifest.json").read_text()))
    assert all("*://*.youtube.com/*" in cs["exclude_matches"] for cs in m["content_scripts"])
    assert "youtube.com" in b.store_policy()
