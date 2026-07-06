# ScreenRec

一款免费、开源的 Windows 录屏软件，支持实时标注。

## 特性

- 全屏 / 区域录制
- 系统声音 + 麦克风（可选）
- 录制时实时标注：画笔、矩形、箭头、文字
- H.264 + AAC 输出 MP4
- 自带 ffmpeg 二进制，无需额外安装

## 安装

```bash
cd screenrec
pip install -e .
```

可选加速（DXGI Desktop Duplication，性能更好）：

```bash
pip install -e ".[fast]"
```

## 运行

```bash
python -m screenrec
```

或安装后直接：

```bash
screenrec
```

## 使用

1. 选择录制区域（全屏或框选）
2. 勾选是否录制系统声音 / 麦克风
3. 点击「开始录制」
4. 录制过程中可启用标注工具栏，在屏幕上画标注
5. 点击「停止」，视频保存到 `~/Videos/ScreenRec/`

## 技术栈

- **UI**：PySide6 (Qt for Python)
- **屏幕捕获**：mss（默认）/ dxcam（可选加速）
- **音频捕获**：PyAudioWPatch（WASAPI loopback）
- **编码**：ffmpeg (libx264 + aac)，通过 imageio-ffmpeg 自带二进制

## 许可证

MIT
