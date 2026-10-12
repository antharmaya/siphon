import asyncio
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


def test_hls_parsing_and_aes128():
    from Cryptodome.Cipher import AES

    from siphon.download.browserfetch import aes128_decrypt, parse_media, pick_variant

    master = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="en",DEFAULT=YES,URI="audio/en.m3u8"\n'
              '#EXT-X-STREAM-INF:BANDWIDTH=300000,AUDIO="a"\nlo.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=900000,AUDIO="a"\nhi.m3u8\n')
    assert pick_variant(master, "https://c.dn/v/master.m3u8") == ("https://c.dn/v/hi.m3u8", "https://c.dn/v/audio/en.m3u8")
    media = parse_media('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n#EXT-X-KEY:METHOD=AES-128,URI="k.key"\n#EXTINF:2,\ns1.ts\n'
                        '#EXT-X-ENDLIST\n', "https://c.dn/v/hi.m3u8")
    assert media["segments"][0] == {"url": "https://c.dn/v/s1.ts", "seq": 7, "key": ("https://c.dn/v/k.key", None),
                                    "range": None}
    assert not media["live"]
    key, plain = bytes(range(16)), b"segment-bytes" + b"\x03" * 3  # PKCS7-padded to 16
    data = AES.new(key, AES.MODE_CBC, (7).to_bytes(16, "big")).encrypt(plain)
    assert aes128_decrypt(data, key, None, 7) == b"segment-bytes"  # IV defaults to the sequence number
    # Single-file HLS: an offset-less BYTERANGE continues where the previous range of that URI ended.
    br = parse_media('#EXTM3U\n#EXT-X-MAP:URI="v.mp4",BYTERANGE="800@0"\n#EXT-X-BYTERANGE:1000@800\nv.mp4\n'
                     '#EXT-X-BYTERANGE:500\nv.mp4\n#EXT-X-ENDLIST\n', "https://c.dn/v/x.m3u8")
    assert br["init"] == {"url": "https://c.dn/v/v.mp4", "range": (0, 799)}
    assert [s["range"] for s in br["segments"]] == [(800, 1799), (1800, 2299)]


def test_local_network_scope():
    from siphon import netcheck

    assert [netcheck.scope(h) for h in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "0.0.0.0", "169.254.169.254",
                                        "localhost")] == [2] * 6
    assert [netcheck.scope(h) for h in ("192.168.1.10", "10.0.0.2", "100.100.1.1", "fd00::1")] == [1] * 4
    assert netcheck.scope("8.8.8.8") == 0
    assert netcheck.inward("http://169.254.169.254/latest/meta-data/x.mp4", "https://8.8.8.8/page")
    assert netcheck.inward("http://192.168.1.1/admin.mp4", "https://8.8.8.8/page")
    assert not netcheck.inward("http://192.168.1.5/a.mp4", "http://192.168.1.2/nas")  # a LAN page's own LAN media
    assert not netcheck.inward("https://8.8.8.8/a.mp4", "http://127.0.0.1:8080/dev")


def test_media_on_a_more_local_network_than_its_page_is_refused(tmp_path, monkeypatch):
    # A public page embedding <video src="http://169.254.169.254/..."> must not get the reply saved
    # where `siphon serve` would hand it to the phone — by the plain downloader or the Chrome fallback.
    from siphon.download import LocalNetworkRefused, browserfetch, download
    from siphon.models import Candidate

    monkeypatch.setattr(browserfetch, "find_chrome", lambda: pytest.fail("launched Chrome for a refused URL"))
    for kind in ("file", "hls"):
        c = Candidate(url=f"http://169.254.169.254/latest/meta-data/v.{'mp4' if kind == 'file' else 'm3u8'}",
                      kind=kind, source="sniff", page_url="https://8.8.8.8/watch")
        with pytest.raises(LocalNetworkRefused):
            download(c, tmp_path / "out", None, 4, None, browser_fallback=lambda *a: pytest.fail("fell back"))
        with pytest.raises(browserfetch.BrowserFetchError, match="more local"):
            browserfetch.browser_fetch(c, tmp_path / "x.mp4")
    assert not (tmp_path / "out").exists() and list(tmp_path.iterdir()) == [], "nothing may be written"
    seg = browserfetch._Fetcher(None, None, "https://8.8.8.8/watch")  # an HLS segment or key pointing inward
    with pytest.raises(browserfetch.BrowserFetchError, match="more local"):
        asyncio.run(seg.get("http://127.0.0.1:9/s1.ts", None))


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
