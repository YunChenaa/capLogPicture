"""继电器自动烧录工具。安装依赖：pip install PySide6 pyserial

运行：python OTAtest/autoburn.py
BAT 必须在当前进程输出日志，并支持从标准输入选择模式（例如 set /p）。
若 BAT 使用 start 启动独立窗口，请改为直接执行或 start /wait。
"""

import codecs
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from log_archive import RotatingLogArchive
from log_view import LogView

import serial
from serial.tools import list_ports
from PySide6.QtCore import QSettings, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)


CHANNEL_PATTERN = re.compile(r"CH\s*([1-4])\s*:\s*(ON|OFF)\b", re.IGNORECASE)
MESSAGE_PATTERN = re.compile(r'core\s*\(int\)\s*message\s*:\s*"?\s*([0-9:]+;)', re.IGNORECASE)
CORE_PATTERN = re.compile(r"core\s*\(int\)\s*message", re.IGNORECASE)
ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SUCCESS_SEQUENCE = ("0:0:1:6:100;", "0:0:1:6:100;", "0:0:0:8:0;")


def relay_command(channel, enabled):
    """协议：A0 + 通道 + 通断 + 前三字节累加校验。"""
    if channel not in (1, 2, 3, 4):
        raise ValueError("继电器通道必须为 1～4")
    state = int(bool(enabled))
    return bytes((0xA0, channel, state, (0xA0 + channel + state) & 0xFF))


def parse_channels(data):
    text = data.decode("ascii", errors="replace") if isinstance(data, bytes) else data
    return {int(ch): state.upper() == "ON" for ch, state in CHANNEL_PATTERN.findall(text)}


class BurnLogTracker:
    """按消息字段匹配完成序列，忽略时间戳及普通非协议日志。"""

    def __init__(self, start_keyword="core (int)message", end_keywords="\n".join(SUCCESS_SEQUENCE)):
        self.start_keyword = start_keyword.strip()
        self.end_keywords = tuple(s.strip() for s in end_keywords.splitlines() if s.strip())
        if not self.start_keyword or not self.end_keywords:
            raise ValueError("烧录开始和结束标识不能为空")
        self.started = False
        self.finished = False
        self.sequence_index = 0

    def feed(self, line):
        line = ANSI_PATTERN.sub("", line)
        if (CORE_PATTERN.search(line) if self.start_keyword == "core (int)message"
                else self.start_keyword in line):
            self.started = True
        if self.end_keywords == SUCCESS_SEQUENCE:
            # 保留原协议的严格消息序列判定，普通日志不打断序列。
            for match in MESSAGE_PATTERN.finditer(line):
                payload = match.group(1)
                if payload == self.end_keywords[self.sequence_index]:
                    self.sequence_index += 1
                    if self.sequence_index == len(self.end_keywords):
                        self.finished = True
                        self.sequence_index = 0
                else:
                    self.sequence_index = 1 if payload == self.end_keywords[0] else 0
        elif self.end_keywords[self.sequence_index] in line:
            self.sequence_index += 1
            if self.sequence_index == len(self.end_keywords):
                self.finished = True
                self.sequence_index = 0


@dataclass(frozen=True)
class BurnConfig:
    bat_path: str
    port: str
    baudrate: int = 9600
    power_channel: int = 1
    boot_channel: int = 2
    step_delay: float = 0.5
    boot_hold: float = 5.0
    entry_timeout: float = 10.0
    burn_timeout: float = 600.0
    max_attempts: int = 3
    mode_delay: float = 0.5
    encoding: str = "gb18030"
    mode: int = 3
    restart_delay: float = 0.2
    burn_start_keyword: str = "core (int)message"
    burn_end_keywords: str = "\n".join(SUCCESS_SEQUENCE)

    def validate(self, query_only=False):
        if not self.port.strip():
            raise ValueError("请选择继电器串口")
        if self.baudrate <= 0:
            raise ValueError("波特率必须大于 0")
        if self.power_channel not in range(1, 5) or self.boot_channel not in range(1, 5):
            raise ValueError("继电器通道必须为 1～4")
        if self.power_channel == self.boot_channel:
            raise ValueError("模组电源和烧录短接线不能使用同一个通道")
        if query_only:
            return
        path = Path(self.bat_path)
        if not path.is_file() or path.suffix.lower() not in (".bat", ".cmd"):
            raise ValueError("请选择存在的 .bat 或 .cmd 烧录脚本")
        if os.name != "nt":
            raise ValueError("BAT 烧录仅支持 Windows")
        if self.mode not in (1, 2, 3):
            raise ValueError("烧录模式必须为 1、2 或 3")
        if self.restart_delay < 0.01:
            raise ValueError("重启断电间隔必须至少为 0.01 秒")
        if self.step_delay < 0 or self.boot_hold < 0 or self.mode_delay < 0:
            raise ValueError("时序延迟不能小于 0")
        if self.entry_timeout <= 0 or self.burn_timeout <= 0 or self.max_attempts < 1:
            raise ValueError("超时时间和尝试次数必须大于 0")
        codecs.lookup(self.encoding)
        BurnLogTracker(self.burn_start_keyword, self.burn_end_keywords)


class UserStopped(Exception):
    pass


