"""`siphon setup browser`: registers the native host so the extension can reach this siphon."""

import json
import os
import re
import struct
import subprocess
from pathlib import Path

import pytest

from siphon import browser, paths, setup

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(paths, "STATE", tmp_path / "state")
    monkeypatch.setattr(browser.sys, "platform", "linux")
    return tmp_path


def ask(launcher: Path, msg: dict) -> dict:
    """One native-messaging round trip, framed the way Chrome frames it."""
    data = json.dumps(msg).encode()
    r = subprocess.run([str(launcher), "chrome-extension://x/"], input=struct.pack("<I", len(data)) + data,
                       capture_output=True, timeout=30, env={**os.environ, "SIPHON_HOST_LOG": os.devnull})
    (n,) = struct.unpack("<I", r.stdout[:4])
    return json.loads(r.stdout[4:4 + n])


def test_registers_with_each_browser_present_and_the_host_answers(home):
    (home / ".config/google-chrome").mkdir(parents=True)
    (home / ".config/BraveSoftware/Brave-Browser").mkdir(parents=True)
    assert browser.main([]) == 0
    files = sorted(home.glob(".config/**/com.antharmaya.siphon.json"))
    assert [f.parent.parent.name for f in files] == ["Brave-Browser", "google-chrome"]
    m = json.loads(files[0].read_text())
    assert m["name"] == "com.antharmaya.siphon" and m["type"] == "stdio"
    assert set(m["allowed_origins"]) == {f"chrome-extension://{i}/" for i in browser.EXTENSION_IDS.values()}
    launcher = Path(m["path"])
    assert launcher.is_absolute() and os.access(launcher, os.X_OK)
    assert "SIPHON_BIN=" in launcher.read_text()  # the host must not depend on the browser's PATH
    assert ask(launcher, {"action": "hello"})["event"] == "hello"
    assert [c for c in setup.capabilities() if c.key == "browser"][0].ok is True


def test_firefox_family_gets_its_own_manifest_keyed_by_addon_id(home):
    for d in (".mozilla", ".zen", ".config/chromium"):
        (home / d).mkdir(parents=True)
    assert browser.main([]) == 0
    for d in (".mozilla", ".zen"):
        m = json.loads((home / d / "native-messaging-hosts/com.antharmaya.siphon.json").read_text())
        assert m["allowed_extensions"] == ["siphon@antharmaya.com"] and "allowed_origins" not in m
        assert ask(Path(m["path"]), {"action": "hello"})["event"] == "hello"
    chromium = json.loads((home / ".config/chromium/NativeMessagingHosts/com.antharmaya.siphon.json").read_text())
    assert "allowed_extensions" not in chromium  # Chrome rejects a manifest it can't parse as its own
    assert browser.main(["--remove"]) == 0 and not list(home.rglob("com.antharmaya.siphon.json"))


def test_a_firefox_that_keeps_its_profile_under_xdg_still_gets_the_legacy_dir(home):
    (home / ".config/mozilla/firefox").mkdir(parents=True)  # Firefox 147+ on a fresh machine: no ~/.mozilla
    assert browser.main([]) == 0
    # A real Firefox 157 only found the manifest in ~/.mozilla (store/src/firefox_check.py --hosts xdg fails)
    assert (home / ".mozilla/native-messaging-hosts/com.antharmaya.siphon.json").is_file()
    assert (home / ".config/mozilla/native-messaging-hosts/com.antharmaya.siphon.json").is_file()


def test_a_sandboxed_browser_is_told_about_the_local_server_instead(home, capsys):
    (home / ".var/app/app.zen_browser.zen").mkdir(parents=True)
    assert browser.main([]) == 1  # nothing could be registered
    out = capsys.readouterr().err
    assert "Zen (Flatpak)" in out and "siphon serve" in out and "127.0.0.1:8765" in out.replace("\n", "")
    assert not list(home.rglob("com.antharmaya.siphon.json"))


def test_says_so_when_no_browser_is_installed_and_writes_nothing(home, capsys):
    assert browser.main([]) == 1
    assert "no Chrome" in capsys.readouterr().err and not (home / "state").exists()
    cap = [c for c in setup.capabilities() if c.key == "browser"][0]
    assert cap.ok is None and cap.fix == "siphon setup browser"


def test_remove_unregisters_everywhere(home):
    (home / ".config/chromium").mkdir(parents=True)
    assert browser.main([]) == 0 and browser.installed()
    assert browser.main(["--remove"]) == 0
    assert not browser.installed() and not (home / "state/browser-host").exists()


def test_setup_routes_browser_to_this_command(home):
    (home / ".config/vivaldi").mkdir(parents=True)
    assert setup.setup_main(["browser"]) == 0 and len(browser.installed()) == 1


def test_extension_ids_match_the_shell_installer():
    sh = (ROOT / "extension/host/install.sh").read_text()
    assert set(re.findall(r'_ID="([a-p]{32})"', sh)) == set(browser.EXTENSION_IDS.values())
    assert f'FIREFOX_ID="{browser.FIREFOX_ID}"' in sh
    build = (ROOT / "scripts/build-extension.py").read_text()
    assert f'FIREFOX_ID = "{browser.FIREFOX_ID}"' in build
