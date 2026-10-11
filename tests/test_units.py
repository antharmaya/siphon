import base64
import hashlib

import pytest

from siphon.download.segmented import SegmentedDownloader
from siphon.media import canonical, classify, is_ad, is_segment, manifest_drm, safe_filename
from siphon.resolve.capture import CaptureSink


@pytest.mark.parametrize("url,ct,kind", [
    ("https://x.com/a/master.m3u8?tok=1", None, "hls"),
    ("https://x.com/play", "application/vnd.apple.mpegurl", "hls"),
    ("https://x.com/manifest.mpd", None, "dash"),
    ("https://x.com/v.mp4", None, "file"),
    ("https://x.com/stream", "video/mp4", "file"),
    ("https://x.com/seg_001.ts", "video/mp2t", None),
    ("https://x.com/chunk.m4s", None, None),
    ("https://x.com/page.html", "text/html", None),
    ("blob:https://x.com/uuid", None, None),
])
def test_classify(url, ct, kind):
    assert classify(url, ct) == kind


def test_segments_ads_canonical():
    assert is_segment("https://cdn/x/segment-12.mp4")
    assert is_segment("https://cdn/x/init.mp4")
    assert not is_segment("https://cdn/x/movie.mp4")
    assert is_ad("https://pubads.g.doubleclick.net/x.mp4")
    assert canonical("https://v.fbcdn.net/v.mp4?bytestart=0&byteend=999&oh=abc") == "https://v.fbcdn.net/v.mp4?oh=abc"


def test_drm_detection_distinguishes_aes128():
    clear = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n'
    fairplay = '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://abc",KEYFORMAT="com.apple.streamingkeydelivery"\n'
    widevine = '<MPD><ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/></MPD>'
    assert manifest_drm(clear, "hls") is None
    assert manifest_drm(fairplay, "hls") == "fairplay"
    assert manifest_drm(widevine, "dash") == "widevine"


def test_safe_filename():
    assert safe_filename('a/b:c*?"<>|d') == "a b c d"
    assert len(safe_filename("é" * 500).encode()) <= 180


def test_capture_sink_reorders(tmp_path):
    s = CaptureSink(tmp_path)
    for seq, piece in [(1, b"BB"), (0, b"AA"), (3, b"DD"), (2, b"CC")]:
        s.chunk("t1", "video/mp4", "mse", seq, base64.b64encode(piece).decode())
    t = s.tracks["t1"]
    assert t.path.read_bytes() == b"AABBCCDD" and t.bytes == 8 and t.path.suffix == ".mp4"


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_segmented_download_matches(site, tmp_path):
    dest = tmp_path / "big.bin"
    SegmentedDownloader(connections=8).download(f"{site['base']}/big.bin", dest)
    assert _sha(dest) == _sha(site["root"] / "big.bin")
    assert not (tmp_path / "big.bin.part").exists() and not (tmp_path / "big.bin.part.json").exists()


def test_segmented_resume_after_interrupt(site, tmp_path):
    dest = tmp_path / "big.bin"

    class Stop(Exception):
        pass

    def bail(done, total):
        if done > 6 << 20:
            raise Stop()

    with pytest.raises(Stop):
        SegmentedDownloader(connections=4, progress=bail).download(f"{site['base']}/big.bin", dest)
    state = tmp_path / "big.bin.part.json"
    assert state.exists(), "checkpoint must survive an interrupt"
    seen = []
    SegmentedDownloader(connections=4, progress=lambda d, t: seen.append(d)).download(f"{site['base']}/big.bin", dest)
    assert _sha(dest) == _sha(site["root"] / "big.bin")
    assert seen[0] > 0, "resume must start from the checkpoint, not zero"


def test_work_stealing_splits_slow_segment(site, tmp_path):
    handler = site["server"].RequestHandlerClass
    handler.slow_first_range = True
    try:
        dl = SegmentedDownloader(connections=4, min_split=256 << 10, adaptive=False)
        seen_segments = []
        orig = dl._save_state
        dl._save_state = lambda part, probe, segs: (seen_segments.append(len(segs)), orig(part, probe, segs))
        dest = tmp_path / "big.bin"
        dl.download(f"{site['base']}/big.bin", dest)
    finally:
        handler.slow_first_range = False
    assert _sha(dest) == _sha(site["root"] / "big.bin")
    assert max(seen_segments) > 4, "idle workers should have stolen the slow segment's tail"


def test_adaptive_grows_when_each_connection_is_capped(site, tmp_path):
    handler = site["server"].RequestHandlerClass
    handler.throttle_all = True  # every connection capped (~1.3 MB/s): parallelism should pay
    try:
        dl = SegmentedDownloader(connections=8, min_split=256 << 10, window=0.5)
        dest = tmp_path / "big.bin"
        dl.download(f"{site['base']}/big.bin", dest)
    finally:
        handler.throttle_all = False
    assert _sha(dest) == _sha(site["root"] / "big.bin")
    # Starts at 1; growth is bounded by the steal floor (~3s of transfer per steal), so on a
    # 24 MB file it tops out around 4. The point is that it grew; the never-grow mutant stays at 1.
    assert dl.peak_workers >= 3, dl.peak_workers


