"""The desktop app against a real engine, rendered offscreen. Skipped without the [app] extra."""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from siphon import client  # noqa: E402
from siphon.app.model import PROGRESS, SIZE, STATUS, JobsFilter, JobsModel, display_name, human_eta  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_for(qapp, cond, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_model_updates_rows_in_place_and_filters(qapp):
    m, f = JobsModel(), JobsFilter()
    f.setSourceModel(m)
    m.reset([{"id": "a1", "url": "https://x.com/a/status/1", "status": "done", "files": ["/d/clip.mp4"], "size": 2048,
              "category": "disc"},
             {"id": "b2", "url": "https://cdn.example.org/big.iso", "status": "active", "stage": "downloading"}])
    assert m.data(m.index(0, 0)) == "clip.mp4" and m.data(m.index(0, SIZE)) == "2.00 KiB"
    changed = []
    m.dataChanged.connect(lambda a, b: changed.append(a.row()))
    m.upsert({"id": "b2", "status": "active", "progress": {"percent": 40.0, "done": 400, "total": 1000, "speed": 100}},
             live_only=True)
    assert changed == [1] and m.data(m.index(1, PROGRESS)) == 40.0 and m.rows[1]["url"]  # url kept on a live tick
    assert m.data(m.index(1, STATUS)) == "Downloading"
    f.set(status="done")
    assert f.rowCount() == 1
    f.set(status=None, category="disc")
    assert f.rowCount() == 1
    f.set(category=None, text="big.iso")
    assert f.rowCount() == 1
    m.remove("a1")
    assert m.index_of == {"b2": 0}
    assert display_name({"url": "https://cdn.example.org/big.iso"}) == "cdn.example.org/big.iso"
    assert human_eta(3725) == "1h 02m" and human_eta(None) == ""


def test_window_follows_the_engine_and_sends_commands(qapp, box, monkeypatch):
    app, base, _ = box
    app.runner.stop()  # jobs stay queued: we are testing the window, not downloads
    from siphon import serve
    from siphon.app.window import MainWindow
    eng = client.Engine(base, serve.api_token())
    eng.status = eng.call("GET", "/status")
    win = MainWindow(eng)
    try:
        assert wait_for(qapp, lambda: "connected" in win.conn_label.text())
        job = eng.call("POST", "/jobs", {"url": "https://a.example/one", "category": "talks"})
        assert wait_for(qapp, lambda: job["id"] in win.model.index_of)  # arrived as a live event
        assert wait_for(qapp, lambda: any(win.cat_root.child(i).text(0) == "talks" for i in range(win.cat_root.childCount())))
        win.table.selectRow(0)
        win._each("pause")
        assert wait_for(qapp, lambda: win.model.rows[win.model.index_of[job["id"]]]["status"] == "paused")
        eng.call("DELETE", f"/jobs/{job['id']}")
        assert wait_for(qapp, lambda: job["id"] not in win.model.index_of)
    finally:
        win.stream.stop()
