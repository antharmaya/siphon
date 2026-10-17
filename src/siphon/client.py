"""Find and talk to a running siphon engine (`siphon serve`) on this machine.

The engine records where it listens and which process it is in $SIPHON_STATE/serve.json, next to
its API token (the file is readable by this user only). A client trusts that record only while
the recorded process is alive and answers /api/v1/status, so a crashed engine is never mistaken
for a running one.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import paths
from .store import alive


class EngineError(RuntimeError):
    pass


class Engine:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token
        self.status: dict = {}

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 10) -> dict | list:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + "/api/v1" + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error")
            except ValueError:
                msg = None
            raise EngineError(msg or f"HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise EngineError(f"the siphon engine at {self.url} didn't answer: {e}") from e

    def has(self, feature: str) -> bool:
        return feature in self.status.get("features", [])


def find() -> Engine | None:
    """The engine running on this machine, or None."""
    try:
        d = json.loads((paths.STATE / "serve.json").read_text())
    except (OSError, ValueError):
        return None
    rec, token = d.get("engine") or {}, d.get("token")
    if not (rec.get("url") and token and alive(rec.get("owner"))):
        return None
    e = Engine(rec["url"], token)
    try:
        e.status = e.call("GET", "/status", timeout=2)
    except EngineError:
        return None
    return e
