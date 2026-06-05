#!/usr/bin/env python3
"""
DiskScope - a fast single-window directory size analyzer for Linux.

Focus: speed of discovering directory sizes and file counts, in-memory caching
(results are kept until a deletion is performed through this tool), efficient
deletion of files/directories, and a clean visualization of partition usage.

Scales to tens of millions of files:
  * Only DIRECTORIES are held in memory (with aggregate size/count); individual
    files are listed on demand with a single scandir when a directory is
    expanded. Memory is proportional to the number of directories, not files.
  * Scanning is parallel: a pool of threads walks directories concurrently so
    the per-file stat() I/O overlaps instead of running one-at-a-time.
  * Cyclic GC is disabled during a scan (the node tree is full of
    parent<->child cycles, so periodic collections over millions of live
    objects are pure, growing overhead).

Built on PyQt5. No third-party dependencies beyond PyQt5.
"""

import os
import sys
import gc
import time
import queue
import shutil
import threading
import traceback

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QRect, QSize
from PyQt5.QtGui import QColor, QPainter, QFont, QLinearGradient
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget,
    QTreeWidgetItem, QPushButton, QLabel, QFileDialog, QMessageBox, QStyle,
    QStyledItemDelegate, QAbstractItemView, QSizePolicy, QLineEdit,
    QProgressBar,
)

__version__ = "1.0.0"
# Set to your public repository URL to show a "project home" link in About.
HOMEPAGE = "https://github.com/nik-007/disk-scope"


# ---------------------------------------------------------------------------
# Model + in-memory cache
# ---------------------------------------------------------------------------

class DirNode:
    """A directory held in memory.

    Files inside it are NOT stored as objects — only the aggregate recursive
    size and file count are kept. The individual files are produced on demand
    by `list_children()` (a single scandir of this directory) when the user
    expands it, so memory stays proportional to the number of directories.
    """
    __slots__ = ("path", "name", "size", "count", "subdirs",
                 "parent", "loaded", "error")
    is_dir = True   # class attribute; DirNode is always a directory

    def __init__(self, path, name, parent=None):
        self.path = path
        self.name = name
        self.size = 0          # recursive total bytes (files + subdirs)
        self.count = 0         # recursive file count
        self.subdirs = None    # list[DirNode]; None until scanned
        self.parent = parent
        self.loaded = False
        self.error = False


class FileEntry:
    """A single file shown on demand. Never cached — created only while its tree
    row exists, then discarded. Mirrors the attribute surface the UI/deletion
    code reads from a node (`is_dir`, `size`, `count`, `path`, `name`,
    `parent`, `subdirs`)."""
    __slots__ = ("path", "name", "size", "parent")
    is_dir = False
    error = False
    count = 1          # one file (used when subtracting from ancestor totals)
    subdirs = None

    def __init__(self, path, name, size, parent):
        self.path = path
        self.name = name
        self.size = size
        self.parent = parent


# Global cache: absolute path -> fully scanned DirNode. Survives across
# re-analyses (e.g. navigating Up) and is only mutated when WE delete something
# through the tool (or on Rescan).
PATH_CACHE = {}

# Cap on how many individual files a single directory shows when expanded (the
# rest are summarised). Bounds UI work for pathological directories with
# millions of direct entries.
DISPLAY_FILE_CAP = 10000


def human_size(n):
    """Human readable binary size."""
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if f < 1024.0:
            if unit == "B":
                return f"{int(f)} B"
            return f"{f:.1f} {unit}"
        f /= 1024.0
    return f"{f:.1f} EiB"


def human_count(n):
    return f"{n:,}".replace(",", " ")


def _rss_mb():
    """Resident memory of this process in MiB (Linux /proc), 0 if unavailable."""
    try:
        with open("/proc/self/statm") as f:
            resident_pages = int(f.read().split()[1])
        page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        return resident_pages * page / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return 0.0


