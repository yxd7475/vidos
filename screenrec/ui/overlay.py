"""全屏透明覆盖层。

两类：
- RegionSelectorOverlay：拖框选录制区域
- AnnotationOverlay：录制中画标注（画笔/矩形/箭头/文字/橡皮）

透明覆盖层画的内容会被 DXGI Desktop Duplication 捕获到录制画面里，
所以标注自然出现在最终视频上。

Windows 上运行时切换 WS_EX_TRANSPARENT / WA_TransparentForMouseEvents
都不可靠（hit-test 行为被缓存）。改用 hide()/show() 切换：
cursor 模式直接隐藏 overlay 让用户操作下层窗口，画标注时再显示。
已绘制的 shapes 不会丢失。
"""
import math
import time
from typing import List, Optional

from PySide6.QtCore import Qt, QPoint, QRect, Signal
from PySide6.QtGui import QPainter, QPen, QColor, QFont, QPainterPath, QGuiApplication
from PySide6.QtWidgets import QWidget


def _now_ms() -> int:
    return int(time.perf_counter() * 1000)


class RegionSelectorOverlay(QWidget):
    """全屏暗化，用户拖框选区域，回车/双击确认，Esc 取消。"""

    region_selected = Signal(QRect)
    cancelled = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        screen = QGuiApplication.primaryScreen().geometry()
        self.setGeometry(screen)

        self._start = QPoint()
        self._end = QPoint()
        self._selecting = False
        self._final_rect: Optional[QRect] = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._start = event.position().toPoint()
            self._end = self._start
            self._selecting = True
            self.update()

    def mouseMoveEvent(self, event):
        if self._selecting:
            self._end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if self._selecting and event.button() == Qt.LeftButton:
            self._selecting = False
            rect = QRect(self._start, self._end).normalized()
            if rect.width() > 10 and rect.height() > 10:
                self._final_rect = rect
                self.region_selected.emit(rect)
                self.close()
            else:
                self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
            self.close()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))
        if self._selecting:
            rect = QRect(self._start, self._end).normalized()
            # 选区"挖空"
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(rect, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            pen = QPen(QColor(231, 76, 60), 2)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(rect.topLeft() + QPoint(8, -8),
                             f"{rect.width()} x {rect.height()}")


class AnnotationOverlay(QWidget):
    """录制中的标注画布。

    全屏透明窗口，专门用来"显示"标注。鼠标事件不靠 Qt 接收，
    而是用 Win32 全局低级鼠标钩子（WH_MOUSE_LL）在任何位置
    捕获鼠标按下/拖动/松开，再转成标注。

    这样：
    - overlay 始终保持鼠标穿透，用户能正常操作下层窗口
    - 鼠标光标正常显示，不隐藏
    - 鼠标按下时在点击位置立即给出视觉反馈（一个小圆点）
    - 工具栏选了画笔/矩形/箭头后，任何位置的拖动都能画标注
    """

    annotation_changed = Signal()
    # 鼠标事件信号：把轮询线程的事件切到主线程处理
    _mouse_event = Signal(str, int, int)

    TOOLS = ("cursor", "pen", "rect", "arrow", "text", "eraser")

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 始终鼠标穿透：用户能操作下层窗口
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        screen = QGuiApplication.primaryScreen().geometry()
        self.setGeometry(screen)

        self.tool = "cursor"
        self.color = QColor(231, 76, 60, 230)
        self.line_width = 4
        self.shapes: List[dict] = []
        self._current: Optional[dict] = None
        self._drawing = False
        self._click_feedback_enabled = True  # 点击反馈开关

        # 鼠标点击反馈动画：{_id: {pos, born}}
        self._clicks: List[dict] = []

        # 信号槽：把轮询线程的鼠标事件切到主线程
        self._mouse_event.connect(self._handle_mouse_event, Qt.QueuedConnection)

        # 用轮询替代钩子：在独立线程跑，不依赖 Qt 消息循环
        from screenrec.platform.mouse_poll import MousePoller
        self._poller = MousePoller(self._on_mouse_event, interval=0.005)
        self._poller.start()

        # 点击反馈动画定时器
        self._anim_timer = self.startTimer(50)

    def __del__(self):
        try:
            if hasattr(self, "_poller"):
                self._poller.stop()
        except Exception:
            pass

    # --- 工具切换 ---

    def set_tool(self, tool: str) -> None:
        if tool not in self.TOOLS:
            return
        print(f"[OVERLAY] set_tool: {tool}", flush=True)
        self.tool = tool

    def set_color(self, color: QColor) -> None:
        self.color = color

    def set_line_width(self, w: int) -> None:
        self.line_width = w

    def set_click_feedback(self, enabled: bool) -> None:
        """开启/关闭点击反馈。"""
        self._click_feedback_enabled = enabled
        if not enabled:
            self._clicks.clear()
            self.update()

    def clear(self) -> None:
        self.shapes = []
        self._current = None
        self.update()
        self.annotation_changed.emit()

    # --- 鼠标轮询回调 ---

    def _on_mouse_event(self, event: str, x: int, y: int) -> None:
        """鼠标轮询回调，在轮询线程中调用。

        通过信号把事件切到主线程处理，避免和 paintEvent 数据竞争。
        """
        # 直接 emit 信号，主线程会调用 _handle_mouse_event
        self._mouse_event.emit(event, x, y)

    def _handle_mouse_event(self, event: str, x: int, y: int) -> None:
        """在主线程处理鼠标事件。

        event: 'down' / 'up' / 'move'
        x, y: 屏幕坐标
        """
        # 把屏幕坐标转成 overlay 坐标（overlay 全屏，左上角 = 屏幕左上角）
        pos = QPoint(x, y)

        if event == "down":
            print(f"[OVERLAY] mouse DOWN at ({x},{y}) tool={self.tool}", flush=True)
            # 点击反馈：在反馈开启时任何工具都画一个圆点
            if self._click_feedback_enabled:
                self._clicks.append({"pos": pos, "born": _now_ms()})
            # 文字工具：弹输入框
            if self.tool == "text":
                self._do_text_input(pos)
                return
            # 橡皮：删除最后一个标注
            if self.tool == "eraser":
                if self.shapes:
                    self.shapes.pop()
                    self.update()
                    self.annotation_changed.emit()
                return
            # cursor 工具：不画标注
            if self.tool == "cursor":
                self.update()
                return
            # pen / rect / arrow：开始绘制
            self._drawing = True
            if self.tool == "pen":
                self._current = {"tool": "pen", "color": QColor(self.color),
                                 "width": self.line_width, "points": [pos]}
            elif self.tool == "rect":
                self._current = {"tool": "rect", "color": QColor(self.color),
                                 "width": self.line_width, "start": pos, "end": pos}
            elif self.tool == "arrow":
                self._current = {"tool": "arrow", "color": QColor(self.color),
                                 "width": self.line_width, "start": pos, "end": pos}
            self.update()

        elif event == "move":
            if not self._drawing or not self._current:
                return
            if self._current["tool"] == "pen":
                self._current["points"].append(pos)
            elif self._current["tool"] in ("rect", "arrow"):
                self._current["end"] = pos
            self.update()

        elif event == "up":
            print(f"[OVERLAY] mouse UP at ({x},{y}) drawing={self._drawing}", flush=True)
            if not self._drawing:
                return
            self._drawing = False
            if self._current:
                self.shapes.append(self._current)
                self._current = None
                self.annotation_changed.emit()
            self.update()

    def _do_text_input(self, pos: QPoint) -> None:
        """文字工具：在点击位置显示内联文本框，避免模态对话框循环弹窗。

        Enter 确认 / Esc 取消 / 失焦自动取消
        """
        from PySide6.QtWidgets import QLineEdit
        from PySide6.QtCore import Qt as QtConst, QObject, QEvent

        # 如果已有打开的输入框，先关掉
        if hasattr(self, "_text_edit") and self._text_edit is not None:
            self._text_edit.close()
            self._text_edit = None

        # 暂停鼠标轮询，避免输入框交互被捕获
        self._poller.stop()

        edit = QLineEdit(self)
        # overlay 是 WA_TransparentForMouseEvents，子控件需要单独取消穿透
        edit.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        edit.setPlaceholderText("输入文字 (Enter确认 / Esc取消)")
        edit.setStyleSheet("""
            QLineEdit {
                background: white;
                color: black;
                border: 2px solid #e74c3c;
                padding: 4px 8px;
                font-size: 14px;
                min-width: 200px;
            }
        """)
        edit.move(pos)
        edit.show()
        edit.setFocus()
        edit.activateWindow()

        self._text_edit = edit
        self._text_pos = pos
        self._text_confirmed = False

        def commit():
            if self._text_confirmed:
                return
            self._text_confirmed = True
            text = edit.text().strip()
            if text:
                self.shapes.append({
                    "tool": "text", "color": QColor(self.color),
                    "pos": pos, "text": text,
                })
                self.update()
                self.annotation_changed.emit()
            edit.close()

        def cancel():
            if self._text_confirmed:
                return
            self._text_confirmed = True
            edit.close()

        def on_destroyed(*args):
            # 输入框关闭后等待鼠标松开，再恢复轮询，避免误触发
            from screenrec.platform.mouse_poll import MousePoller, is_left_button_down
            import time as _time
            deadline = _time.perf_counter() + 2.0
            while is_left_button_down() and _time.perf_counter() < deadline:
                _time.sleep(0.01)
            self._poller = MousePoller(self._on_mouse_event, interval=0.005)
            self._poller.start()
            self._text_edit = None
            self._esc_filter = None

        # Esc 键事件过滤器
        class EscapeFilter(QObject):
            def eventFilter(self, obj, event):
                if event.type() == QEvent.KeyPress and event.key() == QtConst.Key_Escape:
                    cancel()
                    return True
                return False

        self._esc_filter = EscapeFilter(edit)
        edit.installEventFilter(self._esc_filter)

        edit.returnPressed.connect(commit)
        # 关闭时恢复轮询
        edit.destroyed.connect(on_destroyed)

    # --- 点击反馈动画 ---

    def timerEvent(self, event) -> None:
        if not self._clicks:
            return
        now = _now_ms()
        # 点击反馈持续 500ms
        self._clicks = [c for c in self._clicks if now - c["born"] < 500]
        self.update()

    # --- 绘制 ---

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # 画已完成的标注 + 当前正在画的标注
        all_shapes = list(self.shapes)
        if self._current:
            all_shapes.append(self._current)
        for shape in all_shapes:
            self._draw_shape(painter, shape)
        # 画点击反馈
        now = _now_ms()
        for c in self._clicks:
            age = now - c["born"]
            # 0ms: 大圆 r=18, alpha=200
            # 500ms: 小圆 r=4, alpha=0
            t = age / 500  # 0..1
            r = int(18 - 14 * t)
            alpha = int(200 * (1 - t))
            if r > 0 and alpha > 0:
                painter.setBrush(QColor(231, 76, 60, alpha))
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(c["pos"], r, r)

    def _draw_shape(self, painter: QPainter, shape: dict) -> None:
        tool = shape["tool"]
        color = shape.get("color", self.color)
        width = shape.get("width", self.line_width)
        pen = QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        if tool == "pen":
            pts = shape["points"]
            if len(pts) >= 2:
                path = QPainterPath(pts[0])
                for p in pts[1:]:
                    path.lineTo(p)
                painter.drawPath(path)
            elif len(pts) == 1:
                painter.drawPoint(pts[0])
        elif tool == "rect":
            r = QRect(shape["start"], shape["end"]).normalized()
            painter.drawRect(r)
        elif tool == "arrow":
            self._draw_arrow(painter, shape["start"], shape["end"], color, width)
        elif tool == "text":
            painter.setPen(QPen(color))
            painter.setFont(QFont("Arial", 16))
            painter.drawText(shape["pos"], shape["text"])

    @staticmethod
    def _draw_arrow(painter, start: QPoint, end: QPoint, color: QColor, width: int) -> None:
        painter.drawLine(start, end)
        angle = math.atan2(end.y() - start.y(), end.x() - start.x())
        arrow_len = max(12, width * 3)
        arrow_angle = math.pi / 6
        p1 = QPoint(
            end.x() - int(arrow_len * math.cos(angle - arrow_angle)),
            end.y() - int(arrow_len * math.sin(angle - arrow_angle)),
        )
        p2 = QPoint(
            end.x() - int(arrow_len * math.cos(angle + arrow_angle)),
            end.y() - int(arrow_len * math.sin(angle + arrow_angle)),
        )
        painter.drawLine(end, p1)
        painter.drawLine(end, p2)
