"""光标工具提示窗口：显示在屏幕顶部中央，对屏幕捕获隐藏。

AnnotationOverlay 画的所有内容都会被录进视频（标注本来就要被录到），
但工具状态提示文字不应该出现在视频里。把提示拆到这个独立窗口，
调用 WDA_EXCLUDEFROMCAPTURE 让它对屏幕捕获隐身——用户视觉能看到，
但 DXGI Desktop Duplication / GDI / Graphics Capture 都录不到。
"""
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QLabel

from screenrec.platform.win32 import set_exclude_from_capture


class CursorHintWindow(QLabel):
    """屏幕顶部中央的状态提示条，对屏幕捕获隐藏。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)

        self.setStyleSheet("""
            QLabel {
                background: rgba(44, 62, 80, 220);
                color: white;
                padding: 6px 14px;
                border-radius: 6px;
                font-size: 12px;
            }
        """)
        self.setFont(QFont("Microsoft YaHei", 10))
        self.hide()

    def set_text(self, text: str) -> None:
        """更新提示文字。空字符串则隐藏。"""
        if not text:
            self.hide()
            return
        self.setText(text)
        self.adjustSize()
        screen = QGuiApplication.primaryScreen().geometry()
        x = (screen.width() - self.width()) // 2
        self.move(x, 12)
        if not self.isVisible():
            self.show()
        # 确保浮在 AnnotationOverlay 之上
        self.raise_()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        set_exclude_from_capture(self.winId(), exclude=True)
