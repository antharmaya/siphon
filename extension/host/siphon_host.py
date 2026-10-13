#!/usr/bin/env python3
"""siphon native-messaging host: the bridge between the browser extension and the CLI.

Chrome speaks native messaging over stdin/stdout: each message is a little-endian uint32
length followed by that many bytes of UTF-8 JSON. We read one request, run `siphon`, and
stream JSON progress events back in the same framing until the download finishes.

Runs as a short-lived process per connection (the extension opens one port per job), so
there is no shared state and a crash in one download can't touch another.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
from pathlib import Path

__version__ = "0.2.0"

LOG = Path(os.environ.get("SIPHON_HOST_LOG", Path.home() / ".local/share/siphon/host.log"))


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass


# ---- native messaging framing ------------------------------------------------

def read_message() -> dict | None:
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    (length,) = struct.unpack("<I", raw_len)
    data = sys.stdin.buffer.read(length)
    return json.loads(data.decode("utf-8"))


_write_lock = threading.Lock()


def send(obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    with _write_lock:
        sys.stdout.buffer.write(struct.pack("<I", len(data)))
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


# ---- running siphon ----------------------------------------------------------

def find_siphon() -> list[str] | None:
    if exe := (os.environ.get("SIPHON_BIN") or shutil.which("siphon")):
        return [exe]
    for cand in (Path.home() / ".local/bin/siphon", Path("/usr/local/bin/siphon")):
        if cand.exists():
            return [str(cand)]
    # Dev checkout: run the module with the repo's uv environment.
    repo = Path(__file__).resolve().parents[2]
    if (repo / "pyproject.toml").exists() and shutil.which("uv"):
        return ["uv", "run", "--project", str(repo), "siphon"]
    return None


def default_outdir() -> Path:
    for var in ("SIPHON_DOWNLOAD_DIR", "XDG_DOWNLOAD_DIR"):
        if os.environ.get(var):
            return Path(os.environ[var]).expanduser()
    d = Path.home() / "Downloads"
    return d if d.exists() else Path.home()


# Zen, LibreWolf, Floorp and Waterfox say "Firefox" in their user agent but keep profiles elsewhere.
FIREFOX_FORKS = {"zen": "~/.zen", "librewolf": "~/.librewolf", "floorp": "~/.floorp", "waterfox": "~/.waterfox"}


def cookie_source(browser: str, parent_exe: str | None = None) -> str:
    """What to read the login cookies from. For "firefox", look at which browser started this host
    (its parent process) so a fork's own profiles are read, not Firefox's."""
    if browser != "firefox":
        return browser
    if parent_exe is None:
        try:
            parent_exe = os.readlink(f"/proc/{os.getppid()}/exe")
        except OSError:
            return browser
    for name, root in FIREFOX_FORKS.items():
        if name in parent_exe.lower() and Path(root).expanduser().is_dir():
            return f"firefox:{Path(root).expanduser()}"
    return browser


PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)%")
SPEED = re.compile(r"at\s+([\d.]+\s*[KMG]i?B/s)")
SAVED = re.compile(r"(?:✓|Destination:|\[download\] )\s*(/.+?\.(?:mp4|mkv|webm|m4v|mov|m4a|mp3|aac|opus|jpg|jpeg|png|gif|webp|avif))(?:\s|$)")


def run_download(req: dict) -> None:
    base = find_siphon()
    if not base:
        send({"event": "error", "message": "siphon CLI not found (set SIPHON_BIN or install it)"})
        return
    outdir = default_outdir()
    # Prefer the specific stream the user clicked; fall back to the page URL (siphon resolves it).
    target = req.get("media") or req.get("page")
    if not target:
        send({"event": "error", "message": "no URL given"})
        return
    cmd = [*base, target, "-o", str(outdir), "-P", req.get("preset", "best")]
    if req.get("audioOnly"):
        cmd.append("-x")
    # Logged-in sites (X/Instagram image posts, age-gated video) need the browser's cookies.
    # The extension sends which browser it is; SIPHON_COOKIES_BROWSER overrides, "none" disables.
    cookies = os.environ.get("SIPHON_COOKIES_BROWSER") or req.get("browser")
    if cookies and cookies.lower() not in ("none", "off", ""):
        cmd += ["--cookies-from-browser", cookie_source(cookies.lower()), "--cookies-optional"]
    # A direct media URL from the page may 403 without the page context; pass the page too
    # by preferring it when the click was the page-level button (media is None).
    env = dict(os.environ)
    log(f"run: {' '.join(cmd)}")
    send({"event": "resolving", "message": "finding the stream…"})

    # siphon records every saved file in <outdir>/siphon.jsonl. Reading that back is exact and
    # unaffected by console line-wrapping, which can split a long "✓ /path" across two lines and
    # defeat stdout parsing. Snapshot the log's size now; new records are from this run.
    prov = outdir / "siphon.jsonl"
    prov_before = prov.stat().st_size if prov.exists() else 0

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                text=True, bufsize=1)
    except OSError as e:
        send({"event": "error", "message": f"couldn't launch siphon: {e}"})
        return

    saved_files = []  # stdout-parsed, used only if the provenance log is empty (e.g. --no-log)
    last_pct = -1
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        log("  " + line)
        if ("✓" in line or "Destination:" in line) and (m := SAVED.search(line)):
            saved_files.append(m.group(1))
            continue
        if m := PCT.search(line):
            pct = float(m.group(1))
            if pct - last_pct >= 1 or pct >= 100:
                last_pct = pct
                spd = SPEED.search(line)
                send({"event": "progress", "percent": pct, "speed": spd.group(1) if spd else None})
        elif "watching the network" in line or "finding" in line.lower() or "Chrome" in line:
            send({"event": "resolving", "message": "finding the stream…"})

    code = proc.wait()
    if code != 0:
        send({"event": "error", "message": f"siphon exited with code {code} (see {LOG})"})
        return

    files = read_saved(prov, prov_before, outdir) or saved_files
    if len(files) > 1:
        send({"event": "done", "file": f"{len(files)} files in {outdir}", "files": files})
    else:
        send({"event": "done", "file": files[0] if files else f"(saved to {outdir})"})


def read_saved(prov: Path, since: int, outdir: Path) -> list[str]:
    """Full paths of the files this run recorded in siphon.jsonl (new records only)."""
    if not prov.exists():
        return []
    out = []
    try:
        with open(prov) as f:
            f.seek(since)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                name = rec.get("file")
                if name:
                    out.append(str(outdir / name))
    except OSError:
        pass
    return out


def main() -> None:
    try:
        msg = read_message()
    except Exception as e:
        send({"event": "error", "message": f"bad message: {e}"})
        return
    if msg is None:
        return
    action = msg.get("action")
    if action == "hello":
        send({"event": "hello", "version": __version__})
    elif action == "download":
        run_download(msg)
    else:
        send({"event": "error", "message": f"unknown action: {action}"})


if __name__ == "__main__":
    main()
