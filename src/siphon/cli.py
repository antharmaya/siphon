"""siphon — pull the video out of any page.

    siphon URL [URL...]              resolve + download the best stream
    siphon -F URL                    list what was found, download nothing
    siphon --browse URL              you drive Chrome, siphon catches every stream you play
    siphon 'blob:https://site/uuid'  (blob links are tab-local; opens browse mode at that site)
    siphon queue add URL...          add to the download queue (then: queue run -N 3)
    siphon doctor                    what works on this machine, and the fix for what doesn't
    siphon setup                     fetch missing tools (ffmpeg, deno, rclone, cloudflared), no sudo
    siphon serve                     your private download box, reachable from your phone
    siphon cloud add gdrive          send downloads to your own cloud (then: --to gdrive:siphon)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from rich.console import Console
from rich.markup import escape as esc
from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn, TransferSpeedColumn
from rich.prompt import Prompt
from rich.table import Table

from . import __version__, events, netcheck, paths, tools
from .download import DrmProtected, download
from .models import Candidate, human_size
from .resolve import resolve
from .resolve.sniff import PROFILE_DIR, SniffOptions
from .ytdlp import DEFAULT_TEMPLATE, YtdlpConfig, YtdlpEngine, browser_spec

err = Console(stderr=True, highlight=False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="siphon", description="Pull the video out of any page.",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("urls", nargs="*", help="page, media, or blob: URLs (or 'doctor')")
    p.add_argument("-a", "--batch-file", help="file with one URL per line ('-' for stdin)")
    p.add_argument("-o", "--output", default=".", help="output directory (default: .)")
    p.add_argument("-t", "--template", default=DEFAULT_TEMPLATE, help="yt-dlp output template for extractor sites")
    p.add_argument("-f", "--format", help="yt-dlp format selector, e.g. 'bv*[height<=1080]+ba/b'")
    p.add_argument("-P", "--preset", choices=["best", "compat", "small", "archive"], default="best",
                   help="best: max quality (AV1/VP9 ok) · compat: H.264+AAC MP4, plays anywhere · "
                        "small: <=720p smallest · archive: best + metadata/chapters/subs/thumbnail/info.json")
    p.add_argument("-F", "--list", action="store_true", help="list candidates (and formats) without downloading")
    p.add_argument("-j", "--json", action="store_true", help="print candidates as JSON, download nothing")
    p.add_argument("-e", "--engine", choices=["auto", "direct", "ytdlp", "sniff"], default="auto",
                   help="resolver: auto tries direct -> yt-dlp -> browser sniff")
    p.add_argument("-s", "--sniff", action="store_true", help="skip yt-dlp, go straight to the browser sniffer")
    p.add_argument("-B", "--browse", action="store_true", help="open a visible Chrome; you play the video, siphon catches it")
    p.add_argument("--capture", choices=["auto", "force", "off"], default="auto",
                   help="record blob:/MediaSource video in-page. auto = only when no fetchable stream is found")
    p.add_argument("--all", action="store_true", help="download every usable candidate, not just the best")
    p.add_argument("-p", "--pick", action="store_true", help="choose which candidate to download")
    p.add_argument("-N", "--connections", type=int, default=8, help="parallel connections / fragments (default 8)")
    p.add_argument("--timeout", type=float, default=25, help="seconds to watch a page for media (default 25)")
    p.add_argument("--cookies-from-browser", metavar="BROWSER[:PROFILE]", help="use your browser's login (chrome, firefox, brave…)")
    p.add_argument("--cookies", metavar="FILE", help="Netscape cookies.txt")
    p.add_argument("--no-profile", action="store_true", help=f"don't use siphon's persistent browser profile ({PROFILE_DIR})")
    p.add_argument("--proxy", default=os.environ.get("SIPHON_PROXY"),
                   help="http/socks proxy URL, e.g. socks5h://127.0.0.1:1080 (env: SIPHON_PROXY)")
    p.add_argument("--proxy-domains", default=os.environ.get("SIPHON_PROXY_DOMAINS", ""),
                   help="only use the proxy for these comma-separated domains, e.g. tiktok.com,instagram.com "
                        "(env: SIPHON_PROXY_DOMAINS; default: proxy everything)")
    p.add_argument("--no-netcheck", action="store_true", help="skip the ISP/geo-block diagnosis before resolving")
    p.add_argument("--impersonate", metavar="TARGET", help="yt-dlp TLS impersonation target, e.g. chrome")
    p.add_argument("-x", "--audio", action="store_true", help="audio only")
    p.add_argument("--subs", action="store_true", help="download + embed subtitles (extractor sites)")
    p.add_argument("--sponsorblock", nargs="?", const="sponsor", metavar="CATS",
                   help="cut SponsorBlock segments (default: sponsor; or a comma list: sponsor,intro,outro,selfpromo,music_offtopic)")
    p.add_argument("--section", metavar="START-END",
                   help="download only this span, e.g. 1:30-2:45 or 30-90 (seconds); END blank = to the end")
    p.add_argument("-r", "--rate-limit", metavar="RATE", help="cap download speed, e.g. 2M or 500K")
    p.add_argument("--archive", metavar="FILE", help="download-archive file: skip anything already recorded in it")
    p.add_argument("--no-playlist", action="store_true", help="only the video, not the whole playlist")
    p.add_argument("--no-gallery", action="store_true", help="don't use gallery-dl for image posts (tweets, IG, ...)")
    p.add_argument("--gallery-all", action="store_true", help="for a profile/feed URL, download the whole thing (can be huge)")
    p.add_argument("--live-duration", type=float, metavar="SECONDS",
                   help="for live streams: record this many seconds (default: until the stream ends or Ctrl+C)")
    p.add_argument("--keep-ads", action="store_true", help="don't block ad/tracker domains while sniffing")
    p.add_argument("--to", metavar="REMOTE[:FOLDER]",
                   help="upload each file to your cloud (rclone remote), e.g. gdrive:siphon; 'cloud' = your default")
    p.add_argument("--keep", action="store_true", help="with --to: keep the local copy too")
    p.add_argument("--no-log", action="store_true", help="don't append provenance to <output>/siphon.jsonl")
    p.add_argument("--progress-json", action="store_true",
                   help="write machine-readable events (JSON lines) to stdout; human output stays on stderr")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-V", "--version", action="version", version=f"siphon {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    paths.use_managed_bin()  # tools `siphon setup` fetched are found like any other
    if raw[:1] == ["queue"]:  # subcommands with their own flags — dispatch before argparse
        from .queue import main as queue_main
        return queue_main(raw[1:])
    if raw[:1] == ["setup"]:
        from .setup import setup_main
        return setup_main(raw[1:])
    if raw[:1] == ["serve"]:
        from .serve import main as serve_main
        return serve_main(raw[1:])
    if raw[:1] == ["cloud"]:
        from .cloud import main as cloud_main
        return cloud_main(raw[1:])
    args = build_parser().parse_args(argv)
    if args.urls[:1] == ["doctor"]:
        from .setup import doctor
        return doctor()

    urls = list(args.urls)
    if args.batch_file:
        src = sys.stdin if args.batch_file == "-" else open(args.batch_file)
        urls += [ln.strip() for ln in src if ln.strip() and not ln.lstrip().startswith(("#", ";"))]
    if not urls:
        build_parser().print_help()
        return 2

    if args.progress_json:
        events.enable()
    if args.to:
        from .cloud import CloudError, split_target
        try:
            split_target(args.to)
        except CloudError as e:
            err.print(f"[red]✗ {esc(str(e))}[/]", soft_wrap=True)  # never split the fix command
            events.emit("error", message=str(e))
            events.emit("done", ok=False)
            return 2
    outdir = Path(args.output).expanduser()
    sweep_stale_captures(outdir)
    log = (lambda m: None) if args.quiet or args.json else (lambda m: err.print(f"[dim]·[/] {esc(m)}"))
    if not (args.json or args.list):
        from .setup import offer_core
        offer_core(sys.stdin.isatty() and not args.quiet and not args.progress_json, log)
    ydl = YtdlpEngine(YtdlpConfig(
        outdir=outdir, template=args.template, format=args.format, connections=args.connections,
        cookies_file=args.cookies, cookies_from_browser=args.cookies_from_browser, proxy=args.proxy,
        impersonate=args.impersonate, no_playlist=args.no_playlist, gallery_all=args.gallery_all, live_duration=args.live_duration, preset=args.preset,
        sponsorblock=[c.strip() for c in (args.sponsorblock or "").split(",") if c.strip()],
        section=args.section, rate_limit=args.rate_limit, archive=args.archive, audio_only=args.audio, subs=args.subs,
        quiet=args.quiet or args.json, verbose=args.verbose,
    ))

    args.proxy_base = args.proxy
    failures = 0
    all_json = []
    for url in urls:
        try:
            ok, data = run_one(url, args, ydl, outdir, log)
            all_json.extend(data)
            failures += 0 if ok else 1
        except KeyboardInterrupt:
            err.print("[yellow]interrupted[/] — rerun the same command to resume")
            events.emit("error", message="interrupted")
            return 130
    if args.json:
        print(json.dumps(all_json, indent=2))
    events.emit("done", ok=not failures)
    return 1 if failures else 0


def sweep_stale_captures(outdir: Path, max_age: float = 3600) -> None:
    """A killed run (Ctrl+C at the wrong moment, timeout, sleep) never reaches its cleanup;
    its recorded blob tracks are useless after the fact. Drop any older than an hour."""
    try:
        for d in outdir.glob(".siphon-capture-*"):
            if d.is_dir() and time.time() - d.stat().st_mtime > max_age:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def proxy_for(url: str, base: str | None, domains: str) -> str | None:
    """Route only the listed domains through the proxy, so a tunnel abroad isn't used for everything."""
    if not base:
        return None
    wanted = [d.strip().lower().lstrip(".") for d in domains.split(",") if d.strip()]
    host = (urlsplit(url).hostname or "").lower()
    if not wanted or any(host == d or host.endswith("." + d) for d in wanted):
        return base
    return None


