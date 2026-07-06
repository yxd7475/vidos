"""屏幕捕获：mss 默认，dxcam 可选加速。

输出 numpy BGRA 数组 (height, width, 4)，回调方式推帧。
默认在每帧上叠加鼠标光标，让录屏能看到鼠标。
"""
import threading
import time
from typing import Callable, Optional, Tuple

import numpy as np

try:
    import dxcam  # type: ignore
    _HAS_DXCAM = True
except Exception:
    _HAS_DXCAM = False

import mss

from screenrec.platform.cursor import get_cursor_position

# 简易箭头光标位图（16x16，BGRA），白色填充+黑色描边
# 形状：左上角顶点 (0,0)，向右下延伸的箭头
def _make_arrow_cursor() -> np.ndarray:
    """生成 32x32 的箭头光标 BGRA 数组。"""
    W = H = 32
    arr = np.zeros((H, W, 4), dtype=np.uint8)
    # 用简单的多边形顶点定义箭头
    # 黑色描边
    outline = [
        (1, 1), (1, 22), (5, 18), (8, 25), (10, 24),
        (7, 17), (12, 17), (1, 1),
    ]
    # 内部白色填充（缩小一圈）
    fill = [
        (2, 3), (2, 19), (5, 16), (8, 23), (9, 22),
        (6, 15), (11, 15), (2, 3),
    ]

    # 用 PPM 风格画线
    def draw_polygon(points, color):
        for i in range(len(points) - 1):
            x1, y1 = points[i]
            x2, y2 = points[i + 1]
            steps = max(abs(x2 - x1), abs(y2 - y1), 1)
            for s in range(steps + 1):
                t = s / steps
                x = int(x1 + (x2 - x1) * t)
                y = int(y1 + (y2 - y1) * t)
                if 0 <= x < W and 0 <= y < H:
                    arr[y, x] = color

    # 先画黑色描边（粗一点）
    draw_polygon(outline, [0, 0, 0, 255])
    # 再画白色填充
    draw_polygon(fill, [255, 255, 255, 255])
    return arr


_CURSOR_BITMAP = _make_arrow_cursor()


def _draw_cursor_on_frame(frame: np.ndarray, x: int, y: int) -> None:
    """在帧上叠加鼠标光标。frame 是 (H, W, 4) BGRA。"""
    h, w = frame.shape[:2]
    ch, cw = _CURSOR_BITMAP.shape[:2]
    # 计算光标在帧上的位置
    x0, y0 = x, y
    x1, y1 = x0 + cw, y0 + ch
    # 裁剪到帧范围
    if x0 >= w or y0 >= h or x1 <= 0 or y1 <= 0:
        return
    sx = max(0, -x0)
    sy = max(0, -y0)
    ex = min(cw, w - x0)
    ey = min(ch, h - y0)
    if ex <= sx or ey <= sy:
        return
    src = _CURSOR_BITMAP[sy:ey, sx:ex]
    dst = frame[y0 + sy:y0 + ey, x0 + sx:x0 + ex]
    # alpha 混合（src 是 BGRA 4 通道，dst 也是 BGRA）
    alpha = src[:, :, 3:4].astype(np.float32) / 255.0
    # 只对前 3 通道（BGR）做混合，保留 dst 的 alpha
    blended = (src[:, :, :3] * alpha + dst[:, :, :3] * (1 - alpha)).astype(np.uint8)
    dst[:, :, :3] = blended
    dst[:, :, 3] = 255


FrameCallback = Callable[[np.ndarray], None]
Region = Tuple[int, int, int, int]  # left, top, right, bottom


class ScreenCapture:
    """在独立线程里按 FPS 抓取屏幕区域，回调推 BGRA 帧。"""

    def __init__(self, region: Optional[Region], fps: int = 30, use_dxcam: bool = True,
                 draw_cursor: bool = True):
        self.region = region  # None 表示全屏
        self.fps = fps
        self._use_dxcam = use_dxcam and _HAS_DXCAM
        self._draw_cursor = draw_cursor
        self._running = False
        self.paused = False  # 暂停标志：True 时跳过抓帧
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[FrameCallback] = None
        self._camera = None
        self._mss_ctx = None

    def set_callback(self, cb: FrameCallback) -> None:
        self._callback = cb

    def start(self) -> None:
        if self._use_dxcam:
            self._camera = dxcam.create(region=self.region, output_color="BGRA")
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        self._camera = None

    def _loop(self) -> None:
        interval = 1.0 / self.fps
        next_t = time.perf_counter()

        if self._use_dxcam:
            while self._running:
                if self.paused:
                    time.sleep(interval)
                    next_t = time.perf_counter()
                    continue
                frame = self._camera.grab()
                if frame is not None and self._callback is not None:
                    # dxcam 返回的是只读共享内存，叠加光标前必须 copy
                    if self._draw_cursor:
                        frame = frame.copy()
                    self._maybe_draw_cursor(frame)
                    self._callback(frame)
                next_t += interval
                sleep = next_t - time.perf_counter()
                if sleep > 0:
                    time.sleep(sleep)
                else:
                    next_t = time.perf_counter()
            return

        # mss fallback
        with mss.mss() as sct:
            monitor = self._monitor_for(sct)
            while self._running:
                if self.paused:
                    time.sleep(interval)
                    next_t = time.perf_counter()
                    continue
                img = sct.grab(monitor)
                arr = np.asarray(img)
                if self._callback is not None:
                    self._maybe_draw_cursor(arr)
                    self._callback(arr)
                next_t += interval
                sleep = next_t - time.perf_counter()
                if sleep > 0:
                    time.sleep(sleep)
                else:
                    next_t = time.perf_counter()

    def _maybe_draw_cursor(self, frame: np.ndarray) -> None:
        if not self._draw_cursor:
            return
        pos = get_cursor_position()
        if pos is None:
            return
        x, y = pos
        # 如果有 region，把屏幕坐标转成帧坐标
        if self.region is not None and self.region != (0, 0, 0, 0):
            left, top, _, _ = self.region
            x -= left
            y -= top
        _draw_cursor_on_frame(frame, x, y)

    def _monitor_for(self, sct):
        if self.region is None or self.region == (0, 0, 0, 0):
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            return mon
        left, top, right, bottom = self.region
        return {"left": left, "top": top, "width": right - left, "height": bottom - top}

    @staticmethod
    def dxcam_available() -> bool:
        return _HAS_DXCAM