def test_console_output_keeps_brackets(capsys):
    from siphon.cli import err, esc

    with err.capture() as cap:
        err.print(f"[green]✓[/] {esc('/x/Me at the zoo [jNQXAC9IVRw].mkv')}")
    assert "[jNQXAC9IVRw]" in cap.get()


def test_iab_banner_paths_are_ads():
    assert is_ad("https://static.a-ads.com/a-ads-video-banners/162/300x250")
    assert is_ad("https://cdn.example.com/creative/banner_728x90.mp4")
    assert not is_ad("https://cdn.example.com/videos/1300x2500/clip.mp4")
    assert is_ad("https://cdn.secrets.ai/ads/creatives/06c0726d.mp4")
    assert not is_ad("https://cdn.example.com/uploads/adsorption-lecture.mp4")
    assert not is_ad("https://cdn.example.com/roads/drive.mp4")


def test_geofence_signatures():
    from siphon.netcheck import geofence_reason

    assert geofence_reason("https://www.tiktok.com/in/about")
    assert geofence_reason("https://example.com/geo-blocked?from=IN")
    assert geofence_reason("https://www.tiktok.com/@khaby.lame") is None


def test_proxy_routing_per_domain():
    from siphon.cli import proxy_for

    p = "socks5h://127.0.0.1:1080"
    assert proxy_for("https://www.tiktok.com/@x", p, "tiktok.com") == p
    assert proxy_for("https://youtube.com/watch?v=1", p, "tiktok.com") is None
    assert proxy_for("https://youtube.com/watch?v=1", p, "") == p
    assert proxy_for("https://nottiktok.com/", p, "tiktok.com") is None
    assert proxy_for("https://x.com/", None, "") is None


def test_netcheck_respects_port_and_skips_local(site):
    from siphon.netcheck import check

    # Fixture server: plain http on a random port. Hard-coding 443 made this "unreachable".
    assert check(f"{site['base']}/late.html").ok
    assert check("http://192.168.1.20:8096/web/").ok  # LAN media server: never judged by public DNS


def test_pick_richest_prefers_higher_bitrate_response():
    from siphon.ytdlp import pick_richest

    lo = {"requested_formats": [{"format_id": "398", "tbr": 374}, {"format_id": "251", "tbr": 123}]}
    hi = {"requested_formats": [{"format_id": "398", "tbr": 711}, {"format_id": "251", "tbr": 123}]}
    assert pick_richest([lo, hi, lo]) is hi
    assert pick_richest([{"tbr": 500}]) == {"tbr": 500}  # single progressive format


def test_sweep_stale_captures(tmp_path):
    import os
    import time

    from siphon.cli import sweep_stale_captures

    old, new = tmp_path / ".siphon-capture-1", tmp_path / ".siphon-capture-2"
    old.mkdir(); new.mkdir()
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    sweep_stale_captures(tmp_path)
    assert not old.exists() and new.exists(), "only captures older than an hour are swept"


def test_gallery_supported_classification():
    from siphon.resolve import gallery

    assert gallery.available()
    assert gallery.supported("https://x.com/NASA/status/123") == "tweet"
    assert gallery.supported("https://www.instagram.com/p/abc/") in ("post", "image")
    assert gallery.supported("https://www.youtube.com/watch?v=x") is None  # yt-dlp's job, not gallery's
    assert gallery.supported("https://example.com/random/page") is None


def test_gallery_bulk_subcategories_cover_timelines():
    from siphon.resolve import gallery

    # A profile/feed must be flagged so siphon doesn't silently fetch thousands of items.
    assert gallery.supported("https://x.com/NASA") in gallery.BULK_SUBCATEGORIES
    assert gallery.supported("https://x.com/NASA/media") in gallery.BULK_SUBCATEGORIES


def test_parse_section_and_feature_params():
    from siphon.ytdlp import YtdlpConfig, YtdlpEngine, _parse_section

    assert _parse_section("30-90") == (30.0, 90.0)
    assert _parse_section("1:30-2:00") == (90.0, 120.0)
    assert _parse_section("1:00:00-") == (3600.0, None)
    p = YtdlpEngine(YtdlpConfig(sponsorblock=["sponsor"], rate_limit="500K", archive="/a")).params()
    assert p["sponsorblock_remove"] == {"sponsor"} and p["ratelimit"] == 500 * 1024 and p["download_archive"] == "/a"


def test_ytdlp_plugin_sites_count_as_dedicated(tmp_path):
    """A site supported only by a yt-dlp plugin package must be recognised up front, not left
    to the slow sniffer race. Subprocess: yt-dlp caches plugin loading per process."""
    import subprocess
    import sys

    ext = tmp_path / "yt_dlp_plugins" / "extractor"
    ext.mkdir(parents=True)
    (ext / "siphontest.py").write_text(
        "from yt_dlp.extractor.common import InfoExtractor\n"
        "class SiphonTestIE(InfoExtractor):\n"
        "    _VALID_URL = r'https?://siphon-plugin-test\\.example/v/(?P<id>\\w+)'\n"
        "    def _real_extract(self, url):\n"
        "        return {}\n")
    code = "from siphon.ytdlp import dedicated_extractor as d; print(d('https://siphon-plugin-test.example/v/abc'))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**__import__("os").environ, "PYTHONPATH": str(tmp_path)}, timeout=60)
    assert out.stdout.strip() == "SiphonTest", out.stderr[-500:]
