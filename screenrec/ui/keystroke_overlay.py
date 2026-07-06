"""按键回显浮层：在屏幕左下角显示最近按下的键。

- 修饰键（Ctrl/Shift/Alt/Win）按下时显示，松开后淡出
- 普通按键显示 1200ms，末段淡出
- 多个按键水平排列，+ 号分隔
- 浮层对屏幕捕获可见（不调 set_exclude_from_capture），所以会被录进视频
"""
import time
from typing import List

from PySide6.QtCore import Qt, QRect, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QGuiApplication
from PySide6.QtWidgets import QWidget


def _now_ms() -> int:
    return int(time.perf_counter() * 1000)


class KeystrokeOverlay(QWidget):
    """左下角按键回显浮层。"""

    MAX_CARDS = 5  # 最多同时显示的卡片数

    # 钩子线程通过此信号切到主线程
    _key_event = Signal(str, bool, bool)  # name, is_modifier, is_down

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        # 鼠标穿透：不挡下层操作
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)

        # 放在左下角
        screen = QGuiApplication.primaryScreen().geometry()
        w, h = 500, 80
        self.setGeometry(20, screen.height() - h - 60, w, h)

        self._entries: List[dict] = []
        self._timer_id = self.startTimer(33)

        self._key_event.connect(self._handle_key, Qt.QueuedConnection)

    def on_key(self, name: str, is_modifier: bool, is_down: bool) -> None:
        """钩子线程入口：emit 信号切到主线程。"""
        self._key_event.emit(name, is_modifier, is_down)

    def _handle_key(self, name: str, is_modifier: bool, is_down: bool) -> None:
        if is_modifier:
            if is_down:
                # 按住会重复触发，只保留一个 active 状态
                existing = next(
                    (e for e in self._entries
                     if e["name"] == name and e["is_modifier"] and e.get("active")),
                    None
                )
                if existing is None:
                    # 移除已淡出的同类项
                    self._entries = [
                        e for e in self._entries
                        if not (e["name"] == name and e["is_modifier"])
                    ]
                    self._entries.append({
                        "name": name,
                        "born": _now_ms(),
                        "is_modifier": True,
                        "ttl": 999_999_999,
                        "active": True,
                    })
            else:
                # 松开：让修饰键 400ms 内淡出
                now = _now_ms()
                for e in self._entries:
                    if e["name"] == name and e["is_modifier"] and e.get("active"):
                        e["active"] = False
                        e["born"] = now
                        e["ttl"] = 400
        else:
            if is_down:
                # 普通键按下显示 1200ms
                self._entries.append({
                    "name": name,
                    "born": _now_ms(),
                    "is_modifier": False,
                    "ttl": 1200,
                })
        self._trim()
        self.update()

    def _trim(self) -> None:
        """超过 MAX_CARDS 时移除最旧的非 active 条目。

        active 修饰键（还在按住）不会被移除，避免组合键丢失修饰键。
        """
        while len(self._entries) > self.MAX_CARDS:
            # 优先移除非 active 条目中最旧的
            candidate = None
            for e in self._entries:
                if e.get("active"):
                    continue
                if candidate is None or e["born"] < candidate["born"]:
                    candidate = e
            if candidate is None:
                # 全是 active 修饰键，无可移除
                break
            self._entries.remove(candidate)

    def timerEvent(self, event) -> None:
        now = _now_ms()
        new_entries = [e for e in self._entries if now - e["born"] < e["ttl"]]
        if len(new_entries) != len(self._entries):
            self._entries = new_entries
            self._trim()
            self.update()

    def paintEvent(self, event) -> None:
        if not self._entries:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        font = QFont("Microsoft YaHei", 11)
        font.setBold(True)
        p.setFont(font)
        fm = p.fontMetrics()

        card_h = 30
        gap = 6
        sep_w = 12

        now = _now_ms()
        cards = []
        for e in self._entries:
            text = e["name"]
            w = max(34, fm.horizontalAdvance(text) + 16)
            age = now - e["born"]
            if e["is_modifier"] and e.get("active"):
                alpha = 255
            else:
                # 末段 300ms 淡出
                fade_start = max(0, e["ttl"] - 300)
                if age > fade_start:
                    t = (age - fade_start) / 300
                    alpha = int(255 * max(0, 1 - t))
                else:
                    alpha = 255
            if alpha > 0:
                cards.append((text, w, alpha))

        if not cards:
            return

        # 左下角对齐
        x = 0
        y = self.height() - card_h - 4

        for i, (text, w, alpha) in enumerate(cards):
            if i > 0:
                p.setPen(QColor(255, 255, 255, int(180 * alpha / 255)))
                p.drawText(QRect(x, y, sep_w, card_h), Qt.AlignCenter, "+")
                x += sep_w

            bg = QColor(44, 62, 80, int(220 * alpha / 255))
            border = QColor(231, 76, 60, alpha)
            p.setBrush(bg)
            p.setPen(QPen(border, 1))
            p.drawRoundedRect(x, y, w, card_h, 6, 6)
            p.setPen(QColor(255, 255, 255, alpha))
            p.drawText(QRect(x, y, w, card_h), Qt.AlignCenter, text)

            x += w
            if i < len(cards) - 1:
                x += gap
