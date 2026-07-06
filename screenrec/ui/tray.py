"""系统托盘：最小化到托盘、右键菜单、消息通知。"""
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction
from PySide6.QtWidgets import QSystemTrayIcon, QMenu

from screenrec.platform.win32 import set_exclude_from_capture


def _make_icon(color: str = "#e74c3c") -> QIcon:
    """程序化生成一个简单的圆形录制图标。"""
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    # 外圈深色
    p.setPen(QColor("#2c3e50"))
    p.setBrush(QColor("#2c3e50"))
    p.drawEllipse(2, 2, 60, 60)
    # 内圈录制色
    p.setPen(QColor(color))
    p.setBrush(QColor(color))
    p.drawEllipse(8, 8, 48, 48)
    p.end()
    return QIcon(pm)


class TrayController(QObject):
    """管理托盘图标和右键菜单。

    把托盘操作转发给 MainWindow，避免直接引用以减少耦合。
    """
    show_window_requested = Signal()
    start_stop_requested = Signal()
    pause_resume_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._icon_recording = _make_icon("#e74c3c")
        self._icon_idle = _make_icon("#7f8c8d")
        self._icon_paused = _make_icon("#f39c12")

        self.tray = QSystemTrayIcon(self._icon_idle, parent)
        self.tray.setToolTip("ScreenRec - 就绪")

        menu = QMenu()
        self._act_show = QAction("显示主窗口", menu)
        self._act_show.triggered.connect(self.show_window_requested.emit)
        menu.addAction(self._act_show)

        menu.addSeparator()

        self._act_start_stop = QAction("● 开始录制", menu)
        self._act_start_stop.triggered.connect(self.start_stop_requested.emit)
        menu.addAction(self._act_start_stop)

        self._act_pause = QAction("⏸ 暂停", menu)
        self._act_pause.triggered.connect(self.pause_resume_requested.emit)
        menu.addAction(self._act_pause)

        menu.addSeparator()

        self._act_quit = QAction("退出 ScreenRec", menu)
        self._act_quit.triggered.connect(self.quit_requested.emit)
        menu.addAction(self._act_quit)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)

    def show(self) -> None:
        self.tray.show()

    def _on_activated(self, reason) -> None:
        # 双击/单击托盘图标：显示主窗口
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.show_window_requested.emit()

    def update_state(self, state: str) -> None:
        """根据录制状态更新图标和菜单文案。"""
        if state == "recording":
            self.tray.setIcon(self._icon_recording)
            self.tray.setToolTip("ScreenRec - 录制中")
            self._act_start_stop.setText("■ 停止录制")
            self._act_pause.setEnabled(True)
            self._act_pause.setText("⏸ 暂停")
        elif state == "paused":
            self.tray.setIcon(self._icon_paused)
            self.tray.setToolTip("ScreenRec - 已暂停")
            self._act_start_stop.setText("■ 停止录制")
            self._act_pause.setEnabled(True)
            self._act_pause.setText("▶ 继续")
        else:
            self.tray.setIcon(self._icon_idle)
            self.tray.setToolTip("ScreenRec - 就绪")
            self._act_start_stop.setText("● 开始录制")
            self._act_pause.setEnabled(False)
            self._act_pause.setText("⏸ 暂停")

    def notify(self, title: str, message: str, ms: int = 3000) -> None:
        """显示托盘消息。"""
        if self.tray.supportsMessages():
            self.tray.showMessage(title, message, QSystemTrayIcon.Information, ms)
