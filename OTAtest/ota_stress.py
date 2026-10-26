"""OTA 压测配置、日志监控和串行执行流程；不导入主窗口。"""

import codecs
import json
import re
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from serial_open import SerialOpenBlocked, open_serial_bounded

import serial
from serial.tools import list_ports
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)


# 仅加入本项目的协议模块目录，不执行固件目录中的 Python 或脚本。
PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
import protocol_profiles as protocol_defs
from config_manager import load_config


def load_protocol_options():
    config = load_config()
    profiles, errors = protocol_defs.sanitize_custom_profiles(config.get("protocol_profiles", []))
    return profiles, errors


class FrameParser:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        frames = []
        while True:
            start = self.buffer.find(b"\xef\xaa")
            if start < 0:
                self.buffer[:] = self.buffer[-1:] if self.buffer[-1:] == b"\xef" else b""
                break
            if start:
                del self.buffer[:start]
            if len(self.buffer) < 5:
                break
            size = int.from_bytes(self.buffer[3:5], "big")
            if size > 8192:
                del self.buffer[0]
                continue
            total = size + 6
            if len(self.buffer) < total:
                break
            checksum = 0
            for byte in self.buffer[2:total - 1]:
                checksum ^= byte
            if checksum != self.buffer[total - 1]:
                del self.buffer[0]
                continue
            frames.append((self.buffer[2], bytes(self.buffer[5:total - 1])))
            del self.buffer[:total]
        return frames


def find_ota_files(source):
    """只读取固件数据，不解包或执行目录中的任何文件。"""
    if not source.strip():
        raise ValueError("请选择待压测 BIN 文件或文件夹")
    path = Path(source).expanduser().resolve()
    if path.is_file():
        files = [path] if path.suffix.lower() == ".bin" else []
    elif path.is_dir():
        files = sorted((p for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".bin"),
                       key=lambda p: str(p.relative_to(path)).casefold())
    else:
        raise ValueError("待压测路径不存在")
    if not files:
        raise ValueError("所选路径中未找到 .bin OTA 包（文件夹会递归查找）")
    if any(p.stat().st_size == 0 for p in files):
        raise ValueError("待压测文件中存在空 BIN 文件")
    return tuple(str(p) for p in files)


@dataclass(frozen=True)
class OtaStressConfig:
    source: str
    standard: str
    ota_port: str
    log_port: str
    rounds: int = 1
    use_standard: bool = True
    use_test: bool = True
    use_restore: bool = True
    ota_baudrate: int = 115200
    log_baudrate: int = 115200
    log_encoding: str = "utf-8"
    ota_success_keyword: str = "startBurn success"
    recovery_keyword: str = "dwc"
    profile_id: str = "dsm"
    modality: str = "face"
    packet_size: int = 4000
    ack_timeout: float = 3.0
    retries: int = 3
    completion_timeout: float = 180.0
    success_delay: float = 2.0
    power_off_delay: float = 0.5
    recovery_window: float = 15.0
    boot_wait: float = 15.0
    reconnect_timeout: float = 30.0
    reconnect_power_delay: float = 5.0
    reconnect_power_attempts: int = 2
    output_dir: str = str(Path(__file__).resolve().parent / "results")

    @property
    def needs_ota(self):
        return self.use_test or self.use_standard

    def validate(self, relay_port):
        if not (self.needs_ota or self.use_restore):
            raise ValueError("至少选择一个压测步骤")
        if self.rounds < 1 or self.boot_wait <= 0 or not self.output_dir.strip():
            raise ValueError("轮数、启动等待必须大于 0，且须选择结果目录")
        if not relay_port.strip():
            raise ValueError("请选择继电器串口")
        if not self.needs_ota:
            return ()
        ports = [relay_port.strip().upper(), self.ota_port.strip().upper(), self.log_port.strip().upper()]
        if not all(ports):
            raise ValueError("请选择继电器、OTA 通信和模组日志串口")
        if len(set(ports)) != 3:
            raise ValueError("继电器、OTA 通信和模组日志串口必须互不相同")
        if min(self.ota_baudrate, self.log_baudrate) <= 0:
            raise ValueError("串口波特率必须大于 0")
        if self.rounds < 1 or self.packet_size < 1 or self.retries < 1:
            raise ValueError("压测次数、分包大小、发送尝试次数必须大于 0")
        if min(self.ack_timeout, self.completion_timeout, self.recovery_window, self.boot_wait, self.reconnect_timeout) <= 0:
            raise ValueError("响应超时、完成超时、观察窗口、启动等待及串口重连超时必须大于 0")
        if self.reconnect_power_delay < 1 or self.reconnect_power_attempts < 0:
            raise ValueError("找不到串口时的重启间隔至少 1 秒，自动重启次数不能小于 0")
        if self.success_delay < 2 or self.power_off_delay < 0.2:
            raise ValueError("OTA 成功后至少等待 2 秒，重启断电间隔至少 200ms")
        if not self.ota_success_keyword.strip() or not self.recovery_keyword.strip():
            raise ValueError("OTA 升级结束和 Recovery 关键词不能为空")
        codecs.lookup(self.log_encoding)
        profiles, errors = load_protocol_options()
        if not protocol_defs.profile_exists(self.profile_id, profiles):
            raise ValueError("所选 OTA 协议组不存在")
        if self.profile_id not in protocol_defs.BUILTIN_PROFILE_NAMES and errors:
            raise ValueError("自定义协议配置存在错误，请先在原上位机修复：" + "; ".join(errors))
        if self.modality not in protocol_defs.MODALITIES:
            raise ValueError("请选择人脸或手掌协议")
        files = find_ota_files(self.source) if self.use_test else ()
        if self.use_standard:
            standard = Path(self.standard)
            if not standard.is_file() or standard.suffix.lower() != ".bin" or standard.stat().st_size == 0:
                raise ValueError("请选择非空的标准 OTA .bin 包，或取消标准包升级步骤")
        if not self.output_dir.strip():
            raise ValueError("请选择压测日志保存目录")
        return files


