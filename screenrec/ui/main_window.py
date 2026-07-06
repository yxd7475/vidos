"""主窗口：录制控制面板。"""
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QElapsedTimer, Signal, QObject, QThread
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QCheckBox, QFileDialog, QSpinBox,
    QMessageBox, QFrame
)

from screenrec.config import Config
from screenrec.recorder import Recorder
from screenrec.ui.overlay import RegionSelectorOverlay, AnnotationOverlay
from screenrec.ui.annotation_bar import AnnotationBar
from screenrec.platform.win32 import set_exclude_from_capture


class FinalizeWorker(QObject):
    finished = Signal(object)  # Path or Exception

    def __init__(self, recorder: Recorder):
        super().__init__()
        self._recorder = recorder

    def run(self) -> None:
        try:
            path = self._recorder.stop()
            self.finished.emit(path)
        except Exception as e:
            self.finished.emit(e)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ScreenRec")
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setMinimumSize(340, 480)

        self.config = Config()
        self.config.ensure_output_dir()
        self.recorder: Optional[Recorder] = None
        self.region_rect = None  # None=全屏，QRect=选区

        self.overlay: Optional[AnnotationOverlay] = None
        self.bar: Optional[AnnotationBar] = None

        self._state = "idle"  # idle, recording, processing
        self._finalize_thread: Optional[QThread] = None

        self._elapsed = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._update_clock)

        self._build_ui()
        self._update_state()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # 标题
        title = QLabel("ScreenRec")
        title.setStyleSheet("font-size: 22px; font-weight: bold; color: #2c3e50;")
        layout.addWidget(title)

        sub = QLabel("免费录屏 · 实时标注")
        sub.setStyleSheet("color: #7f8c8d; font-size: 12px;")
        layout.addWidget(sub)

        # 计时
        self.clock_label = QLabel("00:00")
        self.clock_label.setStyleSheet(
            "font-size: 32px; font-family: Consolas, monospace; "
            "color: #2c3e50; padding: 8px 0;"
        )
        self.clock_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.clock_label)

        # 状态
        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color: #7f8c8d;")
        layout.addWidget(self.status_label)

        layout.addWidget(self._hline())

        # 录制区域
        region_row = QHBoxLayout()
        region_row.addWidget(QLabel("区域:"))
        from PySide6.QtWidgets import QComboBox
        self.region_combo = QComboBox()
        self.region_combo.addItem("全屏", "fullscreen")
        self.region_combo.addItem("选区…", "select")
        self.region_combo.currentIndexChanged.connect(self._on_region_combo_changed)
        region_row.addWidget(self.region_combo, 1)
        self.select_btn = QPushButton("选择区域")
        self.select_btn.clicked.connect(self._start_region_select)
        region_row.addWidget(self.select_btn)
        layout.addLayout(region_row)

        self.region_info = QLabel("当前: 全屏")
        self.region_info.setStyleSheet("color: #7f8c8d; font-size: 11px;")
        layout.addWidget(self.region_info)

        # 音频
        self.sys_audio_check = QCheckBox("录制系统声音")
        self.sys_audio_check.setChecked(True)
        layout.addWidget(self.sys_audio_check)

        self.mic_check = QCheckBox("录制麦克风")
        layout.addWidget(self.mic_check)

        # FPS
        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("FPS:"))
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(10, 144)
        self.fps_spin.setValue(self.config.fps)
        fps_row.addWidget(self.fps_spin)
        fps_row.addStretch()
        layout.addLayout(fps_row)

        # 清晰度
        quality_row = QHBoxLayout()
        quality_row.addWidget(QLabel("清晰度:"))
        from PySide6.QtWidgets import QComboBox
        self.quality_combo = QComboBox()
        self.quality_combo.addItem("高 (视觉无损)", "high")
        self.quality_combo.addItem("标准", "standard")
        self.quality_combo.addItem("流畅 (小文件)", "smooth")
        # 默认选当前 config.quality
        idx = self.quality_combo.findData(self.config.quality)
        if idx >= 0:
            self.quality_combo.setCurrentIndex(idx)
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        quality_row.addWidget(self.quality_combo, 1)
        layout.addLayout(quality_row)

        layout.addWidget(self._hline())

        # 控制按钮
        btn_row = QHBoxLayout()
        self.record_btn = QPushButton("● 开始录制")
        self.record_btn.setStyleSheet(
            "QPushButton { background-color: #e74c3c; color: white; "
            "padding: 12px; font-size: 14px; font-weight: bold; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #c0392b; }"
            "QPushButton:disabled { background-color: #95a5a6; }"
        )
        self.record_btn.clicked.connect(self._on_record)
        btn_row.addWidget(self.record_btn, 2)

        self.stop_btn = QPushButton("■ 停止")
        self.stop_btn.setStyleSheet(
            "QPushButton { background-color: #34495e; color: white; "
            "padding: 12px; font-size: 14px; font-weight: bold; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #2c3e50; }"
            "QPushButton:disabled { background-color: #95a5a6; }"
        )
        self.stop_btn.clicked.connect(self._on_stop)
        btn_row.addWidget(self.stop_btn, 1)
        layout.addLayout(btn_row)

        # 标注按钮
        self.annotate_btn = QPushButton("✎ 显示标注工具栏")
        self.annotate_btn.clicked.connect(self._show_annotation_bar)
        layout.addWidget(self.annotate_btn)

        # 输出路径
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("保存到:"))
        self.out_label = QLabel(str(self.config.output_dir))
        self.out_label.setStyleSheet("color: #7f8c8d; font-size: 11px;")
        out_row.addWidget(self.out_label, 1)
        browse_btn = QPushButton("…")
        browse_btn.setFixedWidth(32)
        browse_btn.clicked.connect(self._on_browse)
        out_row.addWidget(browse_btn)
        layout.addLayout(out_row)

    @staticmethod
    def _hline() -> QFrame:
        f = QFrame()
        f.setFrameShape(QFrame.HLine)
        f.setStyleSheet("color: #bdc3c7;")
        f.setFixedHeight(1)
        return f

    # --- 区域选择 ---

    def _on_quality_changed(self, idx: int) -> None:
        data = self.quality_combo.itemData(idx)
        if data:
            self.config.quality = data

    def _on_region_combo_changed(self, idx: int) -> None:
        data = self.region_combo.itemData(idx)
        if data == "fullscreen":
            self.region_rect = None
            self.region_info.setText("当前: 全屏")
        # "select" 由按钮触发

    def _start_region_select(self) -> None:
        self.selector = RegionSelectorOverlay()
        self.selector.region_selected.connect(self._on_region_selected)
        self.selector.cancelled.connect(lambda: setattr(self, "selector", None))
        self.selector.show()

    def _on_region_selected(self, rect) -> None:
        self.region_rect = rect
        self.region_info.setText(f"当前: {rect.width()} x {rect.height()} @ ({rect.x()},{rect.y()})")
        self.selector = None

    # --- 录制 ---

    def _on_record(self) -> None:
        if self._state != "idle":
            return
        self.config.fps = self.fps_spin.value()
        self.config.quality = self.quality_combo.currentData()

        if self.region_rect is None:
            geo = QGuiApplication.primaryScreen().geometry()
            region = (geo.x(), geo.y(), geo.x() + geo.width(), geo.y() + geo.height())
        else:
            r = self.region_rect
            region = (r.x(), r.y(), r.x() + r.width(), r.y() + r.height())

        output_path = self.config.default_output_path()
        print(f"[REC] start: region={region} output={output_path}", flush=True)

        include_sys = self.sys_audio_check.isChecked()
        include_mic = self.mic_check.isChecked()

        try:
            self.recorder = Recorder(self.config)
            self.recorder.start(
                region=region,
                output_path=output_path,
                include_system=include_sys,
                include_mic=include_mic,
            )
            print("[REC] recorder.start OK", flush=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "录制失败", str(e))
            self.recorder = None
            return

        # 标注 overlay（用全局鼠标钩子接收事件，始终穿透）
        self.overlay = AnnotationOverlay()
        self.overlay.show()
        # 标注工具栏
        self.bar = AnnotationBar(self.overlay)
        # 把工具栏放到右下角
        screen = QGuiApplication.primaryScreen().geometry()
        self.bar.move(screen.width() - self.bar.width() - 20, screen.height() - self.bar.height() - 20)
        # 绑定点击反馈和光标开关
        self.bar.click_feedback_toggled.connect(self.overlay.set_click_feedback)
        self.bar.cursor_capture_toggled.connect(self._on_cursor_capture_toggled)
        self.bar.show()

        self._state = "recording"
        self._elapsed.start()
        self._timer.start()
        self._update_state()

    def _on_stop(self) -> None:
        if self._state != "recording" or self.recorder is None:
            return
        print("[REC] stop requested", flush=True)
        self._state = "processing"
        self.status_label.setText("处理中…")
        self._timer.stop()

        # 隐藏 overlay（不然 finalize 时 ffmpeg 可能在 capture 它？实际 capture 已停）
        if self.overlay is not None:
            self.overlay.hide()

        # 异步 finalize
        self._finalize_thread = QThread()
        self._worker = FinalizeWorker(self.recorder)
        self._worker.moveToThread(self._finalize_thread)
        self._finalize_thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finalize_done)
        self._finalize_thread.start()

    def _on_finalize_done(self, result) -> None:
        print(f"[REC] finalize done: {type(result).__name__}: {result}", flush=True)
        if self._finalize_thread:
            self._finalize_thread.quit()
            self._finalize_thread.wait(2000)
            self._finalize_thread = None

        # 清理 overlay 和工具栏
        if self.bar is not None:
            self.bar.close()
            self.bar = None
        if self.overlay is not None:
            self.overlay.close()
            self.overlay = None

        self.recorder = None
        self._state = "idle"
        self.clock_label.setText("00:00")
        self._update_state()

        if isinstance(result, Exception):
            QMessageBox.critical(self, "录制失败", f"合并失败：{result}")
        else:
            self.status_label.setText(f"已保存: {Path(result).name}")
            QMessageBox.information(
                self, "录制完成",
                f"视频已保存到:\n{result}\n\n点击「打开所在文件夹」可在资源管理器中查看。"
            )

    # --- 工具栏 ---

    def _show_annotation_bar(self) -> None:
        if self._state != "recording":
            QMessageBox.information(self, "提示", "请先开始录制，再使用标注工具。")
            return
        if self.bar is not None:
            self.bar.show()
            self.bar.raise_()

    def _on_cursor_capture_toggled(self, enabled: bool) -> None:
        """切换鼠标光标是否录制。"""
        if self.recorder is not None and self.recorder.screen_cap is not None:
            self.recorder.screen_cap._draw_cursor = enabled

    # --- 计时与状态 ---

    def _update_clock(self) -> None:
        ms = self._elapsed.elapsed()
        s = ms // 1000
        m = s // 60
        s = s % 60
        self.clock_label.setText(f"{m:02d}:{s:02d}")

    def _update_state(self) -> None:
        recording = self._state == "recording"
        processing = self._state == "processing"
        self.record_btn.setEnabled(not recording and not processing)
        self.stop_btn.setEnabled(recording)
        self.select_btn.setEnabled(not recording and not processing)
        self.region_combo.setEnabled(not recording and not processing)
        self.sys_audio_check.setEnabled(not recording and not processing)
        self.mic_check.setEnabled(not recording and not processing)
        self.fps_spin.setEnabled(not recording and not processing)
        self.quality_combo.setEnabled(not recording and not processing)
        self.annotate_btn.setEnabled(recording)
        if not recording and not processing:
            self.status_label.setText("就绪")

    # --- 其他 ---

    def _on_browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", str(self.config.output_dir))
        if d:
            self.config.output_dir = Path(d)
            self.config.ensure_output_dir()
            self.out_label.setText(str(self.config.output_dir))

    def closeEvent(self, event) -> None:
        if self._state == "recording":
            from PySide6.QtWidgets import QApplication
            QApplication.quit()
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 让主窗口对屏幕捕获隐藏：用户能看到，但录制不到
        set_exclude_from_capture(self.winId(), exclude=True)
