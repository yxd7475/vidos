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

from screenrec.ui.cursor_hint import CursorHintWindow


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
    tool_changed = Signal(str)  # 工具变化通知（让 Bar 同步按钮状态）
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
        self._redo_stack: List[dict] = []  # 重做栈
        self._current: Optional[dict] = None
        self._drawing = False
        self._click_feedback_enabled = True  # 点击反馈开关
        self._trail_enabled = False  # 鼠标轨迹开关
        self._selected_idx: Optional[int] = None  # 选中的标注索引
        self._drag_offset: Optional[QPoint] = None  # 拖动偏移
        self._dragged: bool = False  # 本次按下是否真的发生了拖动

        # 鼠标点击反馈动画：{_id: {pos, born}}
        self._clicks: List[dict] = []
        # 鼠标轨迹点：[{pos, born}]
        self._trail_points: List[dict] = []

        # 信号槽：把轮询线程的鼠标事件切到主线程
        self._mouse_event.connect(self._handle_mouse_event, Qt.QueuedConnection)

        # 顶部提示窗口：对屏幕捕获隐藏，不会出现在录屏里
        self._cursor_hint = CursorHintWindow(self)
        self.tool_changed.connect(self._refresh_cursor_hint)
        self.annotation_changed.connect(self._refresh_cursor_hint)

        # 用轮询替代钩子：在独立线程跑，不依赖 Qt 消息循环
        from screenrec.platform.mouse_poll import MousePoller
        self._poller = MousePoller(self._on_mouse_event, interval=0.005)
        self._poller.start()

        # 点击反馈动画定时器
        self._anim_timer = self.startTimer(50)

        # 初始提示
        self._refresh_cursor_hint()

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
        self.tool_changed.emit(tool)
        # 切换工具时取消选择
        self.deselect()

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

    def set_trail(self, enabled: bool) -> None:
        """开启/关闭鼠标轨迹高亮。"""
        self._trail_enabled = enabled
        if not enabled:
            self._trail_points.clear()
            self.update()
        # 同步调整鼠标轮询：开启轨迹时需要空闲移动事件
        try:
            self._poller.set_report_idle_moves(enabled)
        except Exception:
            pass

    def clear(self) -> None:
        """清空所有标注。不支持 undo 恢复（清空操作不可逆）。"""
        self.shapes = []
        self._redo_stack.clear()
        self._current = None
        self._selected_idx = None
        self._drag_offset = None
        self._dragged = False
        self.update()
        self.annotation_changed.emit()

    # --- 撤销 / 重做 ---

    def undo(self) -> None:
        """撤销最后一次标注。"""
        if not self.shapes:
            return
        last = self.shapes.pop()
        self._redo_stack.append(last)
        # 撤销后修正选中索引：若选中标注被移除，清空选中
        if self._selected_idx is not None and self._selected_idx >= len(self.shapes):
            self._selected_idx = None
            self._drag_offset = None
        self._current = None
        self.update()
        self.annotation_changed.emit()
        print(f"[OVERLAY] undo: shapes={len(self.shapes)} redo={len(self._redo_stack)}", flush=True)

    def redo(self) -> None:
        """重做最后一次撤销的标注。"""
        if not self._redo_stack:
            return
        shape = self._redo_stack.pop()
        self.shapes.append(shape)
        self.update()
        self.annotation_changed.emit()
        print(f"[OVERLAY] redo: shapes={len(self.shapes)} redo={len(self._redo_stack)}", flush=True)

    def can_undo(self) -> bool:
        return bool(self.shapes)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    @staticmethod
    def _is_shape_valid(shape: dict) -> bool:
        """检查标注是否有效（非零尺寸）。无效标注不推入 shapes。"""
        tool = shape.get("tool")
        if tool == "pen":
            return len(shape["points"]) >= 2
        elif tool in ("rect", "arrow"):
            return shape["start"] != shape["end"]
        elif tool == "text":
            return bool(shape.get("text"))
        return False

    # --- 选择与拖动 ---

    def delete_selected(self) -> None:
        """删除当前选中的标注。"""
        if self._selected_idx is None:
            return
        if 0 <= self._selected_idx < len(self.shapes):
            removed = self.shapes.pop(self._selected_idx)
            self._redo_stack.append(removed)
            self._selected_idx = None
            self._drag_offset = None
            self._dragged = False
            self.update()
            self.annotation_changed.emit()

    def deselect(self) -> None:
        """取消选择。"""
        if self._selected_idx is not None:
            self._selected_idx = None
            self._drag_offset = None
            self.update()
            self._refresh_cursor_hint()

    @staticmethod
    def _point_distance(p1: QPoint, p2: QPoint) -> float:
        return math.hypot(p1.x() - p2.x(), p1.y() - p2.y())

    @staticmethod
    def _point_to_segment_distance(p: QPoint, a: QPoint, b: QPoint) -> float:
        """点到线段的距离。"""
        dx, dy = b.x() - a.x(), b.y() - a.y()
        if dx == 0 and dy == 0:
            return AnnotationOverlay._point_distance(p, a)
        t = ((p.x() - a.x()) * dx + (p.y() - a.y()) * dy) / (dx * dx + dy * dy)
        t = max(0, min(1, t))
        proj = QPoint(int(a.x() + t * dx), int(a.y() + t * dy))
        return AnnotationOverlay._point_distance(p, proj)

    def _hit_test(self, pos: QPoint) -> Optional[int]:
        """命中测试：返回最上层的标注索引，没命中返回 None。"""
        threshold = max(8, self.line_width + 4)
        # 从后往前（上层优先）
        for i in range(len(self.shapes) - 1, -1, -1):
            shape = self.shapes[i]
            if self._shape_contains(shape, pos, threshold):
                return i
        return None

    def _shape_contains(self, shape: dict, pos: QPoint, threshold: int) -> bool:
        tool = shape["tool"]
        if tool == "pen":
            pts = shape["points"]
            for i in range(len(pts) - 1):
                if self._point_to_segment_distance(pos, pts[i], pts[i + 1]) <= threshold:
                    return True
            # 单点
            if len(pts) == 1 and self._point_distance(pos, pts[0]) <= threshold:
                return True
            return False
        elif tool == "rect":
            r = QRect(shape["start"], shape["end"]).normalized()
            # 点在矩形内或边框附近都可选中
            if r.contains(pos):
                return True
            left = abs(pos.x() - r.left()) <= threshold and r.top() <= pos.y() <= r.bottom()
            right = abs(pos.x() - r.right()) <= threshold and r.top() <= pos.y() <= r.bottom()
            top = abs(pos.y() - r.top()) <= threshold and r.left() <= pos.x() <= r.right()
            bottom = abs(pos.y() - r.bottom()) <= threshold and r.left() <= pos.x() <= r.right()
            return left or right or top or bottom
        elif tool == "arrow":
            return self._point_to_segment_distance(pos, shape["start"], shape["end"]) <= threshold
        elif tool == "text":
            # 简化：以 pos 为中心的矩形
            text = shape["text"]
            w = max(20, len(text) * 10)
            h = 24
            r = QRect(shape["pos"].x(), shape["pos"].y() - h, w, h + 4)
            return r.contains(pos)
        return False

    @staticmethod
    def _shape_anchor(shape: dict) -> QPoint:
        """获取形状的锚点（拖动时用作基准点）。"""
        tool = shape["tool"]
        if tool == "pen":
            return shape["points"][0]
        elif tool in ("rect", "arrow"):
            return shape["start"]
        elif tool == "text":
            return shape["pos"]
        return QPoint(0, 0)

    @staticmethod
    def _move_shape_to(shape: dict, new_anchor: QPoint) -> None:
        """把形状移动到新锚点。"""
        tool = shape["tool"]
        if tool == "pen":
            old = shape["points"][0]
            dx = new_anchor.x() - old.x()
            dy = new_anchor.y() - old.y()
            shape["points"] = [QPoint(p.x() + dx, p.y() + dy) for p in shape["points"]]
        elif tool in ("rect", "arrow"):
            old = shape["start"]
            dx = new_anchor.x() - old.x()
            dy = new_anchor.y() - old.y()
            shape["start"] = QPoint(old.x() + dx, old.y() + dy)
            shape["end"] = QPoint(shape["end"].x() + dx, shape["end"].y() + dy)
        elif tool == "text":
            shape["pos"] = QPoint(new_anchor)

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
            # 橡皮：删除最后一个标注（等价于撤销，可通过 Ctrl+Y 恢复）
            if self.tool == "eraser":
                self.undo()
                return
            # cursor 工具：选择/拖动已有标注
            if self.tool == "cursor":
                idx = self._hit_test(pos)
                if idx is not None:
                    self._selected_idx = idx
                    shape = self.shapes[idx]
                    self._drag_offset = pos - self._shape_anchor(shape)
                    self._dragged = False
                else:
                    self._selected_idx = None
                self.update()
                self._refresh_cursor_hint()
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
            # 轨迹收集：未绘制时也要记录（用于鼠标轨迹效果）
            if self._trail_enabled and not self._drawing:
                self._trail_points.append({"pos": pos, "born": _now_ms()})
            # 拖动选中的标注
            if self.tool == "cursor" and self._selected_idx is not None and self._drag_offset is not None:
                shape = self.shapes[self._selected_idx]
                anchor = pos - self._drag_offset
                self._move_shape_to(shape, anchor)
                self._dragged = True
                self.update()
                return
            if not self._drawing or not self._current:
                return
            if self._current["tool"] == "pen":
                self._current["points"].append(pos)
            elif self._current["tool"] in ("rect", "arrow"):
                self._current["end"] = pos
            self.update()

        elif event == "up":
            # 结束拖动
            if self._drag_offset is not None:
                # 只有真正拖动了才清空 redo 栈（标准 undo/redo 行为）
                if self._dragged:
                    self._redo_stack.clear()
                    self.annotation_changed.emit()
                    print(f"[OVERLAY] drag done: shapes={len(self.shapes)} redo cleared", flush=True)
                self._drag_offset = None
                self._dragged = False
                return
            if not self._drawing:
                return
            self._drawing = False
            if self._current:
                # 只提交有效标注（非零尺寸），避免误点产生看不见的标注
                if self._is_shape_valid(self._current):
                    self.shapes.append(self._current)
                    # 新标注提交后清空重做栈（标准 undo/redo 行为）
                    self._redo_stack.clear()
                    self.annotation_changed.emit()
                    print(f"[OVERLAY] shape committed: tool={self._current['tool']} shapes={len(self.shapes)}", flush=True)
                else:
                    print(f"[OVERLAY] shape discarded (invalid): tool={self._current.get('tool')}", flush=True)
                self._current = None
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
        edit.setAttribute(Qt.WA_DeleteOnClose, True)
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
                self._redo_stack.clear()
                self.update()
                self.annotation_changed.emit()
            edit.close()
            self._restart_poller()

        def cancel():
            if self._text_confirmed:
                return
            self._text_confirmed = True
            edit.close()
            self._restart_poller()
            # Esc 取消后切回 cursor 工具，避免再点屏幕又出输入框
            self.set_tool("cursor")

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
        # 失焦自动取消
        edit.editingFinished.connect(cancel)

    def _restart_poller(self) -> None:
        """重启鼠标轮询，保留 trail 设置。

        在文字输入完成后调用，确保后续标注工具能继续工作。
        """
        from screenrec.platform.mouse_poll import MousePoller, is_left_button_down
        import time as _time
        # 等待鼠标松开，避免文字提交时的点击被重新捕获
        deadline = _time.perf_counter() + 1.0
        while is_left_button_down() and _time.perf_counter() < deadline:
            _time.sleep(0.01)
        try:
            self._poller = MousePoller(
                self._on_mouse_event, interval=0.005,
                report_idle_moves=self._trail_enabled,
            )
            self._poller.start()
            print("[OVERLAY] poller restarted", flush=True)
        except Exception as e:
            print(f"[OVERLAY] failed to restart poller: {e}", flush=True)
        self._text_edit = None
        self._esc_filter = None

    # --- 点击反馈动画 ---

    def timerEvent(self, event) -> None:
        now = _now_ms()
        # 点击反馈持续 600ms（ripple 动画）
        self._clicks = [c for c in self._clicks if now - c["born"] < 600]
        # 鼠标轨迹点保留 500ms
        if self._trail_points:
            self._trail_points = [p for p in self._trail_points if now - p["born"] < 500]
        if self._clicks or self._trail_points:
            self.update()

    # --- 绘制 ---

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # 画已完成的标注 + 当前正在画的标注
        all_shapes = list(self.shapes)
        if self._current:
            all_shapes.append(self._current)
        for i, shape in enumerate(all_shapes):
            self._draw_shape(painter, shape)
            # 选中高亮
            if i == self._selected_idx and not self._current:
                self._draw_selection(painter, shape)
        # 画鼠标轨迹（在标注之上、点击反馈之下）
        self._draw_trail(painter)
        # 画点击反馈（ripple 动画）
        now = _now_ms()
        for c in self._clicks:
            age = now - c["born"]
            t = age / 600  # 0..1
            cx, cy = c["pos"].x(), c["pos"].y()
            # 中心圆点：快速出现并淡出
            dot_alpha = int(220 * (1 - t))
            if dot_alpha > 0:
                dot_r = int(8 * (1 - t * 0.5))
                painter.setBrush(QColor(231, 76, 60, dot_alpha))
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(c["pos"], dot_r, dot_r)
            # 扩散环
            ring_r = int(8 + 40 * t)
            ring_alpha = int(200 * (1 - t))
            if ring_alpha > 0 and ring_r > 0:
                pen = QPen(QColor(231, 76, 60, ring_alpha), 3)
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(c["pos"], ring_r, ring_r)

    def _refresh_cursor_hint(self, *args) -> None:
        """更新顶部提示窗口的文本。

        提示文字通过 CursorHintWindow 显示——它对屏幕捕获隐藏，
        不会出现在录屏里。tool/shapes/selected_idx 变化时调用。
        """
        if self.tool != "cursor":
            self._cursor_hint.set_text("")
            return
        if self._selected_idx is not None:
            text = "已选中标注 · 拖动移动 · Delete 删除 · Esc 取消"
        elif self.shapes:
            text = "选择模式 · 点击标注选中 · 拖动移动 · Delete 删除"
        else:
            text = "选择模式 · 还没有标注，先切换到画笔/矩形/箭头/文字工具绘制"
        self._cursor_hint.set_text(text)

    def _draw_selection(self, painter: QPainter, shape: dict) -> None:
        """在选中的标注周围画虚线框 + 角点。"""
        bbox = self._shape_bbox(shape)
        if bbox is None:
            return
        pen = QPen(QColor(52, 152, 219, 220), 1, Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(bbox.adjusted(-4, -4, 4, 4))
        # 角点
        painter.setBrush(QColor(52, 152, 219, 220))
        painter.setPen(Qt.NoPen)
        for corner in [
            bbox.topLeft(), bbox.topRight(), bbox.bottomLeft(), bbox.bottomRight()
        ]:
            painter.drawRect(QRect(corner.x() - 4, corner.y() - 4, 8, 8))

    @staticmethod
    def _shape_bbox(shape: dict) -> Optional[QRect]:
        tool = shape["tool"]
        if tool == "pen":
            pts = shape["points"]
            if not pts:
                return None
            xs = [p.x() for p in pts]
            ys = [p.y() for p in pts]
            return QRect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
        elif tool in ("rect", "arrow"):
            return QRect(shape["start"], shape["end"]).normalized()
        elif tool == "text":
            text = shape["text"]
            w = max(20, len(text) * 10)
            h = 24
            return QRect(shape["pos"].x(), shape["pos"].y() - h, w, h + 4)
        return None

    def _draw_trail(self, painter: QPainter) -> None:
        """画鼠标轨迹：渐变淡出的折线 + 圆点。"""
        if not self._trail_points:
            return
        now = _now_ms()
        # 用渐变线段画轨迹
        from PySide6.QtGui import QLinearGradient, QBrush
        n = len(self._trail_points)
        if n < 2:
            # 只有一个点：画一个小圆
            p = self._trail_points[0]
            age = now - p["born"]
            t = age / 500
            alpha = int(180 * (1 - t))
            if alpha > 0:
                painter.setBrush(QColor(231, 76, 60, alpha))
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(p["pos"], 4, 4)
            return
        # 画连续线段，每段透明度按年龄递减
        for i in range(1, n):
            p0 = self._trail_points[i - 1]
            p1 = self._trail_points[i]
            age = now - p1["born"]
            t = age / 500
            alpha = int(180 * (1 - t))
            if alpha <= 0:
                continue
            pen = QPen(QColor(231, 76, 60, alpha), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(p0["pos"], p1["pos"])
        # 末端画一个亮点
        last = self._trail_points[-1]
        age = now - last["born"]
        t = age / 500
        alpha = int(220 * (1 - t))
        if alpha > 0:
            painter.setBrush(QColor(231, 76, 60, alpha))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(last["pos"], 5, 5)

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