class OtaStressDialog(QDialog):
    config_type = OtaStressConfig
    settings_prefix = "ota/"
    title = "OTA 压测"

    def __init__(self, settings, relay_port, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.relay_port = relay_port
        self.config = None
        self.setWindowTitle(self.title)
        self.resize(760, 650)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.source = QLineEdit()
        self.standard = QLineEdit()
        self.output_dir = QLineEdit()
        for label, field, directory in (("待压测文件 / 文件夹", self.source, None),
                                         ("固定标准 OTA 包", self.standard, False),
                                         ("日志保存目录", self.output_dir, True)):
            row = QHBoxLayout()
            row.addWidget(field, 1)
            if directory is None:
                file_button = QPushButton("选文件…")
                file_button.clicked.connect(lambda checked=False: self._browse(self.source, False))
                row.addWidget(file_button)
            button = QPushButton("选文件夹…" if directory is not False else "选文件…")
            button.clicked.connect(lambda checked=False, f=field, d=directory: self._browse(f, d is not False))
            row.addWidget(button)
            form.addRow(label, row)
        self.use_test = QCheckBox("步骤 1：测试包升级及 Recovery 判断")
        self.use_test.setChecked(True)
        self.use_test.toggled.connect(self.source.setEnabled)
        form.addRow(self.use_test)
        self.use_restore = QCheckBox("步骤 3：模组自动烧录（使用主界面 BAT 配置）")
        self.use_restore.setChecked(True)
        self.use_standard = QCheckBox("步骤 2：标准包完整升级及 Recovery 判断")
        self.use_standard.setChecked(True)
        self.use_standard.toggled.connect(self.standard.setEnabled)
        form.addRow(self.use_standard)
        form.addRow(self.use_restore)
        self.ota_port = QComboBox()
        self.log_port = QComboBox()
        for widget in (self.ota_port, self.log_port):
            widget.setEditable(True)
        refresh = QPushButton("刷新串口")
        refresh.clicked.connect(self._refresh_ports)
        form.addRow("串口列表", refresh)
        self.ota_baudrate = self._baudrate()
        self.log_baudrate = self._baudrate()
        for title, port, baud in (("OTA 通信串口 / 波特率", self.ota_port, self.ota_baudrate),
                                  ("模组日志串口 / 波特率", self.log_port, self.log_baudrate)):
            row = QHBoxLayout()
            row.addWidget(port)
            row.addWidget(baud)
            form.addRow(title, row)
        self.advanced_toggle = QPushButton("▶ 高级设置（编码、协议、轮数、时序与重连）")
        self.advanced_toggle.setCheckable(True)
        form.addRow(self.advanced_toggle)
        self.advanced_panel = QWidget()
        advanced_grid = QGridLayout(self.advanced_panel)
        advanced_grid.setContentsMargins(0, 4, 0, 4)
        advanced_fields = []
        self.log_encoding = QComboBox()
        self.log_encoding.addItems(["utf-8", "gb18030", "cp936"])
        advanced_fields.append(("模组日志编码", self.log_encoding))
        self.profile_id = QComboBox()
        profiles, errors = load_protocol_options()
        for profile_id, title in protocol_defs.BUILTIN_PROFILE_NAMES.items():
            self.profile_id.addItem(title, profile_id)
        for profile in profiles:
            self.profile_id.addItem(profile["name"], profile["id"])
        self.modality = QComboBox()
        self.modality.addItem("人脸", "face")
        self.modality.addItem("手掌", "palm")
        protocol_widget = QWidget()
        protocol_row = QHBoxLayout(protocol_widget)
        protocol_row.setContentsMargins(0, 0, 0, 0)
        protocol_row.addWidget(self.profile_id)
        protocol_row.addWidget(self.modality)
        advanced_fields.append(("OTA 协议组 / 模态", protocol_widget))
        self.rounds = QSpinBox()
        self.rounds.setRange(1, 1000000)
        advanced_fields.append(("压测轮数（超过包数则循环）", self.rounds))
        self.packet_size = QSpinBox()
        self.packet_size.setRange(1, 60000)
        advanced_fields.append(("OTA 分包大小（字节）", self.packet_size))
        self.retries = QSpinBox()
        self.retries.setRange(1, 20)
        advanced_fields.append(("每条 OTA 指令最多发送次数", self.retries))
        self.reconnect_power_attempts = QSpinBox()
        self.reconnect_power_attempts.setRange(0, 10)
        advanced_fields.append(("每次连接自动重启次数（0 禁用）", self.reconnect_power_attempts))
        for name, title, minimum, maximum in (
            ("ack_timeout", "OTA 响应超时（秒）", 0.1, 60),
            ("completion_timeout", "等待升级结束标识上限（秒）", 1, 86400),
            ("success_delay", "升级成功后重启前等待（秒）", 2, 60),
            ("power_off_delay", "OTA 重启断电间隔（秒）", 0.2, 60),
            ("recovery_window", "重启后 Recovery 观察时长（秒）", 1, 600),
            ("boot_wait", "烧录还原后启动等待（秒）", 1, 600),
            ("reconnect_timeout", "OTA 串口重连总等待上限（秒，含自动重启）", 1, 600),
            ("reconnect_power_delay", "连续找不到 OTA 串口多久后重启（秒）", 1, 60),
        ):
            widget = QDoubleSpinBox()
            widget.setRange(minimum, maximum)
            widget.setDecimals(2)
            setattr(self, name, widget)
            advanced_fields.append((title, widget))
        for index, (title, widget) in enumerate(advanced_fields):
            field = QWidget()
            field_layout = QVBoxLayout(field)
            field_layout.setContentsMargins(0, 0, 0, 0)
            label = QLabel(title)
            label.setWordWrap(True)
            field_layout.addWidget(label)
            field_layout.addWidget(widget)
            row, column = divmod(index, 2)
            advanced_grid.addWidget(field, row, column)
        advanced_grid.setColumnStretch(0, 1)
        advanced_grid.setColumnStretch(1, 1)
        form.addRow(self.advanced_panel)
        self.advanced_panel.hide()
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        self._extra_fields(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        content.setLayout(form)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        note = QLabel(
            "流程：测试包 OTA → 等待成功 → 重启并观察 Recovery → 标准包 OTA（可跳过）→ 自动烧录还原。\n"
            "沿用主界面的 BAT、模式、继电器通道及烧录时序。BIN 按相对路径排序，轮流使用。\n"
            "OTA 保持所选通信波特率，不发送切换波特率指令；请选择模组当前实际波特率。\n"
            "日志标识统一在主界面“设置”中修改。未检出 Recovery 关键词不保证启动正常；OTA/烧录失败将停止压测。"
        )
        self.flow_note = note
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始压测")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_ports()
        defaults = self.config_type("", "", "", "")
        self.log_keywords = {
            name: str(settings.value("ota/" + name, getattr(defaults, name))).strip()
            for name in ("ota_success_keyword", "recovery_keyword")
        }
        for name in defaults.__dataclass_fields__:
            if name in self.log_keywords:
                continue
            widget = getattr(self, name)
            value = settings.value(self.settings_prefix + name, getattr(defaults, name))
            try:
                if isinstance(widget, QCheckBox):
                    widget.setChecked(str(value).lower() in ("true", "1"))
                elif isinstance(widget, QLineEdit):
                    widget.setText(str(value))
                elif isinstance(widget, QComboBox):
                    if name in ("profile_id", "modality"):
                        index = widget.findData(value)
                        widget.setCurrentIndex(index if index >= 0 else 0)
                    else:
                        widget.setCurrentText(str(value))
                else:
                    widget.setValue(int(value) if isinstance(widget, QSpinBox) else float(value))
            except (ValueError, TypeError):
                pass

    def _toggle_advanced(self, expanded):
        self.advanced_panel.setVisible(expanded)
        self.advanced_toggle.setText(
            ("▼" if expanded else "▶") + " 高级设置（编码、协议、轮数、时序与重连）"
        )

    def _extra_fields(self, form):
        pass

    @staticmethod
    def _baudrate():
        widget = QComboBox()
        widget.setEditable(True)
        widget.addItems(["115200", "1500000", "921600", "460800", "230400", "57600", "9600"])
        return widget

    def _refresh_ports(self):
        devices = [p.device for p in list_ports.comports()]
        for widget in (self.ota_port, self.log_port):
            current = widget.currentText()
            widget.clear()
            widget.addItems(devices)
            if current:
                widget.setCurrentText(current)

    def _browse(self, field, directory):
        if directory:
            path = QFileDialog.getExistingDirectory(self, "选择文件夹", field.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, "选择 OTA BIN 包", field.text(), "OTA 包 (*.bin *.BIN)")
        if path:
            field.setText(path)

    def _accept(self):
        try:
            values = dict(self.log_keywords)
            for name in self.config_type.__dataclass_fields__:
                if name in self.log_keywords:
                    continue
                widget = getattr(self, name)
                if isinstance(widget, QCheckBox):
                    value = widget.isChecked()
                elif isinstance(widget, QLineEdit):
                    value = widget.text().strip()
                elif isinstance(widget, QComboBox):
                    value = widget.currentData() if name in ("profile_id", "modality") else widget.currentText().strip()
                    if name.endswith("baudrate"):
                        value = int(value)
                else:
                    value = widget.value()
                values[name] = value
            config = self.config_type(**values)
            files = config.validate(self.relay_port)
        except (ValueError, OSError, LookupError) as exc:
            QMessageBox.warning(self, "OTA 压测配置有误", str(exc))
            return
        for name, value in values.items():
            self.settings.setValue(self.settings_prefix + name, value)
        self.config = config
        self.files = files
        self.accept()


class SerialLogMonitor:
    """独立读日志串口；关键字跨读取块匹配，无需等待换行。"""

    def __init__(self, connection, encoding, logger, success_keyword="startBurn success", recovery_keyword="dwc"):
        self.success_keyword = success_keyword.strip().lower()
        self.recovery_keyword = recovery_keyword.strip().lower()
        if not self.success_keyword or not self.recovery_keyword:
            raise ValueError("日志监控关键词不能为空")
        self.tail_length = max(len(self.success_keyword), len(self.recovery_keyword), 65) - 1
        self.connection = connection
        self.encoding = encoding
        self.logger = logger
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.success = threading.Event()
        self.recovery = threading.Event()
        self.error = None
        self.watch_success = False
        self.watch_recovery = False
        self.tail = ""
        self.thread_started = False
        self.thread = threading.Thread(target=self._read, daemon=True)

    def start(self):
        self.thread.start()
        self.thread_started = True

    @contextmanager
    def _phase_lock(self):
        if not self.lock.acquire(timeout=2):
            raise RuntimeError("等待日志串口监控锁超过 2 秒，日志串口驱动读取可能阻塞")
        try:
            yield
        finally:
            self.lock.release()

    def arm_success(self):
        with self._phase_lock():
            self.connection.reset_input_buffer()
            self.success.clear()
            self.tail = ""
            self.watch_success = True
            self.watch_recovery = False

    def arm_recovery(self):
        with self._phase_lock():
            self.connection.reset_input_buffer()
            self.recovery.clear()
            self.tail = ""
            self.watch_success = False
            self.watch_recovery = True

    def disarm(self):
        with self._phase_lock():
            self.watch_success = False
            self.watch_recovery = False
            self.tail = ""

    def _feed(self, text):
        combined = self.tail + text.lower()
        if self.watch_success and self.success_keyword in combined:
            self.success.set()
        if self.watch_recovery and self.recovery_keyword in combined:
            self.recovery.set()
        self.tail = combined[-self.tail_length:]

    def _read(self):
        decoder = codecs.getincrementaldecoder(self.encoding)(errors="replace")
        pending = ""
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    waiting = self.connection.in_waiting
                    raw = self.connection.read(waiting) if waiting else b""
                    text = decoder.decode(raw)
                    recovery_before = self.recovery.is_set()
                    self._feed(text)
                    recovery_detected = not recovery_before and self.recovery.is_set()
                if recovery_detected:
                    self.logger(f"【进入 Recovery】重启后检测到 {self.recovery_keyword}")
                if not raw:
                    self.stop_event.wait(0.01)
                pending += text
                lines = re.split(r"\r\n|\r|\n", pending)
                pending = lines.pop()
                for line in lines:
                    if line:
                        self.logger("模组日志 | " + line)
                if len(pending) >= 2048:
                    self.logger("模组日志 | " + pending)
                    pending = ""
            if pending:
                self.logger("模组日志 | " + pending)
        except Exception as exc:
            self.error = str(exc)

    def close(self):
        self.stop_event.set()
        if not self.thread_started:
            return
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError("日志串口读取线程未退出，请检查串口驱动")


class OtaStressMixin:
    """与 BurnWorker 组合，复用 BAT 和继电器协议，不并发占用同一串口。"""

    def setup_stress(self, config, files):
        self.ota_config = config
        self.ota_files = files
        self.monitor = None
        self.report_stream = None
        self.full_log_stream = None
        self.file_lock = threading.Lock()
        self.log_write_error = None
        self.round_records = []
        self.active_record = None

    def _statistics(self):
        records = self.round_records + ([self.active_record] if self.active_record is not None else [])
        def recovery_count(key):
            return sum(isinstance(r.get(key), dict) and r[key].get("recovery") is True for r in records)
        return {
            "completed": sum(r["status"] == "completed" for r in records),
            "total": self.ota_config.rounds,
            "current_round": self.active_record["round"] if self.active_record else 0,
            "test_recovery": recovery_count("test_ota"),
            "standard_recovery": recovery_count("standard_ota"),
            "use_standard": self.ota_config.use_standard,
        }

    def _publish_statistics(self):
        self.statistics.emit(self._statistics())

    def _summary_text(self):
        stats = self._statistics()
        standard = (f"标准包进入 Recovery {stats['standard_recovery']} 轮"
                    if stats["use_standard"] else "标准包升级已跳过（不统计 Recovery）")
        return (f"已完成 {stats['completed']}/{stats['total']} 轮，"
                f"测试包进入 Recovery {stats['test_recovery']} 轮，{standard}，"
                f"已烧录还原 {sum(r.get('restored') is True for r in self.round_records)} 轮")

    @staticmethod
    def _round_result_line(record):
        def stage_text(key):
            value = record.get(key)
            if value == "skipped":
                return "已跳过"
            if not isinstance(value, dict) or value.get("recovery") is None:
                return "未完成 / 未检测"
            return "进入 Recovery" if value.get("recovery") else "观察窗口内未检出 Recovery"
        status = {"completed": "完成", "failed": "失败", "stopped": "已停止"}.get(record["status"], "执行中")
        text = (f"第 {record['round']} 轮 | 测试包：{record['test_package']} | 测试包结果：{stage_text('test_ota')}"
                f" | 标准包：{stage_text('standard_ota')}"
                f" | 标准包路径：{record.get('standard_package') or '已跳过'}"
                f" | 烧录还原：{'已跳过' if record.get('restored') == 'skipped' else ('完成' if record.get('restored') is True else '未完成')}"
                f" | 轮次结果：{status}")
        if record.get("error"):
            text += f" | 中断阶段：{record.get('stage', '未知')} | 原因：{record['error']}"
        return text.replace("\r", " ").replace("\n", " ")

    def emit_log(self, text):
        warning = None
        with self.file_lock:
            if self.full_log_stream is not None and self.log_write_error is None:
                try:
                    self.full_log_stream.write(f"[{datetime.now().isoformat(timespec='milliseconds')}] {text}\n")
                    self.full_log_stream.flush()
                except OSError as exc:
                    self.log_write_error = str(exc)
                    warning = "日志保存失败，将停止压测并清理硬件：" + str(exc)
        if warning:
            super().emit_log(warning)
        super().emit_log(text)

    def _check_log_port(self):
        self.check_stop()
        if self.log_write_error is not None:
            raise RuntimeError("日志保存失败：" + self.log_write_error)
        if self.monitor is not None and self.monitor.error:
            raise RuntimeError("模组日志串口异常：" + self.monitor.error)

    def _check_process(self):
        self._check_log_port()
        super()._check_process()

    def _stress_wait(self, seconds, service_manual=True):
        deadline = time.monotonic() + seconds
        while True:
            self._check_log_port()
            if service_manual:
                self._service_manual()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.stop_event.wait(min(0.05, remaining))

    def _send_ota(self, connection, firmware, on_packet=None):
        cfg = self.ota_config
        profiles = self.protocol_profiles
        metadata = protocol_defs.build_ota_values(firmware, cfg.packet_size)
        skip_header = protocol_defs.ota_header_skipped(cfg.profile_id, cfg.modality, profiles)
        protocol_defs.validate_ota_templates(cfg.profile_id, cfg.modality, profiles, firmware, metadata, skip_header)
        parser = FrameParser()
        connection.reset_input_buffer()

        def send_and_ack(function_key, values):
            frame, mid = protocol_defs.resolve_packet(cfg.profile_id, cfg.modality, function_key, profiles, values)
            for attempt in range(cfg.retries):
                self._check_log_port()
                connection.write(frame)
                connection.flush()
                deadline = time.monotonic() + cfg.ack_timeout
                negative = None
                while time.monotonic() < deadline:
                    self._check_log_port()
                    self._service_manual()
                    frames = parser.feed(connection.read(connection.in_waiting or 1))
                    acknowledged = False
                    for kind, data in frames:
                        if kind == 1 and len(data) >= 2 and data[0] == 3 and data[1] != 0:
                            raise RuntimeError(f"模组报告 OTA 烧写失败，状态 0x{data[1]:02X}")
                        if kind == 0 and len(data) >= 2 and data[0] == mid:
                            if data[1] == 0:
                                acknowledged = True
                            else:
                                negative = data[1]
                    if acknowledged:
                        return
                    if negative is not None:
                        break
                self.emit_log(f"OTA {function_key} 响应{'超时' if negative is None else f'失败 0x{negative:02X}'}，已发送 {attempt + 1}/{cfg.retries} 次")
                self._stress_wait(0.1)
            raise RuntimeError(f"OTA {function_key} 超过发送次数上限；未确认升级完成，不自动断电")

        self.emit_log(f"OTA 元信息：{metadata['FILE_SIZE']} 字节，{metadata['PACKET_COUNT']} 包，MD5={metadata['MD5_ASCII'].decode()}")
        send_and_ack("ota_enter", metadata)
        self._stress_wait(0.1)
        if not skip_header:
            send_and_ack("ota_header", metadata)
            self._stress_wait(0.1)
        last_percent = -1
        for index in range(metadata["PACKET_COUNT"]):
            send_and_ack("ota_packet", protocol_defs.ota_packet_values(firmware, metadata, index))
            if on_packet is not None:
                on_packet(index + 1, metadata["PACKET_COUNT"], time.monotonic())
            percent = (index + 1) * 100 // metadata["PACKET_COUNT"]
            if percent != last_percent:
                self.emit_log(f"OTA 发送进度 {percent}%（{index + 1}/{metadata['PACKET_COUNT']}）")
                last_percent = percent
            self._stress_wait(0.01)
        self.emit_log(f"OTA 所有数据包已确认；仍需等待日志 {self.ota_config.ota_success_keyword!r}，不以发送完毕判定成功")

    def _recover_ota_port(self, label, restart_count, reason=None):
        cfg = self.ota_config
        self._check_log_port()
        self.phase.emit(f"{label}：OTA 串口连接异常，正在重启模组（{restart_count}/{cfg.reconnect_power_attempts}）")
        reason = reason or f"连续 {cfg.reconnect_power_delay:g} 秒未枚举到 {cfg.ota_port}"
        self.emit_log(f"{reason}，执行第 {restart_count} 次电源恢复重启")
        # 只在 OTA 连接阶段调用：尚未发出升级指令，不能用于传输中或烧写等待阶段。
        self.monitor.disarm()
        self.relay.set_channel(self.config.boot_channel, False)
        self.relay.set_channel(self.config.power_channel, False)
        self._stress_wait(cfg.power_off_delay, service_manual=False)
        self._check_log_port()
        self.relay.set_channel(self.config.power_channel, True)
        self.emit_log(f"模组已重新上电，继续等待 {cfg.ota_port} 重新枚举；恢复重启不计入 Recovery 结果")

    def _connect_ota(self, label):
        cfg = self.ota_config
        deadline = time.monotonic() + cfg.reconnect_timeout
        attempt = 0
        restart_count = 0
        missing_since = None
        blocked_since = None
        last_error = "尚未连接"
        while True:
            self._check_log_port()
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"{label}：等待 OTA 串口 {cfg.ota_port} 重连超过 {cfg.reconnect_timeout:g} 秒：{last_error}；"
                    f"已自动重启模组 {restart_count} 次；请检查设备是否重新枚举为其他 COM 号、"
                    "驱动是否正常或端口是否被其他软件占用"
                )
            attempt += 1
            self.phase.emit(f"{label}：正在打开 OTA 串口 {cfg.ota_port}（第 {attempt} 次）")
            self.emit_log(f"准备打开 OTA 串口 {cfg.ota_port}@{cfg.ota_baudrate}（第 {attempt} 次，驱动打开等待最多 5 秒）")
            open_started = time.monotonic()
            try:
                connection = open_serial_bounded(
                    lambda: serial.Serial(cfg.ota_port, cfg.ota_baudrate, timeout=0.1, write_timeout=3),
                    cfg.ota_port, min(5.0, max(0.01, deadline - time.monotonic())), self._check_log_port,
                )
            except SerialOpenBlocked as exc:
                last_error = str(exc)
                now = time.monotonic()
                remaining = deadline - now
                if blocked_since is None:
                    blocked_since = open_started
                # 首次打开已经等了 5 秒；后续仍阻塞时也留出至少 5 秒恢复窗口。
                if (now - blocked_since >= 5.0
                        and restart_count < cfg.reconnect_power_attempts
                        and remaining > cfg.power_off_delay + 1.0):
                    restart_count += 1
                    self._recover_ota_port(label, restart_count, reason=last_error)
                    blocked_since = time.monotonic()
                    missing_since = None
                else:
                    self.emit_log(f"{exc}；等待原打开操作清理，不并发抢开串口"
                                  f"（自动重启 {restart_count}/{cfg.reconnect_power_attempts} 次）")
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._stress_wait(min(1.0, remaining), service_manual=False)
                continue
            except (serial.SerialException, OSError) as exc:
                last_error = str(exc)
                blocked_since = None
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    raise RuntimeError(
                        f"{label}：等待 OTA 串口 {cfg.ota_port} 重连超过 {cfg.reconnect_timeout:g} 秒：{exc}；"
                        f"已自动重启模组 {restart_count} 次；请检查设备是否重新枚举为其他 COM 号、"
                        "驱动是否正常或端口是否被其他软件占用"
                    ) from exc
                try:
                    ports = [port.device for port in list_ports.comports()]
                except OSError as enum_error:
                    missing_since = None
                    self.emit_log(f"查询 COM 列表失败：{enum_error}，本次不触发自动重启")
                else:
                    if cfg.ota_port.strip().casefold() in {port.casefold() for port in ports}:
                        # 存在但打开失败可能是占用或权限问题，不通过断电处理。
                        missing_since = None
                    else:
                        if missing_since is None:
                            missing_since = now
                        if (now - missing_since >= cfg.reconnect_power_delay
                                and restart_count < cfg.reconnect_power_attempts
                                and remaining > cfg.power_off_delay + 1.0):
                            restart_count += 1
                            self.emit_log("当前枚举串口：" + (", ".join(ports) or "无"))
                            self._recover_ota_port(label, restart_count)
                            # 给本次重新上电一个完整的枚举窗口，避免连续断电。
                            missing_since = time.monotonic()
                            remaining = deadline - missing_since
                self.phase.emit(f"{label}：等待 OTA 串口 {cfg.ota_port} 恢复")
                self.emit_log(f"OTA 串口 {cfg.ota_port} 第 {attempt} 次连接失败：{exc}；稍后重新连接")
                if remaining > 0:
                    self._stress_wait(min(1.0, remaining), service_manual=False)
                continue
            self.emit_log(f"OTA 串口已重新连接：{cfg.ota_port}@{cfg.ota_baudrate}（第 {attempt} 次尝试，自动重启 {restart_count} 次）")
            return connection

    def _ota_stage(self, path, label):
        cfg = self.ota_config
        self.phase.emit(f"{label}：发送 OTA 包 {Path(path).name}")
        self.emit_log(f"{label}：{path}")
        self.emit_log("准备切换日志监控阶段（等待监控锁最多 2 秒）")
        self.monitor.disarm()
        self.emit_log("日志监控阶段已切换，开始连接 OTA 串口")
        with self._connect_ota(label) as connection:
            self.emit_log("开始读取 OTA 文件：" + str(path))
            firmware = Path(path).read_bytes()
            if not firmware:
                raise RuntimeError("OTA 包为空：" + str(path))
            self._check_log_port()
            self.emit_log(f"OTA 文件读取完成：{len(firmware)} 字节；准备监听 {cfg.ota_success_keyword!r}")
            self.monitor.arm_success()
            self.emit_log("升级完成关键字监听已就绪，开始准备 OTA 元信息和发送指令")
            self._send_ota(connection, firmware)
            self.phase.emit(f"{label}：等待日志 {cfg.ota_success_keyword}")
            deadline = time.monotonic() + cfg.completion_timeout
            while not self.monitor.success.is_set():
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{label}：等待 {cfg.ota_success_keyword} 超时，不执行断电或烧录还原")
                self._stress_wait(0.05)
        self.emit_log(f"OTA 串口 {cfg.ota_port} 已断开；下次升级前将重新连接")
        self.emit_log(f"{label}：已检测到 {cfg.ota_success_keyword!r}")
        self._stress_wait(cfg.success_delay)
        # OTA 通信串口已关闭；重启时只保留日志串口，防止设备重启后的旧响应串入下一阶段。
        self.phase.emit(f"{label}：电源重启，随后观察 Recovery")
        self.relay.set_channel(self.config.boot_channel, False)
        self.relay.set_channel(self.config.power_channel, False)
        self._stress_wait(cfg.power_off_delay, service_manual=False)
        self.monitor.arm_recovery()
        self.relay.set_channel(self.config.power_channel, True)
        self._stress_wait(cfg.recovery_window, service_manual=False)
        detected = self.monitor.recovery.is_set()
        self.monitor.disarm()
        self.emit_log(f"{label}：" + (f"【进入 Recovery】检测到 {cfg.recovery_keyword!r}" if detected
                                      else f"观察窗口内未检出 {cfg.recovery_keyword!r}（不等同于启动正常）"))
        return {"path": path, "recovery": detected, "observation_seconds": cfg.recovery_window}

    def _restore_version(self):
        self.phase.emit("自动烧录还原上一版本")
        self.emit_log("开始使用主界面配置的 BAT 自动烧录还原")
        if self.monitor is not None:
            self.monitor.disarm()
        self.process = None
        self.reader_thread = None
        self.reader_done.clear()
        self.reader_stop.clear()
        self.reader_error = None
        self.tracker = None
        self.tracking_since = float("inf")
        # 上一次 BAT 的输出已经在 _stop_process 中排空。
        self._start_bat()
        try:
            self._burn(self.relay)
        finally:
            self._stop_process()
            self.process = None
        self.phase.emit("还原烧录完成，等待模组启动")
        self._stress_wait(self.ota_config.boot_wait)

    def _run_rounds(self):
        for index in range(self.ota_config.rounds):
            self._check_log_port()
            path = self.ota_files[index % len(self.ota_files)] if self.ota_config.use_test else "已跳过"
            record = {"round": index + 1, "test_package": path, "status": "running",
                      "standard_package": self.ota_config.standard if self.ota_config.use_standard else None,
                      "stage": "测试包 OTA", "restored": False,
                      "started_at": datetime.now().isoformat(timespec="seconds")}
            if not self.ota_config.use_standard:
                record["standard_ota"] = "skipped"
            if not self.ota_config.use_test:
                record["test_ota"] = "skipped"
            if not self.ota_config.use_restore:
                record["restored"] = "skipped"
            self.active_record = record
            self._publish_statistics()
            self.emit_log(f"========== OTA 压测第 {index + 1}/{self.ota_config.rounds} 轮 ==========")
            try:
                if self.ota_config.use_test:
                    record["test_ota"] = self._ota_stage(path, f"第 {index + 1} 轮 / 测试包")
                    self._publish_statistics()
                if self.ota_config.use_standard:
                    record["stage"] = "标准包 OTA"
                    record["standard_ota"] = self._ota_stage(self.ota_config.standard, f"第 {index + 1} 轮 / 标准包")
                    self._publish_statistics()
                else:
                    self.emit_log("已按配置跳过标准 OTA 包升级")
                if self.ota_config.use_restore:
                    record["stage"] = "自动烧录还原"
                    self._restore_version()
                    record["restored"] = True
                record["status"] = "completed"
                record["stage"] = "完成"
            except Exception as exc:
                record["status"] = "stopped" if self.stop_event.is_set() else "failed"
                record["error"] = str(exc) or "用户停止"
                raise
            finally:
                record["finished_at"] = datetime.now().isoformat(timespec="seconds")
                self.round_records.append(record)
                self.active_record = None
                self._publish_statistics()
                if self.report_stream is not None:
                    self.report_stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    self.report_stream.flush()

    def run(self):
        relay_connection = None
        log_connection = None
        success = False
        message = ""
        self._publish_statistics()
        try:
            self.config.validate(query_only=not self.ota_config.use_restore)
            self.ota_files = self.ota_config.validate(self.config.port)
            self.protocol_profiles, errors = load_protocol_options() if self.ota_config.needs_ota else ([], [])
            if errors and self.ota_config.profile_id not in protocol_defs.BUILTIN_PROFILE_NAMES:
                raise ValueError("自定义协议配置无效：" + "; ".join(errors))
            folder = Path(self.ota_config.output_dir) / datetime.now().strftime("ota_stress_%Y%m%d_%H%M%S_%f")
            folder.mkdir(parents=True, exist_ok=False)
            self.full_log_stream = (folder / "session.log").open("w", encoding="utf-8")
            self.report_stream = (folder / "rounds.jsonl").open("w", encoding="utf-8")
            self.emit_log(f"压测完整日志与逐轮结果保存至：{folder}")
            self.emit_log(f"步骤选择：测试包={self.ota_config.use_test}，标准包={self.ota_config.use_standard}，自动烧录={self.ota_config.use_restore}")
            self.emit_log(f"待测包 {len(self.ota_files)} 个，共 {self.ota_config.rounds} 轮；固定标准包：{self.ota_config.standard if self.ota_config.use_standard else '跳过'}")
            self.emit_log(f"OTA 串口 {self.ota_config.ota_port}@{self.ota_config.ota_baudrate}；日志串口 {self.ota_config.log_port}@{self.ota_config.log_baudrate}；继电器 {self.config.port}@{self.config.baudrate}")
            self.emit_log(f"协议 {self.ota_config.profile_id}/{self.ota_config.modality}；还原 BAT {self.config.bat_path}，模式 {self.config.mode}；Recovery 观察 {self.ota_config.recovery_window:g} 秒")
            for index, path in enumerate(self.ota_files, 1):
                self.emit_log(f"包顺序 {index}：{path}")
            relay_connection = serial.Serial(self.config.port, self.config.baudrate, timeout=0.1, write_timeout=1)
            self.relay = self.make_relay(relay_connection)
            self.relay.query()
            if self.ota_config.needs_ota:
                log_connection = serial.Serial(self.ota_config.log_port, self.ota_config.log_baudrate,
                                               timeout=0.05, write_timeout=1)
                self.monitor = SerialLogMonitor(log_connection, self.ota_config.log_encoding, self.emit_log,
                                                self.ota_config.ota_success_keyword, self.ota_config.recovery_keyword)
                self.monitor.start()
            self.relay.set_channel(self.config.boot_channel, False)
            self.relay.set_channel(self.config.power_channel, True)
            self.phase.emit("压测开始前等待模组启动")
            self._stress_wait(self.ota_config.boot_wait)
            self._run_rounds()
            self._service_manual(closing=True)
            success = True
            message = "OTA 压测完成"
        except Exception as exc:
            message = "OTA 压测已停止" if self.stop_event.is_set() else f"OTA 压测失败：{exc}"
        finally:
            with self.command_lock:
                self.accepting_commands = False
            try:
                self._stop_process()
            except Exception as exc:
                success = False
                message += f"；BAT 清理失败：{exc}"
            if self.relay is not None:
                try:
                    self.relay.release_boot(self.config.boot_channel)
                except Exception as exc:
                    success = False
                    message += f"；短接释放未确认：{exc}"
            if self.monitor is not None:
                try:
                    self.monitor.close()
                except Exception as exc:
                    success = False
                    message += f"；日志监控关闭失败：{exc}"
            for connection in (log_connection, relay_connection):
                if connection is not None:
                    try:
                        connection.close()
                    except Exception as exc:
                        success = False
                        message += f"；串口关闭失败：{exc}"
            self._publish_statistics()
            self.emit_log("========== OTA 压测逐轮结果 ==========")
            if not self.round_records:
                self.emit_log("未产生轮次结果")
            for record in self.round_records:
                self.emit_log(self._round_result_line(record))
            message += "；" + self._summary_text()
            try:
                self.emit_log(message)
            except OSError as exc:
                success = False
                message += f"；日志写入失败：{exc}"
            for stream in (self.report_stream, self.full_log_stream):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError as exc:
                        success = False
                        message += f"；结果文件关闭失败：{exc}"
            with self.file_lock:
                self.full_log_stream = None
            if self.log_write_error is not None:
                success = False
                message += "；完整日志保存失败：" + self.log_write_error
            self.result.emit(success, message)
