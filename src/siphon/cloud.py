"""Cloud destinations through rclone: your own Google Drive, Dropbox, OneDrive, MEGA, R2, S3, ...

    siphon cloud add gdrive              connect a Google Drive (opens the browser to sign in)
    siphon cloud add r2 --type r2 access_key_id=… secret_access_key=… endpoint=…
    siphon cloud list                    what's connected
    siphon cloud default gdrive:siphon   where `--to cloud` and the phone's cloud option send files
    siphon URL --to gdrive:siphon/       download, upload, remove the local copy (--keep keeps it)

rclone does the sign-in and keeps the tokens in its own config (never sent through siphon serve).
siphon only names remotes and shells out to `rclone copyto`, reading its JSON stats for progress.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from . import paths

# friendly name -> (rclone backend type, extra fixed params)
KINDS = {
    "gdrive": ("drive", {}), "drive": ("drive", {}), "gphotos": ("google photos", {}),
    "dropbox": ("dropbox", {}), "onedrive": ("onedrive", {}), "mega": ("mega", {}),
    "pcloud": ("pcloud", {}), "box": ("box", {}), "b2": ("b2", {}), "s3": ("s3", {"provider": "AWS"}),
    "r2": ("s3", {"provider": "Cloudflare"}), "webdav": ("webdav", {}), "sftp": ("sftp", {}),
}
NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,40}")


class CloudError(RuntimeError):
    pass


def rclone() -> str:
    exe = shutil.which("rclone")
    if not exe:
        raise CloudError("rclone isn't installed: siphon setup rclone")
    return exe


def remotes() -> list[tuple[str, str]]:
    """[(name, type)] of the remotes rclone knows about."""
    try:
        exe = rclone()
    except CloudError:
        return []
    r = subprocess.run([exe, "listremotes", "--long"], capture_output=True, text=True, timeout=20)
    out = []
    for line in r.stdout.splitlines():
        name, _, kind = line.partition(":")
        if name and NAME_RE.fullmatch(name.strip()):
            out.append((name.strip(), kind.strip()))
    return out


def split_target(target: str) -> tuple[str, str]:
    """'gdrive:siphon/clips' -> ('gdrive', 'siphon/clips'). 'cloud' -> the configured default."""
    if target in ("cloud", "cloud:"):
        target = paths.load_config().get("cloud_default") or ""
        if not target:
            raise CloudError("no default cloud set: siphon cloud default REMOTE[:folder]")
    if target.startswith("cloud:"):
        target = target[len("cloud:"):]
    name, _, folder = target.partition(":")
    if not NAME_RE.fullmatch(name):
        raise CloudError(f"'{target}' isn't REMOTE[:folder]")
    known = {n for n, _ in remotes()}
    if name not in known:
        hint = f"connected: {', '.join(sorted(known))}" if known else "none connected yet"
        raise CloudError(f"no cloud called '{name}' ({hint}); add one with: siphon cloud add {name}")
    return name, folder.strip("/")


def upload(local: Path, target: str, keep: bool = False,
           progress: Callable[[int, int | None, float | None], None] | None = None) -> str:
    """Copy one file to REMOTE:folder/<name>; remove the local copy unless keep. Returns the remote path."""
    name, folder = split_target(target)
    dest = f"{name}:{folder + '/' if folder else ''}{local.name}"
    cmd = [rclone(), "copyto", str(local), dest, "--use-json-log", "--stats", "500ms",
           "--stats-log-level", "NOTICE", "--log-level", "NOTICE"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)
    errors = []
    for line in proc.stderr:
        try:
            rec = json.loads(line)
        except ValueError:
            errors.append(line.strip())
            continue
        if st := rec.get("stats"):
            if progress:
                progress(st.get("bytes") or 0, st.get("totalBytes") or local.stat().st_size, st.get("speed"))
        elif rec.get("level") in ("error", "critical"):
            errors.append(rec.get("msg", "").strip())
    if proc.wait() != 0:
        raise CloudError(f"upload to {dest} failed: {(errors[-1] if errors else 'rclone exited ' + str(proc.returncode))[:300]}")
    if progress:
        size = local.stat().st_size
        progress(size, size, None)
    if not keep:
        local.unlink(missing_ok=True)
    return dest


PRESIGNABLE = {"s3", "b2"}  # private, expiring links; other backends' "links" change sharing settings


def presign(remote_path: str, expire: str = "24h") -> str:
    """A private link to one uploaded file that expires. Only for S3-type storage (R2, S3, B2):
    for Drive or Dropbox `rclone link` makes the file publicly shared, which siphon won't do quietly."""
    name = remote_path.split(":", 1)[0]
    kinds = dict(remotes())
    if kinds.get(name) not in PRESIGNABLE:
        raise CloudError(f"'{name}' ({kinds.get(name, 'unknown')}) can't make private expiring links; open it in that service's app")
    r = subprocess.run([rclone(), "link", "--expire", expire, remote_path], capture_output=True, text=True, timeout=60)
    url = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if r.returncode != 0 or not url.startswith("https://"):
        raise CloudError(f"couldn't make a link: {(r.stderr.strip() or 'no URL returned')[-200:]}")
    return url


