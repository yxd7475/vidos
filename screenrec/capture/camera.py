"""摄像头捕获：OpenCV 后台抓帧，回调方式推送 BGR 帧。"""
import threading
import time
from typing import Callable, Optional

import cv2
import numpy as np


FrameCallback = Callable[[np.ndarray], None]


class CameraCapture:
    """在独立线程里抓取摄像头帧。

    使用 OpenCV VideoCapture，回调推送 BGR ndarray (h, w, 3)。
    """

    def __init__(self, camera_index: int = 0, fps: int = 30):
        self.camera_index = camera_index
        self.fps = fps
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[FrameCallback] = None
        self._cap: Optional[cv2.VideoCapture] = None
        self._last_frame: Optional[np.ndarray] = None
        self._width = 0
        self._height = 0

    @staticmethod
    def list_cameras(max_check: int = 5) -> list:
        """枚举可用摄像头索引。"""
        result = []
        for i in range(max_check):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened():
                # 试着读一帧确认能用
                ret, _ = cap.read()
                if ret:
                    result.append(i)
            cap.release()
        return result

    def set_callback(self, cb: FrameCallback) -> None:
        self._callback = cb

    @property
    def frame_size(self) -> tuple:
        return (self._width, self._height)

    def start(self) -> bool:
        self._cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not self._cap.isOpened():
            self._cap = None
            return False
        # 设置尽量小的延迟
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self._cap.set(cv2.CAP_PROP_FPS, self.fps)
        self._width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self._height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def _loop(self) -> None:
        interval = 1.0 / max(self.fps, 1)
        next_t = time.perf_counter()
        while self._running:
            if self._paused or self._cap is None:
                time.sleep(interval)
                next_t = time.perf_counter()
                continue
            ret, frame = self._cap.read()
            if ret and frame is not None and self._callback is not None:
                self._last_frame = frame
                try:
                    self._callback(frame)
                except Exception:
                    pass
            next_t += interval
            sleep = next_t - time.perf_counter()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_t = time.perf_counter()
