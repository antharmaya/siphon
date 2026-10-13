"""End-to-end through real Chrome against the local fixture site. Every output file
is checked with ffprobe — "a file appeared" is not the same as "a video downloaded"."""

import asyncio
import json
import shutil
import subprocess
import sys

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


def test_browser_button_downloads_survive_unreadable_browser_cookies(site, tmp_path):
    # The extension's host names its browser so logins carry over. No Firefox profile exists in this
    # HOME: with --cookies-optional the download goes ahead without them; without it, it's an error.
    import os
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    env.pop("XDG_CONFIG_HOME", None)
    cmd = [sys.executable, "-m", "siphon", "--no-netcheck", "--cookies-from-browser", "firefox",
           site["base"] + "/hls/master.m3u8"]
    ok = subprocess.run([*cmd, "-o", str(tmp_path / "a"), "--cookies-optional"], env=env, capture_output=True, text=True, timeout=120)
    assert ok.returncode == 0 and "continuing without your login" in ok.stderr.replace("\n", " "), ok.stderr[-400:]
    assert list((tmp_path / "a").glob("*.mp4"))
    strict = subprocess.run([*cmd, "-o", str(tmp_path / "b")], env=env, capture_output=True, text=True, timeout=120)
    assert strict.returncode != 0 and "cookies" in strict.stderr


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


def test_guard_refuses_python_even_with_page_headers(site):
    import httpx

    port = site["base"].rsplit(":", 1)[1]
    r = httpx.get(f"http://127.0.0.1:{port}/g/media/clip.mp4",
                  headers={"Referer": f"{site['base']}/guarded_file.html", "User-Agent": "Mozilla/5.0 Chrome/152"})
    assert r.status_code == 403  # otherwise the fallback tests below would pass for the wrong reason


@pytest.mark.parametrize("page", ["guarded_file.html", "guarded_hls.html", "guarded_hls_br.html"])
def test_browser_bound_cdn_downloads_via_chrome(site, tmp_path, page):
    out = tmp_path / "out"
    rc = main([f"{site['base']}/{page}", "-o", str(out), "--no-profile", "--timeout", "15", "-q"])
    assert rc == 0
    files = [p for p in out.iterdir() if p.suffix in (".mp4", ".mkv")]
    assert len(files) == 1, list(out.iterdir())
    streams, duration = probe(files[0])
    assert streams == ["audio", "video"] and 5.0 <= duration <= 7.0, (streams, duration)
    # Duration alone can't catch a byte range read as the whole file: remuxing three copies of the
    # same timestamps still probes as 6 s.
    size, src = files[0].stat().st_size, (site["root"] / "media" / "clip.mp4").stat().st_size
    assert 0.8 * src < size < 1.3 * src, (size, src)
    assert not any(p.name.startswith(".siphon-bf-") for p in out.iterdir()), "browser-fetch workdir left behind"


def test_browser_fallback_reports_on_the_progress_channel(site, tmp_path):
    # `siphon serve` only sees stdout events: the fallback must report progress, and nothing else may reach it.
    out = tmp_path / "out"
    r = subprocess.run([sys.executable, "-m", "siphon", "--progress-json", "--no-profile", "--timeout", "15",
                        "-o", str(out), f"{site['base']}/guarded_file.html"], capture_output=True, text=True, timeout=180)
    events = [json.loads(line) for line in r.stdout.splitlines()]
    kinds = [e["event"] for e in events]
    assert "letting Chrome make the requests" in r.stderr, r.stderr[-600:]
    assert "progress" in kinds and "saved" in kinds and events[-1] == {"event": "done", "ok": True}, kinds
    last = [e for e in events if e["event"] == "progress"][-1]
    assert last["percent"] == 100.0 and last["done"] == (site["root"] / "media" / "clip.mp4").stat().st_size


def test_cancel_during_browser_fallback_cleans_up(site, tmp_path):
    # `siphon serve` cancels with SIGTERM. Its default action skips every finally: Chrome (own session)
    # outlived siphon and the fallback's work dir stayed in the download folder.
    import signal
    import time

    out, err = tmp_path / "out", tmp_path / "err.log"
    with open(err, "w") as ef:
        p = subprocess.Popen([sys.executable, "-m", "siphon", "--progress-json", "--no-profile", "--timeout", "15",
                              "-o", str(out), f"{site['base']}/guarded_crawl.html"], stdout=subprocess.DEVNULL, stderr=ef)
        deadline = time.time() + 90
        while "letting Chrome" not in err.read_text() and p.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        time.sleep(3)  # into the visit / the stream
        assert p.poll() is None, err.read_text()[-600:]
        assert any(d.name.startswith(".siphon-bf-") for d in out.iterdir())  # or the test proves nothing
        p.send_signal(signal.SIGTERM)
        assert p.wait(30) == 130
    assert not any(d.name.startswith(".siphon-bf-") for d in out.iterdir()), list(out.iterdir())


def test_browser_fetch_vets_every_redirect_hop(site, tmp_path, monkeypatch):
    # Pretend "localhost" is a public host, so 127.0.0.1 is more local than the page. A redirect
    # there must be stopped before Chrome sends it, not after the reply is in hand.
    from siphon import netcheck
    from siphon.download.browserfetch import BrowserFetchError, browser_fetch
    from siphon.models import Candidate

    real = netcheck.scope
    monkeypatch.setattr(netcheck, "scope", lambda h: 0 if h == "localhost" else real(h))
    port = site["base"].rsplit(":", 1)[1]
    target = f"{site['base']}/media/clip.mp4?probe=hop"
    c = Candidate(url=f"http://localhost:{port}/redir?to={target}", kind="file", source="sniff",
                  page_url=f"http://localhost:{port}/late.html")
    with pytest.raises(BrowserFetchError, match="more local"):
        browser_fetch(c, tmp_path / "x.mp4")
    assert f"/redir?to={target}" in site["server"].RequestHandlerClass.seen, "the first hop should have been made"
    assert "/media/clip.mp4?probe=hop" not in site["server"].RequestHandlerClass.seen
    assert list(tmp_path.iterdir()) == []