class RelayController:
    def __init__(self, connection, logger, state_callback, check_stop):
        self.connection = connection
        self.log = logger
        self.state_callback = state_callback
        self.check_stop = check_stop

    def query(self, honor_stop=True):
        self.connection.reset_input_buffer()
        self.connection.write(b"\xff")
        self.connection.flush()
        self.log("继电器 TX: FF（查询状态）")
        received = bytearray()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if honor_stop:
                self.check_stop()
            received.extend(self.connection.read(self.connection.in_waiting or 1))
            states = parse_channels(bytes(received))
            if len(states) == 4:
                self.log("继电器 RX: " + bytes(received).hex(" ").upper())
                self.log(" | ".join(f"CH{ch}: {'ON' if states[ch] else 'OFF'}" for ch in range(1, 5)))
                self.state_callback(states)
                return states
        raise RuntimeError(f"继电器状态查询超时/响应不完整：{bytes(received)!r}")

    def set_channel(self, channel, enabled, honor_stop=True, force=False):
        states = self.query(honor_stop)
        if not force and states[channel] == enabled:
            self.log(f"CH{channel} 已为 {'ON' if enabled else 'OFF'}，无需切换")
            return
        if honor_stop:
            self.check_stop()
        command = relay_command(channel, enabled)
        self.connection.write(command)
        self.connection.flush()
        self.log(f"继电器 TX: {command.hex(' ').upper()}（CH{channel} {'ON' if enabled else 'OFF'}）")
        time.sleep(0.1)
        actual = self.query(honor_stop)
        if actual[channel] != enabled:
            raise RuntimeError(f"CH{channel} 切换失败，查询状态与目标不一致")

    def cut_power(self, channel, completed=None, on_sent=None):
        """定时掉电先发指令，避免前置查询占用目标时间。"""
        self.check_stop()
        command = relay_command(channel, False)
        if completed is not None and completed.is_set():
            return None
        sent_at = time.monotonic()
        self.connection.write(command)
        if on_sent is not None:
            on_sent(sent_at)
        self.connection.flush()
        self.log(f"掉电注入 TX: {command.hex(' ').upper()}（CH{channel} OFF）")
        time.sleep(0.1)
        if self.query()[channel]:
            raise RuntimeError(f"CH{channel} 掉电未确认，停止压测")
        return sent_at

    def release_boot(self, channel):
        """停止/异常时即使查询失败也先发 OFF；随后尽量确认。"""
        command = relay_command(channel, False)
        self.connection.write(command)
        self.connection.flush()
        self.log(f"清理：发送 CH{channel} OFF：{command.hex(' ').upper()}")
        time.sleep(0.1)
        if self.query(honor_stop=False)[channel]:
            raise RuntimeError(f"CH{channel} 仍为 ON，请人工断开烧录短接线")


