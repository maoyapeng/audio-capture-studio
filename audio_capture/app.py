from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from .audio import AudioWorker, LevelTestWorker, scan_audio
from .discovery import annotate_inputs, is_default_alias, preferred_input_ids, windows_inventory
from . import __version__
from .models import Device, create_session, safe_name, save_manifest, timestamp
from .remote import RemoteWorker, adb_devices, process_options


STYLE = """
QMainWindow, QDialog {background:#f3f5f9; color:#253349;}
QWidget {font-family:'Segoe UI','Microsoft YaHei'; font-size:13px;}
QFrame#panel {background:white; border:1px solid #dce3ee; border-radius:8px;}
QLabel#brand {font-size:19px; font-weight:600; color:#245fd3;}
QLabel#title {font-size:19px; font-weight:600;}
QLabel#muted {color:#66768a;}
QLabel#time {font-family:Consolas; font-size:30px; color:#233047;}
QPushButton {background:white; border:1px solid #d4ddeb; border-radius:5px; padding:8px 13px; color:#253349;}
QPushButton:hover {background:#edf3ff;}
QPushButton:disabled {color:#9ca8b8; background:#edf0f5;}
QPushButton#primary {background:#245fd3; color:white; border:0; font-weight:600;}
QPushButton#primary:disabled {background:#aabbd8;}
QLineEdit, QComboBox, QSpinBox {background:white; border:1px solid #d6dfeb; border-radius:4px; padding:6px; color:#253349;}
QListWidget {background:white; border:0; outline:0; padding:5px;}
QListWidget::item {padding:10px 7px; border-bottom:1px solid #edf0f6; border-radius:4px;}
QListWidget::item:selected {background:#eaf1ff; color:#253349;}
QPlainTextEdit {background:#152031; color:#d8e6f8; border:0; border-radius:5px; padding:12px; font-family:Consolas,'Microsoft YaHei'; font-size:12px;}
QProgressBar {background:#eef2f7; border:0; border-radius:3px; height:10px; text-align:center;}
QProgressBar::chunk {background:#259b7c; border-radius:3px;}
QCheckBox {spacing:7px;}
QScrollArea {border:0; background:white;}
QStatusBar {background:#e9eef6; color:#617087;}
"""


class Bridge(QObject):
    event = Signal(str, str, object)


def label(text, name=""):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    return widget


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


def panel():
    widget = QFrame()
    widget.setObjectName("panel")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(12)
    return widget, layout


class DeviceDialog(QDialog):
    def __init__(self, parent, device=None):
        super().__init__(parent)
        self.original = device
        self.setWindowTitle("设备采集配置" if device else "添加远程设备")
        self.resize(650, 700)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form = QFormLayout(content)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.fields = {}
        self.kind = QComboBox()
        self.kind.addItems(["SSH", "Telnet", "ADB"] if not device or device.kind != "USB" else ["USB"])
        if device:
            self.kind.setCurrentText(device.kind)
            self.kind.setEnabled(False)
        form.addRow("接入方式", self.kind)
        defaults = device or Device("", "SSH")
        for key, title in [
            ("name", "显示名称 / 别名"), ("address", "主机地址"),
            ("username", "登录用户名"), ("password", "密码（仅本次内存保存）"),
            ("key_file", "SSH 私钥路径（可选）"), ("fingerprint", "SSH SHA256 主机指纹"),
            ("serial", "ADB 序列号"), ("adb_path", "adb.exe 路径或命令"),
            ("command", "启动命令（前台运行）"), ("stop_command", "停止命令（空白发送 Ctrl+C）"),
            ("remote_file", "远端 WAV 文件完整路径"), ("http_url", "Telnet HTTP 文件下载地址（可选）"),
            ("encoding", "终端编码"),
        ]:
            entry = QLineEdit(str(getattr(defaults, key)))
            if key == "password":
                entry.setEchoMode(QLineEdit.Password)
            if key == "command":
                entry.setPlaceholderText("例如 tinycap {remote_file} -c 8 -r 48000")
            self.fields[key] = entry
            form.addRow(title, entry)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(defaults.port)
        form.addRow("网络端口", self.port)
        self.channels = QSpinBox()
        self.channels.setRange(1, 256)
        self.channels.setValue(defaults.channels)
        form.addRow("通道数（远程由模板指定）", self.channels)
        self.rate = QComboBox()
        self.rate.setEditable(True)
        self.rate.addItems(["16000", "44100", "48000", "96000", "192000"])
        self.rate.setCurrentText(str(defaults.rate))
        form.addRow("采样率 Hz", self.rate)
        self.bits = QComboBox()
        self.bits.addItems(["16", "24", "32"])
        self.bits.setCurrentText(str(defaults.bits))
        form.addRow("PCM 位深", self.bits)
        self.capture_mode = QComboBox()
        self.capture_mode.addItem("共享模式（兼容）", "shared")
        self.capture_mode.addItem("WASAPI 独占（失败不回退）", "exclusive")
        self.capture_mode.setCurrentIndex(max(0, self.capture_mode.findData(defaults.capture_mode)))
        form.addRow("本地采集模式", self.capture_mode)
        form.setRowVisible(self.capture_mode, bool(device and device.kind == "USB"))
        hint = label("远程命令必须以前台方式采集，并生成带正确头信息的 WAV。\n"
                     "命令中用 {remote_file} 作为路径，软件每次生成唯一远端文件名。\n"
                     "Telnet 下载地址可用 {filename} 或 {remote_file} 占位符。\n"
                     "配置中的通道、采样率、位深用于校验，软件不会自动改写你的命令。\n"
                     "Telnet 自动登录识别 login/username/password 提示；特殊提示可手动登录。", "muted")
        hint.setWordWrap(True)
        form.addRow(hint)
        if device and device.kind == "USB":
            self.kind.setItemText(0, "本地音频")
            for key in self.fields:
                if key not in ("name",):
                    self.fields[key].setEnabled(False)
                    form.setRowVisible(self.fields[key], False)
            self.port.setEnabled(False)
            form.setRowVisible(self.port, False)
            self.channels.setEnabled(False)
            form.labelForField(self.channels).setText("最大输入通道数")
            hint.setText("多台设备可使用不同采样率，分别保存文件；不需要强制设为同一采样率。\n录制时启用设备最大输入通道数。独占模式需要 WASAPI 和硬件支持所选格式；失败不会回退共享模式。\n独占可绕过系统音频引擎，但不能保证关闭设备固件或驱动内的处理。")
            form.addRow(label(f"系统名称：{device.system_name}\n驱动接口：{device.host_api}\n"
                               f"硬件标识：{device.hardware_id or '未取得'}\n"
                               "同名设备尚不能可靠对应序列号，请用别名区分。", "muted"))
        scroll.setWidget(content)
        outer.addWidget(scroll)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.kind.currentTextChanged.connect(self._kind_changed)

    def _kind_changed(self, kind):
        self.port.setValue(23 if kind == "Telnet" else 22)

    def accept(self):
        try:
            if not self.fields["name"].text().strip():
                raise ValueError("请填写设备显示名称")
            rate = int(self.rate.currentText())
            if not 8000 <= rate <= 384000:
                raise ValueError("采样率须在 8000～384000 Hz 之间")
            if self.kind.currentText() in ("SSH", "Telnet") and not self.fields["address"].text().strip():
                raise ValueError("请填写主机地址")
            if self.kind.currentText() == "ADB" and not self.fields["serial"].text().strip():
                raise ValueError("请填写 ADB 序列号，可先使用发现设备按钮")
            if self.kind.currentText() == "Telnet" and self.fields["stop_command"].text().strip():
                raise ValueError("初版 Telnet 使用 Ctrl+C 停止，请留空停止命令")
            import codecs
            codecs.lookup(self.fields["encoding"].text().strip())
        except (ValueError, LookupError) as exc:
            QMessageBox.warning(self, "配置有误", str(exc))
            return
        super().accept()

    def result_device(self):
        d = replace(self.original) if self.original else Device("", self.kind.currentText())
        for key, widget in self.fields.items():
            value = widget.text()
            setattr(d, key, value if key == "password" else value.strip())
        d.channels, d.rate, d.bits, d.port = self.channels.value(), int(self.rate.currentText()), int(self.bits.currentText()), self.port.value()
        d.capture_mode = self.capture_mode.currentData()
        return d