def list_children(dirnode):
    """Display children for an expanded directory: its subdirs (from memory)
    plus its direct files (read on demand). Returns (children, extra) where
    children is a size-sorted list of DirNode|FileEntry and extra is None or a
    (omitted_count, omitted_size) tuple when the file list was capped."""
    subdirs = list(dirnode.subdirs or [])
    files = []
    try:
        with os.scandir(dirnode.path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        continue   # already represented by a DirNode
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                files.append(FileEntry(entry.path, entry.name, st.st_size,
                                       dirnode))
    except OSError:
        pass

    files.sort(key=lambda f: f.size, reverse=True)
    extra = None
    if len(files) > DISPLAY_FILE_CAP:
        omitted = files[DISPLAY_FILE_CAP:]
        files = files[:DISPLAY_FILE_CAP]
        extra = (len(omitted), sum(f.size for f in omitted))

    children = subdirs + files
    children.sort(key=lambda c: c.size, reverse=True)
    return children, extra


# ---------------------------------------------------------------------------
# Background scanner (parallel)
# ---------------------------------------------------------------------------

_SENTINEL = object()


class ScanWorker(QThread):
    """Scans a directory tree using a pool of worker threads.

    Phase 1 (parallel): threads pull directories off a queue, scandir each one,
    stat its direct files to get a direct size/count, and enqueue child
    directories. Because scandir/stat release the GIL, the per-file I/O of many
    directories overlaps. Phase 2 (serial, no I/O): build the DirNode tree and
    roll recursive totals up from the leaves. Only directories are kept.
    """
    progress = pyqtSignal(int, str)        # files seen so far, current dir
    finished_scan = pyqtSignal(object)     # root DirNode (or None on abort)

    def __init__(self, root_path):
        super().__init__()
        self.root_path = os.path.abspath(root_path)
        self._abort = False
        self._files = 0
        self._lock = threading.Lock()
        n = os.environ.get("DISKSCOPE_THREADS")
        if n and n.isdigit() and int(n) > 0:
            self._threads = int(n)
        else:
            # Default to serial. On a WARM page cache (the common re-scan case)
            # scandir/stat return without blocking, so the work is CPU-bound and
            # Python's GIL makes extra threads a net loss (measured ~2x slower at
            # 8 threads on warm /usr). Multiple threads only help when stat() I/O
            # actually blocks — a cold cache, a slow disk, or network storage —
            # where the syscalls release the GIL and overlap. Users on such
            # storage can opt in with DISKSCOPE_THREADS=8.
            self._threads = 1
        # Opt-in benchmark logging: set DISKSCOPE_BENCH=1 (or a file path).
        self._bench_path = os.environ.get("DISKSCOPE_BENCH")
        self._bench_fh = None

    def abort(self):
        self._abort = True

    def run(self):
        self._bench_start()
        # parent<->child cycles mean only cyclic GC can free the tree, and we
        # keep everything until done — so periodic collections are pure overhead
        # that grows with the object count. Disable for the scan.
        gc_was_enabled = gc.isenabled()
        gc.disable()
        root = None
        try:
            root = self._scan_parallel()
        except Exception:
            traceback.print_exc()
            root = None
        finally:
            if gc_was_enabled:
                gc.enable()
            self._bench_finish()
        if self._abort or root is None:
            self.finished_scan.emit(None)
        else:
            self.progress.emit(self._files, "done")
            self.finished_scan.emit(root)

    # -- phase 1: parallel directory discovery -----------------------------

    def _scan_parallel(self):
        root_path = self.root_path
        cached = PATH_CACHE.get(root_path)
        if cached is not None and cached.loaded:
            cached.parent = None
            return cached

        # results[path] -> ("cached", DirNode)
        #               |  (direct_size, direct_count, [child_dir_paths], error)
        results = {}
        q = queue.Queue()
        q.put(root_path)

        def work():
            local = 0
            while True:
                path = q.get()
                try:
                    if path is _SENTINEL:
                        if local:
                            self._bump(local)
                            local = 0
                        return
                    if self._abort:
                        continue
                    cached_node = PATH_CACHE.get(path)
                    if cached_node is not None and cached_node.loaded:
                        results[path] = ("cached", cached_node)
                        continue
                    direct_size = 0
                    direct_count = 0
                    child_dirs = []
                    err = False
                    try:
                        with os.scandir(path) as it:
                            for entry in it:
                                if self._abort:
                                    break
                                try:
                                    if entry.is_dir(follow_symlinks=False):
                                        child_dirs.append(entry.path)
                                        q.put(entry.path)
                                    else:
                                        st = entry.stat(follow_symlinks=False)
                                        direct_size += st.st_size
                                        direct_count += 1
                                        local += 1
                                        if local >= 4000:
                                            self._bump(local)
                                            local = 0
                                except OSError:
                                    pass
                    except OSError:
                        err = True
                    results[path] = (direct_size, direct_count, child_dirs, err)
                finally:
                    q.task_done()

        threads = [threading.Thread(target=work, daemon=True)
                   for _ in range(self._threads)]
        for t in threads:
            t.start()
        q.join()                       # all directories processed
        for _ in threads:
            q.put(_SENTINEL)
        for t in threads:
            t.join()

        if self._abort:
            return None
        return self._build_tree(root_path, results)

    # -- phase 2: build tree + roll up totals (no I/O) ---------------------

    def _build_tree(self, root_path, results):
        # Post-order over the directory tree so children are built before
        # parents (iterative, to avoid Python recursion limits on deep trees).
        post = []
        stack = [(root_path, False)]
        while stack:
            path, processed = stack.pop()
            ent = results.get(path)
            if processed or ent is None or ent[0] == "cached":
                post.append(path)
                continue
            stack.append((path, True))
            for child in ent[2]:
                stack.append((child, False))

        nodes = {}
        for path in post:
            ent = results.get(path)
            if ent is None:                       # never reached (e.g. aborted)
                node = DirNode(path, os.path.basename(path) or path)
                node.error = True
                node.subdirs = []
                nodes[path] = node
                continue
            if ent[0] == "cached":
                nodes[path] = ent[1]
                continue
            direct_size, direct_count, child_paths, err = ent
            if self._bench_fh is not None:
                fan_out = direct_count + len(child_paths)
                if fan_out >= 50000:
                    # Flag high-fan-out dirs: a single directory with a huge
                    # number of direct entries is a structural cost (its sort
                    # and on-demand listing) distinct from overall scale.
                    self._bench_fh.write(
                        f"  big-dir fan-out: {fan_out:>10,} entries  {path}\n")
            node = DirNode(path, os.path.basename(path) or path)
            node.error = err
            total_size = direct_size
            total_count = direct_count
            subs = []
            for cp in child_paths:
                cn = nodes.get(cp)
                if cn is None:
                    continue
                cn.parent = node
                subs.append(cn)
                total_size += cn.size
                total_count += cn.count
            subs.sort(key=lambda d: d.size, reverse=True)
            node.subdirs = subs
            node.size = total_size
            node.count = total_count
            # Don't cache unreadable directories as authoritative — retry later.
            node.loaded = not err
            if node.loaded:
                PATH_CACHE[path] = node
            nodes[path] = node

        root = nodes.get(root_path)
        if root is not None:
            root.parent = None
        return root

    # -- progress / benchmark ----------------------------------------------

    def _bump(self, n):
        with self._lock:
            self._files += n
            files = self._files
            if self._bench_fh is not None:
                self._bench_log()
        self.progress.emit(files, "")

    def _bench_start(self):
        if not self._bench_path:
            return
        path = "/tmp/diskscope_bench.log" if self._bench_path == "1" \
            else self._bench_path
        try:
            self._bench_fh = open(path, "a", buffering=1)
        except OSError:
            self._bench_fh = None
            return
        self._bench_t0 = time.monotonic()
        self._bench_last_t = self._bench_t0
        self._bench_last_f = 0
        self._bench_fh.write(
            f"\n=== scan start: {self.root_path}  "
            f"({self._threads} threads, pid {os.getpid()}) ===\n"
            f"{'elapsed':>9}  {'files':>14}  {'now':>10}  {'avg':>10}  "
            f"{'rss':>9}\n")

    def _bench_log(self):
        # Called under self._lock.
        now = time.monotonic()
        dt = now - self._bench_last_t
        df = self._files - self._bench_last_f
        rate = (df / dt) if dt > 0 else 0.0
        elapsed = now - self._bench_t0
        avg = (self._files / elapsed) if elapsed > 0 else 0.0
        self._bench_last_t = now
        self._bench_last_f = self._files
        self._bench_fh.write(
            f"{elapsed:8.1f}s  {self._files:>14,}  "
            f"{rate / 1000:8.0f}k/s  {avg / 1000:8.0f}k/s  "
            f"{_rss_mb():7.0f}MB\n")

    def _bench_finish(self):
        if self._bench_fh is None:
            return
        elapsed = time.monotonic() - self._bench_t0
        avg = (self._files / elapsed) if elapsed > 0 else 0.0
        self._bench_fh.write(
            f"=== done: {self._files:,} files in {elapsed:.1f}s  "
            f"avg {avg / 1000:.0f}k files/s  rss {_rss_mb():.0f}MB  "
            f"aborted={self._abort} ===\n")
        try:
            self._bench_fh.close()
        finally:
            self._bench_fh = None


# ---------------------------------------------------------------------------
# Tree item + size-bar delegate
# ---------------------------------------------------------------------------

COL_NAME = 0
COL_SIZE = 1
COL_FILES = 2
COL_BAR = 3

# Role used to stash the size fraction (0..1) for the bar delegate.
FRACTION_ROLE = Qt.UserRole + 1
# Role marking a lazy-load placeholder row (robust against a real file that
# happens to share a sentinel name).
PLACEHOLDER_ROLE = Qt.UserRole + 2


class TreeItem(QTreeWidgetItem):
    """A tree row backed by a DirNode or a FileEntry (`self.node`)."""
    def __init__(self, node):
        super().__init__()
        self.node = node


class BarDelegate(QStyledItemDelegate):
    """Draws a proportional, color-graded usage bar in the last column."""

    def paint(self, painter, option, index):
        frac = index.data(FRACTION_ROLE)
        if frac is None:
            super().paint(painter, option, index)
            return

        painter.save()
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())

        rect = option.rect.adjusted(6, 4, -8, -4)
        # Track
        track = QColor(255, 255, 255, 18)
        painter.setPen(Qt.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(rect, 3, 3)

        w = int(rect.width() * max(0.0, min(1.0, frac)))
        if w > 0:
            fill = QRect(rect.x(), rect.y(), max(2, w), rect.height())
            grad = QLinearGradient(fill.topLeft(), fill.topRight())
            # blue -> amber -> coral as the share grows (blue ties in with the
            # disk bar so small items look calm, not "warning green").
            if frac < 0.25:
                c1, c2 = QColor(60, 120, 200), QColor(84, 156, 235)
            elif frac < 0.6:
                c1, c2 = QColor(214, 168, 70), QColor(235, 190, 86)
            else:
                c1, c2 = QColor(224, 110, 96), QColor(236, 134, 116)
            grad.setColorAt(0, c1)
            grad.setColorAt(1, c2)
            painter.setBrush(grad)
            painter.drawRoundedRect(fill, 3, 3)

        # Percentage label — skip sub-0.1% noise. Put it white INSIDE the fill
        # when there's room, otherwise just past the bar on the dark track in a
        # light tone, so the text never sits low-contrast on a coloured bar.
        if frac >= 0.001:
            label = f"{frac * 100:.1f}%"
            f = QFont(option.font)
            ps = f.pointSizeF()
            if ps > 0:
                f.setPointSizeF(max(6.0, ps - 0.5))
            painter.setFont(f)
            tw = painter.fontMetrics().horizontalAdvance(label)
            if w >= tw + 16:
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(QRect(rect.x() + 8, rect.y(),
                                       w - 10, rect.height()),
                                 Qt.AlignVCenter | Qt.AlignLeft, label)
            else:
                tx = rect.x() + w + 6
                painter.setPen(QColor(206, 212, 222))
                painter.drawText(QRect(tx, rect.y(), rect.right() - tx,
                                       rect.height()),
                                 Qt.AlignVCenter | Qt.AlignLeft, label)
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(160, 22)


# ---------------------------------------------------------------------------
# Disk usage bar widget
# ---------------------------------------------------------------------------

class DiskUsageBar(QWidget):
    def __init__(self):
        super().__init__()
        self.total = 0
        self.used = 0
        self.free = 0
        self.mount = ""
        self.setMinimumHeight(60)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_usage(self, total, used, free, mount):
        self.total = total
        self.used = used
        self.free = free
        self.mount = mount
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()

        if self.total <= 0:
            p.setPen(QColor(150, 155, 165))
            p.drawText(self.rect(), Qt.AlignCenter, "No disk info")
            return

        frac = self.used / self.total
        # Header line: mount (accent) + usage on the left, free on the right.
        head = QRect(4, 2, w - 8, 20)
        bold = QFont(self.font())
        bold.setBold(True)
        p.setFont(bold)
        fm = p.fontMetrics()
        mount_txt = self.mount or "/"
        p.setPen(QColor(116, 178, 240))
        p.drawText(head, Qt.AlignVCenter | Qt.AlignLeft, mount_txt)
        x = 4 + fm.horizontalAdvance(mount_txt) + 14
        p.setFont(QFont(self.font()))
        p.setPen(QColor(198, 204, 214))
        p.drawText(QRect(x, 2, w - 8 - x, 20), Qt.AlignVCenter | Qt.AlignLeft,
                   f"{human_size(self.used)} of {human_size(self.total)} used")
        p.setPen(QColor(122, 198, 150))
        p.drawText(head, Qt.AlignVCenter | Qt.AlignRight,
                   f"{human_size(self.free)} free")

        # Bar
        bar = QRect(4, 26, w - 8, h - 32)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 20))
        p.drawRoundedRect(bar, 6, 6)

        fw = int(bar.width() * frac)
        if fw > 0:
            fill = QRect(bar.x(), bar.y(), max(4, fw), bar.height())
            grad = QLinearGradient(fill.topLeft(), fill.topRight())
            if frac < 0.7:
                grad.setColorAt(0, QColor(60, 150, 220))
                grad.setColorAt(1, QColor(90, 180, 240))
            elif frac < 0.9:
                grad.setColorAt(0, QColor(220, 170, 60))
                grad.setColorAt(1, QColor(240, 195, 80))
            else:
                grad.setColorAt(0, QColor(220, 80, 70))
                grad.setColorAt(1, QColor(240, 110, 95))
            p.setBrush(grad)
            p.drawRoundedRect(fill, 6, 6)

        pf = QFont(self.font())
        pf.setBold(True)
        if pf.pointSizeF() > 0:
            pf.setPointSizeF(pf.pointSizeF() + 1)
        p.setFont(pf)
        p.setPen(QColor(245, 248, 252))
        p.drawText(bar, Qt.AlignCenter, f"{frac * 100:.1f}% used")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, start_path):
        super().__init__()
        self.setWindowTitle("DiskScope")
        self.resize(1080, 720)
        self.root_path = os.path.abspath(start_path)
        self.root_node = None
        self.worker = None
        self._workers = []   # keeps retiring threads alive until they finish

        self._build_ui()
        self.analyze(self.root_path)

    def closeEvent(self, ev):
        # Stop any background scans cleanly so a QThread is never destroyed
        # while still running (which would crash on exit).
        workers = list(self._workers)
        if self.worker is not None and self.worker not in workers:
            workers.append(self.worker)
        for w in workers:
            w.abort()
        for w in workers:
            if w.isRunning():
                w.wait(3000)
        super().closeEvent(ev)

    # -- UI construction ----------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        # Toolbar row
        bar = QHBoxLayout()
        bar.setSpacing(6)

        self.btn_open = QPushButton("  Open…  ")
        self.btn_open.setShortcut("Ctrl+O")
        self.btn_open.clicked.connect(self.choose_dir)

        self.btn_up = QPushButton("  ↑ Up  ")
        self.btn_up.setShortcut("Backspace")
        self.btn_up.clicked.connect(self.go_up)

        self.btn_rescan = QPushButton("  ↻ Rescan  ")
        self.btn_rescan.setShortcut("F5")
        self.btn_rescan.clicked.connect(self.rescan)

        self.btn_delete = QPushButton("  \U0001f5d1 Delete  ")
        self.btn_delete.setShortcut("Delete")
        self.btn_delete.clicked.connect(self.delete_selected)
        self.btn_delete.setObjectName("danger")

        self.btn_about = QPushButton("  ?  ")
        self.btn_about.setShortcut("F1")
        self.btn_about.setToolTip("About / help (F1)")
        self.btn_about.clicked.connect(self._show_about)

        self.path_edit = QLineEdit(self.root_path)
        self.path_edit.returnPressed.connect(self._path_entered)

        bar.addWidget(self.btn_open)
        bar.addWidget(self.btn_up)
        bar.addWidget(self.btn_rescan)
        bar.addWidget(self.path_edit, 1)
        bar.addWidget(self.btn_delete)
        bar.addWidget(self.btn_about)
        layout.addLayout(bar)

        # Disk usage
        self.disk_bar = DiskUsageBar()
        layout.addWidget(self.disk_bar)

        # Icons resolved once (not per row) — the style is constant.
        self._icon_dir = self.style().standardIcon(QStyle.SP_DirIcon)
        self._icon_file = self.style().standardIcon(QStyle.SP_FileIcon)

        # Fonts: monospace for column-aligned numbers, semibold for directory
        # names so structure reads apart from file content.
        self._mono = QFont("DejaVu Sans Mono")
        self._mono.setStyleHint(QFont.Monospace)
        self._mono.setPointSize(10)
        self._font_dir = QFont()
        self._font_dir.setPointSize(10)
        self._font_dir.setWeight(QFont.DemiBold)
        self._font_file = QFont()
        self._font_file.setPointSize(10)

        # Tree
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Name", "Size", "Files", "% of parent"])
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # No zebra striping — monospace numbers + the dir/file weight contrast
        # carry row tracking, and a uniform background keeps the share-bar lane
        # consistent instead of patchy.
        self.tree.setAlternatingRowColors(False)
        self.tree.setIndentation(20)
        self.tree.setItemDelegateForColumn(COL_BAR, BarDelegate(self.tree))
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.setColumnWidth(COL_NAME, 520)
        self.tree.setColumnWidth(COL_SIZE, 120)
        self.tree.setColumnWidth(COL_FILES, 110)
        hdr = self.tree.header()
        hdr.setStretchLastSection(True)
        self.tree.headerItem().setTextAlignment(COL_SIZE, Qt.AlignRight)
        self.tree.headerItem().setTextAlignment(COL_FILES, Qt.AlignRight)
        layout.addWidget(self.tree, 1)

        # Status row
        status = QHBoxLayout()
        self.status_label = QLabel("Ready")
        self.spinner = QProgressBar()
        self.spinner.setMaximum(0)  # indeterminate
        self.spinner.setMaximumWidth(160)
        self.spinner.hide()
        status.addWidget(self.status_label, 1)
        status.addWidget(self.spinner)
        layout.addLayout(status)

        self._apply_style()

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #1c1f26; color: #d6dae2;
                font-size: 13px; }
            QPushButton { background: #2a2f3a; border: 1px solid #3a414f;
                border-radius: 6px; padding: 6px 10px; color: #e2e6ee; }
            QPushButton:hover { background: #343b48; }
            QPushButton:pressed { background: #262b34; }
            QPushButton#danger { background: #5a2730; border-color: #7a323d; }
            QPushButton#danger:hover { background: #6e2f3a; }
            QLineEdit { background: #11141a; border: 1px solid #333a47;
                border-radius: 6px; padding: 6px 8px; color: #cfd4dd; }
            QTreeWidget { background: #11141a; border: 1px solid #2a2f3a;
                border-radius: 8px; outline: 0; }
            QTreeWidget::item { height: 27px; border: 0; }
            QTreeWidget::item:hover { background: #1a2030; }
            QTreeWidget::item:selected { background: #274a6b; color: #ffffff; }
            QHeaderView::section { background: #20252e; color: #9aa3b2;
                padding: 8px 8px; border: 0; border-right: 1px solid #2a2f3a;
                font-weight: 600; }
            QScrollBar:vertical { background: #11141a; width: 12px; margin: 0; }
            QScrollBar::handle:vertical { background: #303845;
                border-radius: 6px; min-height: 32px; }
            QScrollBar::handle:vertical:hover { background: #3c4757; }
            QScrollBar:horizontal { background: #11141a; height: 12px; margin: 0; }
            QScrollBar::handle:horizontal { background: #303845;
                border-radius: 6px; min-width: 32px; }
            QScrollBar::handle:horizontal:hover { background: #3c4757; }
            QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
            QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
            QProgressBar { background: #11141a; border: 1px solid #2a2f3a;
                border-radius: 6px; height: 14px; }
            QProgressBar::chunk { background: #3d6ea5; border-radius: 5px; }
            QLabel { color: #aab0bd; }
            QToolTip { background: #232833; color: #d6dae2; border: 1px solid #3a414f; }
        """)

    def _show_about(self):
        home = (f' &middot; <a style="color:#74b2f0;" href="{HOMEPAGE}">'
                f'project home</a>') if HOMEPAGE else ""
        box = QMessageBox(self)
        box.setWindowTitle("About DiskScope")
        box.setTextFormat(Qt.RichText)
        box.setIconPixmap(self._icon_dir.pixmap(56, 56))
        box.setText(f"""
<h2 style="margin-bottom:2px;">DiskScope <span style="color:#8a93a2;
   font-weight:normal;">{__version__}</span></h2>
<p style="color:#aab2c0;">A fast directory-size analyzer — see what is using
your disk and remove it safely, in one window.</p>

<p><b>Getting around</b></p>
<table cellpadding="3">
  <tr><td>Click&nbsp;the&nbsp;arrow&nbsp;/&nbsp;double-click</td>
      <td>&nbsp;&nbsp;expand a folder (sizes &amp; file counts inside)</td></tr>
  <tr><td><b>Ctrl&nbsp;+&nbsp;O</b></td><td>&nbsp;&nbsp;open another folder</td></tr>
  <tr><td><b>Backspace</b></td><td>&nbsp;&nbsp;go up to the parent folder</td></tr>
  <tr><td><b>F5</b></td><td>&nbsp;&nbsp;rescan the current folder from disk</td></tr>
  <tr><td><b>Delete</b></td><td>&nbsp;&nbsp;delete the selected items (asks first)</td></tr>
</table>

<p><b>Good to know</b></p>
<ul style="margin-top:0;">
  <li>The coloured bar shows each item's share of its parent — the biggest
      space users stand out.</li>
  <li>Results stay in memory, so re-opening or going Up is instant; press
      <b>F5</b> to re-read from disk.</li>
  <li>Deletion is <b>permanent</b> (not moved to Trash) and always confirms
      first. The scanned root folder itself can't be deleted.</li>
  <li>Only directories are kept in memory, so it scales to tens of millions
      of files.</li>
</ul>

<p style="color:#8a93a2;">MIT&nbsp;licensed.{home}</p>
""")
        box.setTextInteractionFlags(Qt.TextBrowserInteraction)
        box.setStandardButtons(QMessageBox.Close)
        box.exec_()

    # -- Scanning -----------------------------------------------------------

    def analyze(self, path):
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            QMessageBox.warning(self, "DiskScope", f"Not a directory:\n{path}")
            return
        self.root_path = path
        self.path_edit.setText(path)
        self._update_disk_usage()

        # Retire any in-flight scan WITHOUT blocking the GUI thread. The old
        # worker runs on until it notices the abort flag; its stale signals are
        # ignored by the sender guard in the slots, and _retire_worker disposes
        # of it once it has actually finished (so the QThread is never destroyed
        # while still running).
        if self.worker is not None and self.worker.isRunning():
            self.worker.abort()
        self.worker = None

        cached = PATH_CACHE.get(path)
        if cached is not None and cached.loaded:
            self.spinner.hide()
            self.btn_rescan.setEnabled(True)
            self.status_label.setText(f"Loaded from memory  •  {path}")
            self._populate_root(cached)
            return

        self.tree.clear()
        self.spinner.show()
        self.status_label.setText(f"Scanning {path} …")
        self.btn_rescan.setEnabled(False)
        worker = ScanWorker(path)
        self._workers.append(worker)
        worker.progress.connect(self._on_progress)
        worker.finished_scan.connect(self._on_scan_done)
        worker.finished.connect(lambda w=worker: self._retire_worker(w))
        self.worker = worker
        worker.start()

    def _retire_worker(self, w):
        # Called when a worker thread has actually finished. Clearing the
        # reference here (after finished_scan has already been delivered) keeps
        # self.worker from dangling at a deleted C++ object.
        if w in self._workers:
            self._workers.remove(w)
        if self.worker is w:
            self.worker = None
        w.deleteLater()

    def _on_progress(self, files, current):
        if self.sender() is not self.worker:
            return  # stale signal from a retired scan
        self.status_label.setText(f"Scanning…  {human_count(files)} files")

    def _on_scan_done(self, root):
        if self.sender() is not self.worker:
            return  # stale signal from a retired scan
        self.spinner.hide()
        self.btn_rescan.setEnabled(True)
        if root is None:
            self.status_label.setText("Scan aborted")
            return
        self._populate_root(root)
        self.status_label.setText(
            f"{human_size(root.size)}  in  {human_count(root.count)} files"
            f"  •  {root.path}")
        self._update_disk_usage()

    # -- Tree population (lazy) --------------------------------------------

    def _populate_root(self, root):
        self.root_node = root
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            item = TreeItem(root)
            self._fill_item(item, root, parent_size=root.size or 1)
            self.tree.addTopLevelItem(item)
            self._add_children_lazy(item, root)
        finally:
            self.tree.setUpdatesEnabled(True)
        item.setExpanded(True)

    def _fill_item(self, item, node, parent_size):
        frac = (node.size / parent_size) if parent_size else 0

        # Name: directories brighter + semibold, files dimmer.
        if node.error:
            item.setText(COL_NAME, node.name + "   (no access)")
            item.setForeground(COL_NAME, QColor(214, 130, 130))
            item.setFont(COL_NAME, self._font_file)
        elif node.is_dir:
            item.setText(COL_NAME, node.name)
            item.setForeground(COL_NAME, QColor(233, 237, 244))
            item.setFont(COL_NAME, self._font_dir)
        else:
            item.setText(COL_NAME, node.name)
            item.setForeground(COL_NAME, QColor(166, 175, 191))
            item.setFont(COL_NAME, self._font_file)
        item.setIcon(COL_NAME, self._icon_dir if node.is_dir else self._icon_file)

        # Size: monospace (aligned digits) + a warm tint for the heavy hitters.
        item.setText(COL_SIZE, human_size(node.size))
        item.setFont(COL_SIZE, self._mono)
        item.setForeground(COL_SIZE, self._size_color(frac))
        item.setTextAlignment(COL_SIZE, Qt.AlignRight | Qt.AlignVCenter)

        # Files count: monospace, directories only.
        item.setText(COL_FILES, human_count(node.count) if node.is_dir else "")
        item.setFont(COL_FILES, self._mono)
        item.setForeground(COL_FILES, QColor(138, 146, 160))
        item.setTextAlignment(COL_FILES, Qt.AlignRight | Qt.AlignVCenter)

        item.setData(COL_BAR, FRACTION_ROLE, frac)
        item.setToolTip(COL_NAME, node.path)

    @staticmethod
    def _size_color(frac):
        if frac >= 0.6:
            return QColor(0xEC, 0x9B, 0x8B)   # coral — dominant consumer
        if frac >= 0.25:
            return QColor(0xE3, 0xBE, 0x6E)   # amber — large
        return QColor(0xCB, 0xD2, 0xDC)       # neutral

    def _add_children_lazy(self, item, node):
        """Give a non-empty directory a placeholder so it shows an expand arrow.
        A directory is expandable if it has subdirectories or any files."""
        if node.is_dir and (node.subdirs or node.count > 0):
            ph = QTreeWidgetItem()
            ph.setData(COL_NAME, PLACEHOLDER_ROLE, True)
            item.addChild(ph)

    def _on_expanded(self, item):
        # Replace placeholder with real children on first expansion.
        if not (isinstance(item, TreeItem) and item.childCount() == 1
                and item.child(0).data(COL_NAME, PLACEHOLDER_ROLE)):
            return
        item.takeChild(0)
        node = item.node
        children, extra = list_children(node)
        self.tree.setUpdatesEnabled(False)
        try:
            psize = node.size or 1
            for child in children:
                ci = TreeItem(child)
                self._fill_item(ci, child, psize)
                self._add_children_lazy(ci, child)
                item.addChild(ci)
            if extra is not None:
                omitted_count, omitted_size = extra
                info = QTreeWidgetItem()
                info.setText(COL_NAME,
                             f"… {human_count(omitted_count)} more files")
                info.setText(COL_SIZE, human_size(omitted_size))
                info.setForeground(COL_NAME, QColor(140, 146, 158))
                info.setFlags(Qt.ItemIsEnabled)   # not selectable / deletable
                item.addChild(info)
        finally:
            self.tree.setUpdatesEnabled(True)

    # -- Navigation ---------------------------------------------------------

    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Choose directory to analyze",
                                             self.root_path)
        if d:
            self.analyze(d)

    def go_up(self):
        parent = os.path.dirname(self.root_path.rstrip("/")) or "/"
        if parent != self.root_path:
            self.analyze(parent)

    def rescan(self):
        # Drop this subtree from cache and scan fresh.
        self._invalidate_cache(self.root_path)
        self.analyze(self.root_path)

    def _path_entered(self):
        self.analyze(self.path_edit.text().strip())

    def _invalidate_cache(self, path):
        path = os.path.abspath(path)
        dead = [p for p in PATH_CACHE if p == path or p.startswith(path + os.sep)]
        for p in dead:
            del PATH_CACHE[p]

    # -- Disk usage ---------------------------------------------------------

    def _update_disk_usage(self):
        try:
            usage = shutil.disk_usage(self.root_path)
            mount = self._mount_point(self.root_path)
            self.disk_bar.set_usage(usage.total, usage.used, usage.free, mount)
        except OSError:
            self.disk_bar.set_usage(0, 0, 0, "")

    @staticmethod
    def _mount_point(path):
        path = os.path.abspath(path)
        while not os.path.ismount(path) and path != "/":
            path = os.path.dirname(path)
        return path

    # -- Deletion -----------------------------------------------------------

    def delete_selected(self):
        items = [it for it in self.tree.selectedItems()
                 if isinstance(it, TreeItem) and it.parent() is not None]
        if not items:
            QMessageBox.information(self, "DiskScope",
                                    "Select files or directories to delete "
                                    "(the analyzed root cannot be deleted).")
            return

        # Drop selections nested inside another selected directory: deleting the
        # parent already removes them, and counting both would double the size.
        sel_paths = [it.node.path for it in items]

        def _nested(p):
            return any(p != q and p.startswith(q.rstrip(os.sep) + os.sep)
                       for q in sel_paths)
        items = [it for it in items if not _nested(it.node.path)]

        total = sum(it.node.size for it in items)
        lines = "\n".join(f"  • {it.node.path}" for it in items[:12])
        if len(items) > 12:
            lines += f"\n  … and {len(items) - 12} more"
        msg = (f"Permanently delete {len(items)} item(s)?\n"
               f"Total size: {human_size(total)}\n\n{lines}\n\n"
               "This cannot be undone.")
        box = QMessageBox(self)
        box.setWindowTitle("Confirm delete")
        box.setIcon(QMessageBox.Warning)
        box.setText(msg)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec_() != QMessageBox.Yes:
            return

        errors = []
        freed = 0
        # Delete deepest paths first to avoid parent/child conflicts.
        for it in sorted(items, key=lambda x: len(x.node.path), reverse=True):
            node = it.node
            try:
                if node.is_dir and not os.path.islink(node.path):
                    shutil.rmtree(node.path)
                else:
                    os.remove(node.path)
            except OSError as e:
                errors.append(f"{node.path}: {e}")
                continue
            freed += node.size
            self._remove_node_from_model(it)

        self._update_disk_usage()
        self.status_label.setText(f"Freed {human_size(freed)}")
        if errors:
            QMessageBox.warning(self, "DiskScope",
                                "Some items could not be deleted:\n\n"
                                + "\n".join(errors[:10]))

    def _remove_node_from_model(self, item):
        node = item.node
        size, count = node.size, node.count

        # Walk up the ancestor chain, updating totals and share bars in memory
        # and on screen. `removed` unlinks the node from its immediate parent's
        # subdir list (files aren't in any list, so this is a no-op for them).
        removed = node
        anc_node = node.parent
        anc_item = item.parent()
        while anc_node is not None:
            anc_node.size -= size
            anc_node.count -= count
            if removed is not None and anc_node.subdirs \
                    and removed in anc_node.subdirs:
                anc_node.subdirs.remove(removed)
            removed = None
            if anc_item is not None and isinstance(anc_item, TreeItem):
                an = anc_item.node
                anc_item.setText(COL_SIZE, human_size(an.size))
                if an.is_dir:
                    anc_item.setText(COL_FILES, human_count(an.count))
                gp = an.parent
                if gp is not None and gp.size:
                    anc_item.setData(COL_BAR, FRACTION_ROLE, an.size / gp.size)
            anc_node = anc_node.parent
            anc_item = anc_item.parent() if anc_item is not None else None

        # Purge cache entries for the deleted subtree so a rescan re-reads disk.
        self._invalidate_cache(node.path)

        # Remove the widget item and refresh siblings' share bars.
        parent_item = item.parent()
        if parent_item is None:
            return
        parent_item.removeChild(item)
        if isinstance(parent_item, TreeItem):
            psize = parent_item.node.size or 1
            for i in range(parent_item.childCount()):
                ci = parent_item.child(i)
                if isinstance(ci, TreeItem):
                    ci.setData(COL_BAR, FRACTION_ROLE, ci.node.size / psize)


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~")
    app = QApplication(sys.argv)
    app.setApplicationName("DiskScope")
    # Use the system default UI font (just nudge the size) so the app does not
    # depend on a specific font being installed.
    font = app.font()
    font.setPointSize(10)
    app.setFont(font)
    win = MainWindow(start)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
