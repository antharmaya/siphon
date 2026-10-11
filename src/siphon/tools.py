"""Pick external binaries deliberately instead of trusting PATH order — and fetch the ones
that are missing.

Found 2026-10-08: ~/.local/bin/ffmpeg (7.0.2 static) segfaults on Rumble's live
HLS while /usr/bin/ffmpeg (8.1.2) records it cleanly — and ~/.local/bin wins PATH.
So: collect every ffmpeg on PATH, prefer the newest version.

Fetching: ffmpeg, deno, rclone and cloudflared are all single static binaries, so siphon can
install them into its own bin dir (paths.BIN) with no root: download from the official release,
verify the published SHA-256 where one exists, copy only the named executables out of the
archive (never trusting archive paths), prove the binary runs, then move it into place.
A half-finished fetch never lands in BIN.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from . import paths


def exe(name: str) -> str:
    return name + ".exe" if sys.platform == "win32" else name


def _version(exe_path: str) -> tuple[int, ...]:
    try:
        out = subprocess.run([exe_path, "-version"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ()
    m = re.search(r"version\s+n?(\d+(?:\.\d+)*)", out)
    if m:
        return tuple(int(x) for x in m.group(1).split("."))
    # Nightly builds ("version N-127276-g…", what `siphon setup` fetches) are newer than any release.
    m = re.search(r"version\s+N-(\d+)", out)
    return (999, int(m.group(1))) if m else ()


def candidates(name: str) -> list[str]:
    seen, out = set(), []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d) / exe(name)
        if p.is_file() and os.access(p, os.X_OK):
            real = str(p.resolve())
            if real not in seen:
                seen.add(real)
                out.append(str(p))
    return out


@functools.cache
def ffmpeg() -> str | None:
    """Newest ffmpeg on PATH (override with SIPHON_FFMPEG)."""
    if os.environ.get("SIPHON_FFMPEG"):
        return os.environ["SIPHON_FFMPEG"]
    found = [(v, e) for e in candidates("ffmpeg") if (v := _version(e))]
    if not found:
        return shutil.which("ffmpeg")
    return max(found)[1]


def ffmpeg_dir() -> str | None:
    e = ffmpeg()
    return str(Path(e).parent) if e else None


def version_label(e: str) -> str:
    v = _version(e)
    if v[:1] == (999,):
        return f"nightly N-{v[1]}"
    return ".".join(map(str, v)) or "?"


def ffmpeg_report() -> list[tuple[str, str]]:
    return [(e, version_label(e)) for e in candidates("ffmpeg")]


def js_runtime() -> str | None:
    """YouTube's signature challenges need a JS runtime; yt-dlp takes deno or node."""
    return shutil.which("deno") or shutil.which("node")


# ---------------------------------------------------------------- fetching missing tools

class FetchError(RuntimeError):
    pass


def platform_key() -> str:
    m = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)
    osn = {"linux": "linux", "darwin": "darwin", "win32": "win"}.get(sys.platform, sys.platform)
    return f"{osn}-{arch}"


Source = Callable[[str], "tuple[str, str | None] | None"]  # platform key -> (url, sums_url) or None


@dataclass(frozen=True)
class Fetchable:
    name: str
    unlocks: str
    size_mb: int
    source: Source
    version_args: tuple[str, ...] = ("--version",)
    also: tuple[str, ...] = ()  # more executables shipped in the same archive (ffprobe)
    hint: str = ""  # where siphon can't fetch it (e.g. macOS ffmpeg)
    extra: dict = field(default_factory=dict)


_FF = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"


def _ffmpeg_src(key: str):
    name = {"linux-x64": "ffmpeg-master-latest-linux64-gpl.tar.xz",
            "linux-arm64": "ffmpeg-master-latest-linuxarm64-gpl.tar.xz",
            "win-x64": "ffmpeg-master-latest-win64-gpl.zip"}.get(key)
    return (_FF + name, _FF + "checksums.sha256") if name else None


_DENO = "https://github.com/denoland/deno/releases/latest/download/"


def _deno_src(key: str):
    t = {"linux-x64": "x86_64-unknown-linux-gnu", "linux-arm64": "aarch64-unknown-linux-gnu",
         "darwin-x64": "x86_64-apple-darwin", "darwin-arm64": "aarch64-apple-darwin",
         "win-x64": "x86_64-pc-windows-msvc"}.get(key)
    return (f"{_DENO}deno-{t}.zip", f"{_DENO}deno-{t}.zip.sha256sum") if t else None


def _rclone_src(key: str):
    t = {"linux-x64": "linux-amd64", "linux-arm64": "linux-arm64", "darwin-x64": "osx-amd64",
         "darwin-arm64": "osx-arm64", "win-x64": "windows-amd64"}.get(key)
    if not t:
        return None
    import httpx

    # "current" zips carry no checksum; the versioned directory does.
    ver = httpx.get("https://downloads.rclone.org/version.txt", follow_redirects=True, timeout=20).text.split()[-1]
    base = f"https://downloads.rclone.org/{ver}/"
    return base + f"rclone-{ver}-{t}.zip", base + "SHA256SUMS"


_CF = "https://github.com/cloudflare/cloudflared/releases/latest/download/"


