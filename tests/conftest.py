"""Local fixture web: real media made by ffmpeg, served with Range support, and
test pages that reproduce each hard case without touching the internet."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

FF = shutil.which("ffmpeg")

TYPES = {".mp4": "video/mp4", ".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t",
         ".html": "text/html", ".bin": "application/octet-stream", ".m4a": "audio/mp4"}

PAGES = {
    # <video> whose src is set by JS after a delay — invisible to a static HTML scraper.
    "late.html": """<html><head><title>Late Loaded Clip</title></head><body>
<video id=v controls width=640 height=360></video>
<script>setTimeout(() => { document.getElementById('v').src = '/media/clip.mp4'; }, 800);</script></body></html>""",
    # A player that fetches its HLS manifest by XHR, as hls.js/video.js do.
    "hls.html": """<html><head><title>HLS Player Page</title></head><body><video id=v></video>
<script>setTimeout(() => fetch('/hls/master.m3u8').then(r => r.text()).then(t => document.title = 'HLS Player Page'), 500);</script>
</body></html>""",
    # MediaSource fed from URLs/types that look like nothing — the network pass cannot win;
    # only the appendBuffer recorder can. Video and audio in separate SourceBuffers.
    "mse.html": """<html><head><title>MSE Obscured Stream</title></head><body><video id=v muted autoplay width=640></video>
<script>
const v = document.getElementById('v');
const ms = new MediaSource();
v.src = URL.createObjectURL(ms);
async function feed(sb, path, size) {
  const step = Math.ceil(size / 3);
  for (let o = 0; o < size; o += step) {
    const r = await fetch(path, { headers: { Range: `bytes=${o}-${Math.min(o + step, size) - 1}` } });
    const buf = await r.arrayBuffer();
    await new Promise(res => { sb.addEventListener('updateend', res, { once: true }); sb.appendBuffer(buf); });
  }
}
ms.addEventListener('sourceopen', async () => {
  const vs = ms.addSourceBuffer('video/mp4; codecs="avc1.42E01E"');
  const as = ms.addSourceBuffer('audio/mp4; codecs="mp4a.40.2"');
  const sizes = await (await fetch('/o/sizes')).json();
  await Promise.all([feed(vs, '/o/a7f3', sizes.v), feed(as, '/o/c91d', sizes.a)]);
  ms.endOfStream();
  v.play();
});
</script></body></html>""",
    # Whole file pulled as opaque bytes, wrapped in a Blob, URL revoked immediately.
    "blob.html": """<html><head><title>Revoked Blob Video</title></head><body><video id=v muted autoplay></video>