class BurnWorker(QThread):
    log = Signal(str)
    phase = Signal(str)
    # Python object 保留整数通道键，避免 QVariantMap 转换导致界面查找失败。
    relay_states = Signal(object)
    result = Signal(bool, str)

    def __init__(self, config, query_only=False, manual_command=None):
        super().__init__()
        self.config = config
        self.query_only = query_only
        self.manual_command = manual_command
        self.manual_queue = queue.Queue()
        self.command_lock = threading.Lock()
        self.accepting_commands = True
        self.relay = None
        self.stop_event = threading.Event()
        self.output_queue = queue.Queue()
        self.process = None
        self.reader_thread = None
        self.reader_done = threading.Event()
        self.reader_stop = threading.Event()
        self.reader_error = None
        self.tracker = None
        self.tracking_since = float("inf")

    def stop(self):
        with self.command_lock:
            self.accepting_commands = False
            self.stop_event.set()

    def enqueue_manual(self, channel, enabled):
        with self.command_lock:
            if not self.accepting_commands:
                return False
            self.manual_queue.put((channel, enabled))
            return True

    def _service_manual(self, closing=False):
        if self.relay is None:
            return
        with self.command_lock:
            if closing:
                self.accepting_commands = False
        while not self.manual_queue.empty():
            self.check_stop()
            channel, enabled = self.manual_queue.get_nowait()
            self.emit_log(f"手动控制 CH{channel}：{'接通' if enabled else '断开'}")
            self.relay.set_channel(channel, enabled)

    def check_stop(self):
        if self.stop_event.is_set():
            raise UserStopped()

    def emit_log(self, text):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log.emit(f"[{timestamp}] {text}")

    def _read_output(self):
        decoder = codecs.getincrementaldecoder(self.config.encoding)(errors="replace")
        buffer = ""
        try:
            while not self.reader_stop.is_set():
                if os.name == "nt":
                    # PeekNamedPipe 避免后台子进程持有 stdout 时 read1 永久阻塞。
                    import ctypes
                    import msvcrt
                    from ctypes import wintypes
                    available = wintypes.DWORD()
                    peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
                    peek.argtypes = (wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                     wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
                    peek.restype = wintypes.BOOL
                    handle = msvcrt.get_osfhandle(self.process.stdout.fileno())
                    if not peek(handle, None, 0, None, ctypes.byref(available), None):
                        error = ctypes.get_last_error()
                        if error in (109, 232):
                            break
                        raise OSError(error, "无法读取 BAT 输出管道")
                    if not available.value:
                        self.reader_stop.wait(0.02)
                        continue
                    raw = self.process.stdout.read1(min(4096, available.value))
                else:
                    import select
                    if not select.select([self.process.stdout], [], [], 0.05)[0]:
                        continue
                    raw = self.process.stdout.read1(4096)
                if not raw:
                    break
                buffer += decoder.decode(raw)
                # 同时支持 CR 进度行和 LF 日志，保留跨数据块的半行。
                lines = re.split(r"\r\n|\r|\n", buffer)
                buffer = lines.pop()
                for line in lines:
                    if line:
                        self.output_queue.put((time.monotonic(), line))
            buffer += decoder.decode(b"", final=True)
            if buffer:
                self.output_queue.put((time.monotonic(), buffer))
        except (OSError, ValueError) as exc:
            self.reader_error = str(exc)
        finally:
            self.reader_done.set()

    def _pump_logs(self):
        # 限制单次读取量，避免海量输出导致停止按钮或超时检查失效。
        for _ in range(1000):
            try:
                received_at, line = self.output_queue.get_nowait()
            except queue.Empty:
                break
            self.emit_log("BAT | " + line)
            if self.tracker is not None and received_at >= self.tracking_since:
                self.tracker.feed(line)

    def _check_process(self):
        self._service_manual()
        self._pump_logs()
        if self.reader_done.is_set() and self.output_queue.empty():
            if self.reader_error:
                raise RuntimeError("读取烧录日志失败：" + self.reader_error)
            if self.process.poll() is not None and not (self.tracker and self.tracker.finished):
                raise RuntimeError(f"烧录脚本已退出（退出码 {self.process.returncode}），未检测到完整完成标志")

    def _wait(self, seconds, monitor=True):
        deadline = time.monotonic() + seconds
        while True:
            self.check_stop()
            if monitor:
                self._check_process()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.stop_event.wait(min(0.05, remaining))

    def _start_bat(self):
        path = str(Path(self.config.bat_path).resolve())
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        # cmd /s /c 的最外层引号包裹命令，内部引号保护含空格的 BAT 路径。
        command = f'"{comspec}" /d /q /s /c ""{path}""'
        self.emit_log(f"启动 BAT：{path}")
        self.process = subprocess.Popen(
            command, cwd=str(Path(path).parent), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self.reader_thread.start()
        self._wait(self.config.mode_delay)
        try:
            self.process.stdin.write(f"{self.config.mode}\r\n".encode("ascii"))
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"无法向 BAT 输入模式 {self.config.mode}，请确认脚本支持标准输入") from exc
        self.emit_log(f"已发送模式选择：{self.config.mode} + 回车")

    def _burn(self, relay):
        cfg = self.config
        for attempt in range(1, cfg.max_attempts + 1):
            self.check_stop()
            self.phase.emit(f"第 {attempt}/{cfg.max_attempts} 次：断开模组电源")
            self.tracker = None
            self.tracking_since = float("inf")
            self._check_process()
            relay.set_channel(cfg.power_channel, False)
            self._wait(cfg.step_delay)
            self.phase.emit("接通烧录短接线")
            relay.set_channel(cfg.boot_channel, True)
            self._wait(cfg.step_delay)
            self.phase.emit("接通模组电源，保持烧录短接")
            # 从上电开始记录日志，不能漏掉保持短接期间出现的烧录标志。
            self.tracker = BurnLogTracker(cfg.burn_start_keyword, cfg.burn_end_keywords)
            self.tracking_since = time.monotonic()
            relay.set_channel(cfg.power_channel, True)
            self._wait(cfg.boot_hold)
            relay.set_channel(cfg.boot_channel, False)
            self.phase.emit("已断开短接线，等待进入烧录")
            entry_deadline = time.monotonic() + cfg.entry_timeout
            while not self.tracker.started:
                self._check_process()
                self.check_stop()
                if self.tracker.started:
                    break
                if time.monotonic() >= entry_deadline:
                    break
                self._wait(0.05)
            if not self.tracker.started:
                self.emit_log(f"等待 {cfg.entry_timeout:g} 秒未发现开始标识 {cfg.burn_start_keyword!r}，重新执行断电时序")
                continue
            self.phase.emit("正在烧录，等待完整完成标志")
            self.emit_log(f"已检测到开始标识 {cfg.burn_start_keyword!r}，进入烧录监控（不再自动断电重试）")
            burn_deadline = time.monotonic() + cfg.burn_timeout
            while not self.tracker.finished:
                self._check_process()
                self.check_stop()
                if self.tracker.finished:
                    break
                if time.monotonic() >= burn_deadline:
                    raise RuntimeError(f"烧录超过 {cfg.burn_timeout:g} 秒仍未完成；为避免中断写入，不自动断电重试")
                self._wait(0.05)
            self.emit_log(f"检测到完整结束标识：{' → '.join(self.tracker.end_keywords)}，烧录成功")
            self._restart_module(relay)
            return
        raise RuntimeError(f"已尝试 {cfg.max_attempts} 次，仍未进入烧录；请检查 BAT 模式、接线及模组状态")

    def _restart_module(self, relay):
        self.check_stop()
        # 先执行已排队的手动操作，再完成独立的重启时序，避免指令插入断电间隔。
        self._service_manual()
        self.phase.emit("烧录完成，正在重启模组")
        relay.set_channel(self.config.boot_channel, False)
        relay.set_channel(self.config.power_channel, False)
        self.emit_log(f"模组已断电，等待 {self.config.restart_delay:g} 秒后重新上电")
        self._wait(self.config.restart_delay, monitor=False)
        relay.set_channel(self.config.power_channel, True)
        self.emit_log("模组已重新上电，重启电源时序完成")

    def _stop_process(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            self.emit_log("关闭本次启动的 BAT 及其子进程")
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    capture_output=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if result.returncode and self.process.poll() is None:
                    self.emit_log("警告：结束进程树失败，将结束 BAT 主进程；请检查是否残留烧录程序")
                    self.process.kill()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.emit_log(f"警告：关闭 BAT 失败：{exc}；请人工检查烧录进程")
                if self.process.poll() is None:
                    self.process.kill()
        if self.reader_thread is not None:
            self.reader_thread.join(timeout=0.5)
            if self.reader_thread.is_alive() and self.process.poll() is not None:
                self.emit_log("警告：BAT 已退出但输出管道仍被占用；停止读取，请检查是否有独立烧录子进程残留")
            self.reader_stop.set()
            self.reader_thread.join(timeout=2)
            if self.reader_thread.is_alive():
                raise RuntimeError("BAT 输出读取线程未退出，未关闭被占用的管道；请检查残留烧录程序")
        self._pump_logs()
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                stream.close()

    def run(self):
        connection = None
        relay = None
        success = False
        message = ""
        try:
            self.config.validate(self.query_only)
            if not self.query_only:
                self.phase.emit(f"启动 BAT，自动选择模式 {self.config.mode}")
                self._start_bat()
            self.check_stop()
            self.phase.emit("连接继电器串口")
            connection = serial.Serial(
                self.config.port, self.config.baudrate, timeout=0.1, write_timeout=1,
            )
            relay = RelayController(connection, self.emit_log, self.relay_states.emit, self.check_stop)
            self.relay = relay
            if self.query_only:
                if self.manual_command is not None:
                    channel, enabled = self.manual_command
                    relay.set_channel(channel, enabled)
                    message = f"CH{channel} 已{'接通' if enabled else '断开'}"
                else:
                    relay.query()
                    message = "继电器状态查询完成"
            else:
                self._burn(relay)
                message = "烧录成功，模组已完成断电重启（电源接通，烧录短接线断开）"
            self._service_manual(closing=True)
            success = True
        except UserStopped:
            message = "已停止（模组电源保持现状）"
        except Exception as exc:
            message = f"操作失败：{exc}"
        finally:
            with self.command_lock:
                self.accepting_commands = False
            if not self.manual_queue.empty():
                self.emit_log("操作结束，尚未执行的手动控制已取消；请在空闲后重新操作")
            # 停止时先结束烧录程序，再释放短接；不擅自切断模组电源。
            try:
                self._stop_process()
            except Exception as exc:
                success = False
                message += f"；进程清理异常：{exc}"
            if relay is not None and not self.query_only:
                try:
                    relay.release_boot(self.config.boot_channel)
                except Exception as exc:
                    success = False
                    message += f"；警告：无法确认短接线已断开：{exc}，请人工检查"
            if connection is not None:
                connection.close()
            self.emit_log(message)
            self.result.emit(success, message)


# 延迟到烧录工作类定义后组合，OTA 模块无需反向导入主窗口。
from ota_stress import OtaStressMixin


class OtaStressWorker(OtaStressMixin, BurnWorker):
    statistics = Signal(object)

    def __init__(self, config, ota_config, files):
        super().__init__(config)
        self.setup_stress(ota_config, files)

    def make_relay(self, connection):
        return RelayController(connection, self.emit_log, self.relay_states.emit, self.check_stop)


from power_stress import PowerStressDialog, PowerStressMixin


class PowerStressWorker(PowerStressMixin, OtaStressWorker):
    pass


class AutoBurnWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.log_archive = RotatingLogArchive(Path(__file__).resolve().parent / "logs")
        self.log_archive_failed = False
        self.settings = QSettings("RelayTools", "AutoBurn")
        self.setWindowTitle("模组自动烧录 · 四路串口继电器")
        self.resize(980, 800)
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        self.vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        root_layout.addWidget(self.vertical_splitter)
        self.controls_panel = QWidget()
        layout = QVBoxLayout(self.controls_panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.vertical_splitter.addWidget(self.controls_panel)
        self.logs_fullscreen = False
        self._saved_log_sizes = None
        self.ota_statistics_label = QLabel("OTA 压测：尚未开始")
        self.ota_statistics_label.setWordWrap(True)
        layout.addWidget(self.ota_statistics_label)

        self.config_group = QGroupBox("烧录配置")
        form = QFormLayout(self.config_group)
        self.bat_path = QLineEdit()
        self.bat_path.setPlaceholderText("选择 .bat / .cmd 烧录脚本")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.bat_path, 1)
        path_row.addWidget(browse)
        form.addRow("烧录脚本", path_row)

        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(90)
        self.port.setMinimumContentsLength(6)
        self.port.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.refresh_button = QPushButton("刷新串口")
        self.refresh_button.clicked.connect(self._refresh_ports)
        self.baudrate = QComboBox()
        self.baudrate.setEditable(True)
        self.baudrate.addItems(["9600", "19200", "38400", "57600", "115200"])
        serial_row = QHBoxLayout()
        serial_row.addWidget(self.port, 1)
        serial_row.addWidget(self.refresh_button)
        serial_row.addWidget(QLabel("波特率"))
        serial_row.addWidget(self.baudrate)

        self.power_channel = QComboBox()
        self.boot_channel = QComboBox()
        for ch in range(1, 5):
            self.power_channel.addItem(f"CH{ch}", ch)
            self.boot_channel.addItem(f"CH{ch}", ch)
        self.boot_channel.setCurrentIndex(1)
        serial_row.addWidget(QLabel("电源"))
        serial_row.addWidget(self.power_channel)
        serial_row.addWidget(QLabel("短接"))
        serial_row.addWidget(self.boot_channel)
        self.mode = QComboBox()
        for mode in (1, 2, 3):
            self.mode.addItem(f"模式 {mode}", mode)
        self.mode.setCurrentIndex(2)
        serial_row.addWidget(QLabel("烧录模式"))
        serial_row.addWidget(self.mode)
        form.addRow("继电器串口", serial_row)
        layout.addWidget(self.config_group)

        functions_menu = self.menuBar().addMenu("功能")
        self.ota_action = functions_menu.addAction("OTA 压测…")
        self.ota_action.triggered.connect(self._start_ota_stress)
        settings_menu = self.menuBar().addMenu("设置")
        self.timing_action = settings_menu.addAction("时序、日志编码与标识…")
        self.timing_action.triggered.connect(self._show_settings)
        help_menu = self.menuBar().addMenu("帮助")
        help_menu.addAction("接线与操作说明…", self._show_help)
        self.settings_dialog = QDialog(self)
        self.settings_dialog.setWindowTitle("时序、日志编码与标识")
        settings_layout = QVBoxLayout(self.settings_dialog)
        self.timing_group = QGroupBox("时序与超时（单位：秒）")
        grid = QGridLayout(self.timing_group)
        self.step_delay = self._seconds(0.5, 0, 60)
        self.boot_hold = self._seconds(5, 0, 120)
        self.entry_timeout = self._seconds(10, 0.1, 600)
        self.burn_timeout = self._seconds(600, 1, 86400)
        self.mode_delay = self._seconds(0.5, 0, 60)
        self.restart_delay = self._seconds(0.2, 0.01, 60)
        self.max_attempts = QSpinBox()
        self.max_attempts.setRange(1, 100)
        self.max_attempts.setValue(3)
        fields = [
            ("断电→短接 / 短接→上电间隔", self.step_delay),
            ("上电后保持短接", self.boot_hold),
            ("释放短接后等待进入烧录", self.entry_timeout),
            ("烧录完成等待上限", self.burn_timeout),
            ("BAT 启动后输入模式延迟", self.mode_delay),
            ("进入烧录最大尝试次数（含首次）", self.max_attempts),
            ("成功后重启断电间隔", self.restart_delay),
        ]
        for index, (label, widget) in enumerate(fields):
            row, col = divmod(index, 2)
            grid.addWidget(QLabel(label), row, col * 2)
            grid.addWidget(widget, row, col * 2 + 1)
        settings_layout.addWidget(self.timing_group)
        self.encoding = QComboBox()
        self.encoding.addItems(["gb18030", "utf-8", "cp936", "cp1252"])
        encoding_form = QFormLayout()
        encoding_form.addRow("BAT 日志编码", self.encoding)
        settings_layout.addLayout(encoding_form)
        self.burn_start_keyword = QLineEdit("core (int)message")
        self.burn_end_keywords = QPlainTextEdit("\n".join(SUCCESS_SEQUENCE))
        self.burn_end_keywords.setMaximumHeight(90)
        keyword_form = QFormLayout()
        keyword_form.addRow("烧录开始关键词", self.burn_start_keyword)
        keyword_form.addRow("烧录结束关键词（每行一个，按顺序匹配）", self.burn_end_keywords)
        self.ota_success_keyword = QLineEdit("startBurn success")
        self.recovery_keyword = QLineEdit("dwc")
        keyword_form.addRow("OTA 升级结束关键词", self.ota_success_keyword)
        keyword_form.addRow("进入 Recovery 关键词", self.recovery_keyword)
        settings_layout.addLayout(keyword_form)
        keyword_note = QLabel("关键词不是正则。烧录关键词区分大小写；结束可填单行关键词或多行顺序序列，普通日志不打断顺序；默认三条消息保持原协议严格判定。OTA / Recovery 按文本包含匹配，不区分大小写，支持跨串口读取块。")
        keyword_note.setWordWrap(True)
        settings_layout.addWidget(keyword_note)
        dialog_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        dialog_buttons.accepted.connect(self._accept_settings)
        dialog_buttons.rejected.connect(self.settings_dialog.reject)
        settings_layout.addWidget(dialog_buttons)

        relay_group = QGroupBox("继电器状态与手动控制")
        relay_grid = QGridLayout(relay_group)
        self.channel_labels = {}
        self.channel_buttons = {}
        for ch in range(1, 5):
            row, col = divmod(ch - 1, 2)
            label = QLabel(f"CH{ch}: 未查询")
            self.channel_labels[ch] = label
            relay_grid.addWidget(label, row, col * 3)
            for offset, (enabled, title) in enumerate(((True, "−"), (False, "O")), 1):
                button = QPushButton(title)
                button.setFixedWidth(40)
                action = '接通' if enabled else '断开'
                button.setToolTip(f"CH{ch} {action}")
                button.setAccessibleName(f"CH{ch} {action}")
                button.clicked.connect(lambda checked=False, c=ch, state=enabled: self._manual_control(c, state))
                self.channel_buttons[ch, enabled] = button
                relay_grid.addWidget(button, row, col * 3 + offset)
        self.query_button = QPushButton("查询继电器状态")
        self.query_button.clicked.connect(lambda: self._start(query_only=True))
        self.query_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.query_button.setFixedWidth(130)
        relay_grid.addWidget(self.query_button, 0, 6, 2, 1)
        relay_grid.setColumnStretch(0, 1)
        relay_grid.setColumnStretch(3, 1)
        layout.addWidget(relay_group)

        buttons = QHBoxLayout()
        self.start_button = QPushButton("开始自动烧录")
        self.start_button.setMinimumHeight(36)
        self.start_button.clicked.connect(lambda: self._start(query_only=False))
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        self.archive_button = QPushButton("打开归档")
        self.archive_button.clicked.connect(self._open_archive)
        self.archive_button.setToolTip(str(self.log_archive.output_dir))
        save_button = QPushButton("保存日志…")
        save_button.clicked.connect(self._save_log)
        clear_button = QPushButton("清空日志")
        clear_button.clicked.connect(self._clear_log)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch()
        buttons.addWidget(self.archive_button)
        buttons.addWidget(save_button)
        buttons.addWidget(clear_button)
        layout.addLayout(buttons)
        self.phase_label = QLabel("就绪")
        layout.addWidget(self.phase_label)
        self.logs_panel = QWidget()
        logs_layout = QVBoxLayout(self.logs_panel)
        logs_layout.setContentsMargins(0, 0, 0, 0)
        log_toolbar = QHBoxLayout()
        log_toolbar.addWidget(QLabel("拖动上方分隔条调整高度；拉到顶部可全屏日志"))
        log_toolbar.addStretch()
        self.log_stop_button = QPushButton("停止任务")
        self.log_stop_button.setEnabled(False)
        self.log_stop_button.clicked.connect(self._stop)
        log_toolbar.addWidget(self.log_stop_button)
        self.fullscreen_button = QPushButton("全屏日志（F11）")
        self.fullscreen_button.clicked.connect(self._toggle_logs_fullscreen)
        log_toolbar.addWidget(self.fullscreen_button)
        logs_layout.addLayout(log_toolbar)
        self.log_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.log_splitter.setChildrenCollapsible(False)
        self.log_view = LogView()
        self.module_log_view = LogView()
        for title, view, placeholder in (
            ("操作日志", self.log_view, "OTA 流程、继电器、BAT、状态与错误"),
            ("模组日志", self.module_log_view, "日志串口的模组输出"),
        ):
            group = QGroupBox(title)
            group_layout = QVBoxLayout(group)
            view.setReadOnly(True)
            view.setMaximumBlockCount(20000)
            view.setPlaceholderText(placeholder + "（最近 20000 行，完整日志自动归档）")
            latest = QPushButton("回到最新")
            latest.clicked.connect(view.jump_to_latest)
            latest.setToolTip("回到底部并恢复自动跟随；向上滚动暂停跟随")
            group_layout.addWidget(latest, 0, Qt.AlignmentFlag.AlignRight)
            group_layout.addWidget(view)
            self.log_splitter.addWidget(group)
        self.log_splitter.setSizes([490, 490])
        logs_layout.addWidget(self.log_splitter, 1)
        self.vertical_splitter.addWidget(self.logs_panel)
        self.vertical_splitter.setCollapsible(0, True)
        self.vertical_splitter.setCollapsible(1, False)
        self.vertical_splitter.setStretchFactor(0, 0)
        self.vertical_splitter.setStretchFactor(1, 1)
        self.vertical_splitter.setSizes([300, 500])
        self.vertical_splitter.splitterMoved.connect(self._log_splitter_moved)
        self.fullscreen_shortcut = QShortcut(QKeySequence("F11"), self)
        self.fullscreen_shortcut.activated.connect(self._toggle_logs_fullscreen)
        self.escape_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.escape_shortcut.activated.connect(self._exit_logs_fullscreen)
        self.statusBar().showMessage("准备就绪 · 请确认通道接线后开始")
        self._refresh_ports()
        self._load_settings()

    def _open_archive(self):
        directory = self.log_archive.directory or self.log_archive.output_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
                raise OSError("无法打开归档目录")
        except OSError as exc:
            QMessageBox.warning(self, "打开归档失败", str(exc))

    def _log_splitter_moved(self, position, index):
        sizes = self.vertical_splitter.sizes()
        if not self.logs_fullscreen and sizes[0] == 0:
            self._toggle_logs_fullscreen()
        elif not self.logs_fullscreen:
            self._saved_log_sizes = sizes

    def _toggle_logs_fullscreen(self):
        if self.logs_fullscreen:
            self._exit_logs_fullscreen()
            return
        sizes = self.vertical_splitter.sizes()
        if sizes[0] > 0:
            self._saved_log_sizes = sizes
        self._previous_window_state = self.windowState()
        self.logs_fullscreen = True
        self.controls_panel.hide()
        self.menuBar().hide()
        self.fullscreen_button.setText("退出全屏（Esc / F11）")
        self.showFullScreen()

    def _exit_logs_fullscreen(self):
        if not self.logs_fullscreen:
            return
        self.logs_fullscreen = False
        self.setWindowState(self._previous_window_state)
        self.controls_panel.show()
        self.menuBar().show()
        self.vertical_splitter.setSizes(self._saved_log_sizes or [300, 500])
        self.fullscreen_button.setText("全屏日志（F11）")

    @staticmethod
    def _seconds(default, minimum, maximum):
        widget = QDoubleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setDecimals(2)
        widget.setSingleStep(0.1)
        widget.setValue(default)
        return widget

    def _accept_settings(self):
        try:
            BurnLogTracker(self.burn_start_keyword.text(), self.burn_end_keywords.toPlainText())
            if not self.ota_success_keyword.text().strip() or not self.recovery_keyword.text().strip():
                raise ValueError("OTA 升级结束和 Recovery 关键词不能为空")
        except ValueError as exc:
            QMessageBox.warning(self.settings_dialog, "标识配置有误", str(exc))
            return
        self.settings_dialog.accept()

    def _show_settings(self):
        names = ("step_delay", "boot_hold", "entry_timeout", "burn_timeout", "max_attempts", "mode_delay", "restart_delay")
        previous = {name: getattr(self, name).value() for name in names}
        previous_keywords = {name: getattr(self, name).text()
                             for name in ("burn_start_keyword", "ota_success_keyword", "recovery_keyword")}
        previous_end = self.burn_end_keywords.toPlainText()
        previous_encoding = self.encoding.currentText()
        if self.settings_dialog.exec() == QDialog.DialogCode.Accepted:
            self._save_settings()
        else:
            for name, value in previous.items():
                getattr(self, name).setValue(value)
            self.encoding.setCurrentText(previous_encoding)
            for name, value in previous_keywords.items():
                getattr(self, name).setText(value)
            self.burn_end_keywords.setPlainText(previous_end)

    def _show_help(self):
        QMessageBox.information(
            self, "接线与操作说明",
            "【接线约定】\n使用继电器常开触点：ON = 模组供电 / 烧录线短接，OFF = 断电 / 断开短接。\n"
            "电源与短接线必须使用不同通道，串口默认 8N1、无流控。\n\n"
            "【自动烧录】\n选择 BAT 和模式 1～3（默认 3），设置串口及通道后开始。\n"
            "仅未进入烧录时自动重试；进入烧录后超时不自动断电。\n"
            "匹配完整完成标志后，先确认短接断开，再断开电源，默认等待 200ms 后重新上电。\n"
            "实际断电时长还包含串口查询确认耗时，时序及日志编码可在顶部“设置”中调整。\n\n"
            "【手动控制与停止】\n手动按钮在烧录期间也可操作，但可能中断烧录；后续自动时序可能再次改变状态。\n"
            "停止/异常会关闭本次 BAT 并尝试释放短接，电源保持停止时状态；若在重启断电期间停止，电源将保持断开。\n\n"
            "【OTA 压测】\n从“功能 → OTA 压测”选择 BIN 文件或文件夹（递归查找）、固定标准包、OTA 通信串口及日志串口。\n"
            "三个串口（继电器 / OTA / 日志）必须不同。关闭其他占用串口的软件；OTA 保持所选波特率，不主动切换模组波特率。\n"
            "步骤 1 测试包及 Recovery 判断 → 步骤 2 标准包及 Recovery 判断 → 步骤 3 自动烧录；至少选一步，只执行所选项。\n"
            "步骤 1 可选进度 / 延时掉电；不选掉电即正常 OTA。掉电重启观察后进入下一已选步骤。只选烧录无需 OTA / 日志串口。\n"
            "检测到设置中的 Recovery 关键词会记录进入 Recovery 并继续后续步骤；未检出只表示观察窗口内未出现该关键字。\n"
            "BIN 按相对路径排序，轮数超过包数后循环；请使用仅包含待测 OTA 包的目录，不要混入其他烧录 BIN。\n"
            "完整会话日志及逐轮结果自动保存；通信、升级或烧录失败将停止压测，不自动跳过故障继续断电。\n\n"
            "【BAT 要求】\nBAT 须支持标准输入选择模式，并在当前进程输出日志。独立窗口或特殊控制台输入可能需要适配。",
        )

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择烧录脚本", self.bat_path.text(), "批处理脚本 (*.bat *.cmd)",
        )
        if path:
            self.bat_path.setText(path)

    def _refresh_ports(self):
        current = self.port.currentText()
        self.port.clear()
        for item in list_ports.comports():
            self.port.addItem(item.device)
            self.port.setItemData(self.port.count() - 1, item.description, role=3)
        if current:
            self.port.setCurrentText(current)

    def _config(self):
        return BurnConfig(
            bat_path=self.bat_path.text().strip(), port=self.port.currentText().strip(),
            baudrate=int(self.baudrate.currentText()),
            power_channel=self.power_channel.currentData(), boot_channel=self.boot_channel.currentData(),
            step_delay=self.step_delay.value(), boot_hold=self.boot_hold.value(),
            entry_timeout=self.entry_timeout.value(), burn_timeout=self.burn_timeout.value(),
            max_attempts=self.max_attempts.value(), mode_delay=self.mode_delay.value(),
            encoding=self.encoding.currentText(), mode=self.mode.currentData(),
            restart_delay=self.restart_delay.value(),
            burn_start_keyword=self.burn_start_keyword.text().strip(),
            burn_end_keywords=self.burn_end_keywords.toPlainText().strip(),
        )

    def _start_ota_stress(self, checked=False):
        if self.worker is not None:
            return
        try:
            config = self._config()
            config.validate(query_only=True)
        except (ValueError, LookupError) as exc:
            QMessageBox.warning(self, "继电器配置有误", str(exc))
            return
        dialog = PowerStressDialog(self.settings, config.port, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            config.validate(query_only=not dialog.config.use_restore)
        except (ValueError, LookupError) as exc:
            QMessageBox.warning(self, "自动烧录配置有误", str(exc))
            return
        self._save_settings()
        self._append_log("\n" + "=" * 60 + "\n开始 OTA 压测")
        self.worker = PowerStressWorker(config, dialog.config, dialog.files)
        self._launch_worker()

    def _launch_worker(self):
        if isinstance(self.worker, OtaStressWorker):
            self.worker.statistics.connect(self._show_ota_statistics)
            self._show_ota_statistics(self.worker._statistics())
        self.worker.log.connect(self._append_log)
        self.worker.phase.connect(self.phase_label.setText)
        self.worker.relay_states.connect(self._show_states)
        self.worker.result.connect(self._on_result)
        self.worker.finished.connect(self._on_finished)
        self._set_busy(True)
        self.worker.start()

    def _manual_control(self, channel, enabled):
        if self.worker is not None:
            if self.worker.enqueue_manual(channel, enabled):
                self._append_log(f"已排队：CH{channel} {'接通' if enabled else '断开'}，等待当前串口操作完成")
            else:
                self._append_log("正在结束操作，未发送手动指令；请等待操作结束后再点击")
            return
        self._start(query_only=True, manual_command=(channel, enabled))

    def _start(self, query_only=False, manual_command=None):
        if self.worker is not None:
            return
        try:
            config = self._config()
            config.validate(query_only)
        except (ValueError, LookupError) as exc:
            QMessageBox.warning(self, "配置有误", str(exc))
            return
        self._save_settings()
        for label in self.channel_labels.values():
            label.setText(label.text().split(":")[0] + ": 未查询")
        self._append_log("\n" + "=" * 60)
        self._append_log("开始查询" if query_only else f"开始自动烧录（模式 {config.mode}）")
        self.worker = BurnWorker(config, query_only, manual_command)
        self._launch_worker()

    def _set_busy(self, busy):
        self.config_group.setEnabled(not busy)
        self.timing_group.setEnabled(not busy)
        self.timing_action.setEnabled(not busy)
        self.ota_action.setEnabled(not busy)
        for button in self.channel_buttons.values():
            button.setEnabled(not (busy and isinstance(self.worker, PowerStressWorker)
                                   and self.worker.ota_config.power_cut_enabled))
        self.start_button.setEnabled(not busy)
        self.query_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.log_stop_button.setEnabled(busy)

    def _stop(self):
        if self.worker is not None:
            self.worker.stop()
            self.stop_button.setEnabled(False)
            self.log_stop_button.setEnabled(False)
            self.phase_label.setText("正在停止并清理短接线，请稍候…")

    def _append_log(self, text):
        if not self.log_archive_failed:
            try:
                self.log_archive.append(text)
                self.archive_button.setToolTip(f"{self.log_archive.directory}（每 20000 行一个文件）")
            except OSError as exc:
                self._log_archive_error(exc)
        is_module = re.match(r"^(?:\[\d{2}:\d{2}:\d{2}\.\d{3}\] )?模组日志 \| ", text)
        view = self.module_log_view if is_module else self.log_view
        view.appendPlainText(text)

    def _log_archive_error(self, exc):
        self.log_archive_failed = True
        try:
            self.log_archive.close()
        except OSError:
            pass
        message = f"日志自动归档失败：{exc}。后续日志仅保留界面最近 20000 行，请检查磁盘空间和目录权限。"
        self.archive_button.setText("归档失败 · 打开目录")
        self.archive_button.setToolTip(message)
        self.archive_button.setStyleSheet("color: #c62828;")
        self.log_view.appendPlainText(message)
        self.statusBar().showMessage(message)

    @Slot(object)
    def _show_ota_statistics(self, stats):
        standard = (f"标准包进入 Recovery：{stats['standard_recovery']} 轮"
                    if stats["use_standard"] else "标准包：已跳过")
        current = f" | 当前第 {stats['current_round']} 轮" if stats["current_round"] else ""
        mode = stats.get("mode", "OTA 压测")
        points = f" | 已记录测试点：{stats['points']}" if "points" in stats else ""
        unit = "次" if "points" in stats else "轮"
        test = (f"测试包进入 Recovery：{stats['test_recovery']} {unit}"
                if stats.get("use_test", True) else "测试包：已跳过")
        self.ota_statistics_label.setText(
            f"{mode} | 已完成：{stats['completed']}/{stats['total']} 轮{current}{points}"
            f" | {test} | {standard.replace('轮', unit)}"
        )

    @Slot(object)
    def _show_states(self, states):
        for ch, enabled in states.items():
            channel = int(ch)
            if channel in self.channel_labels:
                self.channel_labels[channel].setText(f"CH{channel}: {'ON（接通）' if enabled else 'OFF（断开）'}")

    def _on_result(self, success, message):
        self.phase_label.setText(message)
        self.phase_label.setWordWrap(True)
        self.statusBar().showMessage(message)
        if not success:
            QMessageBox.warning(self, "操作结束", message)

    def _on_finished(self):
        self.worker.deleteLater()
        self.worker = None
        self._set_busy(False)

    def _save_log(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "保存当前显示日志", f"autoburn_{datetime.now():%Y%m%d_%H%M%S}.log",
            "日志文件 (*.log);;文本文件 (*.txt)",
        )
        if path:
            try:
                contents = ("========== 操作日志 ==========\n" + self.log_view.toPlainText()
                            + "\n\n========== 模组日志 ==========\n" + self.module_log_view.toPlainText())
                Path(path).write_text(contents, encoding="utf-8")
            except OSError as exc:
                QMessageBox.warning(self, "保存失败", str(exc))

    def _clear_log(self):
        self.log_view.clear()
        self.module_log_view.clear()

    def _save_settings(self):
        self.settings.setValue("bat_path", self.bat_path.text())
        self.settings.setValue("port", self.port.currentText())
        self.settings.setValue("baudrate", self.baudrate.currentText())
        self.settings.setValue("power_channel", self.power_channel.currentIndex())
        self.settings.setValue("boot_channel", self.boot_channel.currentIndex())
        self.settings.setValue("encoding", self.encoding.currentText())
        self.settings.setValue("mode", self.mode.currentIndex())
        self.settings.setValue("burn_start_keyword", self.burn_start_keyword.text())
        self.settings.setValue("burn_end_keywords", self.burn_end_keywords.toPlainText())
        self.settings.setValue("ota/ota_success_keyword", self.ota_success_keyword.text().strip())
        self.settings.setValue("ota/recovery_keyword", self.recovery_keyword.text().strip())
        for name in ("step_delay", "boot_hold", "entry_timeout", "burn_timeout", "max_attempts", "mode_delay", "restart_delay"):
            self.settings.setValue(name, getattr(self, name).value())

    def _load_settings(self):
        self.ota_success_keyword.setText(str(self.settings.value("ota/ota_success_keyword", "startBurn success")))
        self.recovery_keyword.setText(str(self.settings.value("ota/recovery_keyword", "dwc")))
        self.burn_start_keyword.setText(str(self.settings.value("burn_start_keyword", "core (int)message")))
        self.burn_end_keywords.setPlainText(str(self.settings.value("burn_end_keywords", "\n".join(SUCCESS_SEQUENCE))))
        self.bat_path.setText(str(self.settings.value("bat_path", "")))
        port = str(self.settings.value("port", ""))
        if port:
            self.port.setCurrentText(port)
        self.baudrate.setCurrentText(str(self.settings.value("baudrate", "9600")))
        self.encoding.setCurrentText(str(self.settings.value("encoding", "gb18030")))
        try:
            self.mode.setCurrentIndex(max(0, min(2, int(self.settings.value("mode", 2)))))
            self.power_channel.setCurrentIndex(int(self.settings.value("power_channel", 0)))
            self.boot_channel.setCurrentIndex(int(self.settings.value("boot_channel", 1)))
            for name in ("step_delay", "boot_hold", "entry_timeout", "burn_timeout", "max_attempts", "mode_delay", "restart_delay"):
                widget = getattr(self, name)
                value = self.settings.value(name, widget.value())
                widget.setValue(int(value) if name == "max_attempts" else float(value))
        except (ValueError, TypeError):
            self.statusBar().showMessage("部分历史配置无效，请检查当前设置")

    def closeEvent(self, event: QCloseEvent):
        if self.worker is not None:
            self._stop()
            QMessageBox.information(self, "正在停止", "正在关闭烧录进程并释放短接线，请等待操作结束后再次关闭窗口。")
            event.ignore()
            return
        self._save_settings()
        try:
            self.log_archive.close()
        except OSError as exc:
            self._log_archive_error(exc)
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = AutoBurnWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
