"""End-to-end through real Chrome against the local fixture site. Every output file
is checked with ffprobe — "a file appeared" is not the same as "a video downloaded"."""

import asyncio
import json
import shutil
import subprocess

import pytest

from siphon.cli import main
from siphon.resolve.cdp import find_chrome, has_proprietary_codecs
from siphon.resolve.sniff import SniffOptions, sniff

pytestmark = pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe required")


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of", "json",
                          str(path)], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    return sorted(s["codec_type"] for s in d["streams"]), float(d["format"]["duration"])


def run_sniff(url, tmp_path, **kw):
    return asyncio.run(sniff(url, SniffOptions(timeout=15, profile=None, workdir=tmp_path / "cap", **kw)))


def test_chrome_present():
    assert has_proprietary_codecs(find_chrome())


def test_sniff_late_loaded_video(site, tmp_path):
    r = run_sniff(f"{site['base']}/late.html", tmp_path)
    best = r.candidates[0]
    assert best.kind == "file" and best.url.endswith("/media/clip.mp4") and best.source == "sniff"
    assert best.title == "Late Loaded Clip"
    assert best.headers.get("referer", "").endswith("/late.html")


def test_sniff_prefers_hls_master(site, tmp_path):
    r = run_sniff(f"{site['base']}/hls.html", tmp_path)
    assert r.candidates[0].kind == "hls" and r.candidates[0].is_master


def test_capture_mse_blob_stream(site, tmp_path):
    r = run_sniff(f"{site['base']}/mse.html", tmp_path)
    assert r.mse_seen
    assert not any(c.source == "sniff" for c in r.candidates), "fixture must defeat the network pass"
    cap = r.candidates[0]
    assert cap.kind == "capture" and len(cap.tracks) == 2, cap
    assert "complete" in cap.note


def test_capture_revoked_blob(site, tmp_path):
    r = run_sniff(f"{site['base']}/blob.html", tmp_path)
    blob = next(c for c in r.candidates if c.kind == "capture" and c.url.startswith("blob-capture://"))
    assert blob.size == (site["root"] / "media" / "clip.mp4").stat().st_size


@pytest.mark.parametrize("page,expect_streams", [
    ("late.html", ["audio", "video"]),
    ("hls.html", ["audio", "video"]),
    ("mse.html", ["audio", "video"]),
    ("blob.html", ["audio", "video"]),
])
def test_cli_downloads_playable_file(site, tmp_path, page, expect_streams):
    out = tmp_path / "out"
    rc = main([f"{site['base']}/{page}", "-o", str(out), "--no-profile", "--timeout", "15", "-q"])
    assert rc == 0
    files = [p for p in out.iterdir() if p.suffix in (".mp4", ".mkv", ".webm")]
    assert len(files) == 1, list(out.iterdir())
    streams, duration = probe(files[0])
    assert streams == expect_streams
    assert 5.0 <= duration <= 7.0, duration
    log = [json.loads(ln) for ln in (out / "siphon.jsonl").read_text().splitlines()]
    assert log[-1]["sha256"] and log[-1]["page"].endswith(page)
    assert not any(p.name.startswith(".siphon-capture") for p in out.iterdir()), "capture workdir left behind"


def test_cli_direct_media_url(site, tmp_path):
    out = tmp_path / "out"
    assert main([f"{site['base']}/media/clip.mp4", "-o", str(out), "-q"]) == 0
    assert (out / "clip.mp4").read_bytes() == (site["root"] / "media" / "clip.mp4").read_bytes()


def test_cli_json_lists_without_downloading(site, tmp_path, capsys):
    out = tmp_path / "out"
    assert main([f"{site['base']}/hls.html", "-o", str(out), "--no-profile", "--timeout", "15", "-j"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["kind"] == "hls" and "cookie" not in {k.lower() for k in data[0]["headers"]}
    assert not out.exists() or not any(out.iterdir())


@pytest.mark.parametrize("mode", ["-j", "-F"])
def test_list_modes_leave_no_capture_dir(site, tmp_path, mode):
    out = tmp_path / "out"
    main([f"{site['base']}/mse.html", "-o", str(out), "--no-profile", "--timeout", "15", mode])
    assert not out.exists() or not any(p.name.startswith(".siphon-capture") for p in out.iterdir())


def test_stalled_iframe_cannot_hang_the_sniffer(site, tmp_path):
    # Regression: an unbounded frame.evaluate() on a never-loading iframe hung real pages for minutes.
    import time

    t0 = time.monotonic()
    r = run_sniff(f"{site['base']}/stall.html", tmp_path)
    elapsed = time.monotonic() - t0
    assert elapsed < 40, f"sniff took {elapsed:.0f}s"
    assert r.candidates and r.candidates[0].url.endswith("/media/clip.mp4")


def test_native_hls_denied_so_players_use_mse(site, tmp_path):
    # Chrome 141+ advertises native HLS; without the override this page plays natively and the
    # capture fallback records nothing.
    r = run_sniff(f"{site['base']}/native_or_mse.html", tmp_path)
    assert r.title == "mse" and r.mse_seen