<script>
fetch('/o/payload').then(r => r.arrayBuffer()).then(buf => {
  const u = URL.createObjectURL(new Blob([buf], { type: 'video/mp4' }));
  const v = document.getElementById('v'); v.src = u; v.play();
  setTimeout(() => URL.revokeObjectURL(u), 50);
});
</script></body></html>""",
}

PAGES["stall.html"] = """<html><head><title>Stalled Iframe Page</title></head><body>
<iframe src="/hang" width=300 height=250></iframe><iframe src="/hang?2" width=300 height=250></iframe>
<video id=v controls width=640 height=360></video>
<script>setTimeout(() => { document.getElementById('v').src = '/media/clip.mp4'; }, 800);</script></body></html>"""

# The standard "native HLS first, else MSE" player pattern (hls.js docs, Plyr, many sites).
PAGES["native_or_mse.html"] = PAGES["mse.html"].replace(
    "const v = document.getElementById('v');",
    "const v = document.getElementById('v');\n"
    "if (v.canPlayType('application/vnd.apple.mpegurl')) { document.title = 'native'; v.src = '/hls/master.m3u8'; throw 0; }\n"
    "document.title = 'mse';")

# Browser-bound CDN on a different origin (localhost vs 127.0.0.1): answers only requests
# carrying Chrome's client hints AND the page's Referer — what Python replay can't fake.
PAGES["guarded_file.html"] = """<html><head><title>Guarded File</title></head><body>
<video id=v muted autoplay src="http://localhost:__PORT__/g/media/clip.mp4"></video></body></html>"""
PAGES["guarded_hls.html"] = """<html><head><title>Guarded HLS</title></head><body><script>
fetch('http://localhost:__PORT__/g/hlsenc/master.m3u8').then(r => r.text()).then(() => document.title = 'Guarded HLS');
</script></body></html>"""
PAGES["guarded_crawl.html"] = PAGES["guarded_file.html"].replace("clip.mp4", "clip.mp4?crawl=1")
# Single-file HLS: every segment is an EXT-X-BYTERANGE of one .ts.
PAGES["guarded_hls_br.html"] = PAGES["guarded_hls.html"].replace("hlsenc/master.m3u8", "hlsbr/br.m3u8")

OBSCURE = {"/o/a7f3": "v_frag.mp4", "/o/c91d": "a_frag.m4a", "/o/payload": "media/clip.mp4"}


def _ff(*args: str) -> None:
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def site(tmp_path_factory):
    if not FF:
        pytest.skip("ffmpeg required")
    root = tmp_path_factory.mktemp("site")
    media = root / "media"
    hls = root / "hls"
    media.mkdir()
    hls.mkdir()
    src = ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=6", "-f", "lavfi", "-i",
           "sine=frequency=440:duration=6"]
    _ff(*src, "-c:v", "libx264", "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(media / "clip.mp4"))
    frag = ["-movflags", "frag_keyframe+empty_moov+default_base_moof"]
    _ff("-i", str(media / "clip.mp4"), "-an", "-c", "copy", *frag, str(root / "v_frag.mp4"))
    _ff("-i", str(media / "clip.mp4"), "-vn", "-c", "copy", *frag, str(root / "a_frag.m4a"))
    for name, br in (("hi", "800k"), ("lo", "200k")):
        _ff("-i", str(media / "clip.mp4"), "-c:v", "libx264", "-b:v", br, "-c:a", "aac", "-hls_time", "2",
            "-hls_playlist_type", "vod", "-hls_segment_filename", str(hls / f"{name}_%03d.ts"), str(hls / f"{name}.m3u8"))
    (hls / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360\nhi.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=640x360\nlo.m3u8\n")
    (root / "big.bin").write_bytes(os.urandom(24 << 20))
    enc = root / "hlsenc"
    enc.mkdir()
    (enc / "enc.key").write_bytes(bytes.fromhex("00112233445566778899aabbccddeeff"))
    (root / "keyinfo").write_text(f"enc.key\n{enc / 'enc.key'}\n")
    _ff("-i", str(media / "clip.mp4"), "-c", "copy", "-hls_time", "2", "-hls_playlist_type", "vod",
        "-hls_key_info_file", str(root / "keyinfo"), "-hls_segment_filename", str(enc / "e_%03d.ts"),
        str(enc / "enc.m3u8"))
    (enc / "master.m3u8").write_text("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nenc.m3u8\n")
    (root / "hlsbr").mkdir()
    # Keyframes every 2 s, or the whole clip is one segment and one range — which tests nothing.
    _ff("-i", str(media / "clip.mp4"), "-c:v", "libx264", "-g", "50", "-c:a", "copy", "-hls_time", "2",
        "-hls_playlist_type", "vod", "-hls_flags", "single_file", str(root / "hlsbr" / "br.m3u8"))
    assert (root / "hlsbr" / "br.m3u8").read_text().count("#EXT-X-BYTERANGE") >= 3

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(root))
    port = srv.server_address[1]
    for name, html in PAGES.items():
        (root / name).write_text(html.replace("__PORT__", str(port)))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield {"root": root, "base": base, "server": srv}
    srv.shutdown()


def _handler(root: Path):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        slow_first_range = False  # toggled by tests to force work-stealing
        throttle_all = False  # per-connection bandwidth cap, like a throttling CDN
        seen: list[str] = []  # every request path, for tests that assert something was never fetched

        def log_message(self, *a):
            pass

        def do_GET(self):
            H.seen.append(self.path)
            self.cors = False
            path = self.path.split("?")[0]
            if path == "/redir":
                return self._send(302, b"", "text/plain", {"Location": self.path.split("to=", 1)[1]})
            if path == "/hang":
                # An ad iframe whose server never answers: its frame never gets a JS context.
                time.sleep(90)
                return self._send(200, b"<html></html>", "text/html")
            if path.startswith("/g/"):
                ok = self.headers.get("sec-ch-ua") and (self.headers.get("Referer") or "").startswith("http://127.0.0.1:")
                if not ok:
                    return self._send(403, b"browser only", "text/plain")
                path = path[2:]
                # The HLS CDN allows script fetches; the file CDN doesn't. Per request: handlers run concurrently.
                self.cors = path.startswith(("/hlsenc/", "/hlsbr/"))
            if path == "/o/sizes":
                body = ('{"v": %d, "a": %d}' % ((root / "v_frag.mp4").stat().st_size,
                                                (root / "a_frag.m4a").stat().st_size)).encode()
                return self._send(200, body, "application/json")
            if path in OBSCURE:
                f, ctype = root / OBSCURE[path], "application/octet-stream"
            else:
                f = root / path.lstrip("/")
                ctype = TYPES.get(f.suffix, "application/octet-stream")
            if not f.is_file():
                return self._send(404, b"nope", "text/plain")
            size = f.stat().st_size
            m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
            if m:
                a = int(m.group(1))
                b = int(m.group(2)) if m.group(2) else size - 1
                b = min(b, size - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
            else:
                a, b = 0, size - 1
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(b - a + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", f'"{size}-{int(f.stat().st_mtime)}"')
            if self.cors:
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            slow = f.name == "big.bin" and b > 0 and (H.throttle_all or (H.slow_first_range and a == 0))
            crawl = "crawl=1" in self.path  # ~5 s for clip.mp4: long enough to cancel mid-download
            with open(f, "rb") as fh:
                fh.seek(a)
                left = b - a + 1
                try:
                    while left > 0:
                        chunk = fh.read(min(64 << 10, left))
                        self.wfile.write(chunk)
                        left -= len(chunk)
                        if slow:
                            time.sleep(0.05)
                        if crawl:
                            time.sleep(0.5)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def _send(self, code, body, ctype, headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

    return H


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A running `siphon serve` engine on a free port, with its own state and download folder."""
    import sys

    from siphon import jobs, paths, queue, serve

    state = tmp_path / "state"
    monkeypatch.setattr(paths, "STATE", state)
    monkeypatch.setattr(jobs, "LOGS", state / "logs")
    monkeypatch.setattr(queue, "QUEUE", state / "queue.json")
    out = tmp_path / "dl"
    out.mkdir()
    app = serve.App(out, 2, allow_loopback_urls=True,
                    runner_kwargs={"siphon_cmd": [sys.executable, "-m", "siphon"], "extra_args": ["--no-netcheck"]})
    srv = serve.make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app.runner.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield app, base, out
    app.runner.stop()
    srv.shutdown()
    srv.server_close()
