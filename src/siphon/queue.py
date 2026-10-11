"""Persistent download queue with a concurrency cap.

The one thing IDM/JDownloader have that a plain CLI doesn't: line up many downloads, run
a few at a time, survive a crash. Each job runs as its OWN `siphon` subprocess — perfect
isolation (a stuck browser or a crash in one job never touches the others) and it reuses
the entire download pipeline with no special-casing. State is one JSON file.

    siphon queue add URL [URL...] [siphon flags]   add jobs (flags stored per job)
    siphon queue list                              show the queue
    siphon queue run [-N 3]                         process pending jobs, N at a time
    siphon queue clear [--done | --all]            drop finished (or all) jobs
    siphon queue rm ID...                           drop specific jobs
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .paths import STATE

QUEUE = Path(os.environ.get("SIPHON_QUEUE", STATE / "queue.json"))

_lock = threading.Lock()


def _siphon_cmd() -> list[str]:
    """How to launch siphon for a job. Prefer the installed binary; fall back to this module."""
    if exe := shutil.which("siphon"):
        return [exe]
    return [sys.executable, "-m", "siphon"]


def load() -> list[dict]:
    try:
        return json.loads(QUEUE.read_text())
    except (OSError, ValueError):
        return []


def save(jobs: list[dict]) -> None:
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, indent=2))
    os.replace(tmp, QUEUE)


def add(urls: list[str], extra: list[str]) -> list[dict]:
    with _lock:
        jobs = load()
        new = []
        for url in urls:
            job = {"id": uuid.uuid4().hex[:8], "url": url, "args": extra, "status": "pending",
                   "added": time.strftime("%Y-%m-%dT%H:%M:%S"), "file": None, "error": None}
            jobs.append(job)
            new.append(job)
        save(jobs)
    return new


def _has_output(args: list[str]) -> bool:
    return any(a in ("-o", "--output") for a in args)


def default_outdir() -> Path:
    if d := os.environ.get("SIPHON_DOWNLOAD_DIR"):
        return Path(d).expanduser()
    dl = Path.home() / "Downloads"
    return dl if dl.exists() else Path.cwd()


def _run_one(job: dict, outdir: Path, log) -> dict:
    cmd = [*_siphon_cmd(), job["url"], *job["args"], "-q"]
    if not _has_output(job["args"]):
        cmd += ["-o", str(outdir)]
    od = outdir
    if _has_output(job["args"]):
        for i, a in enumerate(job["args"]):
            if a in ("-o", "--output") and i + 1 < len(job["args"]):
                od = Path(job["args"][i + 1]).expanduser()
    prov = od / "siphon.jsonl"
    before = prov.stat().st_size if prov.exists() else 0
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        job["status"], job["error"] = "error", "timed out after 1h"
        return job
    if r.returncode == 0:
        files = _new_files(prov, before, od)
        job["status"] = "done"
        job["file"] = files[0] if len(files) == 1 else (f"{len(files)} files" if files else str(od))
    else:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        job["status"], job["error"] = "error", (tail[-1][:200] if tail else f"exit {r.returncode}")
    return job


def _new_files(prov: Path, since: int, outdir: Path) -> list[str]:
    if not prov.exists():
        return []
    out = []
    try:
        with open(prov) as f:
            f.seek(since)
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("file"):
                    out.append(str(outdir / rec["file"]))
    except OSError:
        pass
    return out


def run(concurrency: int, outdir: Path, log) -> int:
    from concurrent.futures import ThreadPoolExecutor

    with _lock:
        jobs = load()
        # A previous run that was interrupted left jobs "active"; they never finished — requeue.
        for j in jobs:
            if j["status"] == "active":
                j["status"] = "pending"
        pending = [j for j in jobs if j["status"] == "pending"]
        for j in pending:
            j["status"] = "active"
        save(jobs)

    if not pending:
        log("queue is empty (nothing pending)")
        return 0

    total = len(pending)
    log(f"running {total} job(s), {concurrency} at a time")
    done = {"n": 0, "ok": 0}

    def work(job: dict) -> None:
        start = time.monotonic()
        _run_one(job, outdir, log)
        job["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with _lock:
            jobs = load()
            for i, j in enumerate(jobs):
                if j["id"] == job["id"]:
                    jobs[i] = job
            save(jobs)
            done["n"] += 1
            done["ok"] += job["status"] == "done"
            mark = "✓" if job["status"] == "done" else "✗"
            detail = job["file"] if job["status"] == "done" else job["error"]
            log(f"[{done['n']}/{total}] {mark} {job['url'][:60]}  ({time.monotonic() - start:.0f}s)  {detail or ''}")

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        list(ex.map(work, pending))

    log(f"done: {done['ok']}/{total} succeeded")
    return 0 if done["ok"] == total else 1


def clear(which: str) -> int:
    with _lock:
        jobs = load()
        if which == "all":
            kept = []
        elif which == "done":
            kept = [j for j in jobs if j["status"] not in ("done",)]
        else:  # finished = done + error
            kept = [j for j in jobs if j["status"] in ("pending", "active")]
        removed = len(jobs) - len(kept)
        save(kept)
    return removed


def remove(ids: list[str]) -> int:
    with _lock:
        jobs = load()
        kept = [j for j in jobs if j["id"] not in ids and not any(j["id"].startswith(i) for i in ids)]
        removed = len(jobs) - len(kept)
        save(kept)
    return removed


# ----------------------------------------------------------------- CLI

def _print_list(err) -> None:
    jobs = load()
    if not jobs:
        err.print("[dim]queue is empty[/]")
        return
    from rich.table import Table

    t = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    for c in ("id", "status", "url", "file / error"):
        t.add_column(c, overflow="ellipsis", no_wrap=True, max_width=60 if c == "url" else None)
    colour = {"pending": "yellow", "active": "cyan", "done": "green", "error": "red"}
    for j in jobs:
        st = f"[{colour.get(j['status'], 'white')}]{j['status']}[/]"
        t.add_row(j["id"], st, j["url"], j.get("file") or j.get("error") or "")
    err.print(t)
    n = {s: sum(j["status"] == s for j in jobs) for s in ("pending", "active", "done", "error")}
    err.print(f"[dim]{n['pending']} pending · {n['active']} active · {n['done']} done · {n['error']} error[/]")


def main(argv: list[str]) -> int:
    from rich.console import Console

    err = Console(stderr=True, highlight=False)
    p = argparse.ArgumentParser(prog="siphon queue", description="Persistent download queue.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add URLs (any extra siphon flags are stored per job)")
    a.add_argument("urls", nargs="+")

    sub.add_parser("list", help="show the queue")

    r = sub.add_parser("run", help="process pending jobs")
    r.add_argument("-N", "--concurrency", type=int, default=3)
    r.add_argument("-o", "--output", help="download dir for jobs without their own -o")

    c = sub.add_parser("clear", help="remove jobs")
    c.add_argument("--all", action="store_true", help="remove every job")
    c.add_argument("--done", action="store_true", help="remove only completed jobs")

    rm = sub.add_parser("rm", help="remove jobs by id (prefix ok)")
    rm.add_argument("ids", nargs="+")

    args, extra = p.parse_known_args(argv)

    if args.cmd == "add":
        new = add(args.urls, extra)
        err.print(f"[green]+[/] queued {len(new)} job(s)" + (f" with {' '.join(extra)}" if extra else ""))
        return 0
    if args.cmd == "list":
        _print_list(err)
        return 0
    if args.cmd == "run":
        outdir = Path(args.output).expanduser() if args.output else default_outdir()
        rc = run(args.concurrency, outdir, lambda m: err.print(f"[dim]·[/] {m}"))
        _print_list(err)
        return rc
    if args.cmd == "clear":
        which = "all" if args.all else ("done" if args.done else "finished")
        err.print(f"removed {clear(which)} job(s)")
        return 0
    if args.cmd == "rm":
        err.print(f"removed {remove(args.ids)} job(s)")
        return 0
    return 2
