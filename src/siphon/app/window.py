"""The main window: a dense transfers table in the qBittorrent mould, driven entirely by the engine API."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow,
                               QMenu, QMessageBox, QPlainTextEdit, QSizePolicy, QSpinBox, QSplitter, QStyle, QStyledItemDelegate,
                               QStyleOptionProgressBar, QSystemTrayIcon, QTableView, QTabWidget, QToolBar, QTreeWidget,
                               QTreeWidgetItem, QWidget)

from .. import client
from .link import EventStream, Link
from .model import (ADDED, NAME, PROGRESS, JobsFilter, JobsModel, display_name,
                    human_bytes, status_text)

PRESETS = [("Best quality", "best"), ("Compatible (H.264 MP4)", "compat"), ("Small (720p or less)", "small"),
           ("Archive (with metadata)", "archive")]
STATUSES = [("All", None), ("Downloading", "active"), ("Queued", "pending"), ("Paused", "paused"),
            ("Completed", "done"), ("Errored", "error")]


class ProgressDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        opt = QStyleOptionProgressBar()
        opt.rect = option.rect.adjusted(2, 3, -2, -3)
        opt.minimum, opt.maximum = 0, 1000
        pct = float(index.data() or 0)
        opt.progress = int(pct * 10)
        opt.text = f"{pct:.1f}%"
        opt.textVisible = True
        QApplication.style().drawControl(QStyle.CE_ProgressBar, opt, painter)


class AddDialog(QDialog):
    def __init__(self, parent, categories: list[str], url: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Add download")
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        self.urls = QPlainTextEdit(url)
        self.urls.setPlaceholderText("One link per line: a video page, a post, or a file")
        self.urls.setFixedHeight(90)
        self.preset = QComboBox()
        for label, value in PRESETS:
            self.preset.addItem(label, value)
        self.audio = QCheckBox("Audio only")
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.addItems([""] + categories)
        self.priority = QSpinBox()
        self.priority.setRange(-100, 100)
        form.addRow("Links", self.urls)
        form.addRow("Quality", self.preset)
        form.addRow("", self.audio)
        form.addRow("Category", self.category)
        form.addRow("Priority", self.priority)
        # Start from the last choices (FDM and AB Download Manager users asked for this).
        st = QSettings("antharmaya", "siphon")
        if (i := self.preset.findData(st.value("add/preset", "best"))) >= 0:
            self.preset.setCurrentIndex(i)
        self.audio.setChecked(st.value("add/audio", False, type=bool))
        self.category.setCurrentText(st.value("add/category", "") or "")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Download")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def intents(self) -> list[dict]:
        st = QSettings("antharmaya", "siphon")
        st.setValue("add/preset", self.preset.currentData())
        st.setValue("add/audio", self.audio.isChecked())
        st.setValue("add/category", self.category.currentText().strip())
        out = []
        for line in self.urls.toPlainText().split():
            it = {"url": line.strip(), "preset": self.preset.currentData()}
            if self.audio.isChecked():
                it["audio"] = True
            if cat := self.category.currentText().strip():
                it["category"] = cat
            if self.priority.value():
                it["priority"] = self.priority.value()
            out.append(it)
        return out


class MainWindow(QMainWindow):
    def __init__(self, engine: client.Engine):
        super().__init__()
        self.engine = engine
        self.link = Link(engine)
        self.link.failed.connect(self._failed)
        self.local = engine.url.startswith(("http://127.0.0.1", "http://[::1]", "http://localhost"))
        self.setWindowTitle("siphon")
        self.setWindowIcon(_icon())
        self.resize(1280, 760)

        self.model = JobsModel()
        self.proxy = JobsFilter()
        self.proxy.setSourceModel(self.model)
        self._build()
        self._restore()

        self.stream = EventStream(engine)
        self.stream.snapshot.connect(self._snapshot)
        self.stream.job.connect(self._job)
        self.stream.removed.connect(self._removed)
        self.stream.progress.connect(lambda j: self.model.upsert(j, live_only=True))
        self.stream.connected.connect(self._connected)
        self.stream.start()

        self.ticker = QTimer(self)  # sidebar counts and totals, at most a few times a second
        self.ticker.timeout.connect(self._refresh_summary)
        self.ticker.start(500)

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        st = self.style()
        self.act_add = self._action(tb, "Add", st.standardIcon(QStyle.SP_FileDialogNewFolder), self.add, QKeySequence.New)
        tb.addSeparator()
        self.act_resume = self._action(tb, "Resume", st.standardIcon(QStyle.SP_MediaPlay), lambda: self._each("resume"), "Ctrl+R")
        self.act_pause = self._action(tb, "Pause", st.standardIcon(QStyle.SP_MediaPause), lambda: self._each("pause"), "Ctrl+P")
        self.act_remove = self._action(tb, "Remove", st.standardIcon(QStyle.SP_TrashIcon), self.remove, QKeySequence.Delete)
        tb.addSeparator()
        self.act_folder = self._action(tb, "Open folder", st.standardIcon(QStyle.SP_DirOpenIcon), self.open_folder, "Ctrl+O")
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter downloads…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(280)
        self.search.textChanged.connect(lambda t: self.proxy.set(text=t.strip()))
        tb.addWidget(self.search)
        self.addAction(QAction(self, shortcut=QKeySequence.Find, triggered=self.search.setFocus))

        # sidebar
        self.side = QTreeWidget()
        self.side.setHeaderHidden(True)
        self.side.setMinimumWidth(170)
        self.status_root = QTreeWidgetItem(self.side, ["STATUS"])
        self.cat_root = QTreeWidgetItem(self.side, ["CATEGORIES"])
        for root in (self.status_root, self.cat_root):
            root.setFlags(Qt.ItemIsEnabled)
            f = root.font(0)
            f.setBold(True)
            root.setFont(0, f)
        self.status_items = {}
        for label, value in STATUSES:
            it = QTreeWidgetItem(self.status_root, [label])
            it.setData(0, Qt.UserRole, ("status", value))
            self.status_items[value] = (it, label)
        self.side.expandAll()
        self.side.currentItemChanged.connect(self._side_pick)
        self.side.setCurrentItem(self.status_items[None][0])

        # table
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(ADDED, Qt.DescendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setItemDelegateForColumn(PROGRESS, ProgressDelegate(self.table))
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setSectionResizeMode(NAME, QHeaderView.Stretch)
        for col, w in ((1, 90), (2, 130), (3, 120), (4, 95), (5, 75), (6, 110), (7, 130)):
            self.table.setColumnWidth(col, w)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.doubleClicked.connect(lambda _: self.open_file())
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._details())

        # details
        self.details = QTabWidget()
        self.general = QLabel()
        self.general.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.general.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.general.setWordWrap(True)
        self.general.setMargin(10)
        self.files = QListWidget()
        self.files.itemDoubleClicked.connect(lambda it: self._open(it.text()))
        self.details.addTab(self.general, "General")
        self.details.addTab(self.files, "Files")

        right = QSplitter(Qt.Vertical)
        right.addWidget(self.table)
        right.addWidget(self.details)
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 1)
        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(self.side)
        self.split.addWidget(right)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([190, 1090])
        self.vsplit = right
        self.setCentralWidget(self.split)

        self.conn_label = QLabel()
        self.speed_label = QLabel()
        self.statusBar().addWidget(self.conn_label)
        self.statusBar().addPermanentWidget(self.speed_label)

        # tray: closing the window keeps downloads going in the engine; the tray brings it back
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(_icon(), self)
            m = QMenu()
            m.addAction("Show siphon", self._show)
            m.addAction("Add download…", lambda: (self._show(), self.add()))
            m.addSeparator()
            m.addAction("Pause all", lambda: self._all("pause"))
            m.addAction("Resume all", lambda: self._all("resume"))
            m.addSeparator()
            # The engine owns the downloads, so quitting the window never stops them.
            m.addAction("Quit window (downloads continue)", QApplication.quit)
            self.tray.setContextMenu(m)
            self.tray.activated.connect(lambda r: self._toggle() if r == QSystemTrayIcon.Trigger else None)
            self.tray.setToolTip("siphon")
            self.tray.messageClicked.connect(self._notification_clicked)
            self.tray.show()

    def _action(self, tb, text, icon, slot, shortcut=None) -> QAction:
        a = QAction(icon, text, self)
        a.triggered.connect(slot)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        tb.addAction(a)
        return a

    # ------------------------------------------------------------------ events
    def _snapshot(self, jobs: list) -> None:
        self.model.reset(jobs)
        self._refresh_categories()

    def _job(self, job: dict) -> None:
        before = self._categories()
        i = self.model.index_of.get(job["id"])
        was = self.model.rows[i].get("status") if i is not None else None
        self.model.upsert(job)
        if job.get("status") in ("done", "error") and was not in (None, "done", "error"):
            self._notify(job)
        if self._categories() != before:  # a new category appeared, or the last job of one left it
            self._refresh_categories()
        self._details()

    def _notify(self, job: dict) -> None:
        """A desktop notification when a download this window watched finishes or fails."""
        if not self.tray:
            return
        name = display_name(job)
        if job["status"] == "done":
            self._last_file = (job.get("files") or [None])[0]
            self.tray.showMessage("Downloaded", name, QSystemTrayIcon.Information, 6000)
        else:
            self._last_file = None
            self.tray.showMessage("Download failed", f"{name}\n{job.get('error') or ''}".strip(), QSystemTrayIcon.Warning, 8000)

    def _notification_clicked(self) -> None:
        if getattr(self, "_last_file", None):
            self._open(self._last_file)
        else:
            self._show()

    def _removed(self, job_id: str) -> None:
        self.model.remove(job_id)
        self._details()

    def _connected(self, ok: bool) -> None:
        self.conn_label.setText(f"● connected to {self.engine.url}" if ok else "○ reconnecting to the siphon engine…")

    def _failed(self, tag: str, msg: str) -> None:
        self.statusBar().showMessage(f"{tag.split(' ')[-1]}: {msg}", 8000)

    # ------------------------------------------------------------------ summary
    def _categories(self) -> list[str]:
        return sorted({j.get("category") for j in self.model.rows if j.get("category")})

    def _refresh_categories(self) -> None:
        current = self.side.currentItem()
        picked = current.data(0, Qt.UserRole) if current else None
        self.cat_root.takeChildren()
        for label, value in [("All", None), ("Uncategorised", "")] + [(c, c) for c in self._categories()]:
            it = QTreeWidgetItem(self.cat_root, [label])
            it.setData(0, Qt.UserRole, ("category", value))
            if picked == ("category", value):
                self.side.setCurrentItem(it)
        self.cat_root.setExpanded(True)

    def _refresh_summary(self) -> None:
        rows = self.model.rows
        for value, (it, label) in self.status_items.items():
            n = len(rows) if value is None else sum(j.get("status") == value for j in rows)
            it.setText(0, f"{label} ({n})")
        speed = sum((j.get("progress") or {}).get("speed") or 0 for j in rows if j.get("status") == "active")
        active = sum(j.get("status") == "active" for j in rows)
        self.speed_label.setText(f"↓ {human_bytes(speed) or '0 B'}/s   {active} downloading")
        if self.tray:
            self.tray.setToolTip(f"siphon: {active} downloading, {human_bytes(speed) or '0 B'}/s")

    def _side_pick(self, item, _prev) -> None:
        if not item or not item.data(0, Qt.UserRole):
            return
        kind, value = item.data(0, Qt.UserRole)
        if kind == "status":
            self.proxy.set(status=value)
        else:
            self.proxy.set(category=value)

    # ------------------------------------------------------------------ selection
    def selected(self) -> list[dict]:
        rows = {self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()}
        return [self.model.job(r) for r in sorted(rows)]

    def _details(self) -> None:
        sel = self.selected()
        self.files.clear()
        if len(sel) != 1:
            self.general.setText(f"{len(sel)} downloads selected" if sel else "")
            return
        j = sel[0]
        prog = j.get("progress") or {}
        lines = [f"<b>{_esc(display_name(j))}</b>", "",
                 f"<b>Link:</b> {_esc(j.get('url', ''))}",
                 f"<b>Status:</b> {_esc(status_text(j))}" + (f" — {_esc(j['error'])}" if j.get("error") else ""),
                 f"<b>Added:</b> {_esc((j.get('added') or '').replace('T', ' '))}"
                 + (f" &nbsp; <b>Finished:</b> {_esc(j['finished'].replace('T', ' '))}" if j.get("finished") else ""),
                 f"<b>Category:</b> {_esc(j.get('category') or '—')} &nbsp; <b>Tags:</b> {_esc(', '.join(j.get('tags') or []) or '—')}"
                 f" &nbsp; <b>Priority:</b> {j.get('priority', 0)}",
                 f"<b>Destination:</b> {_esc(j.get('dest') or 'this computer')}"]
        if prog.get("total"):
            lines.append(f"<b>Downloaded:</b> {human_bytes(prog.get('done'))} of {human_bytes(prog['total'])}")
        self.general.setText("<br>".join(lines))
        for f in j.get("files") or []:
            self.files.addItem(f)

    # ------------------------------------------------------------------ actions
    def add(self, url: str = "") -> None:
        if not url:
            clip = QGuiApplication.clipboard().text().strip()
            url = clip if clip.startswith(("http://", "https://")) else ""
        dlg = AddDialog(self, self._categories(), url)
        if dlg.exec() == QDialog.Accepted:
            for it in dlg.intents():
                self.link.call("POST", "/jobs", it, tag=f"add {it['url']}")

    def _all(self, verb: str) -> None:
        ok = ("pending", "active") if verb == "pause" else ("paused",)
        for j in self.model.rows:
            if j.get("status") in ok:
                self.link.call("POST", f"/jobs/{j['id']}/{verb}", tag=f"{verb} {j['id']}")

    def _label(self, **fields) -> None:
        for j in self.selected():
            self.link.call("PATCH", f"/jobs/{j['id']}", fields, tag=f"label {j['id']}")

    def _each(self, verb: str) -> None:
        ok = {"pause": ("pending", "active"), "resume": ("paused",), "retry": ("error",)}[verb]
        for j in self.selected():
            if j.get("status") in ok:
                self.link.call("POST", f"/jobs/{j['id']}/{verb}", tag=f"{verb} {j['id']}")

    def remove(self) -> None:
        sel = self.selected()
        if not sel:
            return
        if QMessageBox.question(self, "Remove", f"Remove {len(sel)} download(s) from the list?\n"
                                "Files already saved stay on disk.") != QMessageBox.Yes:
            return
        for j in sel:
            self.link.call("DELETE", f"/jobs/{j['id']}", tag=f"remove {j['id']}")

    def set_category(self) -> None:
        sel = self.selected()
        if not sel:
            return
        cat, ok = QInputDialog.getItem(self, "Category", "Category (empty to clear):", [""] + self._categories(),
                                       editable=True)
        if ok:
            for j in sel:
                self.link.call("PATCH", f"/jobs/{j['id']}", {"category": cat.strip() or None}, tag=f"category {j['id']}")

    def open_file(self) -> None:
        sel = self.selected()
        if sel and (sel[0].get("files") or []):
            self._open(sel[0]["files"][0])

    def open_folder(self) -> None:
        sel = self.selected()
        files = (sel[0].get("files") if sel else None) or []
        self._open(str(Path(files[0]).parent) if files else self.engine.status.get("downloads", str(Path.home())))

    def _open(self, path: str) -> None:
        if not self.local:
            self.statusBar().showMessage("those files are on the engine's machine, not this one", 6000)
            return
        if os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            self.statusBar().showMessage(f"not found: {path}", 6000)

    def _menu(self, pos) -> None:
        sel = self.selected()
        if not sel:
            return
        st = {j.get("status") for j in sel}
        m = QMenu(self)
        if st & {"paused"}:
            m.addAction("Resume", lambda: self._each("resume"))
        if st & {"pending", "active"}:
            m.addAction("Pause", lambda: self._each("pause"))
        if st & {"error"}:
            m.addAction("Retry", lambda: self._each("retry"))
        m.addAction("Remove", self.remove)
        m.addSeparator()
        m.addAction("Open", self.open_file).setEnabled(bool(sel[0].get("files")))
        m.addAction("Open destination folder", self.open_folder)
        m.addSeparator()
        # Category and priority one click deep (qBittorrent users objected when this took 5 clicks).
        cats = m.addMenu("Category")
        for c in self._categories():
            cats.addAction(c, lambda c=c: self._label(category=c))
        if self._categories():
            cats.addSeparator()
        cats.addAction("New…", self.set_category)
        cats.addAction("None", lambda: self._label(category=None))
        pm = m.addMenu("Priority")
        for label, value in (("High", 10), ("Normal", 0), ("Low", -10)):
            pm.addAction(label, lambda v=value: self._label(priority=v))
        cm = m.addMenu("Copy")
        cm.addAction("Link", lambda: QGuiApplication.clipboard().setText("\n".join(j.get("url", "") for j in sel)))
        cm.addAction("File path", lambda: QGuiApplication.clipboard().setText(
            "\n".join(f for j in sel for f in (j.get("files") or []))))
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------ window
    def _show(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _toggle(self) -> None:
        self.hide() if self.isVisible() else self._show()

    def closeEvent(self, e) -> None:
        self._save()
        if self.tray and self.tray.isVisible():
            self.hide()  # downloads keep running in the engine either way
            e.ignore()
            return
        self.stream.stop()
        e.accept()

    def _save(self) -> None:
        s = QSettings("antharmaya", "siphon")
        s.setValue("geometry", self.saveGeometry())
        s.setValue("split", self.split.saveState())
        s.setValue("vsplit", self.vsplit.saveState())
        s.setValue("header", self.table.horizontalHeader().saveState())

    def _restore(self) -> None:
        s = QSettings("antharmaya", "siphon")
        for key, fn in (("geometry", self.restoreGeometry), ("split", self.split.restoreState),
                        ("vsplit", self.vsplit.restoreState), ("header", self.table.horizontalHeader().restoreState)):
            if (v := s.value(key)) is not None:
                fn(v)


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _icon() -> QIcon:
    icon = QIcon.fromTheme("siphon")  # installed by `siphon setup app`
    if not icon.isNull():
        return icon
    here = Path(__file__).resolve().parents[1] / "web" / "mark.svg"
    return QIcon(str(here)) if here.exists() else QIcon.fromTheme("folder-download")

