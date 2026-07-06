"""摄像头画中画浮窗：可拖动、可缩放、圆形或方形。"""
from typing import Optional

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer, QPoint, QRect, Signal
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QBitmap, QRegion, QPen, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QWidget, QSizePolicy

from screenrec.capture.camera import CameraCapture


class CameraPiP(QWidget):
    """摄像头画中画浮窗。

    - 默认圆形，可切换方形
    - 鼠标拖动移动
    - 滚轮缩放
    - 双击切换圆/方
    """

    def __init__(self, camera_index: int = 0, size: int = 200, parent=None):
        super().__init__(parent)
        self._size = size
        self._circular = True
        self._drag_offset: Optional[QPoint] = None

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(80, 80)
        self.resize(size, size)

        self._camera = CameraCapture(camera_index=camera_index, fps=30)
        self._camera.set_callback(self._on_frame)
        self._ok = self._camera.start()

        # 定时重绘
        self._timer = QTimer(self)
        self._timer.setInterval(33)  # ~30fps
        self._timer.timeout.connect(self.update)
        self._timer.start()

        self._latest_pixmap: Optional[QPixmap] = None

    @property
    def started_ok(self) -> bool:
        return self._ok

    def _on_frame(self, bgr: np.ndarray) -> None:
        # BGR -> RGB
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        h, w, _ = rgb.shape
        qimg = QImage(rgb.data, w, h, w * 3, QImage.Format_RGB888).copy()
        pm = QPixmap.fromImage(qimg)
        # 缩放到当前窗口大小（保留比例，裁剪填满）
        target = self.size()
        scaled = pm.scaled(target, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        # 居中裁剪
        x = (scaled.width() - target.width()) // 2
        y = (scaled.height() - target.height()) // 2
        self._latest_pixmap = scaled.copy(QRect(x, y, target.width(), target.height()))

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        if self._latest_pixmap is not None:
            if self._circular:
                # 圆形剪裁
                path_clip = QPainter(self)
                from PySide6.QtGui import QPainterPath
                clip = QPainterPath()
                clip.addEllipse(1, 1, w - 2, h - 2)
                p.setClipPath(clip)
                p.drawPixmap(0, 0, self._latest_pixmap)
                # 边框
                p.setClipPath(QPainterPath())
                pen = QPen(QColor(255, 255, 255, 220), 3)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(1, 1, w - 2, h - 2)
            else:
                p.drawPixmap(0, 0, self._latest_pixmap)
                pen = QPen(QColor(255, 255, 255, 220), 2)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawRect(1, 1, w - 2, h - 2)
        else:
            # 占位
            p.setBrush(QColor(0, 0, 0, 180))
            p.setPen(Qt.NoPen)
            if self._circular:
                p.drawEllipse(1, 1, w - 2, h - 2)
            else:
                p.drawRect(1, 1, w - 2, h - 2)
            p.setPen(QColor(255, 255, 255, 200))
            p.drawText(self.rect(), Qt.AlignCenter, "📷\n无信号" if not self._ok else "📷\n加载中…")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        # 切换圆/方
        self._circular = not self._circular
        self.update()
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:
        # 滚轮缩放
        delta = event.angleDelta().y() // 120
        new_size = self._size + delta * 16
        new_size = max(80, min(640, new_size))
        if new_size != self._size:
            self._size = new_size
            # 保持中心点不变
            cx = self.geometry().center().x()
            cy = self.geometry().center().y()
            self.resize(new_size, new_size)
            self.move(cx - new_size // 2, cy - new_size // 2)
            self._latest_pixmap = None  # 触发重绘
        event.accept()

    def closeEvent(self, event) -> None:
        try:
            self._timer.stop()
            self._camera.stop()
        except Exception:
            pass
        super().closeEvent(event)
