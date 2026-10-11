"""Where siphon keeps its state — one place, overridable for tests and portable installs.

    $SIPHON_STATE  (default ~/.local/share/siphon)   queue, config, serve token
    $SIPHON_TOOLS  (default $SIPHON_STATE/bin)        binaries siphon fetched for you
                   (not SIPHON_BIN: the extension host uses that for the siphon executable)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

STATE = Path(os.environ.get("SIPHON_STATE", Path.home() / ".local/share/siphon"))
BIN = Path(os.environ.get("SIPHON_TOOLS", STATE / "bin"))
CONFIG = STATE / "config.json"


def use_managed_bin() -> None:
    """Append siphon's bin dir to PATH so yt-dlp, rclone lookups etc. find fetched tools.
    Append, not prepend: a newer system ffmpeg must still win (see tools.py)."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if str(BIN) not in parts:
        os.environ["PATH"] = os.pathsep.join([p for p in parts if p] + [str(BIN)])


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, CONFIG)
