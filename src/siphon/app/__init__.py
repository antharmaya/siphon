"""siphon's desktop app: a window onto the engine (`siphon app`).

The app holds no download state of its own. It finds the engine on this machine (starting it if
needed), shows the jobs, and sends commands through the v1 API, so closing the window never stops
a download, and the browser button, the CLI and the phone page all see the same list.

Needs the optional Qt dependency: uv tool install "antharmaya-siphon[app] @ git+https://github.com/antharmaya/siphon"
"""

from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print('siphon app needs Qt. Install with: uv tool install --reinstall '
              '"antharmaya-siphon[app] @ git+https://github.com/antharmaya/siphon"', file=sys.stderr)
        return 1
    from .link import ensure_engine
    from .window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1] + argv)
    app.setApplicationName("siphon")
    app.setDesktopFileName("siphon")
    app.setQuitOnLastWindowClosed(False)
    engine = ensure_engine()
    if not engine:
        QMessageBox.critical(None, "siphon", "Couldn't start the siphon engine.\nTry running `siphon serve` in a terminal to see why.")
        return 1
    win = MainWindow(engine)
    app.aboutToQuit.connect(win.stream.stop)
    win.show()
    if not win.tray:
        app.setQuitOnLastWindowClosed(True)
    return app.exec()
