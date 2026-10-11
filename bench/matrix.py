"""Run siphon against real links and record what actually happened.

    uv run python bench/harvest.py > bench/links.tsv
    uv run python bench/matrix.py bench/links.tsv [--per-platform 1] [--height 480]

Each link: full siphon run (capped height to spare bandwidth), ffprobe on the output,
media deleted right after probing. Writes bench/results-<date>.md and .jsonl.
"A file appeared" is not a pass; a pass is a file ffprobe reads with a video stream.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent


def ffprobe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,height:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True)
    try:
        d = json.loads(out.stdout)
    except ValueError:
        return {}
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"video": v.get("codec_name"), "height": v.get("height"),
            "audio": next((s.get("codec_name") for s in d.get("streams", []) if s.get("codec_type") == "audio"), None),
            "duration": round(float(d.get("format", {}).get("duration") or 0), 1)}


def run(platform: str, url: str, height: int, timeout: int) -> dict:
    out = Path(tempfile.mkdtemp(prefix="siphon-matrix-"))
    fmt = f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"
    t0 = time.time()
    try:
        p = subprocess.run(["uv", "run", "siphon", url, "-o", str(out), "-f", fmt, "--no-profile", "--timeout", "30", "--live-duration", "20"],
                           capture_output=True, text=True, timeout=timeout, cwd=HERE.parent)
        rc, log = p.returncode, (p.stderr + p.stdout)
    except subprocess.TimeoutExpired as e:
        rc, log = -9, f"TIMEOUT after {timeout}s\n" + ((e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))
    secs = round(time.time() - t0, 1)
    files = [f for f in out.iterdir() if f.is_file() and f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3")]
    probe = ffprobe(files[0]) if files else {}
    size = files[0].stat().st_size if files else 0
    shutil.rmtree(out, ignore_errors=True)
    via = re.findall(r"^\s+(direct|ytdlp|sniff)\s+(.*)$", log, re.M)
    errors = [ln.strip() for ln in log.splitlines() if ln.strip().startswith(("✗", "ERROR", "TIMEOUT"))]
    ok = rc == 0 and bool(probe.get("video"))
    blocked = (not ok) and bool(re.search(r"blocked by (the platform|your ISP)|doesn't answer from this network", log))
    return {"platform": platform, "url": url, "ok": ok, "blocked": blocked, "rc": rc, "secs": secs, "bytes": size, **probe,
            "path": " → ".join(f"{n}: {o[:60]}" for n, o in via), "error": (errors[-1] if errors else "")[:300]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("links")
    ap.add_argument("--per-platform", type=int, default=1)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()

    by = defaultdict(list)
    for ln in Path(a.links).read_text().splitlines():
        if ln.strip() and not ln.startswith("#"):
            plat, url = ln.split("\t", 1)
            by[plat].append(url.strip())

    stamp = time.strftime("%Y-%m-%d")
    jl = HERE / f"results-{stamp}.jsonl"
    results = []
    for plat, urls in by.items():
        for url in urls[: a.per_platform]:
            r = run(plat, url, a.height, a.timeout)
            results.append(r)
            with open(jl, "a") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            mark = "PASS" if r["ok"] else ("BLOCKED" if r["blocked"] else "FAIL")
            print(f"{mark} {plat:<15} {r['secs']:>6}s  {r.get('height') or '-'}p {r.get('duration') or '-'}s  "
                  f"{r['error'][:90]}", flush=True)

    passed = sum(r["ok"] for r in results)
    blocked = sum(r["blocked"] for r in results)
    md = [f"# siphon platform matrix — {stamp}", "", f"**{passed}/{len(results) - blocked} passed**, "
          f"{blocked} blocked on this network (pass = exit 0 and ffprobe finds a video stream; "
          f"height capped at {a.height}p).", "",
          "| platform | result | time | video | path | error |", "|---|---|---|---|---|---|"]
    for r in results:
        vid = f"{r.get('video') or '-'} {r.get('height') or ''}p {r.get('duration') or ''}s" if r["ok"] else "-"
        md.append(f"| {r['platform']} | {'PASS' if r['ok'] else ('BLOCKED' if r['blocked'] else 'FAIL')} | {r['secs']}s | {vid} | "
                  f"{r['path'].replace('|', '/')} | {r['error'].replace('|', '/')[:120]} |")
    (HERE / f"results-{stamp}.md").write_text("\n".join(md) + "\n")
    print(f"\n{passed}/{len(results)} passed -> bench/results-{stamp}.md")
    return 0 if passed + blocked == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
