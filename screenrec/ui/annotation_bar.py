"""标注工具栏：独立小窗口，浮在录制画面上方。

工具切换 -> 控制 AnnotationOverlay 的工具和模式
"""
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QColor, QIcon, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QPushButton, QButtonGroup,
    QColorDialog, QSpinBox, QLabel, QFrame, QToolButton
)
from typing import List

from screenrec.ui.overlay import AnnotationOverlay
from screenrec.platform.win32 import set_exclude_from_capture


class AnnotationBar(QWidget):
    tool_changed = Signal(str)
    color_changed = Signal(QColor)
    line_width_changed = Signal(int)
    clear_requested = Signal()
    hide_requested = Signal()
    click_feedback_toggled = Signal(bool)  # 点击反馈开关
    cursor_capture_toggled = Signal(bool)  # 鼠标光标录制开关
    trail_toggled = Signal(bool)  # 鼠标轨迹开关

    PRESET_COLORS = [
        "#E74C3C", "#F39C12", "#F1C40F", "#2ECC71",
        "#3498DB", "#9B59B6", "#FFFFFF", "#000000",
    ]

    def __init__(self, overlay: AnnotationOverlay):
        super().__init__()
        self.overlay = overlay
        self.setWindowFlags(
            Qt.WindowStaysOnTopHint |
            Qt.Tool |
            Qt.FramelessWindowHint
        )
        self.setWindowTitle("标注工具栏")
        self.setStyleSheet("""
            QWidget { background: #2c3e50; color: white; }
            QPushButton { background: #34495e; color: white; padding: 6px 10px;
                          border: none; border-radius: 3px; }
            QPushButton:hover { background: #3d566e; }
            QPushButton:checked { background: #e74c3c; }
            QLabel { color: #bdc3c7; padding: 0 6px; }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        # 工具按钮组
        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(True)
        self._tool_to_btn: dict = {}  # tool -> button，用于反向同步

        tools = [
            ("cursor", "↖", "选择/移动 (拖动标注，Delete 删除)"),
            ("pen", "✎", "画笔"),
            ("rect", "▭", "矩形"),
            ("arrow", "➤", "箭头"),
            ("text", "T", "文字"),
            ("eraser", "⌫", "橡皮"),
        ]
        for tool, icon, tip in tools:
            btn = QPushButton(icon)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.setMinimumWidth(36)
            btn.clicked.connect(lambda *args, t=tool: self._on_tool(t))
            self._tool_group.addButton(btn)
            layout.addWidget(btn)
            self._tool_to_btn[tool] = btn
            if tool == "cursor":
                btn.setChecked(True)

        layout.addWidget(self._sep())

        # 颜色选择
        self._color_btns: List[QPushButton] = []
        for c in self.PRESET_COLORS:
            btn = QPushButton()
            btn.setFixedSize(22, 22)
            btn.setStyleSheet(
                f"QPushButton {{ background: {c}; border: 1px solid #555; border-radius: 3px; }}"
                f"QPushButton:hover {{ border: 2px solid white; }}"
            )
            btn.clicked.connect(lambda *args, color=c: self._on_color(QColor(color)))
            layout.addWidget(btn)
            self._color_btns.append(btn)

        more_color = QPushButton("…")
        more_color.setToolTip("自定义颜色")
        more_color.setMinimumWidth(28)
        more_color.clicked.connect(self._on_pick_color)
        layout.addWidget(more_color)

        layout.addWidget(self._sep())

        # 线宽
        layout.addWidget(QLabel("粗细:"))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 30)
        self.width_spin.setValue(4)
        self.width_spin.valueChanged.connect(self.line_width_changed.emit)
        layout.addWidget(self.width_spin)

        layout.addWidget(self._sep())

        # 点击反馈开关
        self.click_feedback_btn = QPushButton("◉ 点击")
        self.click_feedback_btn.setCheckable(True)
        self.click_feedback_btn.setChecked(True)
        self.click_feedback_btn.setToolTip("点击屏幕时显示红点反馈")
        self.click_feedback_btn.setMinimumWidth(56)
        self.click_feedback_btn.clicked.connect(
            lambda checked: self.click_feedback_toggled.emit(checked)
        )
        layout.addWidget(self.click_feedback_btn)

        # 鼠标光标开关
        self.cursor_btn = QPushButton("◉ 光标")
        self.cursor_btn.setCheckable(True)
        self.cursor_btn.setChecked(True)
        self.cursor_btn.setToolTip("录制鼠标光标")
        self.cursor_btn.setMinimumWidth(56)
        self.cursor_btn.clicked.connect(
            lambda checked: self.cursor_capture_toggled.emit(checked)
        )
        layout.addWidget(self.cursor_btn)

        # 鼠标轨迹开关
        self.trail_btn = QPushButton("✦ 轨迹")
        self.trail_btn.setCheckable(True)
        self.trail_btn.setChecked(False)
        self.trail_btn.setToolTip("高亮鼠标移动轨迹")
        self.trail_btn.setMinimumWidth(56)
        self.trail_btn.clicked.connect(
            lambda checked: self.trail_toggled.emit(checked)
        )
        layout.addWidget(self.trail_btn)

        layout.addWidget(self._sep())

        # 撤销 / 重做
        self.undo_btn = QPushButton("↶")
        self.undo_btn.setToolTip("撤销 (Ctrl+Z)")
        self.undo_btn.setMinimumWidth(36)
        self.undo_btn.clicked.connect(self._on_undo)
        layout.addWidget(self.undo_btn)

        self.redo_btn = QPushButton("↷")
        self.redo_btn.setToolTip("重做 (Ctrl+Y)")
        self.redo_btn.setMinimumWidth(36)
        self.redo_btn.clicked.connect(self._on_redo)
        layout.addWidget(self.redo_btn)

        # 快捷键：Ctrl+Z/Y/Delete 已由全局热键处理（不依赖焦点），
        # 这里只保留 Esc 取消选择（局部快捷键即可）
        self._shortcut_esc = QShortcut(QKeySequence("Escape"), self)
        self._shortcut_esc.activated.connect(self._on_deselect)

        layout.addWidget(self._sep())

        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self.clear_requested.emit)
        layout.addWidget(clear_btn)

        hide_btn = QPushButton("✕")
        hide_btn.setToolTip("隐藏工具栏")
        hide_btn.setMinimumWidth(28)
        hide_btn.clicked.connect(self.hide_requested.emit)
        layout.addWidget(hide_btn)

        self.adjustSize()

        # 绑定到 overlay
        self.tool_changed.connect(overlay.set_tool)
        self.color_changed.connect(overlay.set_color)
        self.line_width_changed.connect(overlay.set_line_width)
        self.clear_requested.connect(overlay.clear)
        self.hide_requested.connect(self.hide)

        # overlay 主动改工具时（如文字 Esc 回退）同步按钮状态
        overlay.tool_changed.connect(self._sync_tool_button)

        # 撤销/重做按钮状态跟随 overlay
        overlay.annotation_changed.connect(self._update_undo_redo_state)

        # 初始
        overlay.set_color(QColor(self.PRESET_COLORS[0]))
        overlay.set_line_width(4)
        self._update_undo_redo_state()

    @staticmethod
    def _sep() -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.VLine)
        f.setStyleSheet("color: #7f8c8d;")
        f.setFixedWidth(1)
        return f

    def _on_tool(self, tool: str) -> None:
        print(f"[BAR] tool selected: {tool}", flush=True)
        self.tool_changed.emit(tool)

    def _sync_tool_button(self, tool: str) -> None:
        """overlay 主动改工具时同步按钮 checked 状态。"""
        btn = self._tool_to_btn.get(tool)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)

    def _on_undo(self) -> None:
        self.overlay.undo()

    def _on_redo(self) -> None:
        self.overlay.redo()

    def _on_deselect(self) -> None:
        self.overlay.deselect()

    def _update_undo_redo_state(self) -> None:
        self.undo_btn.setEnabled(self.overlay.can_undo())
        self.redo_btn.setEnabled(self.overlay.can_redo())

    def _on_color(self, color: QColor) -> None:
        self.color_changed.emit(color)

    def _on_pick_color(self) -> None:
        color = QColorDialog.getColor(self.overlay.color, self, "选择颜色")
        if color.isValid():
            self.color_changed.emit(color)

    # 鼠标拖动移动工具栏（无边框窗口）
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and hasattr(self, "_drag_pos"):
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 工具栏也排除捕获
        set_exclude_from_capture(self.winId(), exclude=True)
        # 确保工具栏在 AnnotationOverlay 之上（overlay 全屏会挡住点击）
        self.raise_()
