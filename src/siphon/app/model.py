"""Jobs as a Qt table: one row per job, updated in place from engine events.

Rows are looked up by job id in a dict, so an update touches one row (dataChanged on that row)
instead of resetting the table; that is what keeps 10,000+ rows smooth while many download.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

COLUMNS = ["Name", "Size", "Progress", "Status", "Speed", "ETA", "Category", "Added"]
NAME, SIZE, PROGRESS, STATUS, SPEED, ETA, CATEGORY, ADDED = range(len(COLUMNS))
JOB_ROLE = Qt.UserRole + 1
SORT_ROLE = Qt.UserRole + 2

STATUS_TEXT = {"pending": "Queued", "active": "Downloading", "paused": "Paused", "done": "Completed", "error": "Error"}
STAGE_TEXT = {"resolving": "Finding stream", "uploading": "Uploading", "saved": "Finishing"}


def human_bytes(n) -> str:
    if not n:
        return ""
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return ""


def human_eta(seconds) -> str:
    if seconds is None or seconds < 0:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def display_name(job: dict) -> str:
    files = job.get("files") or []
    if files:
        base = os.path.basename(files[0])
        return base if len(files) == 1 else f"{base} (+{len(files) - 1})"
    if job.get("file") and "/" in str(job["file"]):
        return os.path.basename(job["file"])
    u = urlsplit(job.get("url") or "")
    return (u.netloc + u.path).rstrip("/") or job.get("url", "")


def status_text(job: dict) -> str:
    st = job.get("status")
    if st == "active" and job.get("stage") in STAGE_TEXT:
        return STAGE_TEXT[job["stage"]]
    return STATUS_TEXT.get(st, st or "")


class JobsModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self.index_of: dict[str, int] = {}

    # ---- updates from the engine
    def reset(self, jobs: list[dict]) -> None:
        self.beginResetModel()
        self.rows = list(jobs)
        self.index_of = {j["id"]: i for i, j in enumerate(self.rows)}
        self.endResetModel()

    def upsert(self, job: dict, live_only: bool = False) -> None:
        i = self.index_of.get(job["id"])
        if i is None:
            if live_only:
                return
            self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows))
            self.index_of[job["id"]] = len(self.rows)
            self.rows.append(job)
            self.endInsertRows()
            return
        if live_only:  # progress ticks carry the whole job, but only these fields change between ticks
            self.rows[i] = {**self.rows[i], "progress": job.get("progress"), "stage": job.get("stage")}
        else:
            self.rows[i] = job
        self.dataChanged.emit(self.index(i, 0), self.index(i, len(COLUMNS) - 1))

    def remove(self, job_id: str) -> None:
        i = self.index_of.pop(job_id, None)
        if i is None:
            return
        self.beginRemoveRows(QModelIndex(), i, i)
        self.rows.pop(i)
        self.endRemoveRows()
        self.index_of = {j["id"]: k for k, j in enumerate(self.rows)}

    def job(self, row: int) -> dict:
        return self.rows[row]

    # ---- Qt model API
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        j = self.rows[index.row()]
        col = index.column()
        prog = j.get("progress") or {}
        active = j.get("status") == "active"
        if role == JOB_ROLE:
            return j
        if role == Qt.DisplayRole:
            if col == NAME:
                return display_name(j)
            if col == SIZE:
                return human_bytes(prog.get("total") or j.get("size"))
            if col == PROGRESS:
                return 100.0 if j.get("status") == "done" else (prog.get("percent") or 0.0)
            if col == STATUS:
                return status_text(j)
            if col == SPEED:
                return f"{human_bytes(prog.get('speed'))}/s" if active and prog.get("speed") else ""
            if col == ETA:
                if active and prog.get("speed") and prog.get("total") and prog.get("done") is not None:
                    return human_eta((prog["total"] - prog["done"]) / prog["speed"])
                return ""
            if col == CATEGORY:
                return j.get("category") or ""
            if col == ADDED:
                return (j.get("added") or "").replace("T", " ")[:16]
        if role == SORT_ROLE:
            if col == SIZE:
                return float(prog.get("total") or j.get("size") or 0)
            if col == PROGRESS:
                return float(self.data(index) or 0)
            if col == SPEED:
                return float(prog.get("speed") or 0) if active else 0.0
            return self.data(index) or ""
        if role == Qt.ToolTipRole:
            if col == STATUS and j.get("error"):
                return j["error"]
            if col == NAME:
                return "\n".join([j.get("url", ""), *(j.get("files") or [])])
        if role == Qt.TextAlignmentRole and col in (SIZE, SPEED, ETA):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None


class JobsFilter(QSortFilterProxyModel):
    """Sidebar status/category plus the search box."""

    def __init__(self):
        super().__init__()
        self.status: str | None = None     # None = all
        self.category: str | None = None   # None = all, "" = uncategorised
        self.text = ""
        self.setSortRole(SORT_ROLE)

    def set(self, **kw) -> None:
        if hasattr(self, "beginFilterChange"):  # Qt 6.10+; invalidateFilter() is deprecated there
            self.beginFilterChange()
            for k, v in kw.items():
                setattr(self, k, v)
            self.endFilterChange()
            return
        for k, v in kw.items():
            setattr(self, k, v)
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent) -> bool:
        j = self.sourceModel().job(row)
        if self.status and j.get("status") != self.status:
            return False
        if self.category is not None and (j.get("category") or "") != self.category:
            return False
        if self.text:
            hay = " ".join([display_name(j), j.get("url", ""), j.get("category") or "", *(j.get("tags") or [])]).lower()
            return self.text.lower() in hay
        return True
