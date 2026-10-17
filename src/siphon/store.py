"""The job store: one SQLite database that every siphon process shares.

`siphon queue run`, `siphon serve` and (soon) the desktop app can all run at once. With the old
queue.json each process held its own in-memory lock, so two processes could overwrite each
other's writes, and a starting runner reset *everyone's* running jobs to pending, so the same
download could start twice. Here:

- every write is one SQLite transaction (WAL mode, so readers never block the writer);
- a worker takes a job with `claim()`, a single `BEGIN IMMEDIATE` transaction, so two processes
  can never claim the same job;
- a running job records its owner (pid + process start time), and `recover()` only puts a job
  back in the queue when that owner is gone, not merely because another process started.

A job is a row: a few indexed columns (id, seq, status, owner) plus a JSON blob for everything
else, so new job fields don't need a schema migration.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    id     TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    owner  TEXT,
    data   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, seq);
"""
COLUMNS = ("id", "status", "owner")
_local = threading.local()
_init_lock = threading.Lock()


def _start_time(pid: int) -> str | None:
    """A process's start time (Linux), so a reused pid isn't mistaken for the original owner."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def me() -> str:
    pid = os.getpid()
    return f"{pid}:{_start_time(pid) or '?'}"


def alive(owner: str | None) -> bool:
    if not owner:
        return False
    pid_s, _, started = owner.partition(":")
    try:
        pid = int(pid_s)
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True  # exists, someone else's
    now = _start_time(pid)
    return started in ("?", "") or now is None or now == started


class Store:
    def __init__(self, path: Path, legacy_json: Path | None = None):
        self.path = Path(path)
        self.legacy = legacy_json
        self._ready = False

    # -------------------------------------------------------------- plumbing
    def _conn(self) -> sqlite3.Connection:
        conns = _local.__dict__.setdefault("conns", {})
        key = str(self.path)
        c = conns.get(key)
        if c is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.executescript(SCHEMA)
            conns[key] = c
        if not self._ready:
            with _init_lock:
                if not self._ready:
                    self._import_legacy(c)
                    self._ready = True
        return c

    @contextmanager
    def _tx(self):
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    @staticmethod
    def _row(r) -> dict:
        job = json.loads(r[3])
        job.update(id=r[0], status=r[1])
        if r[2]:
            job["owner"] = r[2]
        else:
            job.pop("owner", None)
        return job

    @staticmethod
    def _split(job: dict) -> tuple[str, str, str | None, str]:
        rest = {k: v for k, v in job.items() if k not in COLUMNS}
        return job["id"], job["status"], job.get("owner"), json.dumps(rest)

    def _import_legacy(self, c: sqlite3.Connection) -> None:
        """One time: move jobs from queue.json into the database, then set the file aside."""
        if not self.legacy or not self.legacy.exists():
            return
        try:
            jobs = json.loads(self.legacy.read_text())
        except (OSError, ValueError):
            jobs = []
        c.execute("BEGIN IMMEDIATE")
        try:
            for j in jobs if isinstance(jobs, list) else []:
                if isinstance(j, dict) and j.get("id") and j.get("status"):
                    c.execute("INSERT OR IGNORE INTO jobs(id, status, owner, data) VALUES (?,?,?,?)", self._split(j))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        self.legacy.replace(self.legacy.with_name(self.legacy.name + ".migrated"))

    # -------------------------------------------------------------- reads
    def all(self) -> list[dict]:
        return [self._row(r) for r in self._conn().execute("SELECT id, status, owner, data FROM jobs ORDER BY seq")]

    def get(self, job_id: str) -> dict | None:
        r = self._conn().execute("SELECT id, status, owner, data FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(r) if r else None

    # -------------------------------------------------------------- writes
    def add(self, urls: list[str], args: list[str], **fields) -> list[dict]:
        new = []
        with self._tx() as c:
            for url in urls:
                job = {"id": uuid.uuid4().hex[:8], "url": url, "args": list(args), "status": "pending",
                       "added": time.strftime("%Y-%m-%dT%H:%M:%S"), "file": None, "error": None, **fields}
                c.execute("INSERT INTO jobs(id, status, owner, data) VALUES (?,?,?,?)", self._split(job))
                new.append(job)
        return new

    def update(self, job_id: str, **fields) -> dict | None:
        with self._tx() as c:
            r = c.execute("SELECT id, status, owner, data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not r:
                return None
            job = self._row(r)
            job.update(fields)
            if job.get("status") != "active":
                job.pop("owner", None)  # only a running job has an owner
            c.execute("UPDATE jobs SET status=?, owner=?, data=? WHERE id=?", self._split(job)[1:] + (job_id,))
        return job

    def replace_all(self, jobs: list[dict]) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM jobs")
            for j in jobs:
                c.execute("INSERT INTO jobs(id, status, owner, data) VALUES (?,?,?,?)", self._split(j))

    def claim(self, owner: str | None = None) -> dict | None:
        """Atomically move the oldest pending job to active, owned by this process."""
        owner = owner or me()
        with self._tx() as c:
            r = c.execute("SELECT id, status, owner, data FROM jobs WHERE status='pending' ORDER BY seq LIMIT 1").fetchone()
            if not r:
                return None
            job = self._row(r)
            job.update(status="active", owner=owner, started=time.strftime("%Y-%m-%dT%H:%M:%S"))
            c.execute("UPDATE jobs SET status=?, owner=?, data=? WHERE id=?", self._split(job)[1:] + (job["id"],))
        return job

    def recover(self) -> int:
        """Put back in the queue the running jobs whose owning process has died. Returns how many."""
        n = 0
        with self._tx() as c:
            for r in c.execute("SELECT id, status, owner, data FROM jobs WHERE status='active'").fetchall():
                if not alive(r[2]):
                    job = self._row(r)
                    job.update(status="pending", stage="queued")
                    job.pop("owner", None)
                    c.execute("UPDATE jobs SET status=?, owner=?, data=? WHERE id=?", self._split(job)[1:] + (job["id"],))
                    n += 1
        return n

    def remove(self, ids: list[str]) -> int:
        with self._tx() as c:
            gone = 0
            for i in ids:
                gone += c.execute("DELETE FROM jobs WHERE id=? OR id LIKE ?", (i, i.replace("%", "") + "%")).rowcount
        return gone

    def clear(self, which: str) -> int:
        where = {"all": "1=1", "done": "status='done'", "finished": "status NOT IN ('pending','active')"}[which]
        with self._tx() as c:
            return c.execute(f"DELETE FROM jobs WHERE {where}").rowcount
