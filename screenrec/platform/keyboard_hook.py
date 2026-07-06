"""全局键盘钩子：WH_KEYBOARD_LL 监听按键事件。

在独立线程里跑消息循环，所有 SetWindowsHookEx/UnhookWindowsHookEx
都在该线程内执行。按键事件通过回调投递到主线程（回调里不要做重活）。

参考 hotkey.py 的线程模型。
"""
import ctypes
import threading
import traceback
from ctypes import wintypes
from typing import Callable, Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

WM_QUIT = 0x0012
WM_USER_TASK = 0x0400

LLKHF_INJECTED = 0x10
LLKHF_UP = 0x80


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


# LowLevelKeyboardProc: LRESULT CALLBACK(int nCode, WPARAM wParam, LPARAM lParam)
HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_long
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


# 虚拟键码 -> 可读名字
VK_TO_NAME = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause",
    0x14: "CapsLock", 0x1B: "Esc", 0x20: "Space",
    0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓",
    0x2D: "Insert", 0x2E: "Delete",
    0x30: "0", 0x31: "1", 0x32: "2", 0x33: "3", 0x34: "4",
    0x35: "5", 0x36: "6", 0x37: "7", 0x38: "8", 0x39: "9",
    0x41: "A", 0x42: "B", 0x43: "C", 0x44: "D", 0x45: "E",
    0x46: "F", 0x47: "G", 0x48: "H", 0x49: "I", 0x4A: "J",
    0x4B: "K", 0x4C: "L", 0x4D: "M", 0x4E: "N", 0x4F: "O",
    0x50: "P", 0x51: "Q", 0x52: "R", 0x53: "S", 0x54: "T",
    0x55: "U", 0x56: "V", 0x57: "W", 0x58: "X", 0x59: "Y", 0x5A: "Z",
    0x5B: "Win", 0x5C: "Win", 0x5D: "Menu",
    0x60: "Num0", 0x61: "Num1", 0x62: "Num2", 0x63: "Num3", 0x64: "Num4",
    0x65: "Num5", 0x66: "Num6", 0x67: "Num7", 0x68: "Num8", 0x69: "Num9",
    0x6A: "*", 0x6B: "+", 0x6C: "Separator", 0x6D: "-", 0x6E: ".", 0x6F: "/",
    0x70: "F1", 0x71: "F2", 0x72: "F3", 0x73: "F4", 0x74: "F5", 0x75: "F6",
    0x76: "F7", 0x77: "F8", 0x78: "F9", 0x79: "F10", 0x7A: "F11", 0x7B: "F12",
    0x90: "NumLock", 0x91: "ScrollLock",
    0xA0: "Shift", 0xA1: "Shift",
    0xA2: "Ctrl", 0xA3: "Ctrl",
    0xA4: "Alt", 0xA5: "Alt",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}

# 修饰键 VK 码集合
MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}


def vk_to_name(vk: int) -> str:
    return VK_TO_NAME.get(vk, f"VK{vk:02X}")


class KeyboardHook:
    """全局键盘钩子。

    在独立线程里跑 SetWindowsHookEx + 消息循环。
    按键事件通过 on_key 回调投递，回调在钩子线程中执行，
    调用方负责用信号槽或 QTimer.singleShot 切回主线程。
    """

    def __init__(self, on_key: Callable[[str, bool, bool], None]):
        """on_key(name, is_modifier, is_down) 在钩子线程调用。"""
        self._on_key = on_key
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._hook: Optional[int] = None
        self._cb_ref = None  # 持有 HOOKPROC 引用，防止 GC

    def start(self) -> bool:
        if self._thread is not None:
            return True
        ready = threading.Event()
        result = {"thread_id": None, "ok": False}

        def thread_fn():
            result["thread_id"] = kernel32.GetCurrentThreadId()
            ready.set()

            def low_level_cb(nCode, wParam, lParam):
                if nCode == HC_ACTION:
                    try:
                        kb = KBDLLHOOKSTRUCT.from_address(lParam)
                        # 排除注入事件，避免回声（比如自己模拟的按键）
                        if not (kb.flags & LLKHF_INJECTED):
                            vk = kb.vkCode
                            is_modifier = vk in MODIFIER_VKS
                            is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN) and not (kb.flags & LLKHF_UP)
                            name = vk_to_name(vk)
                            try:
                                self._on_key(name, is_modifier, is_down)
                            except Exception:
                                traceback.print_exc()
                    except Exception:
                        traceback.print_exc()
                return user32.CallNextHookEx(self._hook, nCode, wParam, lParam)

            cb = HOOKPROC(low_level_cb)
            self._cb_ref = cb  # 防止 GC 导致崩溃
            self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, cb, None, 0)
            if not self._hook:
                print(f"[KBDHOOK] SetWindowsHookExW failed: err={ctypes.get_last_error()}", flush=True)
                return
            result["ok"] = True

            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break

            if self._hook:
                try:
                    user32.UnhookWindowsHookEx(self._hook)
                except Exception:
                    pass
                self._hook = None

        self._thread = threading.Thread(target=thread_fn, daemon=True)
        self._thread.start()
        ready.wait(timeout=2)
        self._thread_id = result["thread_id"]
        return result["ok"]

    def stop(self) -> None:
        if self._thread_id is None:
            return
        try:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._thread = None
        self._thread_id = None
        self._hook = None
        self._cb_ref = None
