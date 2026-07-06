"""鼠标状态轮询：用 GetCursorPos + GetAsyncKeyState 检测鼠标位置和按键。

相比 WH_MOUSE_LL 低级钩子，轮询方式不依赖消息循环，在 PySide6 GUI
主线程下也能稳定工作。10ms 间隔足够流畅（100Hz）。
"""
import ctypes
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional, Tuple

user32 = ctypes.WinDLL("user32", use_last_error=True)

# GetCursorPos
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL

# GetAsyncKeyState: 检测鼠标按键
# VK_LBUTTON = 0x01, VK_RBUTTON = 0x02, VK_MBUTTON = 0x04
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

VK_LBUTTON = 0x01


def get_cursor_pos() -> Optional[Tuple[int, int]]:
    """获取当前鼠标屏幕坐标。"""
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return None
    return (pt.x, pt.y)


def is_left_button_down() -> bool:
    """左键当前是否按下。"""
    state = user32.GetAsyncKeyState(VK_LBUTTON)
    # 最高位 = 1 表示按下
    return bool(state & 0x8000)


class MousePoller:
    """轮询鼠标状态，检测按下/松开/移动事件。

    在独立线程中跑，不依赖 GUI 消息循环。
    """

    def __init__(self, callback: Callable[[str, int, int], None],
                 interval: float = 0.01, report_idle_moves: bool = False):
        """callback(event, x, y) 其中 event 是 'down' / 'up' / 'move'。

        Args:
            report_idle_moves: True 时即使没按下也报告 move 事件（用于鼠标轨迹）。
                                False 时只在按下时报告 move（用于绘制）。
        """
        self._callback = callback
        self._interval = interval
        self._report_idle_moves = report_idle_moves
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_pos: Optional[Tuple[int, int]] = None
        self._last_down = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        self._thread = None

    def set_report_idle_moves(self, enabled: bool) -> None:
        """开启/关闭空闲移动报告（用于鼠标轨迹效果）。"""
        self._report_idle_moves = enabled

    def _loop(self) -> None:
        while self._running:
            pos = get_cursor_pos()
            if pos is None:
                time.sleep(self._interval)
                continue

            down = is_left_button_down()

            # 检测按下边沿
            if down and not self._last_down:
                try:
                    self._callback("down", pos[0], pos[1])
                except Exception:
                    pass

            # 检测松开边沿
            if not down and self._last_down:
                try:
                    self._callback("up", pos[0], pos[1])
                except Exception:
                    pass

            # 移动事件：按下时总是报告；未按下时仅在 report_idle_moves=True 时报告
            if pos != self._last_pos:
                if down or self._report_idle_moves:
                    try:
                        self._callback("move", pos[0], pos[1])
                    except Exception:
                        pass

            self._last_pos = pos
            self._last_down = down
            time.sleep(self._interval)
