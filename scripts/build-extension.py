#!/usr/bin/env python3
"""Build the siphon browser extension's two editions from extension/, deterministically.

    python3 scripts/build-extension.py            # all three zips into dist/
    python3 scripts/build-extension.py --unpacked # also leave unpacked copies in dist/ to load/test

Editions (one source tree, one build flag; never a fork):

  github  full behaviour. The manifest is the source manifest unchanged, `key` included, so the
          extension ID stays pbeimeldfnbenghllloohnnofbfaicfe and host/install.sh keeps matching.
          Ships host/ so "run ./host/install.sh" from the unzipped folder works.

  store   for the Chrome Web Store (and Edge Add-ons). Derived manifest:
          - no `key`: the store assigns its own ID and rejects or ignores a packaged key
          - unused permissions dropped (STORE_DROP_PERMISSIONS, see store/PERMISSIONS.md)
          - the on-video button kept off YouTube (content_scripts.exclude_matches)
          - policy.js rewritten so background.js refuses YouTube downloads with a message
          - store name/summary (the store shows the manifest's name and description)
          - the padded 128 px store icon (store/icon-128.png) when it exists
          No host/ (it can't run from inside a store install; `siphon setup browser` installs it).

  firefox for addons.mozilla.org, and for Zen, LibreWolf and other Firefox-based browsers. Full
          behaviour (Mozilla's add-on policies have no YouTube or download rule). Derived manifest:
          - no `key` or `minimum_chrome_version` (Chrome-only)
          - background as an event page (`scripts`), not a service worker; policy.js listed first
          - a fixed add-on ID, a minimum Firefox, and the data-collection declaration AMO requires
          - unused permissions dropped, as in the store build
          - policy.js rewritten only to change the help text

Deterministic: fixed file order, fixed timestamps (SOURCE_DATE_EPOCH, else 2026-01-01), fixed
permissions, fixed compression level. Two runs over the same tree give byte-identical zips.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "extension"
DIST = ROOT / "dist"
STORE_ICON = ROOT / "store" / "icon-128.png"

# What ships. Anything not listed here (README, .ext-key.txt, .ext-id.txt, __pycache__, editor
# droppings) never reaches a zip, so a stray dev file can't leak into a release.
RUNTIME_FILES = ["manifest.json", "background.js", "policy.js", "content.js", "content.css",
                 "popup.html", "popup.js"]
RUNTIME_GLOBS = ["icons/*.png", "states/*.svg"]
HOST_FILES = ["host/install.sh", "host/uninstall.sh", "host/siphon_host.py"]

# YouTube and its media/embedding hosts. Suffix match: "youtube.com" covers music./m./www.
STORE_BLOCKED_HOSTS = ["youtube.com", "youtu.be", "youtube-nocookie.com", "youtubekids.com",
                       "googlevideo.com"]
STORE_EXCLUDE_MATCHES = ["*://*.youtube.com/*", "*://*.youtu.be/*", "*://*.youtube-nocookie.com/*",
                         "*://*.youtubekids.com/*"]
# Neutral on purpose: a store build that points users to a build without the restriction reads as
# steering around the policy.
STORE_BLOCKED_MESSAGE = "siphon doesn't download from YouTube in this edition."

# Permissions the code never needs (see store/PERMISSIONS.md for the evidence):
#   scripting  no chrome.scripting call anywhere
#   tabs       only chrome.tabs.query/sendMessage/onRemoved are used; tab.url is already
#              readable through the <all_urls> host permission
STORE_DROP_PERMISSIONS = ["scripting", "tabs"]

# The store package has no host/ folder, so point at the GitHub install instead of host/install.sh.
STORE_HOST_HELP = ("the siphon app isn't installed on this computer: see github.com/antharmaya/siphon "
                   "(The browser button), then reload this extension")
POPUP_HOST_HINT_SRC = ("The siphon app isn't connected. In a terminal run:<br><code>siphon setup browser</code>"
                       '<br>then reload this extension.')
POPUP_HOST_HINT_STORE = ('The siphon app isn\'t installed on this computer, so streams can\'t be saved yet '
                         '(plain files and images still can). Install it from <b>github.com/antharmaya/siphon</b> '
                         '(see “The browser button”), then reload this extension. Or send downloads to your siphon box below.')

# Firefox: the same command, plus the one case it can't fix. A Flatpak or Snap browser is sandboxed
# and can't start a program on the computer, so the extension reaches siphon over localhost instead.
FIREFOX_HOST_HELP = ("the siphon app isn't connected: run `siphon setup browser`, then reload this extension. "
                     "Flatpak or Snap browser? Use \"my siphon box\" with `siphon serve` on this computer")
POPUP_HOST_HINT_FIREFOX = ("The siphon app isn't connected, so streams can't be saved yet (plain files and images still can). "
                           'In a terminal run:<br><code>siphon setup browser</code><br>then reload this extension.<br>'
                           '<b>Flatpak or Snap browser?</b> It can\'t start apps on your computer. Run <code>siphon serve</code> '
                           'and choose “my siphon box” below.')
FIREFOX_ID = "siphon@antharmaya.com"  # also in src/siphon/browser.py and extension/host/install.sh
FIREFOX_MIN = "140.0"                 # the built-in data-consent prompt ships in 140
# What leaves the browser (to the local siphon app, or to the server the user names): page and stream
# URLs, media links read from the page, and the user's own box token. Matches the Chrome listing.
FIREFOX_DATA = ["authenticationInfo", "browsingActivity", "websiteContent"]

STORE_NAME = "siphon: download button for web video"
STORE_DESCRIPTION = ("A download button on web videos and X photos. Streams go to the free siphon app on your "
                     "computer, or to your own siphon server.")
FIREFOX_NAME = "siphon: download button for web video"
FIREFOX_DESCRIPTION = STORE_DESCRIPTION

EPOCH = int(os.environ.get("SOURCE_DATE_EPOCH", "1767225600"))  # 2026-01-01T00:00:00Z


def fail(msg: str) -> None:
    sys.exit(f"build-extension: {msg}")


def collect(edition: str) -> list[str]:
    files = list(RUNTIME_FILES)
    for g in RUNTIME_GLOBS:
        files += sorted(str(p.relative_to(SRC)) for p in SRC.glob(g))
    if edition == "github":
        files += HOST_FILES
    for f in files:
        if not (SRC / f).is_file():
            fail(f"missing {SRC / f}")
    return sorted(set(files))


def store_manifest(m: dict) -> dict:
    m = json.loads(json.dumps(m))  # deep copy
    m.pop("key", None)
    m["name"] = STORE_NAME
    m["description"] = STORE_DESCRIPTION
    m["permissions"] = [p for p in m["permissions"] if p not in STORE_DROP_PERMISSIONS]
    for cs in m.get("content_scripts", []):
        cs["exclude_matches"] = sorted(set(cs.get("exclude_matches", []) + STORE_EXCLUDE_MATCHES))
    return m


def firefox_manifest(m: dict) -> dict:
    m = json.loads(json.dumps(m))
    m.pop("key", None)
    m.pop("minimum_chrome_version", None)
    m["name"] = FIREFOX_NAME
    m["description"] = FIREFOX_DESCRIPTION
    m["permissions"] = [p for p in m["permissions"] if p not in STORE_DROP_PERMISSIONS]
    m["background"] = {"scripts": ["policy.js", m["background"]["service_worker"]]}
    m["browser_specific_settings"] = {"gecko": {
        "id": FIREFOX_ID, "strict_min_version": FIREFOX_MIN,
        "data_collection_permissions": {"required": FIREFOX_DATA}}}
    return m


def firefox_policy() -> str:
    return ("// Generated by scripts/build-extension.py for the Firefox edition. Do not edit.\n"
            "self.SIPHON_POLICY = " + json.dumps({
                "edition": "firefox", "blockedHosts": [], "blockedMessage": "", "hostHelp": FIREFOX_HOST_HELP,
            }, indent=2) + ";\n")


def store_policy() -> str:
    return ("// Generated by scripts/build-extension.py for the Chrome Web Store edition. Do not edit.\n"
            "self.SIPHON_POLICY = " + json.dumps({
                "edition": "store",
                "blockedHosts": STORE_BLOCKED_HOSTS,
                "blockedMessage": STORE_BLOCKED_MESSAGE,
                "hostHelp": STORE_HOST_HELP,
            }, indent=2) + ";\n")


def check_manifest(m: dict, files: list[str], edition: str) -> None:
    if len(m["name"]) > 75:
        fail(f"name is {len(m['name'])} chars; the store allows 75")
    if len(m["description"]) > 132:
        fail(f"description is {len(m['description'])} chars; the store allows 132")
    refs = [*m["background"].get("scripts", []), m["action"]["default_popup"],
            *m["icons"].values(), *m["action"]["default_icon"].values()]
    if "service_worker" in m["background"]:
        refs.append(m["background"]["service_worker"])
    for cs in m["content_scripts"]:
        refs += cs.get("js", []) + cs.get("css", [])
    for r in refs:
        if r not in files:
            fail(f"manifest references {r}, which is not in the {edition} package")
    if edition == "store":
        if "key" in m:
            fail("store manifest still carries `key`")
        if not all(cs.get("exclude_matches") for cs in m["content_scripts"]):
            fail("store content scripts are not excluded from YouTube")
    if edition == "firefox":
        if "key" in m or "service_worker" in m["background"]:
            fail("firefox manifest still carries Chrome-only keys")
        if m["background"]["scripts"][0] != "policy.js":
            fail("firefox background must load policy.js first (there is no importScripts in an event page)")


def js_syntax(path: Path) -> None:
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        if r.returncode:
            fail(f"syntax error in {path}:\n{r.stderr}")


def build(edition: str, version: str, src_manifest: dict, unpacked: bool) -> Path:
    files = collect(edition)
    manifest = {"store": store_manifest, "firefox": firefox_manifest}.get(edition, lambda m: m)(src_manifest)
    check_manifest(manifest, files, edition)

    def content(rel: str) -> bytes:
        if rel == "manifest.json" and edition != "github":
            return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()
        if rel == "policy.js" and edition != "github":
            return (store_policy() if edition == "store" else firefox_policy()).encode()
        if rel == "popup.html" and edition != "github":
            html = (SRC / rel).read_text()
            if POPUP_HOST_HINT_SRC not in html:
                fail("popup.html's host hint changed; update POPUP_HOST_HINT_SRC in this script")
            hint = POPUP_HOST_HINT_STORE if edition == "store" else POPUP_HOST_HINT_FIREFOX
            return html.replace(POPUP_HOST_HINT_SRC, hint).encode()
        if rel == "icons/128.png" and edition == "store" and STORE_ICON.is_file():
            return STORE_ICON.read_bytes()
        return (SRC / rel).read_bytes()

    out = DIST / f"siphon-extension-{version}-{edition}.zip"
    DIST.mkdir(exist_ok=True)
    stamp = time.gmtime(max(EPOCH, 315532800))[:6]  # zip can't go below 1980
    tmp = out.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            info = zipfile.ZipInfo(rel, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # unix, so the mode below is honoured on extraction
            mode = 0o755 if rel.endswith((".sh", ".py")) else 0o644
            info.external_attr = (0o100000 | mode) << 16
            z.writestr(info, content(rel), compresslevel=9)
    tmp.replace(out)

    if unpacked:
        d = DIST / f"extension-{edition}"
        shutil.rmtree(d, ignore_errors=True)
        with zipfile.ZipFile(out) as z:
            z.extractall(d)
        for rel in files:
            if rel.endswith(".js"):
                js_syntax(d / rel)
    else:
        for rel in ("background.js", "content.js", "popup.js", "policy.js"):
            p = DIST / f".check-{edition}-{rel}"
            p.write_bytes(content(rel))
            try:
                js_syntax(p)
            finally:
                p.unlink()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--unpacked", action="store_true", help="also extract each edition to dist/extension-<edition>/")
    ap.add_argument("--edition", choices=["store", "github", "firefox", "all"], default="all")
    a = ap.parse_args()

    src_manifest = json.loads((SRC / "manifest.json").read_text())
    version = src_manifest["version"]
    editions = ["store", "github", "firefox"] if a.edition == "all" else [a.edition]
    for ed in editions:
        out = build(ed, version, src_manifest, a.unpacked)
        digest = hashlib.sha256(out.read_bytes()).hexdigest()
        print(f"{out.relative_to(ROOT)}  {out.stat().st_size:>7} bytes  sha256 {digest[:16]}…")


if __name__ == "__main__":
    main()
