"""主窗口：录制控制面板。"""
import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QElapsedTimer, Signal, QObject, QThread
from PySide6.QtGui import QGuiApplication, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QCheckBox, QFileDialog, QSpinBox,
    QMessageBox, QFrame
)

from screenrec.config import Config
from screenrec.recorder import Recorder
from screenrec.ui.overlay import RegionSelectorOverlay, AnnotationOverlay
from screenrec.ui.annotation_bar import AnnotationBar
from screenrec.ui.tray import TrayController, _make_icon
from screenrec.ui.recordings_panel import RecordingsPanel
from screenrec.ui.camera_pip import CameraPiP
from screenrec.ui.keystroke_overlay import KeystrokeOverlay
from screenrec.platform.win32 import set_exclude_from_capture
from screenrec.platform.hotkey import HotkeyManager, MOD_CONTROL, VK_F9, VK_F10, VK_Z, VK_Y, VK_DELETE
from screenrec.platform.keyboard_hook import KeyboardHook


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
    # 跨线程信号：把 hotkey 线程的回调切到 Qt 主线程执行
    _hotkey_start_stop = Signal()
    _hotkey_pause_resume = Signal()
    _hotkey_undo = Signal()
    _hotkey_redo = Signal()
    _hotkey_delete = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ScreenRec")
        self.setWindowIcon(_make_icon("#e74c3c"))
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setMinimumSize(340, 480)

        self.config = Config()
        self.config.ensure_output_dir()
        self.recorder: Optional[Recorder] = None
        self.region_rect = None  # None=全屏，QRect=选区

        self.overlay: Optional[AnnotationOverlay] = None
        self.bar: Optional[AnnotationBar] = None
        self.camera_pip: Optional[CameraPiP] = None
        self.keystroke_overlay: Optional[KeystrokeOverlay] = None
        self._kbd_hook: Optional[KeyboardHook] = None
        self._keystroke_enabled = False

        self._state = "idle"  # idle, recording, paused, processing
        self._finalize_thread: Optional[QThread] = None
        self._hotkeys = HotkeyManager()
        self._hotkeys.start()
        self._hotkey_ids = []
        self._annotation_hotkey_ids = []  # 标注快捷键单独管理
        # 启动时即注册 F9/F10，让 F9 可以从 idle 触发开始录制
        self._register_hotkeys()

        # 系统托盘
        self._tray = TrayController(self)
        self._tray.show_window_requested.connect(self._show_from_tray)
        self._tray.start_stop_requested.connect(self._tray_start_stop)
        self._tray.pause_resume_requested.connect(self._tray_pause_resume)
        self._tray.quit_requested.connect(self._quit_app)
        self._tray.show()
        self._user_quit = False  # 区分"用户退出"和"最小化到托盘"

        self._elapsed = QElapsedTimer()
        self._paused_at = 0  # 暂停时累积的暂停时长（ms）
        self._pause_start = 0  # 本次暂停开始时间
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._update_clock)

        self._build_ui()
        self._update_state()

        # 跨线程信号连接：hotkey 线程的回调通过信号切到主线程
        self._hotkey_start_stop.connect(self._on_hotkey_f9, Qt.QueuedConnection)
        self._hotkey_pause_resume.connect(self._on_hotkey_f10, Qt.QueuedConnection)
        self._hotkey_undo.connect(self._on_hotkey_undo, Qt.QueuedConnection)
        self._hotkey_redo.connect(self._on_hotkey_redo, Qt.QueuedConnection)
        self._hotkey_delete.connect(self._on_hotkey_delete, Qt.QueuedConnection)

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

        self.pause_btn = QPushButton("⏸ 暂停")
        self.pause_btn.setStyleSheet(
            "QPushButton { background-color: #f39c12; color: white; "
            "padding: 12px; font-size: 14px; font-weight: bold; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #d68910; }"
            "QPushButton:disabled { background-color: #95a5a6; }"
        )
        self.pause_btn.clicked.connect(self._on_pause)
        btn_row.addWidget(self.pause_btn, 1)
        layout.addLayout(btn_row)

        # 快捷键提示
        hotkey_hint = QLabel("快捷键: F9 开始/停止  ·  F10 暂停/继续")
        hotkey_hint.setStyleSheet("color: #7f8c8d; font-size: 11px;")
        hotkey_hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hotkey_hint)

        # 标注按钮
        self.annotate_btn = QPushButton("✎ 显示标注工具栏")
        self.annotate_btn.clicked.connect(self._show_annotation_bar)
        layout.addWidget(self.annotate_btn)

        # 摄像头画中画
        cam_row = QHBoxLayout()
        self.camera_btn = QPushButton("📷 摄像头画中画")
        self.camera_btn.setCheckable(True)
        self.camera_btn.clicked.connect(self._toggle_camera_pip)
        cam_row.addWidget(self.camera_btn, 1)
        layout.addLayout(cam_row)

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

        # 最近录制列表
        layout.addWidget(self._hline())
        self.recordings_panel = RecordingsPanel(self.config.output_dir, self)
        self.recordings_panel.open_requested.connect(self._open_file)
        self.recordings_panel.open_folder_requested.connect(self._open_folder)
        layout.addWidget(self.recordings_panel)

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
        self.bar.trail_toggled.connect(self.overlay.set_trail)
        self.bar.keystroke_toggled.connect(self._toggle_keystroke_overlay)
        self.bar.show()

        # 全局热键已在 __init__ 注册
        # 注册标注快捷键（Ctrl+Z/Y, Delete）
        self._register_annotation_hotkeys()

        self._state = "recording"
        self._paused_at = 0
        self._elapsed.start()
        self._timer.start()
        self._update_state()

        # 自动最小化到托盘，避免主窗口挡住录制内容
        self.hide()
        if self._tray is not None:
            self._tray.notify("ScreenRec", "录制中…F9 停止 · F10 暂停 · 双击图标恢复窗口", ms=3000)

    def _register_hotkeys(self) -> None:
        """注册全局热键：F9 开始/停止、F10 暂停/继续。"""
        # 先注销旧热键
        for hid in self._hotkey_ids:
            self._hotkeys.unregister(hid)
        self._hotkey_ids = []

        hid = self._hotkeys.register(0, VK_F9, self._hotkey_start_stop.emit)
        if hid >= 0:
            self._hotkey_ids.append(hid)
        else:
            print("[HOTKEY] F9 registration failed", flush=True)
        hid = self._hotkeys.register(0, VK_F10, self._hotkey_pause_resume.emit)
        if hid >= 0:
            self._hotkey_ids.append(hid)
        else:
            print("[HOTKEY] F10 registration failed", flush=True)

    def _on_hotkey_f9(self) -> None:
        """F9: 录制中则停止，否则开始录制。"""
        if self._state == "recording" or self._state == "paused":
            self._on_stop()
        elif self._state == "idle":
            self._on_record()

    def _on_hotkey_f10(self) -> None:
        """F10: 暂停/继续切换。"""
        if self._state == "recording":
            self._on_pause()
        elif self._state == "paused":
            self._on_resume()

    def _register_annotation_hotkeys(self) -> None:
        """注册标注快捷键：Ctrl+Z 撤销, Ctrl+Y 重做, Delete 删除选中。"""
        # 先注销旧
        for hid in self._annotation_hotkey_ids:
            self._hotkeys.unregister(hid)
        self._annotation_hotkey_ids = []

        hid = self._hotkeys.register(MOD_CONTROL, VK_Z, self._hotkey_undo.emit)
        if hid >= 0:
            self._annotation_hotkey_ids.append(hid)
        else:
            print("[HOTKEY] Ctrl+Z registration failed", flush=True)
        hid = self._hotkeys.register(MOD_CONTROL, VK_Y, self._hotkey_redo.emit)
        if hid >= 0:
            self._annotation_hotkey_ids.append(hid)
        else:
            print("[HOTKEY] Ctrl+Y registration failed", flush=True)
        hid = self._hotkeys.register(0, VK_DELETE, self._hotkey_delete.emit)
        if hid >= 0:
            self._annotation_hotkey_ids.append(hid)
        else:
            print("[HOTKEY] Delete registration failed", flush=True)

    def _unregister_annotation_hotkeys(self) -> None:
        """注销标注快捷键。"""
        for hid in self._annotation_hotkey_ids:
            self._hotkeys.unregister(hid)
        self._annotation_hotkey_ids = []

    def _on_hotkey_undo(self) -> None:
        if self.overlay is not None:
            self.overlay.undo()

    def _on_hotkey_redo(self) -> None:
        if self.overlay is not None:
            self.overlay.redo()

    def _on_hotkey_delete(self) -> None:
        if self.overlay is not None:
            self.overlay.delete_selected()

    def _on_pause(self) -> None:
        if self._state != "recording" or self.recorder is None:
            return
        self._state = "paused"
        self._pause_start = self._elapsed.elapsed()
        if self.recorder is not None:
            self.recorder.pause()
        if self.camera_pip is not None:
            self.camera_pip._camera.pause()
        self.pause_btn.setText("▶ 继续")
        self.pause_btn.setStyleSheet(
            "QPushButton { background-color: #27ae60; color: white; "
            "padding: 12px; font-size: 14px; font-weight: bold; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #229954; }"
            "QPushButton:disabled { background-color: #95a5a6; }"
        )
        self.status_label.setText("⏸ 已暂停")
        self._update_state()

    def _on_resume(self) -> None:
        if self._state != "paused" or self.recorder is None:
            return
        # 累加暂停时长
        self._paused_at += self._elapsed.elapsed() - self._pause_start
        self._state = "recording"
        if self.recorder is not None:
            self.recorder.resume()
        if self.camera_pip is not None:
            self.camera_pip._camera.resume()
        self.pause_btn.setText("⏸ 暂停")
        self.pause_btn.setStyleSheet(
            "QPushButton { background-color: #f39c12; color: white; "
            "padding: 12px; font-size: 14px; font-weight: bold; border: none; border-radius: 4px; }"
            "QPushButton:hover { background-color: #d68910; }"
            "QPushButton:disabled { background-color: #95a5a6; }"
        )
        self.status_label.setText("● 录制中")
        self._update_state()

    def _on_stop(self) -> None:
        if self._state not in ("recording", "paused") or self.recorder is None:
            return
        # 如果在暂停状态停止，先恢复以避免编码器时间基准问题
        if self._state == "paused":
            self.recorder.resume()
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
        # 注销标注快捷键
        self._unregister_annotation_hotkeys()
        # 按键回显：停钩子 + 隐藏浮层，下次录制需要重新打开
        if self._kbd_hook is not None:
            self._kbd_hook.stop()
            self._kbd_hook = None
        if self.keystroke_overlay is not None:
            self.keystroke_overlay.close()
            self.keystroke_overlay = None
        self._keystroke_enabled = False
        # 摄像头 PiP：关闭并清空，下次录制需要重新打开
        if self.camera_pip is not None:
            self.camera_pip.close()
            self.camera_pip = None
            self.camera_btn.setChecked(False)

        self.recorder = None
        self._state = "idle"
        self.clock_label.setText("00:00")
        self._update_state()

        # 刷新录制列表
        if self.recordings_panel is not None:
            self.recordings_panel.refresh()

        if isinstance(result, Exception):
            self.status_label.setText("录制失败")
            if self._tray is not None:
                self._tray.notify("录制失败", f"合并失败：{result}", ms=5000)
            else:
                QMessageBox.critical(self, "录制失败", f"合并失败：{result}")
        else:
            self.status_label.setText(f"已保存: {Path(result).name}")
            if self._tray is not None:
                self._tray.notify("录制完成", f"已保存：{Path(result).name}\n双击托盘图标恢复窗口。", ms=4000)

    # --- 工具栏 ---

    def _show_annotation_bar(self) -> None:
        if self._state != "recording":
            QMessageBox.information(self, "提示", "请先开始录制，再使用标注工具。")
            return
        if self.bar is not None:
            self.bar.show()
            self.bar.raise_()

    def _toggle_camera_pip(self) -> None:
        """切换摄像头画中画。"""
        if self.camera_btn.isChecked():
            if self.camera_pip is None:
                self.camera_pip = CameraPiP(camera_index=0, size=220)
                if not self.camera_pip.started_ok:
                    QMessageBox.warning(self, "摄像头", "无法打开摄像头。请检查设备是否被占用。")
                    self.camera_pip.close()
                    self.camera_pip = None
                    self.camera_btn.setChecked(False)
                    return
                # 放到右下角（在标注工具栏上方）
                screen = QGuiApplication.primaryScreen().geometry()
                x = screen.width() - self.camera_pip.width() - 20
                y = screen.height() - self.camera_pip.height() - 80
                self.camera_pip.move(x, y)
            self.camera_pip.show()
            self.camera_pip.raise_()
        else:
            if self.camera_pip is not None:
                self.camera_pip.hide()

    def _on_cursor_capture_toggled(self, enabled: bool) -> None:
        """切换鼠标光标是否录制。"""
        if self.recorder is not None and self.recorder.screen_cap is not None:
            self.recorder.screen_cap._draw_cursor = enabled

    def _toggle_keystroke_overlay(self, enabled: bool) -> None:
        """切换按键回显浮层。

        开启时：创建浮层 + 启动键盘钩子
        关闭时：停钩子 + 隐藏浮层（保留实例以便复用）
        """
        self._keystroke_enabled = enabled
        if enabled:
            if self.keystroke_overlay is None:
                self.keystroke_overlay = KeystrokeOverlay()
            self.keystroke_overlay.show()
            if self._kbd_hook is None:
                self._kbd_hook = KeyboardHook(self.keystroke_overlay.on_key)
                if not self._kbd_hook.start():
                    print("[KEY] keyboard hook start failed", flush=True)
                    self._kbd_hook = None
        else:
            if self._kbd_hook is not None:
                self._kbd_hook.stop()
                self._kbd_hook = None
            if self.keystroke_overlay is not None:
                self.keystroke_overlay.hide()

    # --- 计时与状态 ---

    def _update_clock(self) -> None:
        ms = self._elapsed.elapsed()
        # 减去累计的暂停时长
        ms -= self._paused_at
        # 如果当前正在暂停，再减去本次暂停已持续时长
        if self._state == "paused":
            ms -= (self._elapsed.elapsed() - self._pause_start)
        if ms < 0:
            ms = 0
        s = ms // 1000
        m = s // 60
        s = s % 60
        self.clock_label.setText(f"{m:02d}:{s:02d}")

    def _update_state(self) -> None:
        recording = self._state == "recording"
        paused = self._state == "paused"
        processing = self._state == "processing"
        active = recording or paused  # 录制中或暂停中都属于"活动"状态
        self.record_btn.setEnabled(not active and not processing)
        self.stop_btn.setEnabled(active)
        self.pause_btn.setEnabled(active)
        self.pause_btn.setText("▶ 继续" if paused else "⏸ 暂停")
        if paused:
            self.pause_btn.setStyleSheet(
                "QPushButton { background-color: #27ae60; color: white; "
                "padding: 12px; font-size: 14px; font-weight: bold; border: none; border-radius: 4px; }"
                "QPushButton:hover { background-color: #229954; }"
                "QPushButton:disabled { background-color: #95a5a6; }"
            )
        else:
            self.pause_btn.setStyleSheet(
                "QPushButton { background-color: #f39c12; color: white; "
                "padding: 12px; font-size: 14px; font-weight: bold; border: none; border-radius: 4px; }"
                "QPushButton:hover { background-color: #d68910; }"
                "QPushButton:disabled { background-color: #95a5a6; }"
            )
        self.select_btn.setEnabled(not active and not processing)
        self.region_combo.setEnabled(not active and not processing)
        self.sys_audio_check.setEnabled(not active and not processing)
        self.mic_check.setEnabled(not active and not processing)
        self.fps_spin.setEnabled(not active and not processing)
        self.quality_combo.setEnabled(not active and not processing)
        self.annotate_btn.setEnabled(active)
        if not active and not processing:
            self.status_label.setText("就绪")
        # 同步托盘状态
        if self._tray is not None:
            self._tray.update_state(self._state)

    # --- 其他 ---

    def _open_file(self, path: Path) -> None:
        """用默认程序打开文件。"""
        try:
            os.startfile(str(path))
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def _open_folder(self, path: Path) -> None:
        """在资源管理器中打开文件夹；如果传的是文件，选中它。"""
        try:
            if path.is_file():
                # /select 语法需要 Windows 路径分隔符
                import subprocess
                subprocess.Popen(['explorer', '/select,', str(path)])
            else:
                os.startfile(str(path))
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def _on_browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", str(self.config.output_dir))
        if d:
            self.config.output_dir = Path(d)
            self.config.ensure_output_dir()
            self.out_label.setText(str(self.config.output_dir))
            if self.recordings_panel is not None:
                self.recordings_panel.set_output_dir(self.config.output_dir)

    # --- 托盘回调 ---

    def _show_from_tray(self) -> None:
        """从托盘恢复主窗口。"""
        self.show()
        self.raise_()
        self.activateWindow()

    def _tray_start_stop(self) -> None:
        """托盘菜单：开始/停止录制。"""
        if self._state in ("recording", "paused"):
            self._on_stop()
        elif self._state == "idle":
            self._on_record()

    def _tray_pause_resume(self) -> None:
        """托盘菜单：暂停/继续。"""
        if self._state == "recording":
            self._on_pause()
        elif self._state == "paused":
            self._on_resume()

    def _quit_app(self) -> None:
        """用户从托盘选择退出。"""
        self._user_quit = True
        if self._state in ("recording", "paused"):
            # 录制中退出：先停止再退出
            self._on_stop()
            # 让 finalize 异步完成；通过定时器轮询状态
            from PySide6.QtCore import QTimer
            QTimer.singleShot(500, self._check_quit_after_stop)
        else:
            from PySide6.QtWidgets import QApplication
            QApplication.quit()

    def _check_quit_after_stop(self) -> None:
        from PySide6.QtWidgets import QApplication
        if self._state == "idle":
            QApplication.quit()
        else:
            # 还在处理中，再等一会
            from PySide6.QtCore import QTimer
            QTimer.singleShot(500, self._check_quit_after_stop)

    def closeEvent(self, event) -> None:
        """关闭按钮：最小化到托盘而不是退出。
        若正在录制，提示用户。
        """
        if self._user_quit:
            # 真正退出
            try:
                self._hotkeys.stop()
            except Exception:
                pass
            super().closeEvent(event)
            return

        # 否则最小化到托盘
        event.ignore()
        self.hide()
        if self._tray is not None:
            self._tray.notify("ScreenRec", "已最小化到托盘，双击图标恢复。", ms=2000)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 让主窗口对屏幕捕获隐藏：用户能看到，但录制不到
        set_exclude_from_capture(self.winId(), exclude=True)
