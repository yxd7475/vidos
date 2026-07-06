"""最近录制列表：显示、播放、定位、删除。"""
import os
import subprocess
from pathlib import Path
from typing import List

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QMenu, QMessageBox, QStyle, QFileDialog
)


class RecordingsPanel(QWidget):
    """最近录制文件列表。"""
    open_requested = Signal(Path)  # 双击或菜单"打开"
    open_folder_requested = Signal(Path)  # 菜单"打开所在文件夹"

    def __init__(self, output_dir: Path, parent=None):
        super().__init__(parent)
        self._output_dir = output_dir
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        head = QHBoxLayout()
        title = QLabel("最近录制")
        title.setStyleSheet("font-weight: bold; color: #2c3e50;")
        head.addWidget(title)
        head.addStretch()
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setFixedWidth(28)
        self.refresh_btn.setToolTip("刷新列表")
        self.refresh_btn.clicked.connect(self.refresh)
        head.addWidget(self.refresh_btn)
        self.open_dir_btn = QPushButton("📂")
        self.open_dir_btn.setFixedWidth(28)
        self.open_dir_btn.setToolTip("打开保存目录")
        self.open_dir_btn.clicked.connect(self._open_output_dir)
        head.addWidget(self.open_dir_btn)
        layout.addLayout(head)

        self.list_widget = QListWidget()
        self.list_widget.setMinimumHeight(120)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.list_widget.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.list_widget)

    def set_output_dir(self, path: Path) -> None:
        self._output_dir = path
        self.refresh()

    def refresh(self) -> None:
        """扫描输出目录，按修改时间倒序列出 mp4 文件。"""
        self.list_widget.clear()
        if not self._output_dir.exists():
            return
        files: List[Path] = []
        for ext in ("*.mp4", "*.mkv", "*.mov", "*.avi"):
            files.extend(self._output_dir.glob(ext))
        files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        # 限制最多 50 条，避免太多
        for p in files[:50]:
            try:
                size_mb = p.stat().st_size / (1024 * 1024)
                from datetime import datetime
                mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                continue
            item = QListWidgetItem(f"🎬 {p.name}  ({size_mb:.1f} MB, {mtime})")
            item.setData(Qt.UserRole, str(p))
            self.list_widget.addItem(item)

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        path = Path(item.data(Qt.UserRole))
        if path.exists():
            self.open_requested.emit(path)

    def _on_context_menu(self, pos) -> None:
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        path = Path(item.data(Qt.UserRole))
        menu = QMenu(self)
        act_open = QAction("播放", menu)
        act_open.triggered.connect(lambda: self.open_requested.emit(path))
        menu.addAction(act_open)
        act_folder = QAction("打开所在文件夹", menu)
        act_folder.triggered.connect(lambda: self.open_folder_requested.emit(path))
        menu.addAction(act_folder)
        menu.addSeparator()
        act_delete = QAction("删除…", menu)
        act_delete.triggered.connect(lambda: self._delete_file(path, item))
        menu.addAction(act_delete)
        menu.exec(self.list_widget.mapToGlobal(pos))

    def _delete_file(self, path: Path, item: QListWidgetItem) -> None:
        ret = QMessageBox.question(
            self, "删除录制",
            f"确定删除 {path.name} 吗？此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if ret == QMessageBox.Yes:
            try:
                path.unlink()
                self.list_widget.takeItem(self.list_widget.row(item))
            except Exception as e:
                QMessageBox.warning(self, "删除失败", str(e))

    def _open_output_dir(self) -> None:
        self.open_folder_requested.emit(self._output_dir)
