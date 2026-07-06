"""录制协调器：把屏幕/音频捕获与编码器串起来。

用 PyAVEncoder 单进程编码，音视频 PTS 严格对齐到同一时间基准，
从根本上解决音画不同步问题。
"""
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from screenrec.capture.screen import ScreenCapture
from screenrec.capture.audio import AudioCapture
from screenrec.encoder.pyav_encoder import PyAVEncoder

Region = Tuple[int, int, int, int]


class Recorder:
    def __init__(self, config):
        self.config = config
        self.screen_cap: Optional[ScreenCapture] = None
        self.audio_cap: Optional[AudioCapture] = None
        self.encoder: Optional[PyAVEncoder] = None
        self._running = False
        self._audio_thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self, region: Region, output_path,
              include_system: bool = True, include_mic: bool = False) -> None:
        width = region[2] - region[0]
        height = region[3] - region[1]
        if width <= 0 or height <= 0:
            raise ValueError(f"无效录制区域: {region}")

        self.encoder = PyAVEncoder(
            output_path=output_path,
            width=width, height=height,
            fps=self.config.fps,
            quality=self.config.quality,
        )
        self.encoder.start_video()
        self._running = True

        if include_system or include_mic:
            if not AudioCapture.available():
                raise RuntimeError("PyAudioWPatch 未安装，无法录制音频")
            self.audio_cap = AudioCapture(
                include_system=include_system,
                include_mic=include_mic,
            )
            self.audio_cap.start()
            sr = self.audio_cap.sample_rate
            if include_system:
                self.encoder.start_audio(sr, channels=2, kind="system")
            if include_mic:
                self.encoder.start_audio(sr, channels=2, kind="mic")
            self._audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
            self._audio_thread.start()

        cap_region = region if region != (0, 0, 0, 0) else None
        self.screen_cap = ScreenCapture(cap_region, fps=self.config.fps)
        self.screen_cap.set_callback(self._on_frame)
        self.screen_cap.start()

    def _on_frame(self, frame) -> None:
        if self.encoder is not None:
            self.encoder.write_video_frame(frame.tobytes())

    def _audio_loop(self) -> None:
        while self._running and self.audio_cap is not None:
            if self.audio_cap.include_system:
                data = self.audio_cap.read_system()
                if data and self.encoder is not None:
                    self.encoder.write_audio(data, kind="system")
            if self.audio_cap.include_mic:
                data = self.audio_cap.read_mic()
                if data and self.encoder is not None:
                    self.encoder.write_audio(data, kind="mic")
            time.sleep(0.005)

    def stop(self) -> Optional[Path]:
        self._running = False
        if self.screen_cap is not None:
            self.screen_cap.stop()
        if self._audio_thread is not None:
            self._audio_thread.join(timeout=2)
        if self.audio_cap is not None:
            self.audio_cap.stop()
        if self.encoder is not None:
            return self.encoder.finalize()
        return None