def run_one(url: str, args, ydl: YtdlpEngine, outdir: Path, log) -> tuple[bool, list[dict]]:
    events.emit("resolving", url=url)
    engine = "sniff" if (args.sniff or args.browse) else args.engine
    page_url = url[5:] if url.startswith("blob:") else url
    args.proxy = proxy_for(page_url, args.proxy_base, args.proxy_domains)
    ydl.cfg.proxy = args.proxy
    if args.proxy:
        where = f"{urlsplit(args.proxy).hostname}:{urlsplit(args.proxy).port}"
        if not netcheck.proxy_alive(args.proxy):
            if args.json:
                return False, [{"page": url, "blocked": "proxy", "detail": f"proxy {where} not reachable"}]
            events.emit("error", message=f"proxy {where} isn't accepting connections")
            err.print(f"[red]✗ proxy {esc(where)} isn't accepting connections.[/] Is the tunnel up? "
                      f"(e.g. [bold]ssh -D {urlsplit(args.proxy).port} user@vm[/])")
            return False, []
        log(f"via proxy {urlsplit(args.proxy).scheme}://{where}")
    elif not args.no_netcheck:
        reach = netcheck.check(page_url)
        if not reach.ok:
            msg = f"✗ {reach.detail}\n  → {netcheck.advice(reach)}"
            events.emit("error", message=reach.detail, blocked=reach.layer)
            if args.json:
                return False, [{"page": url, "blocked": reach.layer, "detail": reach.detail}]
            err.print(f"[red]{esc(msg)}[/]")
            return False, []
    headed = args.browse
    if url.startswith("blob:"):
        origin = url[5:]
        parts = urlsplit(origin)
        url = f"{parts.scheme}://{parts.netloc}/"
        err.print("[yellow]blob: links only exist inside the tab that created them[/] — no tool can fetch one from "
                  f"outside. Opening [bold]{esc(url)}[/] in browse mode: go to the video, play it, close the window.")
        engine, headed = "sniff", True

    sopts = SniffOptions(
        timeout=args.timeout, headed=headed, cookies=_sniff_cookies(args, url, log),
        profile=None if args.no_profile else PROFILE_DIR, proxy=args.proxy, block_ads=not args.keep_ads,
        capture=args.capture, workdir=outdir / f".siphon-capture-{int(time.time())}", log=log,
    )
    if not args.json and not args.quiet:
        err.rule(f"[bold]{esc(url)}", style="dim")

    def on_found(c: Candidate) -> None:
        if headed and not args.json:
            err.print(f"  [green]caught[/] {esc(c.label())}  [dim]{esc(c.url[:110])}[/]")

    from .download.browserfetch import browser_fetch

    args.browser_fallback = lambda c, dest, progress: browser_fetch(c, dest, profile=sopts.profile, proxy=args.proxy,
                                                                    cookies=sopts.cookies, log=log, progress=progress)
    sopts.no_gallery = args.no_gallery
    res = resolve(url, engine, ydl, sopts, proxy=args.proxy, on_found=on_found, log=log)
    sniffs = [res]

    def fallback():
        """A matched extractor can still fail (site changed, pointer to nowhere). The browser
        sniffer is strictly more general, so give it one shot before giving up."""
        r = resolve(url, "sniff", ydl, sopts, proxy=args.proxy, on_found=on_found, log=log)
        sniffs.append(r)
        return r

    try:
        return act(url, res, args, ydl, outdir, fallback if engine == "auto" else None)
    finally:
        # Recorded blob tracks exist only to be muxed in this run; every exit path drops them.
        for r in sniffs:
            if r.sniffed and r.sniffed.workdir:
                shutil.rmtree(r.sniffed.workdir, ignore_errors=True)


