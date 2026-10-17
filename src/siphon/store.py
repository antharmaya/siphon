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

A job is a row: indexed columns for what the queue orders by and the app filters on (status,
owner, priority, category, added, finished) plus a JSON blob for everything else. Promoting a
field to a column is a schema change: add a step to MIGRATIONS and bump nothing else.

Every stored change also appends a row to `events`, in the same transaction. Its seq is the
event id clients resume from (docs/api-v1.md), so ids survive a restart and never repeat.
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

# Each step takes the database from version i to i+1 (PRAGMA user_version). Never edit a shipped
# step; append a new one.
MIGRATIONS = [
    # 1: the first store
    """
    CREATE TABLE IF NOT EXISTS jobs (
        seq    INTEGER PRIMARY KEY AUTOINCREMENT,
        id     TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        owner  TEXT,
        data   TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, seq);
    """,
    # 2: columns the app sorts and filters on, and the event log
    """
    ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE jobs ADD COLUMN category TEXT;
    ALTER TABLE jobs ADD COLUMN added TEXT;
    ALTER TABLE jobs ADD COLUMN finished TEXT;
    UPDATE jobs SET priority = COALESCE(json_extract(data, '$.priority'), 0),
                    category = json_extract(data, '$.category'),
                    added    = json_extract(data, '$.added'),
                    finished = json_extract(data, '$.finished');
    UPDATE jobs SET data = json_remove(data, '$.priority', '$.category', '$.added', '$.finished');
    DROP INDEX IF EXISTS jobs_status;
    CREATE INDEX jobs_queue ON jobs(status, priority DESC, seq);
    CREATE INDEX jobs_category ON jobs(category);
    CREATE TABLE events (
        seq    INTEGER PRIMARY KEY AUTOINCREMENT,
        kind   TEXT NOT NULL,
        job_id TEXT,
        at     TEXT NOT NULL,
        data   TEXT
    );
    """,
]
COLUMNS = ("id", "status", "owner", "priority", "category", "added", "finished")
SELECT = "SELECT " + ", ".join(COLUMNS) + ", data FROM jobs"
KEEP_EVENTS = 10_000  # older events are dropped; a client that far behind gets a fresh snapshot
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
            self._migrate(c)
            conns[key] = c
        if not self._ready:
            with _init_lock:
                if not self._ready:
                    self._import_legacy(c)
                    self._ready = True
        return c

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        c.execute("BEGIN IMMEDIATE")  # two processes opening a new database migrate it once
        try:
            v = c.execute("PRAGMA user_version").fetchone()[0]
            for i in range(v, len(MIGRATIONS)):
                for stmt in filter(str.strip, MIGRATIONS[i].split(";")):
                    c.execute(stmt)
                c.execute(f"PRAGMA user_version={i + 1}")
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

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
        job = json.loads(r[len(COLUMNS)])
        for k, v in zip(COLUMNS, r):
            if k == "owner" and not v:
                job.pop("owner", None)
            else:
                job[k] = v
        return job

    @staticmethod
    def _values(job: dict) -> tuple:
        """Column values then the JSON blob, in COLUMNS order."""
        rest = {k: v for k, v in job.items() if k not in COLUMNS}
        return (*(job.get(k, 0 if k == "priority" else None) for k in COLUMNS), json.dumps(rest))

    def _insert(self, c, job: dict) -> None:
        c.execute(f"INSERT OR IGNORE INTO jobs({', '.join(COLUMNS)}, data) VALUES ({', '.join('?' * (len(COLUMNS) + 1))})",
                  self._values(job))

    def _write(self, c, job: dict) -> None:
        sets = ", ".join(f"{k}=?" for k in COLUMNS[1:])
        c.execute(f"UPDATE jobs SET {sets}, data=? WHERE id=?", self._values(job)[1:] + (job["id"],))

    @staticmethod
    def _event(c, kind: str, job: dict | None = None, job_id: str | None = None) -> None:
        c.execute("INSERT INTO events(kind, job_id, at, data) VALUES (?,?,?,?)",
                  (kind, job_id or (job or {}).get("id"), time.strftime("%Y-%m-%dT%H:%M:%S"),
                   json.dumps(job) if job is not None else None))

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
                    self._insert(c, j)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        self.legacy.replace(self.legacy.with_name(self.legacy.name + ".migrated"))

    # -------------------------------------------------------------- reads
    def all(self) -> list[dict]:
        return [self._row(r) for r in self._conn().execute(SELECT + " ORDER BY seq")]

    def get(self, job_id: str) -> dict | None:
        r = self._conn().execute(SELECT + " WHERE id=?", (job_id,)).fetchone()
        return self._row(r) if r else None

    def last_event(self) -> int:
        return self._conn().execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]

    def events_since(self, seq: int) -> list[dict] | None:
        """Stored events after `seq`, oldest first; None if `seq` is too old to replay (resync)."""
        c = self._conn()
        oldest, last = c.execute("SELECT MIN(seq), COALESCE(MAX(seq), 0) FROM events").fetchone()
        if seq > last or (seq < last and (oldest is None or seq < oldest - 1)):
            return None  # a different database (seq from the future) or pruned past it
        return [{"seq": r[0], "kind": r[1], "job_id": r[2], "at": r[3], "data": json.loads(r[4]) if r[4] else None}
                for r in c.execute("SELECT seq, kind, job_id, at, data FROM events WHERE seq > ? ORDER BY seq", (seq,))]

    # -------------------------------------------------------------- writes
    def add(self, urls: list[str], args: list[str], **fields) -> list[dict]:
        new = []
        with self._tx() as c:
            for url in urls:
                job = {"id": uuid.uuid4().hex[:8], "url": url, "args": list(args), "status": "pending",
                       "added": time.strftime("%Y-%m-%dT%H:%M:%S"), "file": None, "error": None,
                       "priority": 0, "category": None, **fields}
                self._insert(c, job)
                self._event(c, "job", job)
                new.append(job)
            self._prune(c)
        return new

    def update(self, job_id: str, **fields) -> dict | None:
        with self._tx() as c:
            r = c.execute(SELECT + " WHERE id=?", (job_id,)).fetchone()
            if not r:
                return None
            job = self._row(r)
            job.update(fields)
            if job.get("status") != "active":
                job.pop("owner", None)  # only a running job has an owner
            self._write(c, job)
            self._event(c, "job", job)
        return job

    def replace_all(self, jobs: list[dict]) -> None:
        with self._tx() as c:
            for (jid,) in c.execute("SELECT id FROM jobs").fetchall():
                self._event(c, "removed", job_id=jid)
            c.execute("DELETE FROM jobs")
            for j in jobs:
                self._insert(c, j)
                self._event(c, "job", j)

    def claim(self, owner: str | None = None) -> dict | None:
        """Atomically move the next pending job (highest priority, then oldest) to active, owned by this process."""
        owner = owner or me()
        with self._tx() as c:
            r = c.execute(SELECT + " WHERE status='pending' ORDER BY priority DESC, seq LIMIT 1").fetchone()
            if not r:
                return None
            job = self._row(r)
            job.update(status="active", owner=owner, started=time.strftime("%Y-%m-%dT%H:%M:%S"))
            self._write(c, job)
            self._event(c, "job", job)
        return job

    def recover(self) -> int:
        """Put back in the queue the running jobs whose owning process has died. Returns how many."""
        n = 0
        with self._tx() as c:
            for r in c.execute(SELECT + " WHERE status='active'").fetchall():
                if not alive(r[COLUMNS.index("owner")]):
                    job = self._row(r)
                    job.update(status="pending", stage="queued")
                    job.pop("owner", None)
                    self._write(c, job)
                    self._event(c, "job", job)
                    n += 1
        return n

    def remove(self, ids: list[str]) -> int:
        with self._tx() as c:
            gone = 0
            for i in ids:
                for (jid,) in c.execute("SELECT id FROM jobs WHERE id=? OR id LIKE ?", (i, i.replace("%", "") + "%")).fetchall():
                    c.execute("DELETE FROM jobs WHERE id=?", (jid,))
                    self._event(c, "removed", job_id=jid)
                    gone += 1
        return gone

    def clear(self, which: str) -> int:
        where = {"all": "1=1", "done": "status='done'", "finished": "status NOT IN ('pending','active','paused')"}[which]
        with self._tx() as c:
            ids = [r[0] for r in c.execute(f"SELECT id FROM jobs WHERE {where}").fetchall()]
            for jid in ids:
                c.execute("DELETE FROM jobs WHERE id=?", (jid,))
                self._event(c, "removed", job_id=jid)
        return len(ids)

    @staticmethod
    def _prune(c) -> None:
        c.execute("DELETE FROM events WHERE seq <= (SELECT MAX(seq) FROM events) - ?", (KEEP_EVENTS,))
