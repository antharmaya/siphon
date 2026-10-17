"""Persistent download queue with a concurrency cap.

The one thing IDM/JDownloader have that a plain CLI doesn't: line up many downloads, run
a few at a time, survive a crash. Each job runs as its OWN `siphon` subprocess — perfect
isolation (a stuck browser or a crash in one job never touches the others) and it reuses
the entire download pipeline with no special-casing. State is a SQLite database shared by every
siphon process (see store.py); an old queue.json is imported once.

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
from pathlib import Path

from .paths import STATE
from .store import Store

# The pre-0.4 queue file; the database lives next to it and imports it on first use.
QUEUE = Path(os.environ.get("SIPHON_QUEUE", STATE / "queue.json"))

_stores: dict[Path, Store] = {}
_stores_lock = threading.Lock()


def db() -> Store:
    path = QUEUE.with_name("queue.db")
    with _stores_lock:
        if path not in _stores:
            _stores[path] = Store(path, legacy_json=QUEUE)
        return _stores[path]


def _siphon_cmd() -> list[str]:
    """How to launch siphon for a job. Prefer the installed binary; fall back to this module."""
    if exe := shutil.which("siphon"):
        return [exe]
    return [sys.executable, "-m", "siphon"]


def load() -> list[dict]:
    return db().all()


def save(jobs: list[dict]) -> None:
    """Replace the whole queue (tests and tools; workers use claim/update)."""
    db().replace_all(jobs)


def add(urls: list[str], extra: list[str]) -> list[dict]:
    return db().add(urls, extra)


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

    store = db()
    recovered = store.recover()  # only jobs whose runner died; another live runner keeps its own
    if recovered:
        log(f"requeued {recovered} job(s) left running by a stopped siphon")
    pending = [j for j in store.all() if j["status"] == "pending"]
    if not pending:
        log("queue is empty (nothing pending)")
        return 0

    total = len(pending)
    log(f"running {total} job(s), {concurrency} at a time")
    done = {"n": 0, "ok": 0}

    lock = threading.Lock()

    def work() -> None:
        while job := store.claim():  # atomic: a job another process claimed is never run twice
            start = time.monotonic()
            _run_one(job, outdir, log)
            store.update(job["id"], status=job["status"], file=job.get("file"), error=job.get("error"),
                         finished=time.strftime("%Y-%m-%dT%H:%M:%S"))
            with lock:
                done["n"] += 1
                done["ok"] += job["status"] == "done"
                mark = "✓" if job["status"] == "done" else "✗"
                detail = job["file"] if job["status"] == "done" else job["error"]
                log(f"[{done['n']}] {mark} {job['url'][:60]}  ({time.monotonic() - start:.0f}s)  {detail or ''}")

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        for f in [ex.submit(work) for _ in range(max(1, concurrency))]:
            f.result()

    log(f"done: {done['ok']}/{done['n']} succeeded")
    return 0 if done["ok"] == done["n"] else 1


def clear(which: str) -> int:
    return db().clear(which)


def remove(ids: list[str]) -> int:
    return db().remove(ids)


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