def act(url: str, res, args, ydl: YtdlpEngine, outdir: Path, fallback=None) -> tuple[bool, list[dict]]:
    log = (lambda m: None) if args.quiet or args.json else (lambda m: err.print(f"[dim]·[/] {esc(m)}"))
    if not args.json and not args.quiet:
        for name, outcome in res.tried:
            err.print(f"  [dim]{name:>6}[/] {esc(outcome)}")

    if args.json:
        return bool(res.candidates), [{"page": url, **c.to_json()} for c in res.candidates]

    if not res.candidates:
        events.emit("error", message="no media found on the page")
        err.print("[red]✗ no media found.[/] Try [bold]--browse[/] (log in / click through), "
                  "[bold]--cookies-from-browser chrome[/], or a longer [bold]--timeout[/].")
        return False, []

    if not args.quiet or args.list:
        show_table(res.candidates)
    if args.list:
        site = next((c for c in res.candidates if c.kind == "site"), None)
        if site:
            try:
                ydl.list_formats(site)
            except Exception as e:
                err.print(f"[red]✗ couldn't list formats:[/] {esc(str(e)[:300])}")
                return False, []
        return True, []

    drm_only = all(c.drm for c in res.candidates)
    if drm_only:
        events.emit("error", message=f"DRM-protected ({res.candidates[0].drm}); siphon does not decrypt DRM")
        err.print(f"[red]✗ DRM-protected ({esc(res.candidates[0].drm or '')}).[/] siphon does not decrypt DRM; "
                  "the bytes on the wire are ciphertext without a licensed CDM.")
        return False, []

    targets = choose(res.candidates, args)
    ok = True
    for c in targets:
        try:
            files = do_download(c, args, ydl, outdir, log)
        except DrmProtected as e:
            err.print(f"[red]✗ skipped DRM-protected stream ({esc(str(e))})[/]")
            ok = False
            continue
        except Exception as e:
            if c.kind == "site" and fallback:
                log(f"extractor download failed ({str(e)[:120]}); retrying with the browser sniffer")
                return act(url, fallback(), args, ydl, outdir, None)
            events.emit("error", message=f"{type(e).__name__}: {str(e)[:300]}")
            err.print(f"[red]✗ {type(e).__name__}:[/] {esc(str(e)[:300])}")
            ok = False
            continue
        for f in files:
            err.print(f"[green]✓[/] {esc(str(f))}  [dim]{human_size(f.stat().st_size) if f.exists() else ''}[/]")
            if not f.exists():
                continue
            events.emit("saved", file=str(f.resolve()), bytes=f.stat().st_size)
            remote = None
            if args.to:
                remote = send_to_cloud(f, args)
                if remote is None:
                    ok = False
            if not args.no_log:
                provenance(outdir, url, c, f, uploaded_to=remote)  # hashed while the file is still here
            if remote and not args.keep:
                f.unlink(missing_ok=True)
    return ok, []


