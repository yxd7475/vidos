"""音频捕获：WASAPI loopback 抓系统声音，麦克风可选。

用 PyAudioWPatch 的 WASAPI loopback。loopback 设备的采样率由系统决定，
本类暴露实际采样率供编码器使用。
"""
import queue
import threading
from typing import Optional, Tuple

try:
    import pyaudiowpatch as pyaudio
    _HAS_AUDIO = True
except Exception:
    _HAS_AUDIO = False


class AudioCapture:
    """抓系统声音（loopback）和/或麦克风，提供 PCM s16le 字节流。"""

    def __init__(self, include_system: bool = True, include_mic: bool = False,
                 channels: int = 2, chunk: int = 1024):
        self.include_system = include_system
        self.include_mic = include_mic
        self.channels = channels
        self.chunk = chunk
        self.sample_rate = 44100
        self._pyaudio = None
        self._sys_stream = None
        self._mic_stream = None
        self._running = False
        self._sys_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=400)
        self._mic_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=400)

    @staticmethod
    def available() -> bool:
        return _HAS_AUDIO

    def start(self) -> None:
        if not _HAS_AUDIO:
            raise RuntimeError("PyAudioWPatch 未安装，无法捕获音频")
        self._pyaudio = pyaudio.PyAudio()
        self._running = True

        if self.include_system:
            dev = self._find_loopback_device()
            if dev is None:
                raise RuntimeError("未找到 WASAPI loopback 设备")
            self.sample_rate = int(dev["defaultSampleRate"])
            self._sys_stream = self._pyaudio.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=dev["index"],
                frames_per_buffer=self.chunk,
                stream_callback=self._sys_callback,
            )
            self._sys_stream.start_stream()

        if self.include_mic:
            mic_dev = self._find_mic_device()
            if mic_dev is not None:
                self._mic_stream = self._pyaudio.open(
                    format=pyaudio.paInt16,
                    channels=self.channels,
                    rate=self.sample_rate,
                    input=True,
                    input_device_index=mic_dev["index"],
                    frames_per_buffer=self.chunk,
                    stream_callback=self._mic_callback,
                )
                self._mic_stream.start_stream()

    def _find_loopback_device(self):
        try:
            return self._pyaudio.get_default_wasapi_loopback()
        except Exception:
            for i in range(self._pyaudio.get_device_count()):
                info = self._pyaudio.get_device_info_by_index(i)
                if info.get("isLoopbackDevice"):
                    return info
            return None

    def _find_mic_device(self):
        try:
            return self._pyaudio.get_default_input_device_info()
        except Exception:
            return None

    def _sys_callback(self, in_data, frame_count, time_info, status):
        try:
            self._sys_queue.put_nowait(in_data)
        except queue.Full:
            pass
        return (None, pyaudio.paContinue)

    def _mic_callback(self, in_data, frame_count, time_info, status):
        try:
            self._mic_queue.put_nowait(in_data)
        except queue.Full:
            pass
        return (None, pyaudio.paContinue)

    def read_system(self) -> bytes:
        return self._drain(self._sys_queue)

    def read_mic(self) -> bytes:
        return self._drain(self._mic_queue)

    @staticmethod
    def _drain(q: "queue.Queue[bytes]") -> bytes:
        chunks = []
        while True:
            try:
                chunks.append(q.get_nowait())
            except queue.Empty:
                break
        return b"".join(chunks)

    def stop(self) -> None:
        self._running = False
        if self._sys_stream:
            try:
                self._sys_stream.stop_stream()
                self._sys_stream.close()
            except Exception:
                pass
        if self._mic_stream:
            try:
                self._mic_stream.stop_stream()
                self._mic_stream.close()
            except Exception:
                pass
        if self._pyaudio:
            try:
                self._pyaudio.terminate()
            except Exception:
                pass
