"""Keep the siphon engine (`siphon serve`) running in the background, started at login.

    siphon setup engine            install and start it as a systemd user service
    siphon setup engine --remove   stop it and remove the service

The engine is what the browser button, the phone page and (soon) the desktop app talk to. A
sandboxed browser (Flatpak or Snap Zen/Firefox) can't start siphon itself, so for those the
engine must already be running, and it borrows that browser's login from its sandboxed profile.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

UNIT_NAME = "siphon-serve.service"

# Sandboxed browsers' cookie stores, as yt-dlp's firefox:<dir> (it finds the newest cookies.sqlite
# under the dir). Order: the first one present is used.
SANDBOXED_PROFILES = {
    "Zen (Flatpak)": ".var/app/app.zen_browser.zen/.zen",
    "Firefox (Flatpak)": ".var/app/org.mozilla.firefox/.mozilla/firefox",
    "LibreWolf (Flatpak)": ".var/app/io.gitlab.librewolf-community/.librewolf",
    "Firefox (Snap)": "snap/firefox/common/.mozilla/firefox",
}


def unit_path() -> Path:
    return Path.home() / ".config/systemd/user" / UNIT_NAME


def default_cookies() -> tuple[str, str] | None:
    """(browser name, --cookies-from-browser value) for the first sandboxed browser found."""
    home = Path.home()
    for name, rel in SANDBOXED_PROFILES.items():
        if (home / rel).is_dir():
            return name, f"firefox:{home / rel}"
    return None


def unit_text(siphon: str, cookies: str | None) -> str:
    cmd = [siphon, "serve", "--no-qr"] + (["--cookies-from-browser", cookies] if cookies else [])
    return ("[Unit]\nDescription=siphon engine: downloads for the browser button, phone and desktop app\n\n"
            "[Service]\n"
            f"ExecStart={shlex.join(cmd)}\n"
            "Restart=on-failure\n\n"
            "[Install]\nWantedBy=default.target\n")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def main(argv: list[str]) -> int:
    from rich.console import Console
    from rich.markup import escape as esc

    from . import client
    from .browser import siphon_exe

    err = Console(stderr=True, highlight=False)
    p = argparse.ArgumentParser(prog="siphon setup engine", description="Run the siphon engine in the background, started at login.")
    p.add_argument("--remove", action="store_true", help="stop the engine and remove the service")
    p.add_argument("--cookies-from-browser", metavar="BROWSER", help="whose login the engine uses (default: a sandboxed Zen/Firefox if present)")
    p.add_argument("--no-cookies", action="store_true", help="don't use any browser's login")
    a = p.parse_args(argv)
    if sys.platform != "linux" or not shutil.which("systemctl"):
        err.print("[yellow]![/] `siphon setup engine` needs systemd (Linux). Run [bold]siphon serve[/] yourself for now.")
        return 1
    unit = unit_path()
    if a.remove:
        _systemctl("disable", "--now", UNIT_NAME)
        unit.unlink(missing_ok=True)
        _systemctl("daemon-reload")
        err.print("[green]✓[/] the siphon engine is stopped and won't start at login.")
        return 0

    cookies, who = None, None
    if a.cookies_from_browser:
        cookies, who = a.cookies_from_browser, a.cookies_from_browser
    elif not a.no_cookies and (found := default_cookies()):
        who, cookies = found
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(unit_text(siphon_exe(), cookies))
    for step in (("daemon-reload",), ("enable", UNIT_NAME), ("restart", UNIT_NAME)):
        r = _systemctl(*step)
        if r.returncode:
            err.print(f"[red]✗[/] systemctl --user {' '.join(step)} failed: {esc(r.stderr.strip())}")
            return 1
    deadline = time.monotonic() + 20
    eng = None
    while time.monotonic() < deadline and not (eng := client.find()):
        time.sleep(0.5)
    if not eng:
        err.print("[red]✗[/] the service started but the engine didn't answer. See: journalctl --user -u " + UNIT_NAME)
        return 1
    err.print(f"[green]✓[/] the siphon engine is running at [bold]{eng.url}[/] and starts at login.", soft_wrap=True)
    err.print(f"  login used for downloads: {esc(who) if who else 'none (public pages only)'}", soft_wrap=True)
    err.print("  sandboxed browser? In the siphon popup choose [bold]my siphon box[/], address "
              f"{eng.url}, token from [bold]siphon serve token[/].", soft_wrap=True)
    return 0
