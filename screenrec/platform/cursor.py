"""获取系统鼠标光标位置和图标，用于在录屏中叠加显示。

dxcam/Desktop Duplication 默认不捕获光标，需要主动查询并叠加。
"""
import ctypes
import sys
from ctypes import wintypes
from typing import Optional, Tuple

user32 = ctypes.WinDLL("user32", use_last_error=True)

# GetCursorInfo
class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", wintypes.POINT),
    ]

CURSOR_SHOWING = 0x00000001

user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
user32.GetCursorInfo.restype = wintypes.BOOL


def get_cursor_position() -> Optional[Tuple[int, int]]:
    """获取当前鼠标光标在屏幕上的位置（屏幕坐标）。

    Returns:
        (x, y) 屏幕坐标，如果鼠标隐藏则返回 None
    """
    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(info)):
        return None
    if not (info.flags & CURSOR_SHOWING):
        return None
    return (info.ptScreenPos.x, info.ptScreenPos.y)


def is_cursor_visible() -> bool:
    """鼠标光标是否正在显示。"""
    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(CURSORINFO)
    if not user32.GetCursorInfo(ctypes.byref(info)):
        return False
    return bool(info.flags & CURSOR_SHOWING)
