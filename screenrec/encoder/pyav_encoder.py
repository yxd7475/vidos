"""PyAV 编码器：自动选择硬件/软件编码器，PTS 严格对齐。

编码器优先级：
1. h264_mf (Media Foundation 硬件编码) - AMD/Intel/Nvidia 通用，文件小、CPU 占用低
2. libx264 (软件编码) - 通用回退，CRF 模式质量更好但 CPU 占用高

音视频都用 (perf_counter - start_time) 作为 PTS 来源，
确保两者时间基准完全一致，从根本上解决音画不同步问题。

不依赖外部 ffmpeg 二进制，PyAV 自带 ffmpeg 库。
"""
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Optional

import av
import numpy as np


class PyAVEncoder:
    # quality -> (crf_for_sw, bitrate_for_hw_1080p30)
    # HW 编码器不支持 CRF，用 bitrate 模式；码率按分辨率和帧率线性缩放
    _QUALITY_PROFILES = {
        "high":     (18, 12_000_000),  # 视觉无损 / 12 Mbps @ 1080p30
        "standard": (23, 6_000_000),   # 标准     / 6 Mbps
        "smooth":   (28, 3_000_000),   # 流畅     / 3 Mbps
    }

    _HW_ENCODERS = ['h264_mf']  # Windows Media Foundation，覆盖 AMD/Intel/Nvidia
    _SW_ENCODER = 'libx264'

    def __init__(self, output_path, width, height, fps,
                 quality="high", audio_bitrate="160k"):
        self.output_path = Path(output_path)
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = quality
        profile = self._QUALITY_PROFILES.get(quality, self._QUALITY_PROFILES["high"])
        self._sw_crf = profile[0]
        self._hw_bitrate_1080p30 = profile[1]
        self.audio_bitrate = audio_bitrate

        self._container: Optional[av.OutputContainer] = None
        self._vstream = None
        self._astream = None
        self._lock = threading.Lock()
        self._start_time: Optional[float] = None
        self._audio_rate: Optional[int] = None
        self._audio_channels = 2
        self._encoder_name: Optional[str] = None

        # 混音缓冲（系统+麦克风同时录制时）
        self._has_sys = False
        self._has_mic = False
        self._sys_buffer = bytearray()
        self._mic_buffer = bytearray()

        # 音频 PTS：按数据量递增，但起始偏移对齐到 t0
        self._audio_first_pts: Optional[int] = None
        self._audio_samples_written = 0

        # 视频 PTS：单调递增，避免同毫秒多帧导致 mux 失败
        self._last_video_pts = 0

    # --- 编码器选择 ---

    @classmethod
    def _select_video_encoder(cls) -> str:
        """优先硬件编码器，回退到软件。"""
        available = av.codecs_available
        for hw in cls._HW_ENCODERS:
            if hw in available:
                return hw
        return cls._SW_ENCODER

    def _compute_hw_bitrate(self) -> int:
        """按分辨率和帧率线性缩放码率。"""
        pixels = self.width * self.height
        ratio = pixels / (1920 * 1080)
        fps_ratio = self.fps / 30.0
        return int(self._hw_bitrate_1080p30 * ratio * fps_ratio)

    # --- 启动 ---

    def start_video(self) -> None:
        self._container = av.open(str(self.output_path), 'w', options={'movflags': '+faststart'})
        codec_name = self._select_video_encoder()
        self._vstream = self._container.add_stream(codec_name, rate=self.fps)
        self._vstream.width = self.width
        self._vstream.height = self.height
        self._vstream.pix_fmt = 'yuv420p'

        if codec_name == 'libx264':
            # CRF 质量模式：固定视觉质量，文件大小自适应
            self._vstream.options = {
                'preset': 'medium',
                'crf': str(self._sw_crf),
            }
        else:
            # 硬件编码器：bitrate 模式（MF 不支持 CRF）
            bitrate = self._compute_hw_bitrate()
            self._vstream.bit_rate = bitrate
            self._vstream.options = {
                'maxrate': str(bitrate * 2),
                'bufsize': str(bitrate * 4),
            }
        self._encoder_name = codec_name
        self._start_time = time.perf_counter()

    def start_audio(self, sample_rate: int, channels: int = 2, kind: str = "system") -> None:
        if self._astream is None:
            self._astream = self._container.add_stream('aac', rate=sample_rate)
            self._astream.layout = 'stereo' if channels == 2 else 'mono'
            self._astream.options = {'b:a': self.audio_bitrate}
            self._audio_rate = sample_rate
            self._audio_channels = channels
        if kind == "system":
            self._has_sys = True
        else:
            self._has_mic = True

    @property
    def encoder_name(self) -> Optional[str]:
        """暴露当前用的编码器，方便 UI 显示。"""
        return self._encoder_name

    # --- 写入 ---

    def write_video_frame(self, bgra_bytes: bytes) -> None:
        with self._lock:
            if self._vstream is None or self._start_time is None:
                return
            arr = np.frombuffer(bgra_bytes, dtype=np.uint8).reshape(self.height, self.width, 4)
            frame = av.VideoFrame.from_ndarray(arr, format='bgra')
            elapsed = time.perf_counter() - self._start_time

            if self._encoder_name == 'libx264':
                # 软件编码：VFR，PTS = 毫秒，精确反映实际时间
                pts = int(elapsed * 1000)
                if pts <= self._last_video_pts:
                    pts = self._last_video_pts + 1
                self._last_video_pts = pts
                frame.pts = pts
                frame.time_base = Fraction(1, 1000)
            else:
                # 硬件编码 (h264_mf)：必须 CFR，PTS = 帧号，time_base = 1/FPS
                # Media Foundation 不支持 VFR，毫秒 PTS 会导致 EINVAL
                pts = round(elapsed * self.fps)
                if pts <= self._last_video_pts:
                    pts = self._last_video_pts + 1
                self._last_video_pts = pts
                frame.pts = pts
                frame.time_base = Fraction(1, self.fps)

            for packet in self._vstream.encode(frame):
                self._container.mux(packet)

    def write_audio(self, pcm_bytes: bytes, kind: str = "system") -> None:
        with self._lock:
            if self._astream is None or self._start_time is None:
                return
            if kind == "system":
                self._sys_buffer.extend(pcm_bytes)
            else:
                self._mic_buffer.extend(pcm_bytes)

            if self._has_sys and self._has_mic:
                # 两路都有：取较短的长度对齐混音
                n = min(len(self._sys_buffer), len(self._mic_buffer))
                n -= n % (self._audio_channels * 2)  # 对齐到 frame
                if n > 0:
                    sys_arr = np.frombuffer(bytes(self._sys_buffer[:n]), dtype=np.int16)
                    mic_arr = np.frombuffer(bytes(self._mic_buffer[:n]), dtype=np.int16)
                    mixed = (sys_arr.astype(np.int32) + mic_arr.astype(np.int32))
                    mixed = np.clip(mixed // 2, -32768, 32767).astype(np.int16)
                    self._write_audio_bytes(mixed.tobytes())
                    del self._sys_buffer[:n]
                    del self._mic_buffer[:n]
            else:
                # 单路：直接写
                buf = self._sys_buffer if kind == "system" else self._mic_buffer
                # 对齐
                frame_size = self._audio_channels * 2
                n = len(buf) - (len(buf) % frame_size)
                if n > 0:
                    self._write_audio_bytes(bytes(buf[:n]))
                    del buf[:n]

    def _write_audio_bytes(self, pcm: bytes) -> None:
        """把 PCM s16le 字节写入编码器。"""
        if not pcm:
            return
        bytes_per_sample = 2 * self._audio_channels  # s16 = 2 bytes/channel
        samples = len(pcm) // bytes_per_sample
        if samples == 0:
            return
        layout = 'stereo' if self._audio_channels == 2 else 'mono'
        frame = av.AudioFrame(format='s16', layout=layout, samples=samples)
        frame.sample_rate = self._audio_rate
        frame.planes[0].update(pcm)

        # 第一帧时记录起始 PTS（对齐到 t0），后续按数据量递增
        # 这样 AAC 编码器看到严格递增的 PTS，且与视频共用同一时间基准
        if self._audio_first_pts is None:
            elapsed = time.perf_counter() - self._start_time
            self._audio_first_pts = max(0, int(elapsed * self._audio_rate))
        frame.pts = self._audio_first_pts + self._audio_samples_written
        self._audio_samples_written += samples
        frame.time_base = Fraction(1, self._audio_rate)
        for packet in self._astream.encode(frame):
            self._container.mux(packet)

    # --- 结束 ---

    def finalize(self) -> Path:
        with self._lock:
            # flush 剩余音频
            if self._astream is not None:
                for buf in (self._sys_buffer, self._mic_buffer):
                    if buf:
                        try:
                            self._write_audio_bytes(bytes(buf))
                        except Exception:
                            pass
                self._sys_buffer.clear()
                self._mic_buffer.clear()
            # flush encoder
            if self._vstream is not None:
                for packet in self._vstream.encode(None):
                    self._container.mux(packet)
            if self._astream is not None:
                for packet in self._astream.encode(None):
                    self._container.mux(packet)
            self._container.close()
        return self.output_path
