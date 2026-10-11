"""Tool fetching + capability map, offline: a local HTTP server stands in for GitHub releases.
(The real fetch of ffmpeg/deno/rclone/cloudflared is covered by `siphon setup` live runs.)"""

import hashlib
import http.server
import io
import os
import sys
import tarfile
import threading
import zipfile
from functools import partial
from pathlib import Path

import pytest

from siphon import paths, setup, tools

FAKE = b"#!/bin/sh\necho 'fake 1.2.3'\n"
BROKEN = b"#!/bin/sh\nexit 3\n"


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    monkeypatch.setattr(paths, "BIN", d)
    monkeypatch.setattr(paths, "CONFIG", tmp_path / "config.json")
    return d


@pytest.fixture
def release(tmp_path):
    """Serve tmp_path/rel over HTTP like a release page."""
    root = tmp_path / "rel"
    root.mkdir()
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def _zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)


def _tar(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:xz") as t:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))


def _tool(base: str, archive: str, sums: str | None, also=()) -> tools.Fetchable:
    return tools.Fetchable("faketool", "testing", 1, lambda key: (base + archive, base + sums if sums else None),
                           ("--version",), also=also)


# ---------------------------------------------------------------- extraction

def test_extract_by_basename_from_nested_zip_and_tar(tmp_path):
    z = tmp_path / "a.zip"
    _zip(z, {"pkg-1.0/bin/faketool": FAKE, "pkg-1.0/README": b"hi"})
    got = tools._extract(z, {"faketool"}, tmp_path / "out1")
    assert got["faketool"].read_bytes() == FAKE and set(got) == {"faketool"}

    t = tmp_path / "b.tar.xz"
    _tar(t, {"ffmpeg-master/bin/ffmpeg": FAKE, "ffmpeg-master/bin/ffprobe": FAKE, "ffmpeg-master/doc/x": b""})
    got = tools._extract(t, {"ffmpeg", "ffprobe"}, tmp_path / "out2")
    assert set(got) == {"ffmpeg", "ffprobe"}


def test_extract_never_writes_outside_dest(tmp_path):
    z = tmp_path / "evil.zip"
    _zip(z, {"../../escaped": b"x", "../../../tmp/faketool": FAKE})
    out = tmp_path / "deep" / "out"
    got = tools._extract(z, {"faketool"}, out)
    assert got["faketool"] == out / "faketool"  # written by basename, inside dest
    assert not (tmp_path / "escaped").exists() and not (tmp_path / "deep" / "escaped").exists()


def test_extract_bare_binary(tmp_path):
    b = tmp_path / "cloudflared-linux-amd64"
    b.write_bytes(FAKE)
    got = tools._extract(b, {"cloudflared"}, tmp_path / "out")
    assert got["cloudflared"].read_bytes() == FAKE


# ---------------------------------------------------------------- install

@pytest.mark.skipif(sys.platform == "win32", reason="fake tool is a shell script")
def test_install_verifies_checksum_runs_and_records(bin_dir, release):
    root, base = release
    _zip(root / "faketool-linux.zip", {"faketool-1.0/faketool": FAKE})
    digest = hashlib.sha256((root / "faketool-linux.zip").read_bytes()).hexdigest()
    (root / "SUMS").write_text(f"{'0' * 64}  other.zip\n{digest}  faketool-linux.zip\n")

    path, ver = tools.install(_tool(base, "faketool-linux.zip", "SUMS"))
    assert path == bin_dir / "faketool" and os.access(path, os.X_OK)
    assert ver == "fake 1.2.3"
    rec = __import__("json").loads((bin_dir / "manifest.json").read_text())["faketool"]
    assert rec["sha256"] == digest and rec["checksum_verified"] is True
    assert not list(bin_dir.glob(".fetch-*"))  # temp dir cleaned up


@pytest.mark.skipif(sys.platform == "win32", reason="fake tool is a shell script")
def test_install_rejects_checksum_mismatch_and_leaves_bin_clean(bin_dir, release):
    root, base = release
    _zip(root / "faketool-linux.zip", {"faketool": FAKE})
    (root / "SUMS").write_text(f"{'a' * 64}  faketool-linux.zip\n")
    with pytest.raises(tools.FetchError, match="checksum mismatch"):
        tools.install(_tool(base, "faketool-linux.zip", "SUMS"))
    assert not (bin_dir / "faketool").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="fake tool is a shell script")
