"""IDM-style multi-connection downloader for progressive files.

The trick that made IDM fast was never "open 8 connections". It was *dynamic
segmentation*: start with N equal ranges, and whenever a connection finishes early,
split the largest range still in flight in half and give the back half to the idle
connection. A slow connection never becomes the long tail.

Adaptive parallelism: more connections only help when each one is capped (a
throttling CDN, a file host, a long fat pipe). On a link one connection already
saturates, extra ones just add handshakes and contention — measured: 6.3s with 1
connection vs 28s with 16 on a 40 Mbit line, and aria2c degrades the same way.
So we start with 1 worker and add one at a time only while aggregate throughput
keeps climbing (>=15%), the way TCP slow start probes for capacity — skipping one
window after each spawn so a new connection's own ramp-up isn't mistaken for gain.
Work-stealing makes growth cheap: a new worker simply steals half of someone's range.
But every steal makes the victim close its connection early, and the next request
pays a fresh TLS handshake — so a tail is only stolen if it's worth >=3s of transfer.

Resume: segment progress is checkpointed to `<file>.part.json`. On restart, if the
server's size + ETag still match, every segment continues from its saved offset.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

ProgressFn = Callable[[int, int | None], None]

_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")
_HOP_HEADERS = {"range", "accept-encoding", "host", "content-length", "connection", "if-range"}


class RangeUnsupported(Exception):
    pass


@dataclass
class Segment:
    start: int
    end: int  # inclusive; may shrink while in flight when another worker steals the tail
    pos: int
    active: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.end - self.pos + 1)


@dataclass
class Probe:
    url: str  # post-redirect URL; signed CDN URLs must be hit directly
    size: int | None
    ranges: bool
    validator: str  # ETag or Last-Modified, to detect the file changing under a resume


def _clean(headers: dict[str, str]) -> dict[str, str]:
    h = {k: v for k, v in headers.items() if k.lower() not in _HOP_HEADERS and not k.startswith(":")}
    h["Accept-Encoding"] = "identity"  # byte offsets must be offsets into the real file
    return h


class _Writer:
    """Positional writes from many coroutines into one preallocated file."""

    def __init__(self, path: Path):
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        self._lock = threading.Lock()

    def write(self, data: bytes, offset: int) -> None:
        if hasattr(os, "pwrite"):
            os.pwrite(self.fd, data, offset)
        else:  # Windows
            with self._lock:
                os.lseek(self.fd, offset, os.SEEK_SET)
                os.write(self.fd, data)

    def close(self) -> None:
        os.close(self.fd)


class SegmentedDownloader:
    def __init__(
        self,
        connections: int = 8,
        min_split: int = 2 << 20,
        retries: int = 6,
        adaptive: bool = True,
        initial: int = 1,
        window: float = 1.5,
        timeout: float = 30.0,
        proxy: str | None = None,
        progress: ProgressFn | None = None,
    ):
        self.connections = max(1, connections)
        self.min_split = min_split
        self.adaptive = adaptive
        self.initial = max(1, initial)
        self.window = window
        self.peak_workers = 0
        self._steal_floor = 2 * min_split  # raised at runtime to ~3s of per-connection transfer
        self.retries = retries
        self.timeout = timeout
        self.proxy = proxy
        self.progress = progress or (lambda done, total: None)

    # ---------------------------------------------------------------- public

    def download(self, url: str, dest: Path, headers: dict[str, str] | None = None) -> Path:
        return asyncio.run(self.adownload(url, dest, headers or {}))

    async def adownload(self, url: str, dest: Path, headers: dict[str, str]) -> Path:
        headers = _clean(headers)
        dest.parent.mkdir(parents=True, exist_ok=True)
        limits = httpx.Limits(max_connections=self.connections + 2, max_keepalive_connections=self.connections + 2)
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=self.timeout, limits=limits, proxy=self.proxy, headers=headers
        ) as client:
            probe = await self._probe(client, url)
            part = dest.with_name(dest.name + ".part")
            if probe.ranges and probe.size and probe.size > self.min_split and self.connections > 1:
                try:
                    await self._multi(client, probe, part)
                except RangeUnsupported:
                    await self._single(client, probe, part)
            else:
                await self._single(client, probe, part)
        os.replace(part, dest)
        _state_path(part).unlink(missing_ok=True)
        return dest

    # --------------------------------------------------------------- probing

    async def _probe(self, client: httpx.AsyncClient, url: str) -> Probe:
        async with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as r:
            r.raise_for_status()
            final = str(r.url)
            validator = r.headers.get("etag") or r.headers.get("last-modified") or ""
            m = _CONTENT_RANGE.match(r.headers.get("content-range", ""))
            if r.status_code == 206 and m and m.group(3) != "*":
                return Probe(final, int(m.group(3)), True, validator)
            cl = r.headers.get("content-length")
            return Probe(final, int(cl) if cl and cl.isdigit() else None, False, validator)

    # ----------------------------------------------------- multi-connection

    async def _multi(self, client: httpx.AsyncClient, probe: Probe, part: Path) -> None:
        size = probe.size
        assert size is not None
        segs = self._load_state(part, probe)
        if segs is None:
            with open(part, "wb") as f:
                f.truncate(size)  # preallocate; sparse on most filesystems
            step = -(-size // self.connections)
            segs = [Segment(s, min(s + step, size) - 1, s) for s in range(0, size, step)]

        writer = _Writer(part)
        done_bytes = lambda: sum(s.pos - s.start for s in segs)  # noqa: E731
        stop = asyncio.Event()

        async def checkpoint() -> None:
            while not stop.is_set():
                self._save_state(part, probe, segs)
                self.progress(done_bytes(), size)
                try:
                    await asyncio.wait_for(stop.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass

        def claim() -> Segment | None:
            for s in segs:
                if not s.active and s.remaining > 0:
                    s.active = True
                    return s
            # Nothing idle: steal the back half of the biggest in-flight segment.
            victim = max((s for s in segs if s.active), key=lambda s: s.remaining, default=None)
            if victim is None or victim.remaining < self._steal_floor:
                return None
            mid = victim.pos + victim.remaining // 2
            new = Segment(mid, victim.end, mid, active=True)
            victim.end = mid - 1
            segs.append(new)
            return new

        async def worker() -> None:
            while (seg := claim()) is not None:
                try:
                    await self._fetch_segment(client, probe.url, seg, writer)
                finally:
                    seg.active = False

        ticker = asyncio.create_task(checkpoint())
        workers: list[asyncio.Task] = []

        def spawn() -> None:
            workers.append(asyncio.create_task(worker()))
            self.peak_workers = max(self.peak_workers, sum(not t.done() for t in workers))

        try:
            for _ in range(min(self.initial if self.adaptive else self.connections, self.connections)):
                spawn()
            growing, baseline, settle, last_bytes = self.adaptive, None, 1, done_bytes()
            while not all(t.done() for t in workers):
                await asyncio.wait(workers, timeout=self.window, return_when=asyncio.FIRST_EXCEPTION)
                if any(t.done() and not t.cancelled() and t.exception() for t in workers):
                    break
                now = done_bytes()
                rate, last_bytes = (now - last_bytes) / self.window, now
                live = max(1, sum(not t.done() for t in workers))
                self._steal_floor = max(2 * self.min_split, int(3 * rate / live))
                if settle:
                    settle -= 1  # a fresh connection is still in TCP slow start; don't judge yet
                    continue
                left = sum(s.remaining for s in segs)
                if growing and live < self.connections and left > 4 * self._steal_floor:
                    if baseline is None or rate > baseline * 1.15:
                        baseline = rate
                        spawn()  # the last connection paid for itself; probe one more
                        settle = 1
                    else:
                        growing = False  # plateau: the link, not the connection, is the limit
            await asyncio.gather(*workers)
        except BaseException:
            for t in workers:
                t.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        finally:
            stop.set()
            await ticker
            writer.close()
            self._save_state(part, probe, segs)

        missing = sum(s.remaining for s in segs)
        if missing:
            raise IOError(f"{missing} bytes still missing after all retries")
        self.progress(size, size)

    async def _fetch_segment(self, client: httpx.AsyncClient, url: str, seg: Segment, writer: _Writer) -> None:
        attempt = 0
        while seg.remaining > 0:
            try:
                async with client.stream("GET", url, headers={"Range": f"bytes={seg.pos}-{seg.end}"}) as r:
                    if r.status_code == 200:
                        raise RangeUnsupported()  # server ignored Range; offsets would be wrong
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes(1 << 16):
                        take = min(len(chunk), seg.remaining)  # end may have been stolen mid-stream
                        if take <= 0:
                            break
                        writer.write(chunk[:take], seg.pos)
                        seg.pos += take
                        attempt = 0
                        if seg.remaining == 0:
                            break
            except RangeUnsupported:
                raise
            except (httpx.HTTPError, OSError) as e:
                attempt += 1
                status = getattr(getattr(e, "response", None), "status_code", None)
                if attempt > self.retries or status in (401, 403, 404, 410):
                    raise
                await asyncio.sleep(min(30, 0.5 * 2**attempt))

    # ------------------------------------------------------ single stream

    async def _single(self, client: httpx.AsyncClient, probe: Probe, part: Path) -> None:
        have = part.stat().st_size if part.exists() and probe.ranges else 0
        hdrs = {"Range": f"bytes={have}-"} if have else {}
        attempt = 0
        while True:
            try:
                async with client.stream("GET", probe.url, headers=hdrs) as r:
                    r.raise_for_status()
                    if have and r.status_code != 206:
                        have = 0
                    with open(part, "ab" if have else "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 16):
                            f.write(chunk)
                            have += len(chunk)
                            self.progress(have, probe.size)
                break
            except httpx.HTTPError:
                attempt += 1
                if attempt > self.retries or not probe.ranges:
                    raise
                hdrs = {"Range": f"bytes={have}-"}
                await asyncio.sleep(min(30, 0.5 * 2**attempt))
        if probe.size and have != probe.size:
            raise IOError(f"got {have} bytes, expected {probe.size}")

    # ------------------------------------------------------------- state

    def _save_state(self, part: Path, probe: Probe, segs: list[Segment]) -> None:
        tmp = _state_path(part).with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "url": probe.url, "size": probe.size, "validator": probe.validator, "saved": time.time(),
            "segments": [[s.start, s.end, s.pos] for s in segs],
        }))
        os.replace(tmp, _state_path(part))

    def _load_state(self, part: Path, probe: Probe) -> list[Segment] | None:
        sp = _state_path(part)
        if not (sp.exists() and part.exists()):
            return None
        try:
            st = json.loads(sp.read_text())
        except (ValueError, OSError):
            return None
        if st.get("size") != probe.size or st.get("validator") != probe.validator:
            return None  # file changed on the server; partial bytes are garbage
        return [Segment(a, b, p) for a, b, p in st["segments"]]


def _state_path(part: Path) -> Path:
    return part.with_name(part.name + ".json")