def _cloudflared_src(key: str):
    name = {"linux-x64": "cloudflared-linux-amd64", "linux-arm64": "cloudflared-linux-arm64",
            "darwin-x64": "cloudflared-darwin-amd64.tgz", "darwin-arm64": "cloudflared-darwin-arm64.tgz",
            "win-x64": "cloudflared-windows-amd64.exe"}.get(key)
    return (_CF + name, None) if name else None  # no fixed-URL checksum file; verified by running it


FETCHABLE: dict[str, Fetchable] = {
    "ffmpeg": Fetchable("ffmpeg", "merge video+audio, HLS/DASH, blob-capture remux", 149, _ffmpeg_src,
                        ("-version",), also=("ffprobe",), hint="on macOS: brew install ffmpeg"),
    "deno": Fetchable("deno", "YouTube (solves its JS signature challenges)", 40, _deno_src),
    "rclone": Fetchable("rclone", "cloud destinations: Google Drive, Dropbox, OneDrive, R2, S3, 70+", 30,
                        _rclone_src, ("version",)),
    "cloudflared": Fetchable("cloudflared", "a shareable https link to `siphon serve`", 38, _cloudflared_src),
}


def missing_core() -> list[str]:
    """Tools the everyday path needs that this machine doesn't have."""
    out = []
    if not ffmpeg():
        out.append("ffmpeg")
    if not js_runtime():
        out.append("deno")
    return out


def _download(url: str, dest: Path, progress=None) -> str:
    import httpx

    h = hashlib.sha256()
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(30, read=120)) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0) or None
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    return h.hexdigest()


def _expected_sum(sums_url: str, filename: str) -> str | None:
    import httpx

    text = httpx.get(sums_url, follow_redirects=True, timeout=20).text
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


def _extract(archive: Path, wanted: set[str], dest: Path) -> dict[str, Path]:
    """Copy just the named executables out — matched by basename, written by us, so a hostile
    archive path (../../x) can't place anything outside dest."""
    dest.mkdir(parents=True, exist_ok=True)
    found: dict[str, Path] = {}

    def take(base: str, src) -> None:
        out = dest / base
        with open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
        found[base] = out

    name = archive.name
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                base = PurePosixPath(info.filename).name
                if base in wanted and base not in found and not info.is_dir():
                    with z.open(info) as src:
                        take(base, src)
    elif name.endswith((".tar.xz", ".tgz", ".tar.gz")):
        with tarfile.open(archive) as t:
            for m in t:
                base = PurePosixPath(m.name).name
                if base in wanted and base not in found and m.isfile():
                    with t.extractfile(m) as src:
                        take(base, src)
    else:  # the download is the executable itself
        if len(wanted) != 1:
            raise FetchError(f"{name} is a bare binary but {sorted(wanted)} were expected")
        with open(archive, "rb") as src:
            take(next(iter(wanted)), src)
    return found


def _runs(path: Path, args: tuple[str, ...]) -> str:
    try:
        r = subprocess.run([str(path), *args], capture_output=True, text=True, timeout=20)
    except OSError as e:
        raise FetchError(f"{path.name} won't execute here: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise FetchError(f"{path.name} hung on {' '.join(args)}") from e
    if r.returncode != 0:
        raise FetchError(f"{path.name} {' '.join(args)} exited {r.returncode}")
    return (r.stdout or r.stderr).strip().splitlines()[0][:120] if (r.stdout or r.stderr).strip() else "ok"


def install(tool: Fetchable, progress=None) -> tuple[Path, str]:
    """Fetch, verify, install. Returns (path, version line). Raises FetchError."""
    key = platform_key()
    src = tool.source(key)
    if not src:
        raise FetchError(f"no prebuilt {tool.name} for {key}" + (f" — {tool.hint}" if tool.hint else ""))
    url, sums = src
    paths.BIN.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fetch-", dir=paths.BIN) as td:  # same FS → atomic replace
        tmp = Path(td)
        archive = tmp / url.rsplit("/", 1)[1]
        digest = _download(url, archive, progress)
        if sums:
            expected = _expected_sum(sums, archive.name)
            if not expected:
                raise FetchError(f"{sums} lists no checksum for {archive.name}")
            if expected != digest:
                raise FetchError(f"checksum mismatch for {archive.name}: got {digest[:12]}…, expected {expected[:12]}…")
        names = {exe(n) for n in (tool.name, *tool.also)}
        got = _extract(archive, names, tmp / "out")
        if missing := names - set(got):
            raise FetchError(f"{archive.name} didn't contain {sorted(missing)}")
        for p in got.values():
            p.chmod(0o755)
        ver = _runs(got[exe(tool.name)], tool.version_args)
        for n, p in got.items():
            os.replace(p, paths.BIN / n)
    _record(tool.name, url, digest, ver, verified=bool(sums))
    ffmpeg.cache_clear()
    return paths.BIN / exe(tool.name), ver


def _record(name: str, url: str, digest: str, ver: str, verified: bool) -> None:
    mf = paths.BIN / "manifest.json"
    try:
        data = json.loads(mf.read_text())
    except (OSError, ValueError):
        data = {}
    data[name] = {"url": url, "sha256": digest, "checksum_verified": verified, "version": ver,
                  "installed": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    mf.write_text(json.dumps(data, indent=2))


def managed(path: str | None) -> bool:
    return bool(path) and Path(path).resolve().parent == paths.BIN.resolve()
