"""Put siphon in the desktop's app launcher, with its icon, and optionally start it at login.

    siphon setup app               add siphon to the launcher (~/.local/share/applications)
    siphon setup app --autostart   also start it at login, quietly in the tray
    siphon setup app --remove      undo both

Without a .desktop file whose name matches the app id ("siphon"), Wayland compositors can't tie
the window to its icon or taskbar entry.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
from pathlib import Path

APP_ID = "siphon"


def _data() -> Path:
    return Path.home() / ".local/share"


def launcher_path() -> Path:
    return _data() / "applications" / f"{APP_ID}.desktop"


def autostart_path() -> Path:
    return Path.home() / ".config/autostart" / f"{APP_ID}.desktop"


def icon_path() -> Path:
    return _data() / "icons/hicolor/scalable/apps" / f"{APP_ID}.svg"


def desktop_entry(exe: str, hidden: bool = False) -> str:
    cmd = shlex.join([exe, "app"] + (["--hidden"] if hidden else []))
    return ("[Desktop Entry]\nType=Application\nName=siphon\nGenericName=Download Manager\n"
            "Comment=Download videos and images from any page you can watch\n"
            f"Exec={cmd}\nIcon={APP_ID}\nTerminal=false\nCategories=Network;FileTransfer;\n"
            "Keywords=download;video;youtube;twitter;yt-dlp;\nStartupWMClass=siphon\n"
            + ("X-GNOME-Autostart-enabled=true\n" if hidden else ""))


def install(exe: str, autostart: bool) -> list[Path]:
    src = Path(__file__).resolve().parents[1] / "web" / "mark.svg"
    icon = icon_path()
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, icon)
    out = [icon]
    for path, hidden in ((launcher_path(), False), *(((autostart_path(), True),) if autostart else ())):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(desktop_entry(exe, hidden))
        out.append(path)
    if not autostart:
        autostart_path().unlink(missing_ok=True)
    return out


def remove() -> list[Path]:
    gone = [p for p in (launcher_path(), autostart_path(), icon_path()) if p.exists()]
    for p in gone:
        p.unlink()
    return gone


def main(argv: list[str]) -> int:
    from rich.console import Console

    from ..browser import siphon_exe

    err = Console(stderr=True, highlight=False)
    p = argparse.ArgumentParser(prog="siphon setup app", description="Add the siphon app to your launcher.")
    p.add_argument("--autostart", action="store_true", help="also start siphon at login, in the tray")
    p.add_argument("--remove", action="store_true", help="remove it from the launcher and from login")
    a = p.parse_args(argv)
    if a.remove:
        for f in remove():
            err.print(f"  [dim]removed {f}[/]")
        err.print("[green]✓[/] siphon removed from the launcher.")
        return 0
    for f in install(siphon_exe(), a.autostart):
        err.print(f"  [green]✓[/] {f}")
    err.print("[green]✓[/] siphon is in your app launcher" + (" and starts at login (in the tray)." if a.autostart else "."))
    return 0