def choose(cands: list[Candidate], args) -> list[Candidate]:
    usable = [c for c in cands if not c.drm]
    if args.all:
        # One per distinct stream: skip renditions when their master is present.
        has_master = any(c.is_master for c in usable)
        return [c for c in usable if not (c.kind == "hls" and has_master and not c.is_master) and c.score >= 20]
    if args.pick and sys.stdin.isatty():
        i = Prompt.ask("download which #", default="1", console=err)
        idx = [int(x) - 1 for x in i.replace(",", " ").split() if x.isdigit()]
        return [cands[j] for j in idx if 0 <= j < len(cands) and not cands[j].drm]
    return [max(usable, key=lambda c: c.score)]


def do_download(c: Candidate, args, ydl: YtdlpEngine, outdir: Path, log) -> list[Path]:
    fb = getattr(args, "browser_fallback", None)
    if c.kind != "file" or args.quiet or events.enabled():
        cb = events.progress if events.enabled() else None
        return download(c, outdir, ydl, args.connections, args.proxy, progress=cb, log=log, browser_fallback=fb)
    with Progress(TextColumn("[bold]{task.description}"), BarColumn(), DownloadColumn(), TransferSpeedColumn(),
                  TimeRemainingColumn(), console=err, transient=True) as prog:
        task = prog.add_task(f"{args.connections}×", total=c.size)

        def update(done: int, total: int | None) -> None:
            prog.update(task, completed=done, total=total)

        return download(c, outdir, ydl, args.connections, args.proxy, progress=update, log=log, browser_fallback=fb)


