"""The app's connection to the engine: requests off the UI thread, and the live event stream.

The window never blocks on the network. Commands run on a thread pool and report back through
Qt signals; the event stream runs on its own thread and reconnects with Last-Event-ID, so after a
dropped connection or an engine restart the app gets exactly the changes it missed (or a fresh
snapshot), per docs/api-v1.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal

from .. import client, paths


def ensure_engine(timeout: float = 20) -> client.Engine | None:
    """The running engine, starting it if needed: the user service when installed, else a
    background `siphon serve` that outlives the window."""
    if eng := client.find():
        return eng
    from ..engine import UNIT_NAME, unit_path
    if unit_path().exists() and shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "start", UNIT_NAME], capture_output=True)
    else:
        log = paths.STATE / "engine.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as f:
            subprocess.Popen([sys.executable, "-m", "siphon", "serve", "--no-qr"], stdout=f, stderr=f,
                             stdin=subprocess.DEVNULL, start_new_session=True, env=os.environ.copy())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if eng := client.find():
            return eng
        time.sleep(0.3)
    return None


class _Call(QRunnable):
    def __init__(self, link: "Link", method: str, path: str, body, tag: str):
        super().__init__()
        self.link, self.method, self.path, self.body, self.tag = link, method, path, body, tag

    def run(self) -> None:
        try:
            out = self.link.engine.call(self.method, self.path, self.body)
            self.link.done.emit(self.tag, out)
        except client.EngineError as e:
            self.link.failed.emit(self.tag, str(e))


class Link(QObject):
    done = Signal(str, object)    # tag, response
    failed = Signal(str, str)     # tag, message

    def __init__(self, engine: client.Engine):
        super().__init__()
        self.engine = engine
        self.pool = QThreadPool.globalInstance()

    def call(self, method: str, path: str, body=None, tag: str = "") -> None:
        self.pool.start(_Call(self, method, path, body, tag or f"{method} {path}"))


class EventStream(QThread):
    """Reads /api/v1/events forever. Signals carry plain dicts."""
    snapshot = Signal(list)
    job = Signal(dict)
    removed = Signal(str)
    progress = Signal(dict)
    connected = Signal(bool)

    def __init__(self, engine: client.Engine):
        super().__init__()
        self.engine = engine
        self.last_id: int | None = None
        self._stop = False
        self._resp = None

    def stop(self) -> None:
        """Ends the thread promptly: closing the response unblocks the read."""
        self._stop = True
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:  # noqa: BLE001
                pass
        self.wait(3000)

    def run(self) -> None:
        backoff = 0.5
        while not self._stop:
            headers = {"Authorization": f"Bearer {self.engine.token}"}
            if self.last_id is not None:
                headers["Last-Event-ID"] = str(self.last_id)
            req = urllib.request.Request(self.engine.url + "/api/v1/events", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    self._resp = r
                    self.connected.emit(True)
                    backoff = 0.5
                    self._read(r)
            except Exception:  # noqa: BLE001 - any network failure: reconnect
                pass
            self.connected.emit(False)
            if self._stop:
                return
            time.sleep(backoff)
            backoff = min(backoff * 2, 5)

    def _read(self, r) -> None:
        event, data, eid = None, [], None
        for raw in r:
            if self._stop:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith(":"):
                continue
            if line.startswith("id: "):
                eid = int(line[4:])
            elif line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
            elif line == "" and event:
                payload = json.loads("\n".join(data)) if data else None
                if event == "snapshot":
                    self.snapshot.emit(payload)
                elif event == "job":
                    self.job.emit(payload)
                elif event == "removed":
                    self.removed.emit(payload["id"])
                elif event == "progress":
                    self.progress.emit(payload)
                if eid is not None:
                    self.last_id = eid
                event, data, eid = None, [], None