def test_install_rejects_missing_checksum_entry(bin_dir, release):
    root, base = release
    _zip(root / "faketool-linux.zip", {"faketool": FAKE})
    (root / "SUMS").write_text(f"{'a' * 64}  something-else.zip\n")
    with pytest.raises(tools.FetchError, match="lists no checksum"):
        tools.install(_tool(base, "faketool-linux.zip", "SUMS"))


@pytest.mark.skipif(sys.platform == "win32", reason="fake tool is a shell script")
def test_install_rejects_binary_that_doesnt_run(bin_dir, release):
    root, base = release
    _zip(root / "faketool-linux.zip", {"faketool": BROKEN})
    with pytest.raises(tools.FetchError, match="exited 3"):
        tools.install(_tool(base, "faketool-linux.zip", None))
    assert not (bin_dir / "faketool").exists()


def test_install_reports_missing_member(bin_dir, release):
    root, base = release
    _zip(root / "faketool-linux.zip", {"faketool": FAKE})
    with pytest.raises(tools.FetchError, match="didn't contain"):
        tools.install(_tool(base, "faketool-linux.zip", None, also=("fakeprobe",)))


def test_unsupported_platform_gives_the_hint(bin_dir):
    t = tools.Fetchable("x", "y", 1, lambda key: None, hint="on macOS: brew install x")
    with pytest.raises(tools.FetchError, match="brew install x"):
        tools.install(t)


# ---------------------------------------------------------------- PATH, versions, planning

def test_use_managed_bin_appends_once(bin_dir, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    paths.use_managed_bin()
    paths.use_managed_bin()
    parts = os.environ["PATH"].split(os.pathsep)
    assert parts == ["/usr/bin", str(bin_dir)]  # appended (system tools still win), not duplicated


def test_nightly_ffmpeg_counts_as_newest(tmp_path, monkeypatch):
    def fake(out):
        class R:
            stdout = out
        return R()

    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: fake("ffmpeg version N-127276-g8e9db1f152 Copyright"))
    nightly = tools._version("x")
    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: fake("ffmpeg version 8.1.2 Copyright"))
    assert nightly > tools._version("x") == (8, 1, 2)


def test_platform_key_shape():
    osn, arch = tools.platform_key().split("-")
    assert osn and arch


def test_every_fetchable_has_a_source_for_linux_x64():
    for name, t in tools.FETCHABLE.items():
        if name == "rclone":  # resolves its version over the network; covered live
            continue
        url, _ = t.source("linux-x64")
        assert url.startswith("https://"), name


def test_plan_explicit_and_all(monkeypatch):
    monkeypatch.setattr(tools, "missing_core", lambda: ["ffmpeg"])
    monkeypatch.setattr(setup.shutil, "which", lambda n: None)
    assert setup.plan(["rclone"], False) == ["rclone"]
    assert setup.plan([], False) == ["ffmpeg"]
    assert setup.plan([], True) == ["ffmpeg", "rclone", "cloudflared"]


def test_offer_core_noninteractive_only_hints(bin_dir, monkeypatch):
    monkeypatch.setattr(tools, "missing_core", lambda: ["ffmpeg"])
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt"))
    said = []
    setup.offer_core(False, said.append)
    assert "siphon setup" in said[0]


def test_offer_core_remembers_a_decline(bin_dir, monkeypatch):
    monkeypatch.setattr(tools, "missing_core", lambda: ["ffmpeg"])
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    setup.offer_core(True, lambda m: None)
    assert paths.load_config()["declined"] == ["ffmpeg"]
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("asked again after a decline"))
    setup.offer_core(True, lambda m: None)


def test_capabilities_are_data_with_required_flags():
    caps = {c.key: c for c in setup.capabilities()}
    assert {"core", "ffmpeg", "js", "chrome", "rclone", "remote"} <= set(caps)
    assert caps["ffmpeg"].required and caps["js"].required and not caps["rclone"].required
    assert all(set(c.to_json()) >= {"key", "ok", "detail", "fix"} for c in caps.values())
