"""Windows 平台工具：让窗口对屏幕捕获隐藏，以及控制鼠标穿透。

SetWindowDisplayAffinity + WDA_EXCLUDEFROMCAPTURE：
用户视觉上仍能看到窗口，但 DXGI Desktop Duplication、GDI、
Windows Graphics Capture 等捕获 API 都录不到这个窗口。

用于隐藏录屏软件自身的控制 UI（主窗口、工具栏），
但保留 AnnotationOverlay（标注要被录进去）。

set_window_click_through：
通过 Win32 WS_EX_TRANSPARENT 控制顶层透明窗口的鼠标穿透。
Qt 的 WA_TransparentForMouseEvents 在 Windows 上对已显示的顶层窗口
运行时切换不可靠（hit-test 缓存不刷新），用 Win32 API 直接改扩展样式才稳。
"""
import ctypes
import sys
from typing import Union

# WINDOW_DISPLAY_AFFINITY 枚举值
WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Windows 10 2004+

# GetWindowLong / SetWindowLong
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020

# SetWindowPos flags
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGE = 0x0020  # 强制刷新窗口框架，让 EXSTYLE 修改生效

_user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None

# SetWindowLongPtrW 在 64 位 Windows 上是正确的名字；32 位上回退到 SetWindowLongW
if _user32 is not None:
    try:
        _SetWindowLong = _user32.SetWindowLongPtrW
    except AttributeError:
        _SetWindowLong = _user32.SetWindowLongW
    try:
        _GetWindowLong = _user32.GetWindowLongPtrW
    except AttributeError:
        _GetWindowLong = _user32.GetWindowLongW


def set_exclude_from_capture(hwnd: Union[int, object], exclude: bool = True) -> bool:
    """设置窗口是否从屏幕捕获中排除。

    Args:
        hwnd: 窗口句柄（int 或 QWidget.winId() 返回的 sip 对象）
        exclude: True=排除（录不到），False=正常（可录制）

    Returns:
        True 表示 API 调用成功
    """
    if _user32 is None:
        return False
    try:
        hwnd_int = int(hwnd)
    except (TypeError, ValueError):
        return False
    affinity = WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE
    try:
        result = _user32.SetWindowDisplayAffinity(hwnd_int, affinity)
        return bool(result)
    except Exception:
        return False


def set_window_click_through(hwnd: Union[int, object], transparent: bool) -> bool:
    """控制顶层窗口是否让鼠标事件穿透。

    Qt 的 WA_TransparentForMouseEvents 在 Windows 上对已显示的窗口
    运行时切换不稳定（hit-test 行为被缓存），这里直接用 Win32 改
    WS_EX_TRANSPARENT 扩展样式，并调 SetWindowPos 强制刷新框架
    让修改立即生效。

    Args:
        hwnd: 窗口句柄
        transparent: True=鼠标穿透（用户能点透到下层窗口），
                     False=接收鼠标事件

    Returns:
        True 表示成功
    """
    if _user32 is None:
        return False
    try:
        hwnd_int = int(hwnd)
    except (TypeError, ValueError):
        return False
    try:
        style = _GetWindowLong(hwnd_int, GWL_EXSTYLE)
        if transparent:
            style |= WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
        _SetWindowLong(hwnd_int, GWL_EXSTYLE, style)
        # 修改 EXSTYLE 后必须调 SetWindowPos with SWP_FRAMECHANGE，
        # 否则 Windows 不会刷新 hit-test 行为，鼠标事件仍然按旧样式处理
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGE
        _user32.SetWindowPos(hwnd_int, 0, 0, 0, 0, 0, flags)
        return True
    except Exception:
        return False


def is_supported() -> bool:
    """当前平台是否支持排除捕获。"""
    return _user32 is not None
