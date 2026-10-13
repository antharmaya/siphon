"""Connect the browser button to this siphon: install the native-messaging host, no repo needed.

    siphon setup browser            register the host with every Chromium browser on this machine
    siphon setup browser --remove   unregister it

A browser extension can't start programs. Chrome-family browsers instead start a "native host"
named in a small JSON manifest in their config dir, and only for the extension IDs that manifest
allows. siphon writes that manifest, plus a launcher that runs the host with siphon's own Python
and points it at this siphon, so it keeps working whatever PATH the browser was started with.

The host code ships inside the wheel as siphon/browserhost.py (copied from
extension/host/siphon_host.py at build time; one source). In a dev checkout it runs from there.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from . import paths

HOST_NAME = "com.antharmaya.siphon"
# Keep in step with extension/host/install.sh (a test checks).
EXTENSION_IDS = {
    "github": "pbeimeldfnbenghllloohnnofbfaicfe",  # unpacked / GitHub zip (key in manifest.json)
    "store": "dkicnkfpoodpflagdafjoobaoclfndlp",   # Chrome Web Store
}


def browser_dirs() -> list[Path]:
    """Each browser's user-level NativeMessagingHosts dir, for browsers whose config exists."""
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library/Application Support"
        roots = ["Google/Chrome", "Google/Chrome Beta", "Chromium", "BraveSoftware/Brave-Browser",
                 "Microsoft Edge", "Vivaldi"]
    else:
        base = home / ".config"
        roots = ["google-chrome", "google-chrome-beta", "chromium", "BraveSoftware/Brave-Browser",
                 "microsoft-edge", "vivaldi", "vivaldi-snapshot"]
    dirs = [base / r for r in roots]
    if sys.platform != "darwin":
        dirs.append(home / "snap/vivaldi/current/.config/vivaldi")
    return [d / "NativeMessagingHosts" for d in dirs if d.is_dir()]


def host_script() -> Path:
    try:
        from . import browserhost  # present in a built wheel
        return Path(browserhost.__file__)
    except ImportError:
        p = Path(__file__).resolve().parents[2] / "extension" / "host" / "siphon_host.py"
        if p.is_file():
            return p
        raise FileNotFoundError("the browser host isn't in this install; reinstall siphon")


def siphon_exe() -> str:
    """This siphon's own entry point, next to the interpreter running it."""
    exe = Path(sys.executable).parent / ("siphon.exe" if sys.platform == "win32" else "siphon")
    if exe.is_file():
        return str(exe)
    import shutil
    return shutil.which("siphon") or str(exe)


def write_launcher() -> Path:
    launcher = paths.STATE / "browser-host"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "#!/bin/sh\n# Written by `siphon setup browser`. Browsers start this for the siphon extension.\n"
        f"SIPHON_BIN={shlex.quote(siphon_exe())} exec {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(host_script()))} \"$@\"\n")
    launcher.chmod(0o755)
    return launcher


def manifest(launcher: Path) -> dict:
    return {"name": HOST_NAME, "description": "siphon video downloader native host", "path": str(launcher),
            "type": "stdio", "allowed_origins": [f"chrome-extension://{i}/" for i in EXTENSION_IDS.values()]}


def install() -> list[Path]:
    dirs = browser_dirs()
    if not dirs:
        return []
    m = json.dumps(manifest(write_launcher()), indent=2) + "\n"
    out = []
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{HOST_NAME}.json"
        f.write_text(m)
        out.append(f)
    return out


def installed() -> list[Path]:
    return [f for d in browser_dirs() if (f := d / f"{HOST_NAME}.json").is_file()]


def remove() -> list[Path]:
    gone = installed()
    for f in gone:
        f.unlink()
    (paths.STATE / "browser-host").unlink(missing_ok=True)
    return gone


def main(argv: list[str]) -> int:
    from rich.console import Console
    from rich.markup import escape as esc

    err = Console(stderr=True, highlight=False)
    p = argparse.ArgumentParser(prog="siphon setup browser",
                                description="Connect the siphon browser button to this siphon (Chrome, Brave, Edge, Vivaldi, Chromium).")
    p.add_argument("--remove", action="store_true", help="unregister the host from every browser")
    a = p.parse_args(argv)
    if sys.platform == "win32":
        err.print("[yellow]![/] Windows isn't supported by `siphon setup browser` yet (it needs a registry entry).")
        return 1
    if a.remove:
        gone = remove()
        for f in gone:
            err.print(f"  [dim]removed {esc(str(f))}[/]", soft_wrap=True)
        err.print(f"[green]✓[/] browser button disconnected ({len(gone)} browser(s)).")
        return 0
    try:
        done = install()
    except FileNotFoundError as e:
        err.print(f"[red]✗[/] {e}")
        return 1
    if not done:
        err.print("[red]✗[/] no Chrome, Brave, Edge, Vivaldi or Chromium profile found on this machine. "
                  "Open the browser once, then run this again.")
        return 1
    for f in done:
        err.print(f"  [green]✓[/] {esc(str(f))}", soft_wrap=True)
    err.print("Reload the siphon extension (or restart the browser). Its popup should now show [bold]host v…[/] in green.")
    return 0