def show_table(cands: list[Candidate]) -> None:
    # Candidates from one page share a title: print it once instead of on every row.
    titles = {c.title for c in cands if c.title}
    shared = len(titles) == 1
    if shared:
        err.print(f"[bold]{esc(next(iter(titles)))}[/]")
    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    t.add_column("#", no_wrap=True)
    t.add_column("kind", no_wrap=True, min_width=6)
    t.add_column("via", no_wrap=True)
    t.add_column("size", no_wrap=True, justify="right")
    t.add_column("note", overflow="ellipsis", no_wrap=True, max_width=26)
    t.add_column("url", overflow="fold", ratio=1)
    for i, c in enumerate(cands, 1):
        kind = c.kind + (" master" if c.is_master else "")
        if c.drm:
            kind = f"[red]{kind} DRM[/]"
        note = c.note or (c.mime or "")
        if not shared and c.title:
            note = f"{c.title} · {note}"
        t.add_row(str(i), kind, c.source, human_size(c.size) if c.size else "", f"[dim]{esc(note)}[/]",
                  f"[dim]{esc(c.url[:160])}[/]")
    err.print(t)


def send_to_cloud(f: Path, args) -> str | None:
    """Upload one saved file; returns the remote path, or None after reporting the failure."""
    from .cloud import CloudError, upload

    events.emit("uploading", file=str(f.resolve()), to=args.to)
    if not args.quiet and not events.enabled():
        err.print(f"[dim]·[/] uploading to {esc(args.to)}…")
    try:
        dest = upload(f, args.to, keep=True, progress=events.progress if events.enabled() else None)
    except CloudError as e:
        events.emit("error", message=str(e))
        err.print(f"[red]✗ {esc(str(e))}[/] (kept the local copy)", soft_wrap=True)
        return None
    events.emit("uploaded", file=str(f.resolve()), remote=dest, kept=args.keep)
    err.print(f"[green]☁[/] {esc(dest)}")
    return dest


def provenance(outdir: Path, page: str, c: Candidate, f: Path, uploaded_to: str | None = None) -> None:
    """Research-grade record: where every file came from, when, and its hash."""
    h = hashlib.sha256()
    with open(f, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "page": page, "media": c.url if c.kind != "capture" else None,
           "kind": c.kind, "via": c.source, "title": c.title, "file": f.name, "bytes": f.stat().st_size,
           "sha256": h.hexdigest(), "note": c.note or None}
    if uploaded_to:
        rec["uploaded_to"] = uploaded_to
    with open(outdir / "siphon.jsonl", "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _site(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-2]) <= 3 and len(parts[-1]) == 2:  # example.co.in / bbc.co.uk
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _sniff_cookies(args, url: str, log) -> list[dict]:
    """Load your real browser's cookies for THIS site only into the sniffer's browser."""
    if not args.cookies_from_browser and not args.cookies:
        return []
    from http.cookiejar import MozillaCookieJar

    from yt_dlp.cookies import extract_cookies_from_browser

    try:
        if args.cookies_from_browser:
            b, profile, keyring, container = browser_spec(args.cookies_from_browser)
            jar = extract_cookies_from_browser(b, profile, keyring=keyring, container=container)
        else:
            jar = MozillaCookieJar(args.cookies)
            jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as e:
        log(f"could not read cookies: {e}")
        return []
    site = _site(urlsplit(url).hostname or "")
    out = []
    for ck in jar:
        if not ck.domain.lstrip(".").endswith(site):
            continue
        d = {"name": ck.name, "value": ck.value or "", "domain": ck.domain, "path": ck.path or "/",
             "secure": bool(ck.secure), "httpOnly": bool(ck.has_nonstandard_attr("HttpOnly"))}
        if ck.expires:
            d["expires"] = float(ck.expires)
        out.append(d)
    log(f"loaded {len(out)} cookies for {site}")
    return out
