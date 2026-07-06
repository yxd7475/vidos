"""全局热键：用 Win32 RegisterHotKey 注册系统级热键。

关键设计：所有 RegisterHotKey/UnregisterHotKey 都在 hotkey 线程内执行，
确保 WM_HOTKEY 消息由该线程的 GetMessageW 接收。
（Windows 把热键消息投递到调用 RegisterHotKey 的线程的消息队列，
若在主线程注册，主线程的 Qt 事件循环不会处理 WM_HOTKEY，热键就失效。）
"""
import ctypes
import threading
import traceback
from ctypes import wintypes
from typing import Callable, Dict, Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)

# 修饰键
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

# 虚拟键码
VK_F9 = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A
VK_F12 = 0x7B
VK_ESCAPE = 0x1B
VK_Z = 0x5A
VK_Y = 0x59
VK_DELETE = 0x2E

# 窗口消息
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_USER_TASK = 0x0400  # 自定义任务消息

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class HotkeyManager:
    """全局热键管理器。

    在独立线程里跑 GetMessage 消息循环。
    所有 RegisterHotKey/UnregisterHotKey 调用都通过 PostThreadMessage
    转到 hotkey 线程内执行，保证 WM_HOTKEY 能被本线程的 GetMessage 收到。
    """

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._callbacks: Dict[int, Callable[[], None]] = {}
        self._next_id = 1
        self._lock = threading.Lock()
        self._registered = {}  # id -> (modifiers, vk)
        self._pending_tasks = []  # 待在 hotkey 线程执行的任务

    def start(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()
        result = {"thread_id": None}

        def thread_fn():
            result["thread_id"] = kernel32.GetCurrentThreadId()
            ready.set()
            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                if msg.message == WM_HOTKEY:
                    hotkey_id = msg.wParam
                    with self._lock:
                        cb = self._callbacks.get(hotkey_id)
                    if cb is not None:
                        try:
                            cb()
                        except Exception:
                            traceback.print_exc()
                elif msg.message == WM_USER_TASK:
                    with self._lock:
                        tasks = self._pending_tasks
                        self._pending_tasks = []
                    for task in tasks:
                        try:
                            task()
                        except Exception:
                            traceback.print_exc()

        self._thread = threading.Thread(target=thread_fn, daemon=True)
        self._thread.start()
        ready.wait(timeout=2)
        self._thread_id = result["thread_id"]

    def stop(self) -> None:
        if self._thread_id is None:
            return
        for hid in list(self._registered.keys()):
            self.unregister(hid)
        user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._thread = None
        self._thread_id = None

    def register(self, modifiers: int, vk: int, callback: Callable[[], None]) -> int:
        """注册全局热键，返回热键 ID（用于注销）。失败返回 -1。"""
        if self._thread_id is None:
            self.start()
        with self._lock:
            hid = self._next_id
            self._next_id += 1

        done = threading.Event()
        result = {'success': False}

        def do_register():
            try:
                ok = bool(user32.RegisterHotKey(None, hid, modifiers | MOD_NOREPEAT, vk))
                result['success'] = ok
                if not ok:
                    err = ctypes.get_last_error()
                    print(f"[HOTKEY] RegisterHotKey failed hid={hid} mod={modifiers:#x} vk={vk:#x} err={err}", flush=True)
            finally:
                done.set()

        with self._lock:
            self._pending_tasks.append(do_register)
        user32.PostThreadMessageW(self._thread_id, WM_USER_TASK, 0, 0)
        done.wait(timeout=2)

        if not result['success']:
            return -1

        with self._lock:
            self._callbacks[hid] = callback
            self._registered[hid] = (modifiers, vk)
        return hid

    def unregister(self, hid: int) -> None:
        if hid < 0:
            return
        done = threading.Event()

        def do_unregister():
            try:
                user32.UnregisterHotKey(None, hid)
            finally:
                done.set()

        with self._lock:
            self._callbacks.pop(hid, None)
            self._registered.pop(hid, None)
            self._pending_tasks.append(do_unregister)
        if self._thread_id is not None:
            user32.PostThreadMessageW(self._thread_id, WM_USER_TASK, 0, 0)
            done.wait(timeout=1)
