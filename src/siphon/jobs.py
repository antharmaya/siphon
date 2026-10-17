"""Run download jobs in the background and report live progress: the engine behind `siphon serve`.

Jobs live in the same database as `siphon queue`, so both see the same list. Each job runs as
its own `siphon --progress-json` subprocess (a stuck job can't touch the others) with a log file
under $SIPHON_STATE/logs/. Remote callers describe *intent* — URL, preset, destination — and
`build_args` turns that into flags from a fixed allowlist; they never pass raw CLI arguments,
which could point `-o` anywhere or read local files with `--cookies`/`-a`.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import paths, queue

PRESETS = ("best", "compat", "small", "archive")
SPONSOR_CATS = {"sponsor", "intro", "outro", "selfpromo", "preview", "filler", "interaction", "music_offtopic"}
FORMAT_RE = re.compile(r"[A-Za-z0-9_\[\]<>=!*+/,.:?|^$~-]{1,200}")
SECTION_RE = re.compile(r"[0-9:.]*-[0-9:.]*")
LOGS = paths.STATE / "logs"


class BadRequest(ValueError):
    pass


def validate_url(url: str, allow_loopback: bool = False) -> str:
    """http(s) only, and not this machine or its link-local neighbours (cloud metadata lives at
    169.254.169.254). A LAN NAS is fine. `allow_loopback` exists for tests and local dev."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BadRequest("only http(s) links can be downloaded")
    if allow_loopback:
        return url
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError):
        return url  # can't resolve here; siphon's own block diagnosis will explain it
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise BadRequest(f"{parts.hostname} points at this machine or its local link ({ip}); refused")
    return url


def build_args(intent: dict) -> list[str]:
    """Intent → CLI flags, from a fixed allowlist. Anything unrecognised is an error, not ignored."""
    known = {"url", "preset", "audio", "format", "section", "sponsorblock", "playlist", "dest"}
    if extra := set(intent) - known:
        raise BadRequest(f"unknown option(s): {', '.join(sorted(extra))}")
    args: list[str] = []
    preset = intent.get("preset") or "best"
    if preset not in PRESETS:
        raise BadRequest(f"preset must be one of {', '.join(PRESETS)}")
    if preset != "best":
        args += ["-P", preset]
    if intent.get("audio"):
        args.append("-x")
    if fmt := intent.get("format"):
        if not isinstance(fmt, str) or fmt.startswith("-") or not FORMAT_RE.fullmatch(fmt):
            raise BadRequest("format must be a yt-dlp format selector, e.g. bv*[height<=1080]+ba/b")
        args += ["-f", fmt]
    if sec := intent.get("section"):
        if not isinstance(sec, str) or not SECTION_RE.fullmatch(sec):
            raise BadRequest("section must look like 1:30-2:45")
        args += ["--section", sec]
    if sb := intent.get("sponsorblock"):
        cats = ["sponsor"] if sb is True else sb
        if not isinstance(cats, list) or not cats or not set(cats) <= SPONSOR_CATS:
            raise BadRequest(f"sponsorblock categories: {', '.join(sorted(SPONSOR_CATS))}")
        args += ["--sponsorblock", ",".join(cats)]
    if intent.get("playlist") is False:
        args.append("--no-playlist")
    return args


def parse_dest(dest: str | None) -> str:
    dest = dest or "box"
    if dest in ("box", "device", "cloud") or (dest.startswith("cloud:") and re.fullmatch(r"cloud:[\w.-]+(:[\w./ -]*)?", dest)):
        if dest.startswith("cloud"):
            from .cloud import CloudError, split_target
            try:
                split_target(dest)
            except CloudError as e:
                raise BadRequest(str(e)) from e
        return dest
    raise BadRequest("dest must be box, device, cloud or cloud:REMOTE[:folder]")


def organise(priority=None, category=None, tags=None) -> dict:
    """The user's own labels on a job: validated, never passed to siphon."""
    out: dict = {}
    if priority is not None:
        if isinstance(priority, bool) or not isinstance(priority, int) or not -100 <= priority <= 100:
            raise BadRequest("priority must be a whole number from -100 to 100")
        out["priority"] = priority
    if category is not None:
        if not isinstance(category, str) or not 0 < len(category.strip()) <= 64:
            raise BadRequest("category must be text, 1-64 characters")
        out["category"] = category.strip()
    if tags is not None:
        if not isinstance(tags, list) or len(tags) > 32 or not all(isinstance(t, str) and 0 < len(t.strip()) <= 64 for t in tags):
            raise BadRequest("tags must be a list of up to 32 short texts")
        out["tags"] = sorted({t.strip() for t in tags})
    return out


