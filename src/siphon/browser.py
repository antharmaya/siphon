"""Connect the browser button to this siphon: install the native-messaging host, no repo needed.

    siphon setup browser            register the host with every Chromium and Firefox-family browser here
    siphon setup browser --remove   unregister it

A browser extension can't start programs. Browsers instead start a "native host" named in a small
JSON manifest in their config dir, and only for the extension IDs that manifest allows. Chromium
and Firefox use different dirs and a different key for the IDs, so each family gets its own file.

A Flatpak or Snap browser is sandboxed and can't start a host at all. For those, `siphon serve` on
this computer plus "my siphon box" in the extension does the same job over localhost. siphon writes that manifest, plus a launcher that runs the host with siphon's own Python
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
FIREFOX_ID = "siphon@antharmaya.com"  # browser_specific_settings.gecko.id in the Firefox build

# Sandboxed browser installs (their per-app data dir), which can't use a native host.
SANDBOXED = {
    "Firefox (Flatpak)": ".var/app/org.mozilla.firefox", "Firefox (Snap)": "snap/firefox",
    "Zen (Flatpak)": ".var/app/app.zen_browser.zen", "LibreWolf (Flatpak)": ".var/app/io.gitlab.librewolf-community",
    "Floorp (Flatpak)": ".var/app/one.ablaze.floorp", "Chrome (Flatpak)": ".var/app/com.google.Chrome",
    "Chromium (Flatpak)": ".var/app/org.chromium.Chromium", "Brave (Flatpak)": ".var/app/com.brave.Browser",
    "Chromium (Snap)": "snap/chromium",
}


def firefox_dirs() -> list[Path]:
    """Each Firefox-family browser's user-level native-messaging dir, for browsers whose config exists."""
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library/Application Support"
        return [base / "Mozilla/NativeMessagingHosts"] if (base / "Firefox").is_dir() or (base / "Mozilla").is_dir() else []
    out = []
    # Firefox 157 reads ~/.mozilla/native-messaging-hosts even when its profile lives under ~/.config/mozilla
    # (checked in a real Firefox: a manifest only in the XDG dir is not found). Write both when XDG is in use.
    xdg = home / ".config/mozilla"
    if (home / ".mozilla").is_dir() or xdg.is_dir():
        out.append(home / ".mozilla/native-messaging-hosts")
    if xdg.is_dir():
        out.append(xdg / "native-messaging-hosts")
    # Zen, LibreWolf, Floorp and Waterfox each read their own dir, not ~/.mozilla
    return out + [home / d / "native-messaging-hosts" for d in (".zen", ".librewolf", ".floorp", ".waterfox")
                  if (home / d).is_dir()]


def sandboxed() -> list[str]:
    home = Path.home()
    return [name for name, d in SANDBOXED.items() if (home / d).is_dir()]


def browser_dirs() -> list[Path]:
    """Each Chromium browser's user-level NativeMessagingHosts dir, for browsers whose config exists."""
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


def manifest(launcher: Path, firefox: bool = False) -> dict:
    allowed = ({"allowed_extensions": [FIREFOX_ID]} if firefox
               else {"allowed_origins": [f"chrome-extension://{i}/" for i in EXTENSION_IDS.values()]})
    return {"name": HOST_NAME, "description": "siphon video downloader native host", "path": str(launcher),
            "type": "stdio", **allowed}


def install() -> list[Path]:
    chromium, firefox = browser_dirs(), firefox_dirs()
    if not chromium and not firefox:
        return []
    launcher = write_launcher()
    out = []
    for dirs, is_firefox in ((chromium, False), (firefox, True)):
        m = json.dumps(manifest(launcher, is_firefox), indent=2) + "\n"
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"{HOST_NAME}.json"
            f.write_text(m)
            out.append(f)
    return out


def installed() -> list[Path]:
    return [f for d in browser_dirs() + firefox_dirs() if (f := d / f"{HOST_NAME}.json").is_file()]


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
                                description="Connect the siphon browser button to this siphon "
                                            "(Chrome, Brave, Edge, Vivaldi, Chromium, Firefox, Zen, LibreWolf).")
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
    boxed = sandboxed()
    for f in done:
        err.print(f"  [green]✓[/] {esc(str(f))}", soft_wrap=True)
    if done:
        err.print("Reload the siphon extension (or restart the browser). Its popup should now show [bold]host v…[/] in green.")
    if boxed:
        err.print(f"[yellow]![/] {esc(', '.join(boxed))}: sandboxed, so the browser can't start the siphon app. "
                  "There, run [bold]siphon serve[/] on this computer and choose [bold]my siphon box[/] in the extension "
                  "(address http://127.0.0.1:8765, token from: siphon serve token).", soft_wrap=True)
    if not done and not boxed:
        err.print("[red]✗[/] no Chrome, Brave, Edge, Vivaldi, Chromium, Firefox, Zen or LibreWolf profile found on this "
                  "machine. Open the browser once, then run this again.")
    return 0 if done else 1