# ------------------------------------------------------------------ CLI

def main(argv: list[str]) -> int:
    from rich.console import Console

    err = Console(stderr=True, highlight=False)
    p = argparse.ArgumentParser(prog="siphon cloud", description="Send downloads to your own cloud storage (via rclone).")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="connect a cloud (signs in through the browser for Drive/Dropbox/OneDrive/...)")
    a.add_argument("name", help="a name for it, e.g. gdrive")
    a.add_argument("--type", help=f"one of {', '.join(sorted(KINDS))} (default: guessed from the name)")
    a.add_argument("params", nargs="*", metavar="KEY=VALUE", help="extra rclone settings (e.g. for S3/R2 keys)")
    sub.add_parser("list", help="show connected clouds and the default")
    d = sub.add_parser("default", help="set where 'cloud' sends files, e.g. gdrive:siphon")
    d.add_argument("target")
    args = p.parse_args(argv)

    try:
        if args.cmd == "list":
            rs = remotes()
            default = paths.load_config().get("cloud_default")
            if not rs:
                err.print("no clouds connected. Try: [bold]siphon cloud add gdrive[/]")
            for n, kind in rs:
                mark = "  [green]← default[/]" if default and default.split(":")[0] == n else ""
                err.print(f"  {n:<14} [dim]{kind}[/]{mark}", soft_wrap=True)
            return 0
        if args.cmd == "default":
            name, folder = split_target(args.target)
            cfg = paths.load_config()
            cfg["cloud_default"] = f"{name}:{folder}" if folder else name
            paths.save_config(cfg)
            err.print(f"[green]✓[/] 'cloud' now means [bold]{cfg['cloud_default']}[/]")
            return 0
        if args.cmd == "add":
            if not NAME_RE.fullmatch(args.name):
                raise CloudError("name: letters, digits, dot, dash or underscore")
            kind = args.type or args.name.lower().rstrip("0123456789")
            if kind not in KINDS:
                raise CloudError(f"what kind of cloud is '{args.name}'? pass --type ({', '.join(sorted(KINDS))})")
            backend, fixed = KINDS[kind]
            extra = [f"{k}={v}" for k, v in fixed.items()] + list(args.params)
            err.print(f"connecting [bold]{args.name}[/] ({backend}); rclone may open your browser to sign in…")
            # stdout carries the finished config, OAuth refresh token included; the sign-in link is on stderr
            r = subprocess.run([rclone(), "config", "create", args.name, backend, *extra], stdout=subprocess.DEVNULL)
            if r.returncode != 0:
                raise CloudError("rclone couldn't create it (see above)")
            cfg = paths.load_config()
            if not cfg.get("cloud_default"):
                cfg["cloud_default"] = f"{args.name}:siphon"
                paths.save_config(cfg)
            err.print(f"[green]✓[/] connected. Downloads with [bold]--to {args.name}:siphon[/] (or --to cloud) land there.")
            return 0
    except CloudError as e:
        err.print(f"[red]✗[/] {e}", soft_wrap=True)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