class Runner:
    """A small pool of workers that pulls pending jobs from the queue and runs them."""

    def __init__(self, outdir: Path, concurrency: int = 2, on_update: Callable[[dict], None] = lambda j: None,
                 siphon_cmd: list[str] | None = None, extra_args: list[str] | None = None):
        self.outdir = outdir
        self.n = max(1, concurrency)
        self.on_update = on_update
        self.cmd = siphon_cmd or queue._siphon_cmd()
        self.extra = extra_args or []
        self.live: dict[str, dict] = {}  # job id → {"proc", "stage", "progress"}
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.threads: list[threading.Thread] = []

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        # Requeue only jobs whose runner process died; a `siphon queue run` still working keeps its jobs.
        queue.db().recover()
        for _ in range(self.n):
            t = threading.Thread(target=self._loop, daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()
        for live in list(self.live.values()):
            if p := live.get("proc"):
                p.terminate()

    def submit(self, intent: dict, allow_loopback: bool = False) -> dict:
        intent = dict(intent)
        meta = organise(intent.pop("priority", None), intent.pop("category", None), intent.pop("tags", None))
        url = validate_url(intent.get("url", ""), allow_loopback)
        args = build_args(intent)
        dest = parse_dest(intent.get("dest"))
        job = queue.db().add([url], args, dest=dest, source="serve", files=[], stage="queued", **meta)[0]
        out = self.view(job)
        self.on_update(out)
        self.wake.set()
        return out

    # -------------------------------------------------------------- state
    def update(self, job_id: str, **fields) -> dict:
        j = queue.db().update(job_id, **fields)
        if j is None:
            return {}
        out = self.view(j)
        self.on_update(out)
        return out

    def view(self, job: dict) -> dict:
        """A job as clients see it: stored fields plus live progress."""
        v = dict(job)
        if live := self.live.get(job["id"]):
            v["stage"] = live.get("stage", v.get("stage"))
            v["progress"] = live.get("progress")
        return v

    def jobs(self) -> list[dict]:
        return [self.view(j) for j in reversed(queue.load())]

    def get(self, job_id: str) -> dict | None:
        j = queue.db().get(job_id)
        return self.view(j) if j else None

    def cancel(self, job_id: str) -> bool:
        if live := self.live.get(job_id):
            if p := live.get("proc"):
                p.terminate()
        return queue.remove([job_id]) > 0

    def retry(self, job_id: str) -> dict:
        out = self.update(job_id, status="pending", error=None, stage="queued", files=[], file=None)
        self.wake.set()
        return out

    # -------------------------------------------------------------- work
    def _claim(self) -> dict | None:
        return queue.db().claim()

    def _loop(self) -> None:
        while not self.stopping.is_set():
            job = self._claim()
            if not job:
                self.wake.wait(2)
                self.wake.clear()
                continue
            try:
                self._run(job)
            except Exception as e:  # never let one job kill a worker
                self.update(job["id"], status="error", error=f"{type(e).__name__}: {e}"[:300])
            finally:
                self.live.pop(job["id"], None)

    def _notify(self, job_id: str) -> None:
        if j := self.get(job_id):
            self.on_update(j)

    def _run(self, job: dict) -> None:
        LOGS.mkdir(parents=True, exist_ok=True)
        log_path = LOGS / f"{job['id']}.log"
        to = []
        if (job.get("dest") or "").startswith("cloud"):
            to = ["--to", job["dest"][len("cloud:"):] if job["dest"].startswith("cloud:") else "cloud"]
        cmd = [*self.cmd, "--progress-json", job["url"], *job["args"], *to, *self.extra, "-o", str(self.outdir)]
        live = self.live[job["id"]] = {"stage": "resolving", "progress": None}
        self._notify(job["id"])
        files: list[str] = []
        remote: list[str] = []
        last_error = None
        with open(log_path, "w") as logf:
            logf.write("$ " + " ".join(cmd) + "\n")
            logf.flush()
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=logf, text=True, bufsize=1)
            live["proc"] = proc
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                kind = ev.get("event")
                if kind == "progress":
                    live["stage"] = "uploading" if live.get("stage") == "uploading" else "downloading"
                    live["progress"] = {k: ev.get(k) for k in ("percent", "speed", "done", "total")}
                elif kind == "saved":
                    files.append(ev["file"])
                    live["stage"] = "saved"
                elif kind == "uploading":
                    live["stage"] = "uploading"
                    live["progress"] = None
                elif kind == "uploaded":
                    remote.append(ev["remote"])
                    if not ev.get("kept") and ev.get("file") in files:
                        files.remove(ev["file"])  # it lives in the cloud now
                elif kind == "error":
                    last_error = ev.get("message")
                elif kind == "resolving":
                    live["stage"] = "resolving"
                self._notify(job["id"])
            rc = proc.wait()
        if self.stopping.is_set():
            return
        self.live.pop(job["id"], None)  # the stored record is the truth from here on
        if rc == 0 and (files or remote):
            first = (files or remote)[0]
            self.update(job["id"], status="done", stage="done", files=files, remote=remote,
                        file=first if len(files) + len(remote) == 1 else f"{len(files) + len(remote)} files",
                        finished=time.strftime("%Y-%m-%dT%H:%M:%S"), error=None)
        else:
            self.update(job["id"], status="error", stage="error", files=files, remote=remote,
                        error=last_error or f"siphon exited with code {rc}", log=str(log_path),
                        finished=time.strftime("%Y-%m-%dT%H:%M:%S"))
