"""What this machine can do, and getting it the rest of the way with no manual labour.

    siphon doctor                 capability map: what works, what each missing piece unlocks, the fix
    siphon setup                  fetch whatever the everyday path is missing (ffmpeg, a JS runtime)
    siphon setup --all            + the optional tools: rclone (cloud), cloudflared (share link)
    siphon setup rclone ...       just those
    siphon setup browser          connect the browser button (native-messaging host)
    siphon setup -y               don't ask

The capability list is data (`capabilities()`), so doctor, setup and `siphon serve`'s status
page all read the same truth.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import asdict, dataclass

from rich.console import Console
from rich.markup import escape as esc

from . import __version__, paths, tools

err = Console(stderr=True, highlight=False)

TAILSCALE_HINT = "curl -fsSL https://tailscale.com/install.sh | sh   (needs sudo; then: sudo tailscale up)"


@dataclass
class Cap:
    key: str
    label: str
    ok: bool | None  # True works · False missing and it matters · None optional, not set up
    detail: str
    fix: str = ""
    required: bool = False

    def to_json(self) -> dict:
        return asdict(self)


def _where(p: str | None) -> str:
    return f"{p} (fetched by siphon)" if tools.managed(p) else (p or "")


def capabilities() -> list[Cap]:
    import yt_dlp

    from .resolve.cdp import find_chrome, has_proprietary_codecs

    try:
        from gallery_dl import version as gdl
        gdl_v = gdl.__version__
    except Exception:
        gdl_v = "?"
    caps = [Cap("core", "core", True, f"siphon {__version__} · yt-dlp {yt_dlp.version.__version__} · gallery-dl {gdl_v}")]

    ff = tools.ffmpeg()
    caps.append(Cap("ffmpeg", "merge audio+video", bool(ff),
                    f"ffmpeg {tools.version_label(ff)} — {_where(ff)}" if ff
                    else "ffmpeg missing: separate audio/video streams can't be merged",
                    "" if ff else "siphon setup ffmpeg", required=True))

    js = tools.js_runtime()
    caps.append(Cap("js", "YouTube", bool(js), f"JS runtime {_where(js)}" if js
                    else "no deno/node: YouTube's signature challenge can't be solved",
                    "" if js else "siphon setup deno", required=True))

    ch = find_chrome()
    good = has_proprietary_codecs(ch)
    caps.append(Cap("chrome", "any-page sniff + blob capture", good,
                    (f"{ch} (H.264/AAC)" if good else f"{ch}: Chromium builds lack H.264/AAC, many players won't load")
                    if ch else "no Chrome: only sites yt-dlp/gallery-dl know will work",
                    "" if good else "install Google Chrome (or point SIPHON_CHROME at it)"))

    try:
        import curl_cffi  # noqa: F401
        caps.append(Cap("impersonate", "Cloudflare-fronted sites", True, "curl_cffi TLS impersonation"))
    except ImportError:
        caps.append(Cap("impersonate", "Cloudflare-fronted sites", None, "optional: some sites 403 without TLS impersonation",
                        # from the repo, not PyPI: the PyPI name isn't ours yet, and anyone could register it
                        "uv tool install --force 'antharmaya-siphon[impersonate] @ git+https://github.com/antharmaya/siphon'"))

    rc = shutil.which("rclone")
    caps.append(Cap("rclone", "cloud destinations", True if rc else None,
                    f"rclone — {_where(rc)}" if rc else "optional: send downloads to Drive/Dropbox/OneDrive/R2/S3",
                    "" if rc else "siphon setup rclone"))

    ts, cf = shutil.which("tailscale"), shutil.which("cloudflared")
    have = [n for n, p in (("tailscale", ts), ("cloudflared", cf)) if p]
    caps.append(Cap("remote", "reach siphon serve from anywhere", True if have else None,
                    " · ".join(have) + " available" if have else "optional: private (tailscale) or shareable (cloudflared) access",
                    "" if have else f"siphon setup cloudflared   or   {TAILSCALE_HINT}"))

    from . import browser
    hooked = browser.installed()
    caps.append(Cap("browser", "browser button (streams)", True if hooked else None,
                    f"native host registered with {len(hooked)} browser(s)" if hooked
                    else "optional: the extension can't hand streams to siphon yet",
                    "" if hooked else "siphon setup browser"))
    return caps


def doctor() -> int:
    paths.use_managed_bin()
    caps = capabilities()
    for c in caps:
        mark = "[green]✓[/]" if c.ok else ("[dim]·[/]" if c.ok is None else "[yellow]![/]")
        err.print(f"  {mark} {c.label:<34} [dim]{esc(c.detail)}[/]", soft_wrap=True)
        if c.fix:
            err.print(f"    {'':<34} → [bold]{esc(c.fix)}[/]", soft_wrap=True)
    broken = [c for c in caps if c.required and not c.ok]
    if broken:
        err.print(f"\n  [yellow]{len(broken)} needed piece(s) missing.[/] [bold]siphon setup[/] fetches them (no sudo).")
    return 1 if broken else 0


def _install_all(names: list[str]) -> int:
    from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn

    failed = 0
    for name in names:
        tool = tools.FETCHABLE[name]
        try:
            with Progress(TextColumn(f"  {name:<12}"), BarColumn(), DownloadColumn(), TransferSpeedColumn(),
                          console=err, transient=True) as prog:
                task = prog.add_task(name, total=tool.size_mb << 20)
                path, ver = tools.install(tool, lambda d, t: prog.update(task, completed=d, total=t))
            err.print(f"  [green]✓[/] {name:<12} [dim]{esc(ver[:60])} → {esc(str(path))}[/]", soft_wrap=True)
        except Exception as e:  # FetchError, network, disk — report and keep going with the rest
            failed += 1
            err.print(f"  [red]✗[/] {name:<12} {esc(str(e)[:200])}")
    return failed


def plan(names: list[str], everything: bool) -> list[str]:
    if names:
        return names
    want = tools.missing_core()
    if everything:
        want += [n for n in ("rclone", "cloudflared") if not shutil.which(n)]
    return want


def setup_main(argv: list[str]) -> int:
    if argv[:1] == ["browser"]:
        from . import browser
        return browser.main(argv[1:])
    if argv[:1] == ["engine"]:
        from . import engine
        return engine.main(argv[1:])
    if argv[:1] == ["app"]:
        from .app import desktop
        return desktop.main(argv[1:])
    paths.use_managed_bin()
    p = argparse.ArgumentParser(prog="siphon setup", description="Fetch the tools siphon needs, into its own bin dir (no sudo).")
    p.add_argument("tools", nargs="*", metavar="TOOL",
                   help=f"one or more of: {', '.join(tools.FETCHABLE)} (default: whatever is missing); "
                        "or `browser` to connect the browser button, or `engine` to run the engine in the background")
    p.add_argument("--all", action="store_true", help="also the optional tools (rclone, cloudflared)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask")
    a = p.parse_args(argv)
    if bad := [t for t in a.tools if t not in tools.FETCHABLE]:
        p.error(f"unknown tool {', '.join(bad)} (choose from {', '.join(tools.FETCHABLE)})")
    todo = plan(a.tools, a.all)
    if not todo:
        err.print("[green]✓[/] nothing to fetch — everything siphon needs is here.")
        return doctor()
    total = sum(tools.FETCHABLE[n].size_mb for n in todo)
    err.print(f"siphon will fetch into [bold]{esc(str(paths.BIN))}[/] (no sudo, official releases, checksummed):", soft_wrap=True)
    for n in todo:
        t = tools.FETCHABLE[n]
        err.print(f"  · {n:<12} ~{t.size_mb:>3} MB   [dim]{esc(t.unlocks)}[/]", soft_wrap=True)
    if not a.yes and sys.stdin.isatty():
        if input(f"  fetch ~{total} MB now? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            return 1
    failed = _install_all(todo)
    cfg = paths.load_config()
    if cfg.pop("declined", None) is not None:
        paths.save_config(cfg)
    err.print()
    rc = doctor()
    return 1 if failed else rc


def offer_core(interactive: bool, log) -> None:
    """Before a real download: if the everyday tools are missing, offer to fetch them once.
    Declining is remembered (no nagging); `siphon setup` always works later."""
    todo = tools.missing_core()
    if not todo:
        return
    cfg = paths.load_config()
    if not interactive or set(todo) <= set(cfg.get("declined", [])):
        log(f"missing {', '.join(todo)} — run `siphon setup` to fetch (no sudo)")
        return
    what = ", ".join(f"{n} (~{tools.FETCHABLE[n].size_mb} MB: {tools.FETCHABLE[n].unlocks})" for n in todo)
    ans = input(f"siphon is missing {what}.\nFetch into {paths.BIN} now? [Y/n] ").strip().lower()
    if ans in ("", "y", "yes"):
        _install_all(todo)
    else:
        cfg["declined"] = sorted(set(cfg.get("declined", [])) | set(todo))
        paths.save_config(cfg)
        log("ok — `siphon setup` fetches them whenever you want")
