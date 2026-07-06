"""配置数据类。"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple


@dataclass
class Config:
    output_dir: Path = field(default_factory=lambda: Path.home() / "Videos" / "ScreenRec")
    fps: int = 30
    quality: str = "high"  # high / standard / smooth
    audio_bitrate: str = "160k"
    include_system_audio: bool = True
    include_mic: bool = False
    region: Tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom；0 表示全屏

    def ensure_output_dir(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def default_output_path(self) -> Path:
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.output_dir / f"recording_{ts}.mp4"