class MainWindow(QMainWindow):
    def __init__(self, demo=False, config_dir: Path | None = None):
        super().__init__()
        self.demo = demo
        self.setWindowTitle(f"AudioCapture Studio {__version__}" + (" · 演示模式" if demo else ""))
        self.resize(1220, 900)
        self.setMinimumSize(950, 760)
        self.bridge = Bridge()
        self.bridge.event.connect(self.handle_event)
        self.devices: list[Device] = []
        self.states, self.logs, self.workers = {}, {}, {}
        self.last_levels = {}
        self._shown_uid = None
        self._meter_key = None
        self.active = set()
        self.level_test = None
        self.session_dir = None
        self.manifest = None
        self.device_dirs = {}
        self.scan_busy = False
        self._inventory_cache = None
        self._inventory_signature = None
        self._inventory_error = ""
        self.discovery_snapshot = {}
        self.started_monotonic = 0.0
        self.config_dir = config_dir or Path(os.getenv("LOCALAPPDATA", str(Path.home() / ".config"))) / "AudioCaptureStudio"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.config_dir / "devices.local.json"
        self._build_ui()
        self._load()
        if demo:
            self.devices = [Device("USB 麦克风 A", "USB", channels=8, host_api="演示", system_name="模拟 USB 输入", bus="usb"),
                            Device("USB 麦克风 B", "USB", channels=4, host_api="演示", system_name="模拟 USB 输入", bus="usb"),
                            Device("开发板 A", "SSH", channels=6, address="192.168.1.101", command="演示"),
                            Device("Android 测试机", "ADB", channels=8, serial="DEMO-ADB-001", command="演示")]
            for d in self.devices:
                self.states[d.uid] = "演示设备"
                self.logs[d.uid] = "[演示模式] 不连接设备；开始后生成多通道正弦测试信号。\n"
        self.refresh_list()
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.timeout.connect(self.update_elapsed)
        self.elapsed_timer.start(250)
        self.scan_timer = QTimer(self)
        self.scan_timer.timeout.connect(self.discover)
        self.scan_timer.start(10000)
        if not demo:
            QTimer.singleShot(200, self.discover)

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(20, 16, 20, 10)
        outer.setSpacing(14)
        top = QHBoxLayout()
        top.addWidget(label("AudioCapture Studio", "brand"))
        top.addWidget(label("多设备音频采集" + ("  /  演示模式" if self.demo else ""), "muted"))
        top.addStretch()
        self.refresh_button = button("刷新设备", lambda: self.discover(force=True))
        self.add_button = button("＋ 添加远程设备", self.add_device, True)
        top.addWidget(self.refresh_button)
        top.addWidget(self.add_button)
        outer.addLayout(top)
        splitter = QSplitter(Qt.Horizontal)
        self.splitter = splitter
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)
        side, side_layout = panel()
        self.sidebar = side
        side.setMinimumWidth(280)
        side.setMaximumWidth(430)
        side_header = QHBoxLayout()
        side_header.addWidget(label("输入设备", "title"))
        self.count_label = label("", "muted")
        side_header.addStretch()
        side_header.addWidget(self.count_label)
        side_layout.addLayout(side_header)
        self.selected_only = QCheckBox("仅显示已勾选设备（精简列表）")
        self.selected_only.setToolTip("关闭可查找和勾选其他设备；不会删除设备或改变录音选择。")
        side_layout.addWidget(self.selected_only)
        self.selected_only.toggled.connect(self.filter_changed)
        self.source_filter = QComboBox()
        self.source_filter.addItem("全部本地音频输入", "all")
        self.source_filter.addItem("仅已确认的 USB 输入", "usb")
        self.source_filter.setToolTip("远程设备始终显示；USB 分类来自 Windows 硬件信息。")
        self.host_filter = QComboBox()
        self.host_filter.addItem("按设备自动选择驱动（优先 WASAPI）", "")
        self.host_filter.addItem("全部驱动接口（查找旧版可用输入）", "__all__")
        side_layout.addWidget(self.source_filter)
        side_layout.addWidget(self.host_filter)
        self.source_filter.currentIndexChanged.connect(self.filter_changed)
        self.host_filter.currentIndexChanged.connect(self.filter_changed)
        self.discovery_hint = label("正在查找 USB 音频设备…", "muted")
        self.discovery_hint.setWordWrap(True)
        side_layout.addWidget(self.discovery_hint)
        self.device_list = QListWidget()
        self.device_list.setTextElideMode(Qt.ElideRight)
        self.device_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.device_list.currentItemChanged.connect(self.show_current)
        self.device_list.itemChanged.connect(self.selection_changed)
        self.device_list.itemDoubleClicked.connect(lambda _: self.configure())
        side_layout.addWidget(self.device_list, 1)
        self.remove_button = button("移除选中设备", self.remove_device)
        side_layout.addWidget(self.remove_button)
        self.clear_selection_button = button("清空全部勾选", self.clear_selection)
        side_layout.addWidget(self.clear_selection_button)
        info = label("☑ 可勾选多台设备，一起开始和停止\n筛选不取消勾选，隐藏的已选设备也会录制。\n同一设备的多个驱动接口请只勾选一个。", "muted")
        info.setWordWrap(True)
        side_layout.addWidget(info)
        splitter.addWidget(side)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(14)
        detail, detail_layout = panel()
        detail_layout.setSpacing(8)
        header = QHBoxLayout()
        self.device_title = label("请选择设备", "title")
        self.device_title.setWordWrap(True)
        self.device_title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status_label = label("", "muted")
        header.addWidget(self.device_title, 1)
        header.addWidget(self.status_label)
        detail_layout.addLayout(header)
        self.facts = label("USB 麦克风、SSH、Telnet 和 ADB 统一管理", "muted")
        self.facts.setWordWrap(True)
        detail_layout.addWidget(self.facts)
        toolbar = QHBoxLayout()
        self.connect_button = button("连接终端", self.connect_device)
        self.config_button = button("设备配置", self.configure)
        self.test_button = button("测试音量", self.toggle_level_test)
        self.retry_button = button("重试取回文件", self.retry_download)
        self.clear_button = button("清空显示", self.clear_log)
        for w in (self.connect_button, self.config_button, self.test_button, self.retry_button, self.clear_button):
            toolbar.addWidget(w)
        toolbar.addStretch()
        detail_layout.addLayout(toolbar)
        self.local_hint = label("通道电平在录制时显示；选择设备不会自动开启麦克风。", "muted")
        self.local_hint.setWordWrap(True)
        detail_layout.addWidget(self.local_hint)
        self.meter_area = QScrollArea()
        self.meter_area.setWidgetResizable(True)
        self.meter_area.setMinimumHeight(150)
        self.meter_content = QWidget()
        self.meter_content.setObjectName("meter-content")
        self.meter_content.setStyleSheet("QWidget#meter-content {background:white;}")
        self.meter_grid = QGridLayout(self.meter_content)
        self.meter_grid.setAlignment(Qt.AlignTop)
        self.meter_area.setWidget(self.meter_content)
        self.meters = []
        detail_layout.addWidget(self.meter_area, 1)
        self.log_toggle = QCheckBox("显示设备日志")
        self.log_toggle.toggled.connect(self.update_detail_visibility)
        detail_layout.addWidget(self.log_toggle)
        self.terminal = QPlainTextEdit()
        self.terminal.setReadOnly(True)
        self.terminal.setMinimumHeight(100)
        self.terminal.setMaximumBlockCount(4000)
        self.terminal.setPlaceholderText("设备输出会显示在这里")
        detail_layout.addWidget(self.terminal, 1)
        self.termtools_widget = QWidget()
        termtools = QHBoxLayout(self.termtools_widget)
        termtools.setContentsMargins(0, 0, 0, 0)
        self.auto_scroll = QCheckBox("自动滚动")
        self.auto_scroll.setChecked(True)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索当前设备日志")
        self.search.returnPressed.connect(self.find_log)
        termtools.addWidget(self.auto_scroll)
        termtools.addWidget(self.search, 1)
        termtools.addWidget(button("查找", self.find_log))
        termtools.addWidget(button("导出日志", self.export_log))
        detail_layout.addWidget(self.termtools_widget)
        self.command_widget = QWidget()
        command_row = QHBoxLayout(self.command_widget)
        command_row.setContentsMargins(0, 0, 0, 0)
        self.command = QLineEdit()
        self.command.setPlaceholderText("手动发送命令（发往当前设备）")
        self.command.returnPressed.connect(self.send_command)
        self.send_button = button("发送", self.send_command)
        self.interrupt_button = button("Ctrl+C", self.interrupt)
        command_row.addWidget(self.command, 1)
        command_row.addWidget(self.send_button)
        command_row.addWidget(self.interrupt_button)
        detail_layout.addWidget(self.command_widget)
        right_layout.addWidget(detail, 1)
        output, output_layout = panel()
        row = QHBoxLayout()
        row.addWidget(label("本次输出"))
        row.addStretch()
        row.addWidget(button("打开输出目录", self.open_output))
        output_layout.addLayout(row)
        self.output_label = label("每台设备独立目录：audio.wav + terminal.log", "muted")
        self.output_label.setWordWrap(True)
        self.output_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.output_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        output_layout.addWidget(self.output_label)
        right_layout.addWidget(output)
        splitter.addWidget(right)
        splitter.setSizes([340, 840])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        settings = QHBoxLayout()
        settings.addWidget(label("任务名称"))
        self.task = QLineEdit("语音采集_001")
        self.task.setMaximumWidth(210)
        settings.addWidget(self.task)
        settings.addWidget(label("保存根目录"))
        self.destination = QLineEdit(str(Path.home() / "Documents" / "AudioCapture"))
        settings.addWidget(self.destination, 1)
        self.browse_button = button("浏览…", self.browse)
        settings.addWidget(self.browse_button)
        outer.addLayout(settings)
        self.selection_summary = label("", "muted")
        self.selection_summary.setWordWrap(True)
        outer.addWidget(self.selection_summary)
        transport = QHBoxLayout()
        self.clock = label("00:00:00", "time")
        self.record_state = label("准备就绪", "muted")
        transport.addWidget(self.clock)
        transport.addWidget(self.record_state)
        transport.addStretch()
        self.start_button = button("● 开始录制已勾选设备", self.start_recording, True)
        self.stop_button = button("■ 停止并取回文件", self.stop_recording)
        self.stop_button.setEnabled(False)
        transport.addWidget(self.start_button)
        transport.addWidget(self.stop_button)
        outer.addLayout(transport)
        self.statusBar().showMessage("独立设备时钟，统一启停不保证采样同步。" + (" 当前为演示模式。" if self.demo else ""))
        menu = self.menuBar().addMenu("帮助")
        about = QAction("关于 / 使用说明", self)
        about.triggered.connect(lambda: QMessageBox.information(self, f"AudioCapture Studio {__version__}", "USB / SSH / Telnet / ADB 多设备音频采集\n\n"
            "1. 发现设备或添加远程设备。\n2. 双击设备配置采样格式或采集模板。\n3. 远程设备先连接并确认 shell 就绪。\n4. 勾选设备，统一开始、停止。\n\n"
            "USB 使用驱动报告的最大输入通道数。\n远程只校验输出 WAV，不自动改写采集命令。\n启动命令必须前台运行，不能添加 & 或 nohup。\n"
            "USB 同名设备暂按枚举顺序区分，建议分别设置别名。\n设备密码仅在内存中保存。"))
        menu.addAction(about)
        diagnostics = QAction("导出音频设备诊断", self)
        diagnostics.triggered.connect(self.export_diagnostics)
        menu.addAction(diagnostics)

    def emitter(self, uid):
        return lambda kind, data: self.bridge.event.emit(uid, kind, data)

    def _load(self):
        if self.demo or not self.config_path.exists():
            return
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.devices = [Device.from_dict(d) for d in data.get("devices", [])]
            for d in self.devices:
                d.available = False if d.kind == "USB" else True
                self.states[d.uid] = "待发现" if d.kind == "USB" else "未连接"
            self.destination.setText(data.get("destination", self.destination.text()))
            self.selected_only.blockSignals(True)
            self.selected_only.setChecked(data.get("selected_only", any(d.selected for d in self.devices)))
            self.selected_only.blockSignals(False)
        except Exception as exc:
            QMessageBox.warning(self, "读取配置失败", str(exc))

    def save_config(self):
        if self.demo:
            return
        try:
            data = {"devices": [d.public() for d in self.devices], "destination": self.destination.text(),
                    "selected_only": self.selected_only.isChecked()}
            temp = self.config_path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.config_path)
        except Exception as exc:
            self.statusBar().showMessage(f"配置保存失败：{exc}")

    def current_device(self):
        item = self.device_list.currentItem()
        return next((d for d in self.devices if item and d.uid == item.data(Qt.UserRole)), None)

    def active_host(self):
        return self.host_filter.currentData() or ""

    def visible_devices(self):
        if self.selected_only.isChecked():
            return [d for d in self.devices if d.selected]
        host = self.active_host()
        automatic = preferred_input_ids(self.devices) if not host else set()
        usb_only = self.source_filter.currentData() == "usb"
        return [d for d in self.devices if d.kind != "USB" or (
            (host == "__all__" or (not host and d.uid in automatic) or
             (host and d.host_api == host and not is_default_alias(d.system_name or d.name)))
            and (not usb_only or d.bus == "usb"))]

    def filter_changed(self, *_):
        if self.active:
            return
        self.refresh_list()
        self.save_config()

    def clear_selection(self):
        if self.active:
            return
        for device in self.devices:
            device.selected = False
        self.selected_only.blockSignals(True)
        self.selected_only.setChecked(False)
        self.selected_only.blockSignals(False)
        self.refresh_list()
        self.save_config()

    def update_selection_summary(self):
        selected = [d for d in self.devices if d.selected]
        visible = {d.uid for d in self.visible_devices()}
        hidden = sum(d.uid not in visible for d in selected)
        self.count_label.setText(f"已选 {len(selected)} 台" + (f" · 隐藏 {hidden}" if hidden else ""))
        names = [f"{d.name}（{d.channels} 通道 / {d.host_api or d.kind}）" for d in selected]
        self.selection_summary.setText("本次录制：" + ("；".join(names) if names else "请勾选需要同时录制的设备"))
        self.start_button.setText(f"● 开始录制（{len(selected)} 台）")

    @staticmethod
    def device_type(d):
        if d.kind != "USB":
            return d.kind
        return {"usb": "USB", "bluetooth": "蓝牙", "other": "本地音频"}.get(d.bus, "本地音频 · 类型待确认")

    def refresh_list(self):
        self.source_filter.setVisible(not self.selected_only.isChecked())
        self.host_filter.setVisible(not self.selected_only.isChecked())
        previous = self.current_device()
        scroll = self.device_list.verticalScrollBar().value()
        self.device_list.blockSignals(True)
        self.device_list.clear()
        current_item = None
        visible = self.visible_devices()
        for d in visible:
            state = self.states.get(d.uid, "待连接")
            identity = d.system_name if d.kind == "USB" else d.serial if d.kind == "ADB" else f"{d.address}:{d.port}"
            description = f"{d.name}\n{self.device_type(d)} · {d.channels} 通道 · {state}"
            if d.kind == "USB":
                description += f"\n{d.host_api}"
            else:
                description += f"\n{identity}"
            item = QListWidgetItem(description)
            item.setToolTip(f"{d.name}\n{identity}\n{d.host_api if d.kind == 'USB' else d.kind}")
            item.setData(Qt.UserRole, d.uid)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | (Qt.ItemIsUserCheckable if not self.active else Qt.NoItemFlags))
            item.setCheckState(Qt.Checked if d.selected else Qt.Unchecked)
            if not d.available:
                item.setForeground(QColor("#9b7636"))
            self.device_list.addItem(item)
            if previous and previous.uid == d.uid:
                current_item = item
        self.device_list.setCurrentItem(current_item or self.device_list.item(0))
        self.device_list.verticalScrollBar().setValue(scroll)
        self.device_list.blockSignals(False)
        self.update_selection_summary()
        local_count = sum(d.kind == "USB" and d.available for d in visible)
        self.discovery_hint.setText(
            f"显示 {local_count} 个音频输入 · {('全部驱动接口' if self.active_host() == '__all__' else self.active_host()) or '按设备自动选择驱动'}"
            if local_count else "未发现符合筛选的音频输入。可刷新，或切换到全部本地音频。")
        if self.selected_only.isChecked():
            self.discovery_hint.setText(f"本次录制 {len(visible)} 台设备；取消上方精简选项可添加设备。")
        self.show_current()

    def selection_changed(self, item):
        d = next(d for d in self.devices if d.uid == item.data(Qt.UserRole))
        d.selected = item.checkState() == Qt.Checked
        self.update_selection_summary()
        self.show_current()
        self.save_config()
        if self.selected_only.isChecked():
            self.refresh_list()

    def update_detail_visibility(self, *_):
        d = self.current_device()
        local = bool(d and d.kind == "USB")
        remote = bool(d and not local)
        show_log = remote or (local and self.log_toggle.isChecked())
        self.meter_area.setVisible(local)
        self.local_hint.setVisible(local)
        self.log_toggle.setVisible(local)
        self.terminal.setVisible(show_log)
        self.termtools_widget.setVisible(show_log)
        self.command_widget.setVisible(remote)
        self.connect_button.setVisible(remote)
        self.retry_button.setVisible(remote)
        self.clear_button.setVisible(show_log)
        self.config_button.setVisible(bool(d))
        self.test_button.setVisible(local)

    def show_current(self, *_):
        d = self.current_device()
        if not d:
            self.device_title.setText("请选择设备")
            self.status_label.setText("")
            self.facts.setText("当前没有符合筛选条件的输入。\n如果未识别，可切换左侧“全部本地音频输入”检查设备。")
            self.terminal.clear()
            self._shown_uid = None
            self.update_detail_visibility()
            return
        changed = self._shown_uid != d.uid
        self._shown_uid = d.uid
        self.device_title.setText(d.name + f"  /  {self.device_type(d)}")
        self.status_label.setText(("☑ 参与录制 · " if d.selected else "未勾选 · ") + self.states.get(d.uid, "未连接"))
        identity = f"{d.system_name} · {d.host_api}" if d.kind == "USB" else d.serial if d.kind == "ADB" else f"{d.address}:{d.port}"
        self.facts.setText(f"{identity}\n{d.channels} 通道 · {d.rate} Hz · {d.bits} bit PCM" + (" · 最大输入通道 · " + ("WASAPI 独占" if d.capture_mode == "exclusive" else "共享模式") if d.kind == "USB" else " · 模板参数，保存时校验"))
        if changed:
            self.terminal.setPlainText(self.logs.get(d.uid, ""))
            if self.auto_scroll.isChecked():
                self.terminal.verticalScrollBar().setValue(self.terminal.verticalScrollBar().maximum())
        meter_key = (d.uid, d.channels)
        if meter_key != self._meter_key:
            self._meter_key = meter_key
            while self.meter_grid.count():
                entry = self.meter_grid.takeAt(0)
                widget = entry.widget()
                if widget:
                    widget.hide()
                    widget.setParent(None)
                    widget.deleteLater()
            self.meters = []
            for i in range(d.channels if d.kind == "USB" else 0):
                w = QWidget()
                layout = QVBoxLayout(w)
                layout.setContentsMargins(0, 0, 5, 12)
                text = label(f"CH {i + 1:02d}   等待录制", "muted")
                meter = QProgressBar()
                meter.setRange(0, 600)
                meter.setValue(0)
                meter.setTextVisible(False)
                layout.addWidget(text)
                layout.addWidget(meter)
                self.meter_grid.addWidget(w, i // 2, i % 2)
                self.meters.append((meter, text))
            self.paint_levels(d.uid)
        self.local_hint.setText("正在显示通道峰值电平。" if d.uid in self.active else
                                "通道电平在录制时显示；停止后保留最后一次读数。")
        remote = d.kind != "USB" and not self.demo
        worker = self.workers.get(d.uid)
        connected = isinstance(worker, RemoteWorker) and worker.connected
        self.connect_button.setEnabled(remote and not self.active)
        self.connect_button.setText("断开终端" if connected else "连接终端")
        self.command.setEnabled(remote and connected)
        self.send_button.setEnabled(remote and connected)
        self.interrupt_button.setEnabled(remote and connected)
        self.config_button.setEnabled(not self.active and self.level_test is None)
        testing = self.level_test is not None
        self.test_button.setText("停止音量测试" if testing else "测试音量")
        self.test_button.setEnabled(not self.active and not self.scan_busy and (testing or d.available or self.demo))
        if testing:
            self.local_hint.setText(f"正在测试：{self.level_test.device.name}（不保存音频）。选择该设备查看电平。")
        elif not self.active:
            self.local_hint.setText("点击“测试音量”查看各通道电平，不保存录音文件。")
        self.retry_button.setEnabled(remote and connected and not self.active and d.uid in self.device_dirs)
        self.remove_button.setEnabled(not self.active and not testing and (not worker or not worker.is_alive()))
        self.update_detail_visibility()

    def toggle_level_test(self):
        if self.level_test is not None:
            self.level_test.stop()
            self.test_button.setEnabled(False)
            self.test_button.setText("正在停止测试…")
            return
        d = self.current_device()
        if not d or d.kind != "USB" or self.active or self.scan_busy:
            return
        self.last_levels[d.uid] = [0.0] * d.channels
        self.paint_levels(d.uid)
        self.level_test = LevelTestWorker(replace(d),
            lambda kind, data, uid=d.uid: self.bridge.event.emit(uid, "test_" + kind, data), self.demo)
        self.start_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.level_test.start()
        self.show_current()
        self.update_detail_visibility()

    def paint_levels(self, uid):
        data = self.last_levels.get(uid, [])
        for i, (meter, text) in enumerate(self.meters):
            if i < len(data):
                db = 20 * math.log10(max(float(data[i]), 1e-8))
                meter.setValue(max(0, min(600, int((db + 60) * 10))))
                text.setText(f"CH {i + 1:02d}   {db:.1f} dBFS")

    def discover(self, force=False):
        if self.demo or self.active or self.scan_busy or self.level_test is not None:
            return
        self.scan_busy = True
        self.refresh_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self.discovery_hint.setText("正在刷新设备及硬件信息…")

        def work():
            data = {"audio": [], "adb": [], "errors": [], "audio_ok": False}
            try:
                inputs = scan_audio(reinitialize=True)
                data["audio_ok"] = True
                signature = tuple((a["index"], a["name"], a["host"], a["channels"]) for a in inputs)
                try:
                    if force or self._inventory_cache is None or signature != self._inventory_signature:
                        try:
                            self._inventory_cache = windows_inventory()
                            self._inventory_error = ""
                        except Exception as exc:
                            self._inventory_cache = []
                            self._inventory_error = str(exc)
                        self._inventory_signature = signature
                    if self._inventory_error:
                        data["errors"].append(self._inventory_error)
                    data["audio"] = annotate_inputs(inputs, self._inventory_cache)
                except Exception as exc:
                    data["audio"] = annotate_inputs(inputs, [])
                    data["errors"].append(f"USB 类型未确认：{exc}")
            except Exception as exc:
                data["errors"].append(f"音频发现失败：{exc}")
            executable = next((d.adb_path for d in self.devices if d.kind == "ADB"), "adb")
            if shutil.which(executable) or Path(executable).is_file():
                try:
                    data["adb"] = adb_devices(executable)
                    data["adb_path"] = executable
                except Exception as exc:
                    data["errors"].append(f"ADB 发现失败：{exc}")
            self.bridge.event.emit("", "discovery", data)
        threading.Thread(target=work, daemon=True).start()

    def _discovered(self, data):
        self.discovery_snapshot = data
        self.scan_busy = False
        self.refresh_button.setEnabled(not self.active)
        self.start_button.setEnabled(not self.active)
        old = [d for d in self.devices if d.kind == "USB"]
        old_states = {d.uid: (d.available, self.states.get(d.uid)) for d in old}
        if data.get("audio_ok", True):
            for d in old:
                d.available = False
                self.states[d.uid] = "已断开"
        seen = {}
        for a in sorted(data["audio"], key=lambda a: ("WASAPI" not in a["host"], a["index"])):
            key = (a["name"], a["host"])
            ordinal = seen.get(key, 0)
            seen[key] = ordinal + 1
            endpoint = a.get("endpoint_id", "")
            matches = [d for d in old if d.host_api == a["host"] and (
                (endpoint and d.endpoint_id == endpoint) or d.system_name == a["name"])]
            if ordinal < len(matches):
                d = matches[ordinal]
            else:
                d = Device(a["name"], "USB", channels=a["channels"], system_name=a["name"], host_api=a["host"],
                           selected=False, rate=int(a.get("default_rate", 48000)))
                self.devices.append(d)
            d.audio_index, d.channels, d.available = a["index"], a["channels"], True
            d.system_name = a["name"]
            for field in ("bus", "endpoint_id", "container_id", "hardware_id"):
                setattr(d, field, a.get(field, "unknown" if field == "bus" else ""))
            was_available, old_state = old_states.get(d.uid, (False, ""))
            self.states[d.uid] = old_state if was_available and old_state in ("已保存", "部分保存 / 有异常", "异常") else "已连接"
        for serial, detail in data["adb"]:
            found = next((d for d in self.devices if d.kind == "ADB" and d.serial == serial), None)
            if not found:
                found = Device(f"ADB {serial}", "ADB", serial=serial, selected=False, adb_path=data.get("adb_path", "adb"))
                self.devices.append(found)
            if not (worker := self.workers.get(found.uid)) or not worker.is_alive():
                self.states[found.uid] = detail
        chosen = self.host_filter.currentData()
        self.host_filter.blockSignals(True)
        self.host_filter.clear()
        self.host_filter.addItem("按设备自动选择驱动（优先 WASAPI）", "")
        self.host_filter.addItem("全部驱动接口（查找旧版可用输入）", "__all__")
        for host in dict.fromkeys(d.host_api for d in self.devices if d.kind == "USB" and d.available):
            self.host_filter.addItem(host, host)
        index = self.host_filter.findData(chosen)
        self.host_filter.setCurrentIndex(max(0, index))
        self.host_filter.blockSignals(False)
        self.refresh_list()
        self.save_config()
        if data["errors"]:
            self.statusBar().showMessage("；".join(data["errors"]))

    def add_device(self):
        if self.demo:
            QMessageBox.information(self, "演示模式", "请以普通模式启动以添加真实设备。")
            return
        dialog = DeviceDialog(self)
        if dialog.exec():
            d = dialog.result_device()
            self.devices.append(d)
            self.states[d.uid] = "未连接"
            self.save_config()
            self.refresh_list()
            self.device_list.setCurrentRow(self.device_list.count() - 1)

    def configure(self):
        d = self.current_device()
        if not d or self.active:
            return
        worker = self.workers.get(d.uid)
        if worker and worker.is_alive():
            QMessageBox.information(self, "请先断开", "修改配置前请断开当前设备终端。")
            return
        dialog = DeviceDialog(self, d)
        if dialog.exec():
            self.devices[self.devices.index(d)] = dialog.result_device()
            self.save_config()
            self.refresh_list()

    def remove_device(self):
        d = self.current_device()
        if d and not self.active:
            worker = self.workers.get(d.uid)
            if worker and worker.is_alive():
                return
            self.devices.remove(d)
            self.save_config()
            self.refresh_list()

    def connect_device(self):
        d = self.current_device()
        if not d or self.active or d.kind == "USB":
            return
        worker = self.workers.get(d.uid)
        if worker and worker.is_alive():
            worker.close()
            self.states[d.uid] = "断开中"
            self.refresh_list()
            return
        self.workers[d.uid] = RemoteWorker(replace(d), self.emitter(d.uid))
        self.workers[d.uid].start()

    def send_command(self):
        d = self.current_device()
        worker = self.workers.get(d.uid) if d else None
        if isinstance(worker, RemoteWorker) and worker.connected and self.command.text().strip():
            worker.post("send", self.command.text())
            self.command.clear()

    def interrupt(self):
        d = self.current_device()
        worker = self.workers.get(d.uid) if d else None
        if isinstance(worker, RemoteWorker) and worker.connected:
            worker.post("interrupt")

    def retry_download(self):
        d = self.current_device()
        if not d or self.active:
            return
        worker = self.workers.get(d.uid)
        if isinstance(worker, RemoteWorker) and worker.connected:
            self.active.add(d.uid)
            self.set_recording_ui(True)
            worker.post("retry")

    def browse(self):
        folder = QFileDialog.getExistingDirectory(self, "选择保存目录", self.destination.text())
        if folder:
            self.destination.setText(folder)
            self.save_config()

    def start_recording(self):
        if self.active or self.scan_busy or self.level_test is not None:
            return
        selected = [d for d in self.devices if d.selected]
        if not selected:
            QMessageBox.information(self, "选择设备", "请至少勾选一台设备。")
            return
        errors = []
        for d in selected:
            worker = self.workers.get(d.uid)
            if not self.demo:
                if d.kind == "USB" and not d.available:
                    errors.append(f"{d.name}：已断开")
                elif d.kind != "USB":
                    if not isinstance(worker, RemoteWorker) or not worker.connected:
                        errors.append(f"{d.name}：请先连接终端")
                    if not d.command.strip() or not d.remote_file.strip():
                        errors.append(f"{d.name}：缺少采集命令或远端路径")
                    elif "{remote_file}" not in d.command:
                        errors.append(f"{d.name}：启动命令中的文件路径请使用 {{remote_file}} 占位符")
                    if d.kind == "Telnet" and not d.http_url:
                        errors.append(f"{d.name}：请配置 HTTP 下载地址后参与统一采集（终端可独立使用）")
                    elif d.kind == "Telnet" and not any(token in d.http_url for token in ("{filename}", "{remote_file}")):
                        errors.append(f"{d.name}：HTTP 地址须包含 {{filename}} 或 {{remote_file}} 以下载本次唯一文件")
        if errors:
            QMessageBox.warning(self, "尚未就绪", "\n".join(errors))
            return
        try:
            self.session_dir, self.manifest = create_session(Path(self.destination.text()).expanduser(), self.task.text(), selected, self.demo)
            self.device_dirs = {}
            for d in selected:
                folder = self.session_dir / f"{safe_name(d.name)}_{d.uid[:8]}"
                folder.mkdir()
                self.device_dirs[d.uid] = folder
                self.manifest["devices"][d.uid]["directory"] = folder.name
            save_manifest(self.session_dir, self.manifest)
        except Exception as exc:
            QMessageBox.critical(self, "无法创建输出目录", str(exc))
            return
        self.active = {d.uid for d in selected}
        self.started_monotonic = time.monotonic()
        self.clock.setText("00:00:00")
        self.record_state.setText("演示采集中" if self.demo else "正在采集")
        self.output_label.setText(str(self.session_dir) + "\n" + " · ".join(f"{d.name} / {d.channels}ch" for d in selected))
        self.set_recording_ui(True)
        for d in selected:
            self.states[d.uid] = "启动中"
            self.append_log(d.uid, f"\n===== 新任务 {timestamp()} =====\n")
            if d.kind == "USB" or self.demo:
                worker = AudioWorker(replace(d), self.device_dirs[d.uid], self.emitter(d.uid), self.demo)
                self.workers[d.uid] = worker
                worker.start()
            else:
                self.workers[d.uid].post("begin", self.device_dirs[d.uid])
        self.refresh_list()

    def stop_recording(self):
        for uid in list(self.active):
            worker = self.workers.get(uid)
            if isinstance(worker, AudioWorker):
                worker.stop()
            elif isinstance(worker, RemoteWorker):
                worker.post("stop")
        self.stop_button.setEnabled(False)
        self.record_state.setText("正在停止 / 取回文件…")

    def set_recording_ui(self, recording):
        for widget in (self.start_button, self.add_button, self.refresh_button, self.task, self.destination,
                       self.browse_button, self.source_filter, self.host_filter, self.clear_selection_button, self.selected_only):
            widget.setEnabled(not recording)
        self.stop_button.setEnabled(recording)
        self.refresh_list()

    def update_elapsed(self):
        if self.active and self.started_monotonic:
            seconds = int(time.monotonic() - self.started_monotonic)
            self.clock.setText(f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}")

    def append_log(self, uid, text):
        d = next((d for d in self.devices if d.uid == uid), None)
        if d and d.password:
            text = text.replace(d.password, "[密码已隐藏]")
        self.logs[uid] = (self.logs.get(uid, "") + text)[-250000:]
        current = self.current_device()
        if current and current.uid == uid:
            scrollbar = self.terminal.verticalScrollBar()
            previous = scrollbar.value()
            cursor = self.terminal.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText(text)
            if self.auto_scroll.isChecked():
                scrollbar.setValue(scrollbar.maximum())
            else:
                scrollbar.setValue(previous)
        if uid in self.active and uid in self.device_dirs:
            try:
                with (self.device_dirs[uid] / "terminal.log").open("a", encoding="utf-8") as output:
                    output.write(text)
            except OSError as exc:
                self.statusBar().showMessage(f"日志写入失败：{exc}")

    def handle_event(self, uid, kind, data):
        if kind.startswith("test_"):
            if kind == "test_levels":
                self.last_levels[uid] = data
                if self.current_device() and self.current_device().uid == uid:
                    self.paint_levels(uid)
            elif kind in ("test_error", "test_log"):
                self.append_log(uid, str(data) + "\n")
                if kind == "test_error":
                    self.statusBar().showMessage(str(data))
                    self.log_toggle.setChecked(True)
            elif kind == "test_finished":
                self.level_test = None
                self.last_levels[uid] = [0.0] * len(self.last_levels.get(uid, []))
                if self.current_device() and self.current_device().uid == uid:
                    self.paint_levels(uid)
                self.start_button.setEnabled(True)
                self.refresh_button.setEnabled(True)
                self.show_current()
            return
        if kind == "discovery":
            self._discovered(data)
            return
        if kind == "log":
            self.append_log(uid, str(data))
            return
        if kind == "levels":
            self.last_levels[uid] = data
            d = self.current_device()
            if d and d.uid == uid:
                self.paint_levels(uid)
            return
        record = self.manifest["devices"].get(uid) if self.manifest else None
        participating = uid in self.active
        if kind == "hostkey":
            self.append_log(uid, f"\nSSH 主机指纹：{data}\n核对指纹后在设备配置中填写，再次连接。\n")
        elif kind == "state":
            self.states[uid] = str(data)
        elif kind == "started":
            self.states[uid] = "采集中" if not self.demo else "演示采集中"
            if record and participating:
                record.update(data)
                record["state"] = "采集中"
        elif kind in ("saved", "partial"):
            self.states[uid] = "已保存" if kind == "saved" else "部分保存 / 有异常"
            if record and participating:
                record.update(data)
                record["state"] = self.states[uid]
            self.append_log(uid, f"\n[{timestamp()}] {self.states[uid]}：{data.get('file', '')}\n")
        elif kind == "error":
            self.states[uid] = "异常"
            self.append_log(uid, f"\n[{timestamp()}] 错误：{data}\n")
            self.statusBar().showMessage(str(data))
            current = self.current_device()
            if current and current.uid == uid and current.kind == "USB":
                self.log_toggle.setChecked(True)
            if record and participating:
                record.setdefault("errors", []).append({"time": timestamp(), "message": str(data)})
                record["state"] = "异常"
        elif kind == "disconnected":
            self.states[uid] = "已断开"
            if participating:
                if record:
                    record["state"] = "异常断开"
                self.active.discard(uid)
        elif kind == "finished":
            self.active.discard(uid)
            if record and participating:
                record["finished_at"] = timestamp()
        if record and (participating or kind in ("saved", "partial")):
            try:
                save_manifest(self.session_dir, self.manifest)
            except OSError as exc:
                self.statusBar().showMessage(f"采集信息保存失败：{exc}")
        if not self.active and participating:
            errors = any(r.get("state") not in ("已保存",) for r in self.manifest["devices"].values())
            self.record_state.setText("已结束 · 请检查异常设备" if errors else "已完成 · 文件已保存")
            self.set_recording_ui(False)
        else:
            self.refresh_list()

    def clear_log(self):
        d = self.current_device()
        if d:
            self.logs[d.uid] = ""
            self.terminal.clear()

    def find_log(self):
        if self.search.text() and not self.terminal.find(self.search.text()):
            cursor = self.terminal.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            self.terminal.setTextCursor(cursor)
            self.terminal.find(self.search.text())

    def export_diagnostics(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出音频设备诊断", "audio-diagnostics.json", "JSON (*.json)")
        if not path:
            return
        # Audio-only fields: never export remote credentials, commands or addresses.
        data = {"version": __version__, "time": timestamp(),
                "audio_inputs": self.discovery_snapshot.get("audio", []),
                "hardware": self._inventory_cache or [],
                "errors": self.discovery_snapshot.get("errors", []),
                "host_filter": self.host_filter.currentText(),
                "devices": [{key: getattr(d, key) for key in
                             ("name", "system_name", "host_api", "audio_index", "channels", "rate", "bits", "capture_mode", "bus", "available")}
                            for d in self.devices if d.kind == "USB"]}
        try:
            Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def export_log(self):
        d = self.current_device()
        if not d:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", safe_name(d.name) + ".log", "日志 (*.log)")
        if path:
            try:
                Path(path).write_text(self.logs.get(d.uid, ""), encoding="utf-8")
            except OSError as exc:
                QMessageBox.warning(self, "导出失败", str(exc))

    def open_output(self):
        path = self.session_dir or Path(self.destination.text())
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def closeEvent(self, event):
        if self.level_test is not None:
            self.level_test.stop()
            event.ignore()
            QTimer.singleShot(200, self.close)
            return
        if self.active:
            QMessageBox.information(self, "仍在采集", "请先停止并等待各设备保存或取回文件，再关闭窗口。")
            event.ignore()
            return
        self.scan_timer.stop()
        self.save_config()
        for worker in self.workers.values():
            if isinstance(worker, RemoteWorker):
                worker.close()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description="AudioCapture Studio")
    parser.add_argument("--demo", action="store_true", help="生成测试信号，不访问真实设备")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("AudioCaptureStudio")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MainWindow(demo=args.demo)
    window.show()
    sys.exit(app.exec())
