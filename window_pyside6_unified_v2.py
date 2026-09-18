"""
串口日志采集工具 - PySide6 统一窗口优化版
单窗口设计：启动配置 + 日志显示 + 图片预览 + 手动保存
优化：配置移至设置对话框，日志窗口扩大，字段联动逻辑
"""
import os
import time
import shutil
import threading
import subprocess
import argparse
import serial
import queue
import json
import sys
import tempfile
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
import serial.tools.list_ports
import re

if sys.platform == 'win32':
    import winreg
else:
    winreg = None

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSizePolicy,
    QLabel, QPushButton, QTextEdit, QComboBox, QLineEdit, QCheckBox,
    QRadioButton, QButtonGroup, QTabWidget, QMessageBox, QFileDialog,
    QDialog, QGroupBox, QFrame, QSplitter, QScrollArea, QSpinBox, QInputDialog,
    QGridLayout, QMenu, QListWidget, QWidgetAction, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QDialogButtonBox
)
from PySide6.QtCore import Qt, Signal, QTimer, QThread, QObject, QSize, QEvent, QPoint
from PySide6.QtGui import QFont, QTextCursor, QPalette, QColor, QPixmap, QImage, QTextDocument, QShortcut, QKeySequence, QTransform, QTextCharFormat, QIcon, QActionGroup, QTextBlockUserData

import theme_icons_rc  # 注册内嵌图标，源码运行和打包后均不依赖外部图片路径

from datetime import datetime
from collections import deque
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from config_manager import load_config, save_config

# ==============================
# ANSI 转义序列清理
# ==============================

ANSI_ESCAPE_PATTERN = re.compile(r'\x1b\[[0-9;]*m')

def strip_ansi_codes(text):
    """移除文本中的 ANSI 转义序列（颜色代码等）"""
    return ANSI_ESCAPE_PATTERN.sub('', text)

# ==============================
# 全局日志缓存
# ==============================

LOG_CACHE_SIZE = 1000
log_cache = deque(maxlen=LOG_CACHE_SIZE)
full_log_cache = []
log_cache_lock = threading.RLock()
module_log_cache = []  # 模组响应日志缓存（格式：[(text, success, error), ...]）

LOG_POLL_INTERVAL_MS = 50
LOG_BATCH_MAX_LINES = 200
LOG_BATCH_BUDGET_SECONDS = 0.008
LOG_DISPLAY_MAX_LINES = 5000

# 预编译高亮规则，数字越大优先级越高；只对关键字忽略大小写。
LOG_HIGHLIGHT_RULES = (
    ('timestamp', re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]'), 8),
    ('hex', re.compile(r'\b0x[0-9A-Fa-f]+\b'), 5),
    ('number', re.compile(r'\b\d+\b'), 3),
    ('keyword', re.compile(r'\b(成功|warning|success|connected|发送|接收|下载)\b', re.IGNORECASE), 7),
    ('bracket', re.compile(r'[\[\](){}]'), 2),
    ('symbol', re.compile(r'[,:;=<>+\-*/]'), 1),
    ('keyword_err', re.compile(r'\b(失败|错误|error|failed|timeout|disconnected)\b', re.IGNORECASE), 6),
    ('upper_letter', re.compile(r'\b[A-Z]+\b'), 4),
)
LOG_HIGHLIGHT_COLORS = {
    False: {
        'timestamp': '#0066CC', 'bracket': '#216AAF', 'number': '#098658',
        'hex': '#78E22E', 'keyword': '#0000FF', 'keyword_err': '#A31515',
        'symbol': '#41B9EF', 'text': '#000000', 'upper_letter': '#EC5800',
        'background': 'white',
    },
    True: {
        'timestamp': '#87CEEB', 'bracket': '#216AAF', 'number': '#098658',
        'hex': '#78E22E', 'keyword': '#87CEEB', 'keyword_err': '#EC5800',
        'symbol': '#41B9EF', 'text': '#F8F9FA', 'upper_letter': '#FFB6C1',
        'background': '#1e1e1e',
    },
}

# ==============================
# 日志提取与外部查看工具
# ==============================

FULL_LOG_SNAPSHOT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
FULL_LOG_SNAPSHOT_DIR = Path(tempfile.gettempdir()) / 'capLG' / 'full_logs'


class LogBlockData(QTextBlockUserData):
    """记录可见文本块对应的完整缓存绝对索引。"""

    def __init__(self, log_index):
        super().__init__()
        self.log_index = log_index


@dataclass(frozen=True)
class QueuedLog:
    """携带完整缓存绝对索引的待渲染串口日志。"""
    log_index: int
    text: str


@dataclass
class LogMarker:
    """绑定到完整日志绝对索引的会话内打点。"""
    marker_id: int
    name: str
    created_at: datetime
    log_index: int
    log_count: int
    line_text: str

    @property
    def summary(self):
        match = re.search(r'[A-Za-z]', self.line_text)
        if match:
            return self.line_text[match.start():match.start() + 20]
        # 纯中文等没有英文字母的日志，退回到去除开头时间戳后的内容。
        content = re.sub(r'^\[[^\]]*\]\s*', '', self.line_text, count=1)
        return content[:20]

    @property
    def line_number(self):
        return self.log_index + 1


@dataclass(frozen=True)
class TextViewer:
    """本机可直接启动的文本查看器。"""
    name: str
    executable: str
    arguments: tuple = ('%1',)


TEXT_VIEWER_NAMES = {
    'notepad.exe': '记事本',
    'notepad++.exe': 'Notepad++',
    'code.exe': 'Visual Studio Code',
    'sublime_text.exe': 'Sublime Text',
    'wordpad.exe': '写字板',
    'write.exe': '写字板',
}


def extract_logs(strategy, param=None):
    """根据策略从 full_log_cache 中提取日志"""
    with log_cache_lock:
        logs = list(full_log_cache)
    if not logs:
        return []

    if strategy == 'all':
        return logs
    elif strategy == 'recent_n':
        n = int(param) if param else 100
        return logs[-n:] if n > 0 else []
    elif strategy == 'from_keyword':
        keyword = str(param) if param else ''
        if not keyword:
            return []
        for i in range(len(logs) - 1, -1, -1):
            if keyword in logs[i]:
                return logs[i:]
        return []
    else:
        return logs


def markers_for_save(markers, start_index=0, start_marker_id=None):
    """返回所选记录之后创建、且位于保存日志范围内的打点。"""
    return [marker for marker in markers
            if marker.log_index >= start_index
            and (start_marker_id is None or marker.marker_id >= start_marker_id)]


def format_marker_section(markers, start_index=0, start_marker_id=None):
    """生成追加在日志正文后的可读打点信息区块。"""
    applicable = markers_for_save(markers, start_index, start_marker_id)
    if not applicable:
        return ''
    lines = ['', '========== 打点记录 ==========']
    for marker in applicable:
        relative_line = marker.log_index - start_index + 1
        lines.extend((
            f'名称: {marker.name}',
            f'打点时间: {marker.created_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}',
            f'原始日志行号: {marker.line_number}',
            f'保存文件相对行号: {relative_line}',
            f'打点时日志总行数: {marker.log_count}',
            f'日志摘要: {marker.summary}',
            '------------------------------',
        ))
    return '\n'.join(lines) + '\n'


def build_marked_log_text(logs, markers, start_index=0, start_marker_id=None):
    """构建选定范围日志正文及其适用打点区块。"""
    body = '\n'.join(logs[start_index:])
    if body:
        body += '\n'
    return body + format_marker_section(markers, start_index, start_marker_id)


def _windows_command_line_to_argv(command):
    """按Windows命令行规则拆分注册表中的打开命令。"""
    if not command or sys.platform != 'win32':
        return []
    command = os.path.expandvars(command.strip())
    argc = ctypes.c_int()
    shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv:
        return []
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        kernel32.LocalFree(argv)


def _normalize_viewer_command(command):
    """将注册命令转换为可安全交给Popen的程序和参数。"""
    argv = _windows_command_line_to_argv(command)
    if not argv:
        return None
    executable = os.path.abspath(os.path.expandvars(argv[0]))
    if not os.path.isfile(executable):
        return None
    arguments = []
    has_file_placeholder = False
    for argument in argv[1:]:
        # Shell动态占位符无法通过普通Popen安全复现，交给系统“打开方式”。
        if re.search(r'%[2-9*]', argument):
            continue
        if re.search(r'%(?:1|l|L)', argument):
            argument = re.sub(r'%(?:1|l|L)', '%1', argument)
            has_file_placeholder = True
        arguments.append(argument)
    if not has_file_placeholder:
        arguments.append('%1')
    return executable, tuple(arguments)


def _read_registry_default(root, path, access=0):
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | access) as key:
            return winreg.QueryValueEx(key, '')[0]
    except (FileNotFoundError, OSError):
        return None


def _registry_values(root, path, access=0):
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | access) as key:
            return [winreg.EnumValue(key, index)[0]
                    for index in range(winreg.QueryInfoKey(key)[1])]
    except (FileNotFoundError, OSError):
        return []


def _registry_subkeys(root, path, access=0):
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | access) as key:
            return [winreg.EnumKey(key, index)
                    for index in range(winreg.QueryInfoKey(key)[0])]
    except (FileNotFoundError, OSError):
        return []


def _viewer_display_name(executable):
    basename = os.path.basename(executable).lower()
    return TEXT_VIEWER_NAMES.get(basename, Path(executable).stem)


def discover_text_viewers():
    """发现Windows已注册且能解析到本地EXE的文本查看器。"""
    if sys.platform != 'win32' or winreg is None:
        return []

    candidates = []
    system_notepad = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                                  'System32', 'notepad.exe')
    if os.path.isfile(system_notepad):
        candidates.append((system_notepad, ('%1',)))

    views = [0]
    for flag in (getattr(winreg, 'KEY_WOW64_64KEY', 0),
                 getattr(winreg, 'KEY_WOW64_32KEY', 0)):
        if flag and flag not in views:
            views.append(flag)

    # App Paths覆盖了常用编辑器不完整或间接的文件关联注册。
    # 仅主动探测明确的文本编辑器，避免把Office、浏览器等泛型文件处理器列入菜单。
    app_names = set(TEXT_VIEWER_NAMES)
    for view in views:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for app_name in app_names:
                path = _read_registry_default(
                    root,
                    rf'Software\Microsoft\Windows\CurrentVersion\App Paths\{app_name}',
                    view,
                )
                if path and os.path.isfile(os.path.expandvars(path)):
                    candidates.append((os.path.abspath(os.path.expandvars(path)), ('%1',)))

    progids = set(_registry_values(winreg.HKEY_CLASSES_ROOT,
                                   r'.txt\OpenWithProgids'))
    progids.update(_registry_values(
        winreg.HKEY_CURRENT_USER,
        r'Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.txt\OpenWithProgids',
    ))
    command_paths = [rf'{progid}\shell\open\command' for progid in progids]
    command_paths.extend((
        r'SystemFileAssociations\text\shell\open\command',
        r'SystemFileAssociations\.txt\shell\open\command',
    ))

    for app_name in app_names:
        supported = _registry_values(
            winreg.HKEY_CLASSES_ROOT,
            rf'Applications\{app_name}\SupportedTypes',
        )
        if any(extension.lower() == '.txt' for extension in supported):
            command_paths.append(rf'Applications\{app_name}\shell\open\command')

    for view in views:
        for path in command_paths:
            normalized = _normalize_viewer_command(
                _read_registry_default(winreg.HKEY_CLASSES_ROOT, path, view)
            )
            if normalized:
                candidates.append(normalized)

    viewers = {}
    for executable, arguments in candidates:
        basename = os.path.basename(executable).lower()
        # 文件关联可能包含Office、浏览器等程序；直接菜单只保留文本编辑器。
        if basename not in TEXT_VIEWER_NAMES:
            continue
        key = basename
        current = viewers.get(key)
        viewer = TextViewer(_viewer_display_name(executable), executable, tuple(arguments))
        # 同名程序优先采用App Paths/商店版等非System32的实际安装路径。
        if current is None or ('system32' in current.executable.lower()
                               and 'system32' not in executable.lower()):
            viewers[key] = viewer
    return sorted(
        viewers.values(),
        key=lambda viewer: (os.path.basename(viewer.executable).lower() != 'notepad.exe',
                            viewer.name.casefold(), viewer.executable.casefold()),
    )


def cleanup_old_log_snapshots(folder=FULL_LOG_SNAPSHOT_DIR, now=None):
    """尽力删除七天前的临时快照，单个文件失败不影响查看。"""
    now = time.time() if now is None else now
    try:
        entries = Path(folder).glob('capLG_full_log_*.txt')
        for path in entries:
            try:
                if now - path.stat().st_mtime > FULL_LOG_SNAPSHOT_MAX_AGE_SECONDS:
                    path.unlink()
            except OSError:
                pass
    except OSError:
        pass


def create_full_log_snapshot(lines=None, folder=FULL_LOG_SNAPSHOT_DIR, now=None):
    """将完整缓存的点击时刻快照写成Windows文本编辑器友好的UTF-8文件。"""
    if lines is None:
        with log_cache_lock:
            snapshot = list(full_log_cache)
    else:
        snapshot = list(lines)
    if not snapshot:
        raise ValueError('当前还没有产生任何日志')
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    cleanup_old_log_snapshots(folder, now=now)
    moment = datetime.now()
    filename = moment.strftime('capLG_full_log_%Y%m%d_%H%M%S_%f.txt')
    path = folder / filename
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        stream.write('\r\n'.join(snapshot))
        stream.write('\r\n')
    return path


def viewer_command(viewer, log_path):
    """以参数列表替换文件占位符，绝不经由shell拼接。"""
    path = str(log_path)
    return [viewer.executable] + [argument.replace('%1', path)
                                  for argument in viewer.arguments]


def show_windows_open_with(log_path, owner=0):
    """显示Windows原生“打开方式”对话框并执行用户选择。"""
    if sys.platform != 'win32':
        raise OSError('系统“打开方式”仅适用于Windows')

    class OPENASINFO(ctypes.Structure):
        _fields_ = (
            ('pcszFile', wintypes.LPCWSTR),
            ('pcszClass', wintypes.LPCWSTR),
            ('oaifInFlags', wintypes.DWORD),
        )

    shell32 = ctypes.WinDLL('shell32', use_last_error=True)
    shell32.SHOpenWithDialog.argtypes = [wintypes.HWND, ctypes.POINTER(OPENASINFO)]
    shell32.SHOpenWithDialog.restype = ctypes.c_long
    info = OPENASINFO(str(log_path), None, 0x00000004)  # OAIF_EXEC
    result = shell32.SHOpenWithDialog(owner, ctypes.byref(info))
    if result < 0:
        raise OSError(f'系统“打开方式”返回错误 0x{result & 0xffffffff:08X}')

# ==============================
# 配置路径
# ==============================

CONFIG_PATH_DOWNLOAD = os.path.join(os.path.expanduser('~'), '.save_name_dialog_last.json')

def _load_last_selection():
    """读取上一次的选择"""
    try:
        with open(CONFIG_PATH_DOWNLOAD, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def _save_last_selection(data):
    """保存本次选择"""
    try:
        with open(CONFIG_PATH_DOWNLOAD, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

# ==============================
# 自定义命令发送对话框
# ==============================

# ==============================
# 图片查看器窗口
# ==============================

class ImageViewerDialog(QDialog):
    """图片查看器 - 支持缩放、旋转、翻转等操作"""

    def __init__(self, image_path, parent=None, image_paths=None):
        super().__init__(parent)
        self.image_path = image_path
        self.image_paths = list(image_paths) if image_paths else [image_path]
        if image_path not in self.image_paths:
            self.image_paths.insert(0, image_path)
        self.image_index = self.image_paths.index(image_path)
        self.fit_mode = True
        self.drag_position = None
        self.resize(800, 600)

        # 图片变换参数
        self.zoom_scale = 1.0
        self.rotation = 0
        self.flip_h = False
        self.flip_v = False

        # 矩形框参数
        self.draw_rect = False
        self.rect_left = 0
        self.rect_top = 0
        self.rect_right = 0
        self.rect_bottom = 0

        try:
            self.original_pixmap, self.image_file_size = self.load_pixmap(image_path)
        except (ValueError, OSError):
            self.deleteLater()
            raise
        self.setup_ui()
        self.update_image_info()

        # 合并布局变化产生的适应请求，等待视口尺寸稳定
        self.fit_timer = QTimer(self)
        self.fit_timer.setSingleShot(True)
        self.fit_timer.timeout.connect(self.refit_image)
        self.fit_timer.start(0)

    @staticmethod
    def load_pixmap(image_path):
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            raise ValueError(f'无法加载图片：{image_path}')
        return pixmap, os.path.getsize(image_path) / 1024

    def update_image_info(self):
        self.setWindowTitle(f'图片查看器 - {os.path.basename(self.image_path)}')
        self.info_label.setText(
            f'{self.original_pixmap.width()} × {self.original_pixmap.height()} px'
            f' | {self.image_file_size:.1f} KB'
        )
        self.position_label.setText(f'{self.image_index + 1} / {len(self.image_paths)}')
        self.btn_previous.setEnabled(self.image_index > 0)
        self.btn_next.setEnabled(self.image_index < len(self.image_paths) - 1)

    def change_image(self, offset):
        index = self.image_index + offset
        if not 0 <= index < len(self.image_paths):
            return
        path = self.image_paths[index]
        try:
            pixmap, file_size = self.load_pixmap(path)
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, '错误', str(e))
            return

        self.image_index = index
        self.image_path = path
        self.original_pixmap = pixmap
        self.image_file_size = file_size
        self.rotation = 0
        self.flip_h = False
        self.flip_v = False
        self.draw_rect = False
        self.rect_left = self.rect_top = self.rect_right = self.rect_bottom = 0
        for field in (self.smart_rect_input, self.rect_left_input, self.rect_top_input,
                      self.rect_right_input, self.rect_bottom_input):
            field.clear()
        self.update_image_info()
        self.zoom_fit()
        self.scroll_area.horizontalScrollBar().setValue(0)
        self.scroll_area.verticalScrollBar().setValue(0)

    def setup_ui(self):
        layout = QVBoxLayout(self)

        # 工具栏
        toolbar = QHBoxLayout()

        btn_zoom_in = QPushButton('🔍+')
        btn_zoom_in.setMaximumWidth(50)
        btn_zoom_in.setToolTip('放大 (Ctrl++)')
        btn_zoom_in.clicked.connect(self.zoom_in)
        toolbar.addWidget(btn_zoom_in)

        btn_zoom_out = QPushButton('🔍-')
        btn_zoom_out.setMaximumWidth(50)
        btn_zoom_out.setToolTip('缩小 (Ctrl+-)')
        btn_zoom_out.clicked.connect(self.zoom_out)
        toolbar.addWidget(btn_zoom_out)

        btn_zoom_fit = QPushButton('📐')
        btn_zoom_fit.setMaximumWidth(50)
        btn_zoom_fit.setToolTip('适应窗口 (Ctrl+0)')
        btn_zoom_fit.clicked.connect(self.zoom_fit)
        toolbar.addWidget(btn_zoom_fit)

        btn_zoom_100 = QPushButton('1:1')
        btn_zoom_100.setMaximumWidth(50)
        btn_zoom_100.setToolTip('实际大小 (Ctrl+1)')
        btn_zoom_100.clicked.connect(self.zoom_actual)
        toolbar.addWidget(btn_zoom_100)

        toolbar.addWidget(QLabel('|'))

        btn_rotate_left = QPushButton('↺')
        btn_rotate_left.setMaximumWidth(50)
        btn_rotate_left.setToolTip('逆时针旋转 (Ctrl+L)')
        btn_rotate_left.clicked.connect(self.rotate_left)
        toolbar.addWidget(btn_rotate_left)

        btn_rotate_right = QPushButton('↻')
        btn_rotate_right.setMaximumWidth(50)
        btn_rotate_right.setToolTip('顺时针旋转 (Ctrl+R)')
        btn_rotate_right.clicked.connect(self.rotate_right)
        toolbar.addWidget(btn_rotate_right)

        toolbar.addWidget(QLabel('|'))

        btn_flip_h = QPushButton('⇄')
        btn_flip_h.setMaximumWidth(50)
        btn_flip_h.setToolTip('水平翻转 (Ctrl+H)')
        btn_flip_h.clicked.connect(self.flip_horizontal)
        toolbar.addWidget(btn_flip_h)

        btn_flip_v = QPushButton('⇅')
        btn_flip_v.setMaximumWidth(50)
        btn_flip_v.setToolTip('垂直翻转 (Ctrl+V)')
        btn_flip_v.clicked.connect(self.flip_vertical)
        toolbar.addWidget(btn_flip_v)

        for button in (btn_zoom_in, btn_zoom_out, btn_zoom_fit, btn_zoom_100,
                       btn_rotate_left, btn_rotate_right, btn_flip_h, btn_flip_v):
            button.setProperty('compactButton', True)

        toolbar.addWidget(QLabel('|'))

        btn_reset = QPushButton('🔄 重置')
        btn_reset.setToolTip('重置所有变换')
        btn_reset.clicked.connect(self.reset_transforms)
        toolbar.addWidget(btn_reset)

        # 缩放比例显示
        self.zoom_label = QLabel('100%')
        self.zoom_label.setStyleSheet('color: #666666; font-weight: bold;')
        self.zoom_label.setMinimumWidth(60)
        self.zoom_label.setAlignment(Qt.AlignCenter)
        toolbar.addWidget(self.zoom_label)

        toolbar.addStretch()

        # 图片信息
        self.info_label = QLabel()
        self.info_label.setStyleSheet('color: #666666; font-size: 9pt;')
        toolbar.addWidget(self.info_label)

        layout.addLayout(toolbar)

        navigation = QHBoxLayout()
        self.btn_previous = QPushButton('◀ 上一张')
        self.btn_previous.setToolTip('上一张（←）')
        self.btn_previous.clicked.connect(lambda: self.change_image(-1))
        navigation.addWidget(self.btn_previous)
        self.position_label = QLabel()
        self.position_label.setAlignment(Qt.AlignCenter)
        navigation.addWidget(self.position_label)
        self.btn_next = QPushButton('下一张 ▶')
        self.btn_next.setToolTip('下一张（→）')
        self.btn_next.clicked.connect(lambda: self.change_image(1))
        navigation.addWidget(self.btn_next)
        navigation.addStretch()
        navigation.addWidget(QLabel('滚轮缩放 · 按住左键拖动'))
        layout.addLayout(navigation)

        # 图片显示区域（带滚动）
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(False)  # 改回 False，这样才能滚动
        self.scroll_area.setAlignment(Qt.AlignCenter)
        self.scroll_area.setStyleSheet('QScrollArea { background-color: #f0f0f0; }')
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setScaledContents(False)
        self.scroll_area.setWidget(self.image_label)
        for widget in (self.image_label, self.scroll_area.viewport()):
            widget.installEventFilter(self)
            widget.setCursor(Qt.OpenHandCursor)

        layout.addWidget(self.scroll_area, 1)

        # 矩形框绘制区域（默认折叠，为图片留出更多空间）
        self.btn_toggle_rect = QPushButton('▶ 绘制矩形框')
        self.btn_toggle_rect.setCheckable(True)
        self.btn_toggle_rect.setStyleSheet('text-align: left;')
        self.btn_toggle_rect.toggled.connect(self.toggle_rect_panel)
        layout.addWidget(self.btn_toggle_rect)

        self.rect_group = QGroupBox()
        rect_main_layout = QVBoxLayout(self.rect_group)

        # 第一行：智能输入框
        smart_input_layout = QHBoxLayout()
        smart_input_layout.addWidget(QLabel('快速输入:'))
        self.smart_rect_input = QLineEdit()
        self.smart_rect_input.setPlaceholderText('例如: [50, 100, 300, 400] 或 50, 100, 300, 400')
        self.smart_rect_input.returnPressed.connect(self.parse_smart_input)
        smart_input_layout.addWidget(self.smart_rect_input)

        btn_parse = QPushButton('📋 解析')
        btn_parse.setToolTip('从输入框解析坐标并填充到下方')
        btn_parse.clicked.connect(self.parse_smart_input)
        smart_input_layout.addWidget(btn_parse)

        rect_main_layout.addLayout(smart_input_layout)

        # 第二行：详细输入框
        rect_layout = QHBoxLayout()

        rect_layout.addWidget(QLabel('Left:'))
        self.rect_left_input = QLineEdit()
        self.rect_left_input.setMaximumWidth(60)
        self.rect_left_input.setPlaceholderText('0')
        rect_layout.addWidget(self.rect_left_input)

        rect_layout.addWidget(QLabel('Top:'))
        self.rect_top_input = QLineEdit()
        self.rect_top_input.setMaximumWidth(60)
        self.rect_top_input.setPlaceholderText('0')
        rect_layout.addWidget(self.rect_top_input)

        rect_layout.addWidget(QLabel('Right:'))
        self.rect_right_input = QLineEdit()
        self.rect_right_input.setMaximumWidth(60)
        self.rect_right_input.setPlaceholderText('0')
        rect_layout.addWidget(self.rect_right_input)

        rect_layout.addWidget(QLabel('Bottom:'))
        self.rect_bottom_input = QLineEdit()
        self.rect_bottom_input.setMaximumWidth(60)
        self.rect_bottom_input.setPlaceholderText('0')
        rect_layout.addWidget(self.rect_bottom_input)

        btn_draw_rect = QPushButton('✏️ 绘制')
        btn_draw_rect.clicked.connect(self.draw_rectangle)
        rect_layout.addWidget(btn_draw_rect)

        btn_clear_rect = QPushButton('🗑️ 清除')
        btn_clear_rect.clicked.connect(self.clear_rectangle)
        rect_layout.addWidget(btn_clear_rect)

        rect_layout.addStretch()

        rect_main_layout.addLayout(rect_layout)

        layout.addWidget(self.rect_group)
        self.rect_group.hide()

        # 底部按钮
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        btn_close = QPushButton('关闭')
        btn_close.clicked.connect(self.accept)
        button_layout.addWidget(btn_close)

        layout.addLayout(button_layout)
        for widget in self.findChildren(QWidget):
            widget.installEventFilter(self)

    def toggle_rect_panel(self, expanded):
        """展开/收起参数区，不改变绘制参数和已有矩形框"""
        self.rect_group.setVisible(expanded)
        self.btn_toggle_rect.setText('▼ 绘制矩形框' if expanded else '▶ 绘制矩形框')

    def zoom_in(self):
        """放大"""
        self.set_zoom(self.zoom_scale * 1.25)

    def zoom_out(self):
        """缩小"""
        self.set_zoom(self.zoom_scale / 1.25)

    def set_zoom(self, scale, anchor=None):
        """缩放时尽量保持鼠标位置或视口中心的图片内容不动"""
        self.fit_mode = False
        viewport = self.scroll_area.viewport()
        if anchor is None:
            anchor = viewport.rect().center()
        origin = self.image_label.mapTo(viewport, QPoint(0, 0))
        old_size = self.image_label.size()
        relative_x = (anchor.x() - origin.x()) / max(1, old_size.width())
        relative_y = (anchor.y() - origin.y()) / max(1, old_size.height())
        # 适应窗口可能低于 5%，此时缩小不应反向放大
        minimum = min(0.05, self.zoom_scale)
        self.zoom_scale = max(minimum, min(8.0, scale))
        self.update_image()
        origin = self.image_label.mapTo(viewport, QPoint(0, 0))
        for bar, delta in (
            (self.scroll_area.horizontalScrollBar(),
             origin.x() + relative_x * self.image_label.width() - anchor.x()),
            (self.scroll_area.verticalScrollBar(),
             origin.y() + relative_y * self.image_label.height() - anchor.y()),
        ):
            bar.setValue(bar.value() + round(delta))

    def zoom_fit(self):
        """根据旋转后的尺寸适应窗口"""
        self.fit_mode = True
        available_width = max(1, self.scroll_area.viewport().width() - 20)
        available_height = max(1, self.scroll_area.viewport().height() - 20)
        width, height = self.original_pixmap.width(), self.original_pixmap.height()
        if self.rotation % 180:
            width, height = height, width
        self.zoom_scale = min(available_width / width, available_height / height, 1.0)
        self.update_image()

    def refit_image(self):
        if self.fit_mode:
            self.zoom_fit()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.KeyPress and event.modifiers() == Qt.NoModifier:
            if not isinstance(self.focusWidget(), QLineEdit):
                if event.key() in (Qt.Key_Left, Qt.Key_Right):
                    self.change_image(-1 if event.key() == Qt.Key_Left else 1)
                    return True

        viewport = self.scroll_area.viewport()
        if watched not in (viewport, self.image_label):
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Resize and watched is viewport:
            if self.fit_mode and hasattr(self, 'fit_timer'):
                self.fit_timer.start(0)
        elif event.type() == QEvent.Wheel:
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if delta:
                anchor = viewport.mapFromGlobal(event.globalPosition().toPoint())
                self.set_zoom(self.zoom_scale * (1.25 if delta > 0 else 1 / 1.25), anchor)
            event.accept()
            return True
        elif event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self.drag_position = event.globalPosition().toPoint()
            self.scroll_area.setFocus()
            for widget in (viewport, self.image_label):
                widget.setCursor(Qt.ClosedHandCursor)
            return True
        elif event.type() == QEvent.MouseMove and self.drag_position is not None:
            position = event.globalPosition().toPoint()
            delta = position - self.drag_position
            self.drag_position = position
            hbar = self.scroll_area.horizontalScrollBar()
            vbar = self.scroll_area.verticalScrollBar()
            hbar.setValue(hbar.value() - delta.x())
            vbar.setValue(vbar.value() - delta.y())
            return True
        elif event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            self.drag_position = None
            for widget in (viewport, self.image_label):
                widget.setCursor(Qt.OpenHandCursor)
            return True
        return super().eventFilter(watched, event)

    def zoom_actual(self):
        """实际大小"""
        self.set_zoom(1.0)

    def rotate_left(self):
        """逆时针旋转"""
        print(f'[调试] rotate_left 被调用')
        self.rotation = (self.rotation - 90) % 360
        print(f'[调试] 旋转角度变为: {self.rotation}')
        if self.fit_mode:
            self.zoom_fit()
        else:
            self.update_image()

    def rotate_right(self):
        """顺时针旋转"""
        print(f'[调试] rotate_right 被调用')
        self.rotation = (self.rotation + 90) % 360
        print(f'[调试] 旋转角度变为: {self.rotation}')
        if self.fit_mode:
            self.zoom_fit()
        else:
            self.update_image()

    def flip_horizontal(self):
        """水平翻转"""
        print(f'[调试] flip_horizontal 被调用')
        self.flip_h = not self.flip_h
        print(f'[调试] 水平翻转状态: {self.flip_h}')
        self.update_image()

    def flip_vertical(self):
        """垂直翻转"""
        print(f'[调试] flip_vertical 被调用')
        self.flip_v = not self.flip_v
        print(f'[调试] 垂直翻转状态: {self.flip_v}')
        self.update_image()

    def reset_transforms(self):
        """重置所有变换"""
        self.fit_mode = False
        self.zoom_scale = 1.0
        self.rotation = 0
        self.flip_h = False
        self.flip_v = False
        self.update_image()

    def parse_smart_input(self):
        """解析智能输入框，提取四个数字并填充到详细输入框"""
        text = self.smart_rect_input.text().strip()
        if not text:
            return

        try:
            # 使用正则表达式提取所有数字（支持负数）
            import re
            numbers = re.findall(r'-?\d+', text)

            if len(numbers) < 4:
                QMessageBox.warning(self, '解析错误', f'需要4个数字，但只找到了 {len(numbers)} 个\n示例格式:\n- [left, top, right, bottom]\n- rgb:[386 232 122 99]\n- left:117 top:386 right:280 bottom:548')
                return

            if len(numbers) > 4:
                # 如果找到超过4个数字，提示用户并使用前4个
                reply = QMessageBox.question(
                    self,
                    '多个数字',
                    f'找到了 {len(numbers)} 个数字: {numbers}\n是否使用前4个数字？',
                    QMessageBox.Yes | QMessageBox.No
                )
                if reply == QMessageBox.No:
                    return

            # 转换为整数（使用前4个）
            left = int(numbers[0])
            top = int(numbers[1])
            right = int(numbers[2])
            bottom = int(numbers[3])

            # 填充到详细输入框
            self.rect_left_input.setText(str(left))
            self.rect_top_input.setText(str(top))
            self.rect_right_input.setText(str(right))
            self.rect_bottom_input.setText(str(bottom))

            print(f'[调试] 智能解析成功: left={left}, top={top}, right={right}, bottom={bottom}')

            # 自动绘制
            self.draw_rectangle()

        except ValueError as e:
            QMessageBox.warning(self, '解析错误', f'无法解析数字：{str(e)}')
        except Exception as e:
            QMessageBox.warning(self, '解析错误', f'解析失败：{str(e)}')

    def draw_rectangle(self):
        """绘制矩形框"""
        try:
            left = int(self.rect_left_input.text() or 0)
            top = int(self.rect_top_input.text() or 0)
            right = int(self.rect_right_input.text() or 0)
            bottom = int(self.rect_bottom_input.text() or 0)

            # 验证输入
            if left < 0 or top < 0 or right < 0 or bottom < 0:
                QMessageBox.warning(self, '输入错误', '坐标值不能为负数')
                return

            if right <= left or bottom <= top:
                QMessageBox.warning(self, '输入错误', 'Right 必须大于 Left，Bottom 必须大于 Top')
                return

            # 保存矩形框参数
            self.draw_rect = True
            self.rect_left = left
            self.rect_top = top
            self.rect_right = right
            self.rect_bottom = bottom

            print(f'[调试] 绘制矩形框: left={left}, top={top}, right={right}, bottom={bottom}')

            # 更新图片显示
            self.update_image()

        except ValueError:
            QMessageBox.warning(self, '输入错误', '请输入有效的数字')

    def clear_rectangle(self):
        """清除矩形框"""
        self.draw_rect = False
        self.rect_left_input.clear()
        self.rect_top_input.clear()
        self.rect_right_input.clear()
        self.rect_bottom_input.clear()
        print(f'[调试] 清除矩形框')
        self.update_image()

    def update_image(self):
        """更新图片显示"""
        print(f'[调试] update_image 被调用, zoom_scale={self.zoom_scale:.3f}')
        # 应用变换
        pixmap = self.apply_transforms(self.original_pixmap)
        print(f'[调试] 变换后的图片大小: {pixmap.width()} x {pixmap.height()}')

        # 设置 label 的尺寸为图片的实际尺寸
        self.image_label.setFixedSize(pixmap.size())
        self.image_label.setPixmap(pixmap)
        print(f'[调试] 图片已设置到 label')

        # 更新缩放比例显示
        self.zoom_label.setText(f'{int(self.zoom_scale * 100)}%')

    def apply_transforms(self, pixmap):
        """应用所有变换"""
        if pixmap.isNull():
            return pixmap

        # 转换为 QImage
        image = pixmap.toImage()

        # 翻转
        if self.flip_h:
            image = image.mirrored(True, False)
        if self.flip_v:
            image = image.mirrored(False, True)

        # 转回 QPixmap
        transformed = QPixmap.fromImage(image)

        # 旋转
        if self.rotation != 0:
            transform = QTransform()
            transform.rotate(self.rotation)
            transformed = transformed.transformed(transform, Qt.SmoothTransformation)

        # 绘制矩形框（在缩放之前）
        if self.draw_rect:
            from PySide6.QtGui import QPainter, QPen
            from PySide6.QtCore import QRect

            painter = QPainter(transformed)
            pen = QPen(QColor(255, 0, 0))  # 红色
            pen.setWidth(2)
            painter.setPen(pen)

            # 绘制矩形
            rect = QRect(self.rect_left, self.rect_top,
                        self.rect_right - self.rect_left,
                        self.rect_bottom - self.rect_top)
            painter.drawRect(rect)
            painter.end()

        # 缩放（始终应用，即使是1.0）
        new_w = int(transformed.width() * self.zoom_scale)
        new_h = int(transformed.height() * self.zoom_scale)

        # 确保尺寸至少为1像素
        new_w = max(1, new_w)
        new_h = max(1, new_h)

        transformed = transformed.scaled(new_w, new_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        return transformed

    def keyPressEvent(self, event):
        """键盘快捷键"""
        if event.modifiers() == Qt.ControlModifier:
            if event.key() == Qt.Key_Plus or event.key() == Qt.Key_Equal:
                self.zoom_in()
            elif event.key() == Qt.Key_Minus:
                self.zoom_out()
            elif event.key() == Qt.Key_0:
                self.zoom_fit()
            elif event.key() == Qt.Key_1:
                self.zoom_actual()
            elif event.key() == Qt.Key_L:
                self.rotate_left()
            elif event.key() == Qt.Key_R:
                self.rotate_right()
            elif event.key() == Qt.Key_H:
                self.flip_horizontal()
            elif event.key() == Qt.Key_V:
                self.flip_vertical()
        else:
            super().keyPressEvent(event)

# ==============================
# 自定义命令发送对话框
# ==============================

class CustomCommandDialog(QDialog):
    """自定义命令发送窗口"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self.setWindowTitle('自定义命令发送')
        self.resize(700, 500)

        # 快捷命令列表 [{"name": "命令名称", "command": "AA55..."}]
        self.shortcut_commands = []
        self.load_shortcut_commands()

        # 布局
        layout = QVBoxLayout(self)

        # 说明标签
        info_label = QLabel('💡 输入十六进制命令（空格分隔），例如: AA 55 01 02 03')
        info_label.setStyleSheet('background-color: #e3f2fd; padding: 8px; border-radius: 4px;')
        layout.addWidget(info_label)

        # 命令输入区域
        input_group = QGroupBox('命令输入')
        input_layout = QVBoxLayout(input_group)

        # 输入框
        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText('例如: AA 55 00 10 或 AA55001000')
        self.command_input.setFont(QFont('Consolas', 10))
        input_layout.addWidget(self.command_input)

        # 按钮行
        button_layout = QHBoxLayout()

        self.btn_send = QPushButton('📤 发送命令')
        self.btn_send.setStyleSheet('QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 8px 16px; }')
        self.btn_send.clicked.connect(self.send_command)
        button_layout.addWidget(self.btn_send)

        self.btn_clear_input = QPushButton('🗑️ 清空输入')
        self.btn_clear_input.clicked.connect(lambda: self.command_input.clear())
        button_layout.addWidget(self.btn_clear_input)

        self.btn_add_shortcut = QPushButton('➕')
        self.btn_add_shortcut.setMaximumWidth(40)
        self.btn_add_shortcut.setProperty('compactButton', True)
        self.btn_add_shortcut.setToolTip('添加快捷命令')
        self.btn_add_shortcut.setStyleSheet('QPushButton { background-color: #FF9800; color: white; font-weight: bold; padding: 4px 2px; }')
        self.btn_add_shortcut.clicked.connect(self.add_shortcut_command)
        button_layout.addWidget(self.btn_add_shortcut)

        # 快捷命令按钮容器（动态添加）
        self.shortcut_buttons_layout = button_layout

        button_layout.addStretch()
        input_layout.addLayout(button_layout)

        layout.addWidget(input_group)

        # 刷新快捷命令按钮
        self.refresh_shortcut_buttons()

        # 响应显示区域
        output_group = QGroupBox('命令日志 (发送/接收)')
        output_layout = QVBoxLayout(output_group)

        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setFont(QFont('Consolas', 9))
        output_layout.addWidget(self.output_text)

        # 清空日志按钮
        clear_layout = QHBoxLayout()
        self.btn_clear_log = QPushButton('🗑️ 清空日志')
        self.btn_clear_log.clicked.connect(self.clear_all_logs)
        clear_layout.addWidget(self.btn_clear_log)
        clear_layout.addStretch()
        output_layout.addLayout(clear_layout)

        layout.addWidget(output_group)

        # 快捷键：回车发送
        self.command_input.returnPressed.connect(self.send_command)

    def load_shortcut_commands(self):
        """加载快捷命令（从配置文件）"""
        try:
            config = load_config()
            self.shortcut_commands = config.get('shortcut_commands', [])
        except Exception as e:
            print(f'[调试] 加载快捷命令失败: {e}')
            self.shortcut_commands = []

    def save_shortcut_commands(self):
        """保存快捷命令（到配置文件）"""
        try:
            config = load_config()
            config['shortcut_commands'] = self.shortcut_commands
            save_config(config)
        except Exception as e:
            print(f'[调试] 保存快捷命令失败: {e}')

    def clear_all_logs(self):
        """真正清空所有日志缓存"""
        global log_cache, full_log_cache

        print(f'[调试] 清空前 - log_cache长度: {len(log_cache)}, full_log_cache长度: {len(full_log_cache)}')

        # 清空显示窗口
        self.output_text.clear()
        print(f'[调试] 已清空显示窗口')

        # 清空所有日志缓存
        log_cache.clear()
        full_log_cache.clear()

        print(f'[调试] 清空后 - log_cache长度: {len(log_cache)}, full_log_cache长度: {len(full_log_cache)}')

        # 更新日志行数显示
        self.log_count_label.setText('日志行数: 0')
        print(f'[调试] 已更新日志行数显示为0')

        print('[调试] 已清空所有日志缓存')

    def clear_module_logs(self):
        """清空模组响应日志缓存"""
        global module_log_cache, full_log_cache, log_cache

        if hasattr(self, 'clear_log_markers'):
            self.clear_log_markers()
        print(f'[调试] 清空前 - module_log_cache长度: {len(module_log_cache)}, full_log_cache长度: {len(full_log_cache)}, log_cache长度: {len(log_cache)}')

        # 清空显示窗口
        self.log_text.clear()
        print(f'[调试] 已清空模组日志显示窗口')

        # 清空所有日志缓存
        module_log_cache.clear()
        full_log_cache.clear()
        log_cache.clear()

        print(f'[调试] 清空后 - module_log_cache长度: {len(module_log_cache)}, full_log_cache长度: {len(full_log_cache)}, log_cache长度: {len(log_cache)}')

        # 更新日志行数显示为0
        self.log_count_label.setText('日志行数: 0')
        print(f'[调试] 已更新模组日志行数显示为0')

        print('[调试] 已清空所有日志缓存')

    def add_shortcut_command(self):
        """添加快捷命令"""
        # 获取当前输入的命令
        command_str = self.command_input.text().strip()
        if not command_str:
            QMessageBox.warning(self, '输入错误', '请先输入要添加为快捷命令的十六进制字符串！')
            return

        # 验证命令格式
        command_str = command_str.replace(' ', '').replace(',', '').replace('-', '').upper()
        if not all(c in '0123456789ABCDEF' for c in command_str):
            QMessageBox.warning(self, '输入错误', '请输入有效的十六进制字符串！')
            return

        if len(command_str) % 2 != 0:
            QMessageBox.warning(self, '输入错误', '十六进制字符串长度必须为偶数！')
            return

        # 弹出对话框，让用户输入命令名称
        name, ok = QInputDialog.getText(self, '添加快捷命令', '请输入快捷命令的名称（例如: 获取版本号）:')
        if not ok or not name.strip():
            return

        name = name.strip()

        # 检查是否已存在同名命令
        for cmd in self.shortcut_commands:
            if cmd['name'] == name:
                reply = QMessageBox.question(
                    self,
                    '重复的命令名称',
                    f'快捷命令 "{name}" 已存在，是否覆盖？',
                    QMessageBox.Yes | QMessageBox.No
                )
                if reply == QMessageBox.Yes:
                    cmd['command'] = command_str
                    self.save_shortcut_commands()
                    self.refresh_shortcut_buttons()
                    self.append_log(f'[系统] 已更新快捷命令: {name}')
                return

        # 添加新命令
        self.shortcut_commands.append({'name': name, 'command': command_str})
        self.save_shortcut_commands()
        self.refresh_shortcut_buttons()
        self.append_log(f'[系统] 已添加快捷命令: {name}')

    def refresh_shortcut_buttons(self):
        """刷新快捷命令按钮"""
        # 找到所有快捷命令按钮并删除（保留前3个按钮：发送、清空、加号）
        items_to_remove = []
        for i in range(self.shortcut_buttons_layout.count()):
            item = self.shortcut_buttons_layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                # 如果是快捷命令按钮（通过objectName识别）
                if hasattr(widget, 'objectName') and widget.objectName() == 'shortcut_btn':
                    items_to_remove.append(widget)

        for widget in items_to_remove:
            widget.deleteLater()

        # 在加号按钮后面添加快捷命令按钮
        insert_position = 3  # 发送、清空、加号之后
        for idx, cmd in enumerate(self.shortcut_commands):
            btn = QPushButton(f'⚡ {cmd["name"]}')
            btn.setObjectName('shortcut_btn')  # 标记为快捷命令按钮
            btn.setStyleSheet('QPushButton { background-color: #2196F3; color: white; padding: 6px 12px; border-radius: 4px; }')
            btn.clicked.connect(lambda checked, c=cmd['command']: self.send_shortcut_command(c))

            # 右键菜单：删除
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(lambda pos, button=btn, command=cmd: self.show_shortcut_menu(button, command))

            self.shortcut_buttons_layout.insertWidget(insert_position + idx, btn)

    def show_shortcut_menu(self, button, command):
        """显示快捷命令右键菜单"""
        menu = QMenu(self)
        delete_action = menu.addAction('🗑️ 删除此快捷命令')
        action = menu.exec_(button.mapToGlobal(button.rect().center()))

        if action == delete_action:
            reply = QMessageBox.question(
                self,
                '确认删除',
                f'确定要删除快捷命令 "{command["name"]}" 吗？',
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.shortcut_commands.remove(command)
                self.save_shortcut_commands()
                self.refresh_shortcut_buttons()
                self.append_log(f'[系统] 已删除快捷命令: {command["name"]}')

    def send_shortcut_command(self, command_str):
        """发送快捷命令"""
        # 将命令填入输入框
        self.command_input.setText(command_str)
        # 发送命令
        self.send_command()

    def send_command(self):
        """发送自定义命令"""
        if not self.parent_window or not self.parent_window.module_connected:
            self.append_log('[错误] 模组未连接，无法发送命令', error=True)
            QMessageBox.warning(self, '错误', '请先连接模组！')
            return

        # 获取输入
        command_str = self.command_input.text().strip()
        if not command_str:
            self.append_log('[错误] 命令为空', error=True)
            return

        # 移除空格和常见分隔符
        command_str = command_str.replace(' ', '').replace(',', '').replace('-', '').replace('0x', '').replace('0X', '')

        # 验证是否为有效的十六进制字符串
        if not all(c in '0123456789ABCDEFabcdef' for c in command_str):
            self.append_log('[错误] 输入包含非十六进制字符', error=True)
            QMessageBox.warning(self, '输入错误', '请输入有效的十六进制字符串！\n例如: AA 55 01 02 03')
            return

        # 检查长度是否为偶数
        if len(command_str) % 2 != 0:
            self.append_log('[错误] 十六进制字符串长度必须为偶数', error=True)
            QMessageBox.warning(self, '输入错误', '十六进制字符串长度必须为偶数！\n例如: AA55 而不是 AA5')
            return

        try:
            # 转换为字节
            command_bytes = bytes.fromhex(command_str)

            # 记录发送的命令
            hex_str = ' '.join([f'{b:02X}' for b in command_bytes])
            self.append_log(f'[发送] {hex_str} ({len(command_bytes)} 字节)', send=True)

            # 发送命令
            if self.parent_window.module_serial and self.parent_window.module_serial.is_open:
                self.parent_window.module_serial.write(command_bytes)

                # 也记录到主窗口日志
                self.parent_window.append_module_log(f'[自定义命令] 发送: {hex_str}')
            else:
                self.append_log('[错误] 串口未打开', error=True)

        except Exception as e:
            self.append_log(f'[错误] 发送失败: {str(e)}', error=True)
            QMessageBox.critical(self, '发送失败', f'发送命令时出错：\n{str(e)}')

    def append_log(self, text, send=False, receive=False, error=False):
        """添加日志"""
        timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]

        if error:
            color = '#d32f2f'
        elif send:
            color = '#1976d2'
        elif receive:
            color = '#388e3c'
        else:
            color = '#333333'

        html = f'<span style="color: {color};">[{timestamp}] {text}</span>'
        self.output_text.append(html)

        # 自动滚动到底部
        cursor = self.output_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.output_text.setTextCursor(cursor)

    def receive_data(self, data_bytes):
        """接收到数据时调用（由主窗口调用）"""
        print(f'[调试-receive_data] 被调用，收到 {len(data_bytes)} 字节')
        print(f'[调试-receive_data] 数据: {data_bytes.hex().upper()}')
        hex_str = ' '.join([f'{b:02X}' for b in data_bytes])
        self.append_log(f'[接收] {hex_str} ({len(data_bytes)} 字节)', receive=True)
        print(f'[调试-receive_data] 已调用 append_log')

# ==============================
# 图片数据类
# ==============================

class DownloadPerformance:
    """只在主线程聚合下载计时，包级数据只保存计数/累计/最大值。"""

    def __init__(self, image_type):
        self.image_type = image_type
        self.started = time.perf_counter()
        self.stages = {}
        self.metrics = {}
        self.counts = {}
        self.request_started = None
        self.request_retried = False
        self.handled_at = None
        self.last_progress = self.started
        self.baudrate = 1500000
        self.packets = 0
        self.bytes_received = 0

    def mark(self, name):
        self.stages[name] = time.perf_counter()

    def add(self, name, seconds):
        count, total, maximum = self.metrics.get(name, (0, 0.0, 0.0))
        seconds = max(0.0, seconds)
        self.metrics[name] = (count + 1, total + seconds, max(maximum, seconds))

    def count(self, name):
        self.counts[name] = self.counts.get(name, 0) + 1

    def summary(self, outcome):
        now = time.perf_counter()
        lines = [f'[下载性能] {self.image_type.upper()} {outcome}，'
                 f'全流程 {now - self.started:.3f}s，{self.bytes_received} 字节/{self.packets} 包']
        previous = self.started
        for key, label in (
            ('high_baud', '高速波特率准备'), ('transfer', '获取大小/稳定等待'),
            ('received', '数据传输'), ('split', '数据分离'),
            ('saved', '目录创建/文件写入'), ('preview', '预览/历史更新'),
            ('restore_sent', '恢复请求准备/发送'), ('restored', '恢复波特率'),
        ):
            if key in self.stages:
                stamp = self.stages[key]
                lines.append(f'[下载性能] 阶段 {label}: {(stamp - previous) * 1000:.2f}ms')
                previous = stamp
        if 'transfer' in self.stages:
            seconds = max(1e-9, self.stages.get('received', now) - self.stages['transfer'])
            rate = self.bytes_received / seconds
            lines.append(f'[下载性能] 传输平均 {rate / 1024:.2f} KiB/s，'
                         f'8N1 理论线路利用率 {rate / (self.baudrate / 10) * 100:.1f}% '
                         f'（{self.baudrate}bps；含停等开销）')
        for name, (count, total, maximum) in self.metrics.items():
            lines.append(f'[下载性能] {name}: 样本 {count}，累计 {total * 1000:.2f}ms，'
                         f'平均 {total / count * 1000:.2f}ms，最大 {maximum * 1000:.2f}ms')
        lines.append('[下载性能] 异常计数: ' + (', '.join(
            f'{name}={value}' for name, value in self.counts.items()) or '无'))
        return lines


class OtaPerformance(DownloadPerformance):
    """复用计时聚合，OTA吞吐仅按成功确认的固件字节计算。"""

    def __init__(self):
        super().__init__('ota')

    def summary(self, outcome):
        now = time.perf_counter()
        lines = [f'[OTA性能] {outcome}，全流程 {now - self.started:.3f}s，'
                 f'已确认 {self.bytes_received} 字节/{self.packets} 包']
        previous = self.started
        for key, label in (
            ('loaded', '读取固件'), ('transfer', '波特率/OTA准备/header'),
            ('received', '固件传输'), ('burned', '模组烧录等待'),
            ('ready', '模组重启等待'),
        ):
            if key in self.stages:
                stamp = self.stages[key]
                lines.append(f'[OTA性能] 阶段 {label}: {(stamp - previous) * 1000:.2f}ms')
                previous = stamp
        if 'transfer' in self.stages:
            seconds = max(1e-9, self.stages.get('received', now) - self.stages['transfer'])
            rate = self.bytes_received / seconds
            lines.append(f'[OTA性能] 有效传输 {rate / 1024:.2f} KiB/s，'
                         f'8N1理论线路利用率 {rate / (self.baudrate / 10) * 100:.1f}% '
                         f'（{self.baudrate}bps）')
        for name, (count, total, maximum) in self.metrics.items():
            lines.append(f'[OTA性能] {name}: 样本 {count}，累计 {total * 1000:.2f}ms，'
                         f'平均 {total / count * 1000:.2f}ms，最大 {maximum * 1000:.2f}ms')
        lines.append('[OTA性能] 异常计数: ' + (', '.join(
            f'{name}={value}' for name, value in self.counts.items()) or '无'))
        return lines


class ImageData:
    """图片数据类"""
    def __init__(self, folder_path, display_name=None):
        self.folder_path = folder_path
        self.timestamp = datetime.now()
        self.saved = False
        # 显示名称，如果未指定则使用文件夹名
        self.display_name = display_name if display_name else os.path.basename(folder_path)

        # 查找文件夹中的图片文件
        self.image_files = []
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                if file.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.raw')):
                    self.image_files.append(os.path.join(root, file))

        # 为了兼容性，设置 ir_path 和 rgb_path（如果有至少2张图）
        if len(self.image_files) >= 2:
            self.ir_path = self.image_files[0]
            self.rgb_path = self.image_files[1]
        elif len(self.image_files) == 1:
            self.ir_path = self.image_files[0]
            self.rgb_path = None
        else:
            self.ir_path = None
            self.rgb_path = None

# ==============================
# 串口日志线程
# ==============================

class SerialReaderControl:
    """协调日志串口线程停止、句柄唤醒和关闭确认。"""

    def __init__(self):
        self.stop_event = threading.Event()
        self.closed_event = threading.Event()
        self._lock = threading.Lock()
        self._serial = None
        self.close_error = None

    def set_serial(self, serial_instance):
        with self._lock:
            self._serial = serial_instance

    def clear_serial(self, serial_instance=None):
        with self._lock:
            if serial_instance is None or self._serial is serial_instance:
                self._serial = None

    def request_stop(self):
        self.stop_event.set()
        with self._lock:
            serial_instance = self._serial
        if serial_instance is not None:
            cancel_read = getattr(serial_instance, 'cancel_read', None)
            if callable(cancel_read):
                try:
                    cancel_read()
                except (OSError, serial.SerialException):
                    pass

    def is_active(self):
        with self._lock:
            has_serial = self._serial is not None
        return (self.close_error is not None
                or (not self.closed_event.is_set()
                    and (has_serial or not self.stop_event.is_set())))


def serial_reader(port, baudrate, error_queue, connected_event=None, log_queue=None,
                  send_queue=None, control=None):
    """串口读取线程函数"""
    has_explicit_control = control is not None
    control = control or SerialReaderControl()
    ser = None
    try:
        if control.stop_event.is_set():
            return
        available_ports = [p.device for p in serial.tools.list_ports.comports()]
        if port not in available_ports:
            raise serial.SerialException(f'串口 {port} 不存在或未连接')

        ser = serial.Serial(port=port, baudrate=baudrate, timeout=1, write_timeout=0.5)
        control.set_serial(ser)
        if control.stop_event.is_set():
            return
        print('串口打开成功:', port, baudrate)

        if connected_event is not None:
            connected_event.set()

        while (not control.stop_event.is_set()
               and (has_explicit_control or connected_event is None
                    or connected_event.is_set())):
            # 检查发送队列
            if send_queue is not None:
                try:
                    data_to_send = send_queue.get_nowait()
                    ser.write(data_to_send)
                    ser.flush()
                    print(f'[发送] {len(data_to_send)} 字节')
                except queue.Empty:
                    pass
                except Exception as e:
                    print(f'[发送失败] {e}')

            # 接收数据
            if ser.in_waiting:
                data = ser.readline()
                line = data.decode('utf-8', errors='ignore').rstrip()
                line = strip_ansi_codes(line)
                line = line.lstrip()  # 清除前导空格
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                log = f'[{timestamp}] {line}'
                with log_cache_lock:
                    log_cache.append(log)
                    full_log_cache.append(log)
                    log_index = len(full_log_cache) - 1
                    if log_queue is not None:
                        log_queue.put(QueuedLog(log_index, log))
            else:
                time.sleep(0.01)

    except Exception as e:
        if not control.stop_event.is_set():
            print('串口异常:', e)
            error_queue.put(f'串口 {port} 已断开或无法访问：\n{e}')
    finally:
        # 确保工作线程退出前已释放Windows串口句柄。
        if ser is not None and getattr(ser, 'is_open', False):
            try:
                ser.close()
                print(f'串口 {port} 已关闭')
            except Exception as e:
                control.close_error = e
                print(f'关闭串口失败: {e}')
        control.clear_serial(ser)
        if connected_event is not None:
            connected_event.clear()
        control.closed_event.set()

# ==============================
# 文件夹监听
# ==============================

class DownloadFolderHandler(FileSystemEventHandler):
    """下载文件夹监听处理器"""

    def __init__(self, main_window):
        self.main_window = main_window

    def on_created(self, event):
        """文件夹创建事件"""
        if not event.is_directory:
            return

        src_folder = event.src_path
        print('\n发现新的下载文件夹:', src_folder)
        time.sleep(2)  # 等待上位机写入完成

        # 通知主窗口
        if self.main_window:
            self.main_window.new_image_signal.emit(src_folder)

# ==============================
# 日志打点与设置对话框
# ==============================

class LogMarkerWindow(QDialog):
    """非模态日志打点工具窗；关闭时仅隐藏。"""

    def __init__(self, main_window):
        super().__init__(main_window, Qt.Tool)
        self.main_window = main_window
        self.setWindowTitle('📍 日志打点')
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.resize(480, 215)
        self._updating = False

        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.btn_mark_current = QPushButton('📍 当前打点')
        self.btn_mark_current.clicked.connect(main_window.add_current_log_marker)
        controls.addWidget(self.btn_mark_current)
        self.btn_return_live = QPushButton('↩ 返回实时日志')
        self.btn_return_live.clicked.connect(main_window.return_to_live_logs)
        self.btn_return_live.setEnabled(False)
        controls.addWidget(self.btn_return_live)
        self.btn_delete = QPushButton('🗑️ 删除选中')
        self.btn_delete.clicked.connect(self.delete_selected)
        controls.addWidget(self.btn_delete)
        controls.addStretch()
        layout.addLayout(controls)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(('名称', '时间', '日志总行数', '目标行号', '日志摘要（20字符）'))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked |
                                   QAbstractItemView.EditKeyPressed)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.itemChanged.connect(self.on_item_changed)
        self.table.cellClicked.connect(self.on_row_clicked)
        layout.addWidget(self.table)

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def refresh(self, selected_marker_id=None):
        self._updating = True
        try:
            self.table.setRowCount(0)
            selected_row = -1
            for marker in self.main_window.log_markers:
                row = self.table.rowCount()
                self.table.insertRow(row)
                values = (
                    marker.name,
                    marker.created_at.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                    str(marker.log_count),
                    str(marker.line_number),
                    marker.summary,
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.UserRole, marker.marker_id)
                    if column != 0:
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    self.table.setItem(row, column, item)
                if marker.marker_id == selected_marker_id:
                    selected_row = row
            if selected_row >= 0:
                self.table.selectRow(selected_row)
                self.table.scrollToItem(self.table.item(selected_row, 0))
        finally:
            self._updating = False
        self.update_history_state()

    def update_history_state(self):
        self.btn_return_live.setEnabled(self.main_window.log_history_marker_id is not None)

    def marker_for_row(self, row):
        item = self.table.item(row, 0)
        if item is None:
            return None
        marker_id = item.data(Qt.UserRole)
        return self.main_window.marker_by_id(marker_id)

    def on_row_clicked(self, row, column):
        marker = self.marker_for_row(row)
        if marker:
            self.main_window.show_marker_context(marker)

    def on_item_changed(self, item):
        if self._updating or item.column() != 0:
            return
        marker = self.main_window.marker_by_id(item.data(Qt.UserRole))
        if marker is None:
            return
        name = item.text().strip()
        if name:
            marker.name = name
        else:
            self._updating = True
            item.setText(marker.name)
            self._updating = False

    def delete_selected(self):
        row = self.table.currentRow()
        marker = self.marker_for_row(row) if row >= 0 else None
        if marker:
            self.main_window.delete_log_marker(marker.marker_id)


class SaveLogDialog(QDialog):
    """选择完整日志或从某次打点开始保存。"""

    def __init__(self, markers, parent=None):
        super().__init__(parent)
        self.setWindowTitle('保存日志')
        self.setMinimumWidth(470)
        layout = QVBoxLayout(self)
        range_layout = QHBoxLayout()
        range_layout.addWidget(QLabel('保存范围:'))
        self.range_combo = QComboBox()
        self.range_combo.addItem('全部日志', None)
        for marker in markers:
            label = (f'{marker.name} · {marker.created_at.strftime("%H:%M:%S.%f")[:-3]} '
                     f'· 第{marker.line_number}行')
            self.range_combo.addItem(label, marker.marker_id)
        range_layout.addWidget(self.range_combo, 1)
        layout.addLayout(range_layout)
        remark_layout = QHBoxLayout()
        remark_layout.addWidget(QLabel('备注（可选）:'))
        self.remark_input = QLineEdit()
        remark_layout.addWidget(self.remark_input, 1)
        layout.addLayout(remark_layout)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText('保存')
        buttons.button(QDialogButtonBox.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_marker_id(self):
        return self.range_combo.currentData()

    def remark(self):
        return self.remark_input.text().strip()


class SettingsDialog(QDialog):
    """设置对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('⚙️ 参数设置')
        self.setMinimumWidth(600)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        # 上位机配置
        host_group = QGroupBox('上位机配置')
        host_layout = QVBoxLayout(host_group)

        download_layout = QHBoxLayout()
        download_layout.addWidget(QLabel('图片目录:'))
        self.download_combo = QComboBox()
        self.download_combo.setEditable(True)
        self.download_combo.setMinimumWidth(400)
        download_layout.addWidget(self.download_combo)
        btn_download = QPushButton('📂')
        btn_download.setMaximumWidth(40)
        btn_download.clicked.connect(lambda: self.browse_directory_combo(self.download_combo))
        download_layout.addWidget(btn_download)
        host_layout.addLayout(download_layout)

        program_layout = QHBoxLayout()
        program_layout.addWidget(QLabel('上位机程序:'))
        self.program_combo = QComboBox()
        self.program_combo.setEditable(True)
        self.program_combo.setMinimumWidth(400)
        program_layout.addWidget(self.program_combo)
        btn_program = QPushButton('📂')
        btn_program.setMaximumWidth(40)
        btn_program.clicked.connect(self.browse_program)
        program_layout.addWidget(btn_program)
        host_layout.addLayout(program_layout)

        hint_label = QLabel('提示：留空则不自动启动上位机程序')
        hint_label.setStyleSheet('color: #888888; font-size: 9pt;')
        host_layout.addWidget(hint_label)

        layout.addWidget(host_group)

        # 测试信息
        test_group = QGroupBox('测试信息')
        test_layout = QVBoxLayout(test_group)

        output_layout = QHBoxLayout()
        output_layout.addWidget(QLabel('保存目录:'))
        self.output_combo = QComboBox()
        self.output_combo.setEditable(True)
        self.output_combo.setMinimumWidth(400)
        output_layout.addWidget(self.output_combo)
        btn_output = QPushButton('📂')
        btn_output.setMaximumWidth(40)
        btn_output.clicked.connect(lambda: self.browse_directory_combo(self.output_combo))
        output_layout.addWidget(btn_output)
        test_layout.addLayout(output_layout)

        for button in (btn_download, btn_program, btn_output):
            button.setProperty('compactButton', True)

        version_layout = QHBoxLayout()
        version_layout.addWidget(QLabel('测试版本:'))
        self.version_entry = QLineEdit()
        self.version_entry.setPlaceholderText('如：V1.0.0')
        version_layout.addWidget(self.version_entry)
        test_layout.addLayout(version_layout)

        layout.addWidget(test_group)

        # 按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton('取消')
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        apply_btn = QPushButton('应用')
        apply_btn.setStyleSheet('QPushButton { background-color: #2196F3; color: white; font-weight: bold; padding: 6px 20px; }')
        apply_btn.clicked.connect(self.accept)
        btn_layout.addWidget(apply_btn)

        layout.addLayout(btn_layout)

    def browse_directory_combo(self, combo):
        """浏览目录（带历史记录）"""
        current = combo.currentText()
        initial_dir = current if current and os.path.exists(current) else os.path.expanduser('~')

        path = QFileDialog.getExistingDirectory(self, '选择目录', initial_dir)
        if path:
            combo.setCurrentText(path)

    def browse_program(self):
        """浏览程序（带历史记录）"""
        current = self.program_combo.currentText()
        initial_dir = os.path.dirname(current) if current else os.path.expanduser('~')

        path, _ = QFileDialog.getOpenFileName(self, '选择上位机程序', initial_dir, '可执行文件 (*.exe);;所有文件 (*.*)')
        if path:
            self.program_combo.setCurrentText(path)


# ==============================
# 主窗口
# ==============================

class MainWindow(QMainWindow):
    """统一主窗口"""

    # 定义信号
    log_signal = Signal(str)
    error_signal = Signal(str)
    connected_signal = Signal()
    new_image_signal = Signal(str)
    module_response_signal = Signal(str, bytes)  # 模组响应信号 (command_type, data)
    custom_command_data_signal = Signal(bytes)  # 自定义命令原始数据信号

    def __init__(self):
        super().__init__()

        self.setWindowTitle('串口日志采集工具 v1.0.0.8')
        self.resize(1400, 800)

        # 串口相关
        self.port = None
        self.baudrate = None
        self.error_queue = queue.Queue()
        self.log_queue = queue.Queue()
        self.send_queue = queue.Queue()
        self.connected_event = threading.Event()
        self.log_connection_notified = False
        self.serial_thread = None
        self.serial_control = None

        # 模组串口相关
        self.module_port = None
        self.module_baudrate = None
        self.module_serial = None
        self.module_connected = False
        self.module_receive_thread = None
        self.module_response_queue = queue.Queue()
        self.module_command_start_time = {}  # 跟踪每个指令的发送时间
        self.current_command_type = None  # 跟踪当前正在执行的命令类型（用于区分Note消息）

        # 图片下载相关
        self.image1_size = 0  # 第一张图片大小
        self.image2_size = 0  # 第二张图片大小
        self.download_buffer = bytearray()  # 图片下载缓冲区
        self.download_offset = 0  # 当前下载偏移量
        self.download_total_size = 0  # 总下载大小
        self.is_downloading = False  # 是否正在下载
        self.download_perf = None
        self.last_upload_command = b''  # 最后一次上传指令（用于重传）
        self.retry_timer = None  # 重传定时器
        self.download_type = 'jpeg'  # 下载类型：'jpeg' 或 'raw'
        self.raw_mode = 'Y+IR'  # RAW图模式：'Y+RGB'(40%+60%) 或 'Y+IR'(50%+50%)
        self.pending_command = None  # 等待响应的指令（msg_id, retry_count）
        self.command_timeout_timer = None  # 指令超时定时器

        # 重复执行相关
        self.repeat_mode = False  # 是否处于重复执行模式
        self.repeat_current = 0  # 当前执行次数
        self.repeat_total = 1  # 总共要执行的次数
        self.repeat_command = None  # 要重复执行的命令函数
        self.repeat_success_count = 0  # 成功次数统计
        self.repeat_reply_received = False  # 当前操作是否已收到Reply（防止多次Reply触发）

        # OTA升级相关
        self.ota_in_progress = False  # 是否正在进行OTA
        self.ota_file_path = None  # OTA固件包路径
        self.ota_file_data = None  # OTA固件包数据
        self.ota_packet_size = 4096  # 每包大小（默认4096字节）
        self.ota_total_packets = 0  # 总包数
        self.ota_current_packet = 0  # 当前发送的包序号
        self.ota_stage = 0  # OTA阶段：0=未开始, 1=设置波特率, 2=进入OTA, 3=发送header, 4=发送固件包, 5=等待烧录完成
        self.ota_retry_timer = None  # OTA超时重传定时器
        self.ota_retry_count = 0  # 当前包的重传次数
        self.ota_perf = None
        self.ota_step_timer = None
        self.ota_waiting_ack = False

        # 待机相关
        self.is_standby_restoring = False  # 是否正在待机恢复波特率

        # 自动执行序列相关
        self.sequence_list = []  # 操作序列列表
        self.sequence_running = False  # 是否正在执行序列
        self.sequence_index = 0  # 当前执行到的索引
        self.sequence_wait_response = None  # 等待响应的操作类型
        self.sequence_current_loop = 0  # 当前循环次数
        self.sequence_total_loops = 1  # 总循环次数

        # 文件监控相关
        self.download_dir = None
        self.output_base = None
        self.output = None
        self.test_version = None
        self.host_program = None
        self.observer = None
        self.monitoring_enabled = False

        # 当前待保存的图片
        self.current_image_data = None

        # 历史图片记录（最多保存10个）
        self.image_history = []  # 存储 ImageData 对象
        self.max_history = 10

        # 主题模式
        self.dark_mode = False

        # 日志打点与历史上下文（仅当前运行会话）
        self.log_markers = []
        self.next_log_marker_id = 1
        self.log_marker_window = None
        self.log_history_marker_id = None
        self.log_history_start = None
        self.log_history_target_row = None
        self.log_marker_selection = None

        # 连接超时定时器
        self.connection_timeout_timer = None

        # 自定义命令对话框
        self.custom_command_dialog = None

        self.setup_ui()
        self.setup_signals()
        self.load_saved_config()

        # 强制应用浅色主题（确保跨系统一致性）
        if not self.dark_mode:
            self.apply_light_theme()

    def setup_ui(self):
        """构建UI"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # 主布局：左右分割
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # 创建左右分割器
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(True)
        self.main_splitter = splitter

        # ========== 左侧：工具栏 + 日志 ==========
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        # 串口配置（主窗口）
        serial_config_layout = QHBoxLayout()
        serial_config_layout.addWidget(QLabel('串口:'))

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(100)
        serial_config_layout.addWidget(self.port_combo)

        btn_refresh = QPushButton()
        btn_refresh.setObjectName('refreshPortsButton')
        btn_refresh.setProperty('compactButton', True)
        btn_refresh.setIconSize(QSize(16, 16))
        btn_refresh.setFixedSize(30, 28)
        btn_refresh.setAccessibleName('刷新日志串口列表')
        btn_refresh.setToolTip('刷新串口列表')
        btn_refresh.clicked.connect(self.refresh_ports)
        serial_config_layout.addWidget(btn_refresh)

        serial_config_layout.addWidget(QLabel('波特率:'))
        self.baudrate_combo = QComboBox()
        self.baudrate_combo.setEditable(True)
        self.baudrate_combo.addItems(['9600', '19200', '38400', '57600', '115200', '230400', '460800', '1500000', '2000000'])
        self.baudrate_combo.setCurrentText('115200')
        self.baudrate_combo.setMinimumWidth(100)
        serial_config_layout.addWidget(self.baudrate_combo)

        self.btn_connect = QPushButton('🔌 连接')
        self.btn_connect.clicked.connect(self.connect_serial)
        self.btn_connect.setStyleSheet('QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 6px 12px; }')
        serial_config_layout.addWidget(self.btn_connect)

        self.btn_more_functions = QPushButton('☰ 更多功能')
        self.btn_more_functions.setAccessibleName('日志工具更多功能')
        self.btn_more_functions.setToolTip('设置、图片监控、日志保存和主题')
        self.main_functions_menu = QMenu(self.btn_more_functions)
        self.action_settings = self.main_functions_menu.addAction('⚙️ 参数设置')
        self.action_monitor = self.main_functions_menu.addAction('👁️ 启用图片监控')
        self.image_paths_menu = self.main_functions_menu.addMenu('🖼️ 图片路径')
        self.action_copy_output_path = self.image_paths_menu.addAction('复制图片保存路径')
        self.action_open_output_path = self.image_paths_menu.addAction('跳转图片保存目录')
        self.image_paths_menu.addSeparator()
        self.action_copy_monitor_path = self.image_paths_menu.addAction('复制图片监控目录')
        self.action_open_monitor_path = self.image_paths_menu.addAction('跳转图片监控目录')
        self.action_log_markers = self.main_functions_menu.addAction('📍 日志打点')
        self.main_functions_menu.addSeparator()
        self.action_save_log = self.main_functions_menu.addAction('💾 保存日志')
        self.action_open_output = self.main_functions_menu.addAction('📂 打开保存目录')
        self.main_functions_menu.addSeparator()
        self.action_theme = self.main_functions_menu.addAction('🌙 切换至深色模式')
        self.action_settings.triggered.connect(self.open_settings_dialog)
        self.action_monitor.triggered.connect(self.toggle_monitoring)
        self.action_copy_output_path.triggered.connect(self.copy_image_output_path)
        self.action_open_output_path.triggered.connect(self.open_image_output_path)
        self.action_copy_monitor_path.triggered.connect(self.copy_image_monitor_path)
        self.action_open_monitor_path.triggered.connect(self.open_image_monitor_path)
        self.action_log_markers.triggered.connect(self.show_log_marker_window)
        self.action_save_log.triggered.connect(self.save_log_only)
        self.action_open_output.triggered.connect(self.open_output_directory)
        self.action_theme.triggered.connect(self.toggle_theme)
        self.btn_more_functions.setMenu(self.main_functions_menu)
        serial_config_layout.addWidget(self.btn_more_functions)
        serial_config_layout.addStretch()

        left_layout.addLayout(serial_config_layout)

        # 刷新串口列表
        self.refresh_ports()
        self.set_status('● 未连接', '#999999')

        # 日志显示区直接进入左侧布局，省去标题框占用的空间
        log_layout = QVBoxLayout()
        log_layout.setContentsMargins(0, 0, 0, 0)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setUndoRedoEnabled(False)
        # 最后一块为空行，额外保留一块以显示配置数量的逻辑日志
        self.log_text.document().setMaximumBlockCount(LOG_DISPLAY_MAX_LINES + 1)
        self.log_text.setFont(QFont('Consolas', 9))
        self.log_text.setContextMenuPolicy(Qt.CustomContextMenu)
        self.log_text.customContextMenuRequested.connect(self.show_log_context_menu)
        log_layout.addWidget(self.log_text)

        self.log_history_bar = QWidget()
        history_layout = QHBoxLayout(self.log_history_bar)
        history_layout.setContentsMargins(5, 3, 5, 3)
        self.log_history_label = QLabel()
        history_layout.addWidget(self.log_history_label)
        history_layout.addStretch()
        btn_return_live = QPushButton('↩ 返回实时日志')
        btn_return_live.clicked.connect(self.return_to_live_logs)
        history_layout.addWidget(btn_return_live)
        log_layout.addWidget(self.log_history_bar)
        self.log_history_bar.setVisible(False)

        # 嵌入式搜索栏（默认隐藏）
        self.search_bar = QWidget()
        self.search_bar.setObjectName('logSearchBar')
        search_bar_layout = QHBoxLayout(self.search_bar)
        search_bar_layout.setContentsMargins(5, 5, 5, 5)
        self.search_bar.setStyleSheet('QWidget#logSearchBar { background-color: #f0f0f0; border: 1px solid #ccc; }')

        search_bar_layout.addWidget(QLabel('🔍'))

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('输入搜索内容...')
        self.search_input.setFont(QFont('Consolas', 9))
        self.search_input.textChanged.connect(self.on_search_text_changed)
        self.search_input.returnPressed.connect(self.find_next)
        search_bar_layout.addWidget(self.search_input)

        self.search_result_label = QLabel('0/0')
        self.search_result_label.setStyleSheet('color: #666666; font-size: 9pt;')
        self.search_result_label.setMinimumWidth(50)
        search_bar_layout.addWidget(self.search_result_label)

        btn_prev = QPushButton('▲')
        btn_prev.setMaximumWidth(30)
        btn_prev.setToolTip('上一个 (Shift+Enter)')
        btn_prev.clicked.connect(self.find_previous)
        search_bar_layout.addWidget(btn_prev)

        btn_next = QPushButton('▼')
        btn_next.setMaximumWidth(30)
        btn_next.setToolTip('下一个 (Enter)')
        btn_next.clicked.connect(self.find_next)
        search_bar_layout.addWidget(btn_next)

        self.cb_case_sensitive = QCheckBox('Aa')
        self.cb_case_sensitive.setToolTip('区分大小写')
        self.cb_case_sensitive.stateChanged.connect(self.on_search_text_changed)
        search_bar_layout.addWidget(self.cb_case_sensitive)

        self.cb_whole_word = QCheckBox('[ ]')
        self.cb_whole_word.setToolTip('全字匹配')
        self.cb_whole_word.stateChanged.connect(self.on_search_text_changed)
        search_bar_layout.addWidget(self.cb_whole_word)

        btn_close_search = QPushButton('✕')
        btn_close_search.setMaximumWidth(30)
        btn_close_search.setToolTip('关闭搜索 (Esc)')
        btn_close_search.clicked.connect(self.hide_search_bar)
        search_bar_layout.addWidget(btn_close_search)

        for button in (btn_prev, btn_next, btn_close_search):
            button.setProperty('compactButton', True)

        log_layout.addWidget(self.search_bar)
        self.search_bar.setVisible(False)

        # 搜索相关变量
        self.search_matches = []
        self.current_match_index = -1
        self.search_extra_selections = []

        # 日志工具栏
        log_tool_layout = QHBoxLayout()

        btn_clear = QPushButton('🗑️ 清空')
        btn_clear.clicked.connect(self.clear_module_logs)
        log_tool_layout.addWidget(btn_clear)

        self.btn_more_log_viewers = QPushButton('📖 更多查看方式')
        self.btn_more_log_viewers.setToolTip('将完整日志快照交给本机文本编辑器查看')
        self.btn_more_log_viewers.clicked.connect(self.show_full_log_viewer_menu)
        log_tool_layout.addWidget(self.btn_more_log_viewers)

        log_tool_layout.addStretch()

        self.log_count_label = QLabel('日志行数: 0')
        self.log_count_label.setStyleSheet('color: #666666; font-size: 9pt;')
        log_tool_layout.addWidget(self.log_count_label)

        log_layout.addLayout(log_tool_layout)

        left_layout.addLayout(log_layout, 1)

        # 快速发送区（可折叠）
        send_outer_layout = QVBoxLayout()

        # 标题栏和折叠按钮
        send_header_layout = QHBoxLayout()
        self.btn_toggle_send = QPushButton('▶ 📤 快速发送')
        self.btn_toggle_send.clicked.connect(self.toggle_send_group)
        send_header_layout.addWidget(self.btn_toggle_send)
        send_header_layout.addStretch()
        send_outer_layout.addLayout(send_header_layout)

        # 可折叠的快速发送内容区域
        self.send_content_widget = QWidget()
        self.send_content_widget.setVisible(False)
        send_group = QGroupBox()
        send_layout = QVBoxLayout(send_group)
        self.send_content_widget.setLayout(QVBoxLayout())
        self.send_content_widget.layout().setContentsMargins(0, 0, 0, 0)
        self.send_content_widget.layout().addWidget(send_group)

        send_mode_layout = QHBoxLayout()
        self.mode_group = QButtonGroup()
        self.rb_text = QRadioButton('文本')
        self.rb_hex = QRadioButton('HEX')
        self.rb_binary = QRadioButton('BIN')
        self.rb_text.setChecked(True)

        self.mode_group.addButton(self.rb_text, 0)
        self.mode_group.addButton(self.rb_hex, 1)
        self.mode_group.addButton(self.rb_binary, 2)

        send_mode_layout.addWidget(self.rb_text)
        send_mode_layout.addWidget(self.rb_hex)
        send_mode_layout.addWidget(self.rb_binary)

        self.cb_add_newline = QCheckBox('自动换行')
        self.cb_add_newline.setChecked(True)
        send_mode_layout.addWidget(self.cb_add_newline)

        # 添加快捷命令按钮
        btn_add_shortcut = QPushButton('➕')
        btn_add_shortcut.setMaximumWidth(30)
        btn_add_shortcut.setProperty('compactButton', True)
        btn_add_shortcut.setToolTip('添加快捷命令')
        btn_add_shortcut.setStyleSheet('QPushButton { font-weight: bold; padding: 2px; }')
        btn_add_shortcut.clicked.connect(self.add_quick_command)
        send_mode_layout.addWidget(btn_add_shortcut)

        send_mode_layout.addStretch()

        send_layout.addLayout(send_mode_layout)

        # 快捷命令按钮区域
        self.shortcut_commands_layout = QHBoxLayout()
        self.shortcut_commands_layout.setSpacing(5)
        send_layout.addLayout(self.shortcut_commands_layout)

        # 加载保存的快捷命令
        self.quick_commands = []
        self.load_quick_commands()

        send_input_layout = QHBoxLayout()
        self.send_entry = QLineEdit()
        self.send_entry.setFont(QFont('Consolas', 10))
        self.send_entry.setPlaceholderText('输入要发送的数据...')
        self.send_entry.returnPressed.connect(self.quick_send)
        send_input_layout.addWidget(self.send_entry)

        btn_send = QPushButton('发送')
        btn_send.clicked.connect(self.quick_send)
        send_input_layout.addWidget(btn_send)

        send_layout.addLayout(send_input_layout)

        # 将内容添加到外层布局
        send_outer_layout.addWidget(self.send_content_widget)
        left_layout.addLayout(send_outer_layout)

        splitter.addWidget(left_widget)

        # ========== 右侧：图片预览 + 模组控制 + 保存控制（可滚动） ==========
        # 创建可滚动的右侧区域
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(5, 5, 5, 5)

        # === 上部：图片预览 ===
        preview_group = QGroupBox('🖼️ 图片预览')
        preview_layout = QVBoxLayout(preview_group)

        # 历史图片选择
        history_layout = QHBoxLayout()
        history_layout.addWidget(QLabel('历史图片:'))
        self.history_combo = QComboBox()
        self.history_combo.setMinimumWidth(250)
        self.history_combo.addItem('当前图片')
        self.history_combo.currentIndexChanged.connect(self.on_history_changed)
        history_layout.addWidget(self.history_combo)

        btn_clear_history = QPushButton('🗑️ 清空历史')
        btn_clear_history.setMaximumWidth(100)
        btn_clear_history.clicked.connect(self.clear_image_history)
        history_layout.addWidget(btn_clear_history)

        history_layout.addStretch()
        preview_layout.addLayout(history_layout)

        # 图片显示区域
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setMinimumWidth(400)
        scroll_area.setMinimumHeight(220)
        scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.image_container = QWidget()
        self.image_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image_layout = QVBoxLayout(self.image_container)
        self.image_layout.setAlignment(Qt.AlignCenter)
        self.image_layout.setContentsMargins(8, 8, 8, 8)

        self.no_image_label = QLabel('暂无图片\n\n启用图片监控后\n这里会显示检测到的图片')
        self.no_image_label.setAlignment(Qt.AlignCenter)
        self.no_image_label.setStyleSheet('color: #999999; font-size: 12pt; padding: 50px;')
        self.image_layout.addWidget(self.no_image_label)

        scroll_area.setWidget(self.image_container)
        preview_layout.addWidget(scroll_area)

        # 路径状态保留为兼容属性，不再占用预览区域的可见空间。
        self.image_info_label = QLabel('路径: 无')
        self.image_info_label.setVisible(False)
        self.monitor_status_label = QLabel('图片监控：未启用')
        self.monitor_status_label.setObjectName('monitorStatusLabel')
        self.monitor_status_label.setVisible(False)

        preview_layout.setStretch(preview_layout.indexOf(scroll_area), 1)
        right_layout.addWidget(preview_group, 1)

        # === 模组控制区域 ===
        module_outer_layout = QVBoxLayout()

        # 标题栏和折叠按钮
        module_header_layout = QHBoxLayout()
        self.btn_toggle_module = QPushButton('▼ 🔧 模组控制')
        self.btn_toggle_module.clicked.connect(self.toggle_module_group)
        module_header_layout.addWidget(self.btn_toggle_module)

        self.btn_module_modes = QPushButton('⚙️ 模式设置')
        self.btn_module_modes.setAccessibleName('模组模式设置')
        self.btn_module_modes.setToolTip('设置操作、RAW、项目、停止条件和重复次数')
        self.module_modes_menu = QMenu(self.btn_module_modes)
        self.btn_module_modes.setMenu(self.module_modes_menu)
        self.setup_module_modes_menu()
        module_header_layout.addWidget(self.btn_module_modes)

        # 模组串口控制紧跟模式设置，减少内容区占用的垂直空间。
        self.module_port_combo = QComboBox()
        self.module_port_combo.setMinimumWidth(100)
        self.module_port_combo.setToolTip('模组串口')
        module_header_layout.addWidget(self.module_port_combo)

        btn_refresh_module = QPushButton()
        btn_refresh_module.setObjectName('refreshModulePortsButton')
        btn_refresh_module.setProperty('compactButton', True)
        btn_refresh_module.setIconSize(QSize(16, 16))
        btn_refresh_module.setFixedSize(30, 28)
        btn_refresh_module.setAccessibleName('刷新模组串口列表')
        btn_refresh_module.setToolTip('刷新模组串口列表')
        btn_refresh_module.clicked.connect(self.refresh_module_ports)
        module_header_layout.addWidget(btn_refresh_module)

        self.module_baudrate_combo = QComboBox()
        self.module_baudrate_combo.setEditable(True)
        self.module_baudrate_combo.addItems(['9600', '19200', '38400', '57600', '115200', '230400', '460800', '1500000', '921600'])
        self.module_baudrate_combo.setCurrentText('115200')
        self.module_baudrate_combo.setMinimumWidth(100)
        self.module_baudrate_combo.setToolTip('模组波特率')
        module_header_layout.addWidget(self.module_baudrate_combo)

        self.btn_module_connect = QPushButton('🔌 连接模组')
        self.btn_module_connect.clicked.connect(self.connect_module)
        self.btn_module_connect.setStyleSheet('QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 6px 12px; }')
        module_header_layout.addWidget(self.btn_module_connect)
        module_header_layout.addStretch()
        module_outer_layout.addLayout(module_header_layout)

        # 可折叠的模组控制内容区域
        self.module_content_widget = QWidget()
        module_group = QGroupBox()
        module_layout = QVBoxLayout(module_group)
        self.module_content_widget.setLayout(QVBoxLayout())
        self.module_content_widget.layout().setContentsMargins(0, 0, 0, 0)
        self.module_content_widget.layout().addWidget(module_group)

        # 串口控制已移动到标题行；保留状态对象供业务逻辑和兼容调用使用，但不占用布局。
        self.module_status_label = QLabel('● 未连接')
        self.module_status_label.setVisible(False)
        self.module_status_label.setToolTip('模组连接状态通过连接按钮显示')

        # 指令按钮区域直接作为模组内容的第一部分。

        # 创建按钮容器，使用FlowLayout样式的网格布局
        button_container = QWidget()
        button_grid = QGridLayout(button_container)
        button_grid.setSpacing(8)
        button_grid.setContentsMargins(0, 0, 0, 0)

        # 定义固定按钮大小
        button_width = 140
        button_height = 32

        # 第一行：人脸注册、识别D、获取版本号、获取所有用户ID
        # 人脸注册（原单帧注册）
        btn_register = QPushButton('📸 人脸注册')
        btn_register.clicked.connect(self.register_single_frame)
        btn_register.setEnabled(False)
        btn_register.setFixedSize(button_width, button_height)
        self.btn_register = btn_register
        button_grid.addWidget(btn_register, 0, 0)

        # 识别D（原人脸识别）
        btn_face_recognition = QPushButton('🔍 识别D')
        btn_face_recognition.clicked.connect(self.face_recognition)
        btn_face_recognition.setEnabled(False)
        btn_face_recognition.setFixedSize(button_width, button_height)
        self.btn_face_recognition = btn_face_recognition
        button_grid.addWidget(btn_face_recognition, 0, 1)

        # 获取版本号
        btn_get_version = QPushButton('📋 获取版本号')
        btn_get_version.clicked.connect(self.get_module_version)
        btn_get_version.setEnabled(False)
        btn_get_version.setFixedSize(button_width, button_height)
        self.btn_get_version = btn_get_version
        button_grid.addWidget(btn_get_version, 0, 2)

        # 获取所有用户ID
        btn_get_all_user_ids = QPushButton('👥 获取所有用户ID')
        btn_get_all_user_ids.clicked.connect(self.get_all_user_ids)
        btn_get_all_user_ids.setEnabled(False)
        btn_get_all_user_ids.setFixedSize(button_width, button_height)
        self.btn_get_all_user_ids = btn_get_all_user_ids
        button_grid.addWidget(btn_get_all_user_ids, 0, 3)

        # 手掌注册 - 第二行
        btn_palm_register = QPushButton('🖐️ 手掌注册')
        btn_palm_register.clicked.connect(self.palm_register)
        btn_palm_register.setEnabled(False)
        btn_palm_register.setFixedSize(button_width, button_height)
        self.btn_palm_register = btn_palm_register
        button_grid.addWidget(btn_palm_register, 1, 0)

        # 识别K（原手掌识别）- 第二行
        btn_palm_recognition = QPushButton('🔍 识别K')
        btn_palm_recognition.clicked.connect(self.palm_recognition)
        btn_palm_recognition.setEnabled(False)
        btn_palm_recognition.setFixedSize(button_width, button_height)
        self.btn_palm_recognition = btn_palm_recognition
        button_grid.addWidget(btn_palm_recognition, 1, 1)

        # 下载JPEG - 第二行
        btn_download_image = QPushButton('📥 下载JPEG')
        btn_download_image.clicked.connect(self.download_image)
        btn_download_image.setEnabled(False)
        btn_download_image.setFixedSize(button_width, button_height)
        self.btn_download_image = btn_download_image
        button_grid.addWidget(btn_download_image, 1, 2)

        # 下载RAW - 第二行
        btn_download_raw = QPushButton('📥 下载RAW')
        btn_download_raw.clicked.connect(self.download_raw_image)
        btn_download_raw.setEnabled(False)
        btn_download_raw.setFixedSize(button_width, button_height)
        self.btn_download_raw = btn_download_raw
        button_grid.addWidget(btn_download_raw, 1, 3)

        # 添加弹性空间，让按钮靠左对齐
        button_grid.setColumnStretch(4, 1)

        module_layout.addWidget(button_container)

        # 折叠/展开更多指令按钮
        toggle_layout = QHBoxLayout()
        self.btn_toggle_commands = QPushButton('▼ 更多指令')
        self.btn_toggle_commands.setMaximumWidth(120)
        self.btn_toggle_commands.clicked.connect(self.toggle_command_panel)
        toggle_layout.addWidget(self.btn_toggle_commands)
        toggle_layout.addStretch()
        module_layout.addLayout(toggle_layout)

        # 可折叠的指令区域（第三行和第四行）
        self.more_commands_widget = QWidget()
        more_commands_layout = QVBoxLayout(self.more_commands_widget)
        more_commands_layout.setContentsMargins(0, 5, 0, 5)

        # 创建第三行和第四行的按钮容器
        more_button_container = QWidget()
        more_button_grid = QGridLayout(more_button_container)
        more_button_grid.setSpacing(8)
        more_button_grid.setContentsMargins(0, 0, 0, 0)

        # 删除指定用户ID - 第三行
        btn_delete_user = QPushButton('🗑️ 删除指定用户ID')
        btn_delete_user.clicked.connect(self.delete_user_by_id)
        btn_delete_user.setEnabled(False)
        btn_delete_user.setFixedSize(button_width, button_height)
        self.btn_delete_user = btn_delete_user
        more_button_grid.addWidget(btn_delete_user, 0, 0)

        # 删除所有用户 - 第三行
        btn_delete_all = QPushButton('🗑️ 删除所有用户')
        btn_delete_all.clicked.connect(self.delete_all_users)
        btn_delete_all.setEnabled(False)
        btn_delete_all.setFixedSize(button_width, button_height)
        self.btn_delete_all = btn_delete_all
        more_button_grid.addWidget(btn_delete_all, 0, 1)

        # 重启模组 - 第三行
        btn_restart_module = QPushButton('🔄 重启模组')
        btn_restart_module.clicked.connect(self.restart_module)
        btn_restart_module.setEnabled(False)
        btn_restart_module.setFixedSize(button_width, button_height)
        self.btn_restart_module = btn_restart_module
        more_button_grid.addWidget(btn_restart_module, 0, 2)

        # 待机 - 第三行
        btn_standby = QPushButton('😴 待机')
        btn_standby.clicked.connect(self.standby_module)
        btn_standby.setEnabled(False)
        btn_standby.setFixedSize(button_width, button_height)
        self.btn_standby = btn_standby
        more_button_grid.addWidget(btn_standby, 0, 3)

        # 进入演示 - 第四行
        btn_enter_demo = QPushButton('🎬 进入演示')
        btn_enter_demo.clicked.connect(self.enter_demo_mode)
        btn_enter_demo.setEnabled(False)
        btn_enter_demo.setFixedSize(button_width, button_height)
        self.btn_enter_demo = btn_enter_demo
        more_button_grid.addWidget(btn_enter_demo, 1, 0)

        # 退出演示 - 第四行
        btn_exit_demo = QPushButton('🎬 退出演示')
        btn_exit_demo.clicked.connect(self.exit_demo_mode)
        btn_exit_demo.setEnabled(False)
        btn_exit_demo.setFixedSize(button_width, button_height)
        self.btn_exit_demo = btn_exit_demo
        more_button_grid.addWidget(btn_exit_demo, 1, 1)

        # 进入Debug - 第四行
        btn_enter_debug = QPushButton('🐛 进入Debug')
        btn_enter_debug.clicked.connect(self.enter_debug_mode)
        btn_enter_debug.setEnabled(False)
        btn_enter_debug.setFixedSize(button_width, button_height)
        self.btn_enter_debug = btn_enter_debug
        more_button_grid.addWidget(btn_enter_debug, 1, 2)

        # 退出Debug - 第四行
        btn_exit_debug = QPushButton('🐛 退出Debug')
        btn_exit_debug.clicked.connect(self.exit_debug_mode)
        btn_exit_debug.setEnabled(False)
        btn_exit_debug.setFixedSize(button_width, button_height)
        self.btn_exit_debug = btn_exit_debug
        more_button_grid.addWidget(btn_exit_debug, 1, 3)

        # 自定义命令 - 第五行
        btn_custom_command = QPushButton('⚡ 自定义命令')
        btn_custom_command.clicked.connect(self.open_custom_command_dialog)
        btn_custom_command.setEnabled(False)
        btn_custom_command.setFixedSize(button_width, button_height)
        btn_custom_command.setStyleSheet('QPushButton { background-color: #ff9800; color: white; font-weight: bold; }')
        self.btn_custom_command = btn_custom_command
        more_button_grid.addWidget(btn_custom_command, 2, 0)

        # OTA升级 - 第五行
        btn_ota = QPushButton('🔄 OTA升级')
        btn_ota.clicked.connect(self.start_ota_upgrade)
        btn_ota.setEnabled(False)
        btn_ota.setFixedSize(button_width, button_height)
        btn_ota.setStyleSheet('QPushButton { background-color: #9c27b0; color: white; font-weight: bold; }')
        self.btn_ota = btn_ota
        more_button_grid.addWidget(btn_ota, 2, 1)

        # 添加弹性空间
        more_button_grid.setColumnStretch(4, 1)

        more_commands_layout.addWidget(more_button_container)
        module_layout.addWidget(self.more_commands_widget)

        # 默认隐藏更多指令区域
        self.more_commands_widget.setVisible(False)

        # 响应显示区域
        # 响应信息标题和清除按钮
        response_header_layout = QHBoxLayout()
        response_label = QLabel('响应信息:')
        response_label.setStyleSheet('font-weight: bold; margin-top: 10px;')
        response_header_layout.addWidget(response_label)

        btn_clear_response = QPushButton('🗑️ 清空')
        btn_clear_response.setMaximumWidth(80)
        btn_clear_response.setToolTip('清空响应信息')
        btn_clear_response.clicked.connect(self.clear_module_response)
        response_header_layout.addWidget(btn_clear_response)

        response_header_layout.addStretch()
        module_layout.addLayout(response_header_layout)

        self.module_response_text = QTextEdit()
        self.module_response_text.setReadOnly(True)
        response_font = QFont('Consolas', 10)
        response_font.setBold(False)
        self.module_response_text.setFont(response_font)
        self.module_response_text.setStyleSheet('QTextEdit { color: #000000; }')
        self.module_response_text.setMinimumHeight(300)
        self.module_response_text.setPlaceholderText('模组响应信息将显示在这里...')
        # 连接鼠标点击事件
        self.module_response_text.mousePressEvent = self.on_response_text_clicked
        module_layout.addWidget(self.module_response_text)

        # 将模组内容添加到折叠容器，然后添加到右侧布局
        module_outer_layout.addWidget(self.module_content_widget)
        right_layout.addLayout(module_outer_layout)

        # === 自动执行序列区域 ===
        sequence_outer_layout = QVBoxLayout()

        # 标题本身作为折叠入口，与模组控制区域一致
        sequence_header_layout = QHBoxLayout()
        self.btn_toggle_sequence = QPushButton('▶ 🔄 自动执行序列')
        self.btn_toggle_sequence.clicked.connect(self.toggle_sequence_panel)
        sequence_header_layout.addWidget(self.btn_toggle_sequence)
        sequence_header_layout.addStretch()
        sequence_outer_layout.addLayout(sequence_header_layout)

        # 序列内容容器
        self.sequence_content_widget = QGroupBox()
        self.sequence_content_widget.setVisible(False)
        sequence_layout = QVBoxLayout(self.sequence_content_widget)

        # 操作选择和添加
        add_layout = QHBoxLayout()
        add_layout.addWidget(QLabel('选择操作:'))
        self.sequence_operation_combo = QComboBox()
        self.sequence_operation_combo.addItems([
            '人脸注册', '手掌注册', '识别D', '识别K',
            '下载JPEG', '下载RAW', '获取版本号', '获取所有用户ID',
            '删除指定用户ID', '删除所有用户', '重启模组', '待机',
            '进入演示', '退出演示', '进入Debug', '退出Debug'
        ])
        add_layout.addWidget(self.sequence_operation_combo)

        btn_add_operation = QPushButton('➕ 添加')
        btn_add_operation.clicked.connect(self.add_sequence_operation)
        add_layout.addWidget(btn_add_operation)
        sequence_layout.addLayout(add_layout)

        # 序列列表
        self.sequence_list_widget = QListWidget()
        self.sequence_list_widget.setMaximumHeight(150)
        sequence_layout.addWidget(self.sequence_list_widget)

        # 管理按钮
        manage_layout = QHBoxLayout()
        btn_move_up = QPushButton('⬆️ 上移')
        btn_move_up.clicked.connect(self.move_sequence_up)
        manage_layout.addWidget(btn_move_up)

        btn_move_down = QPushButton('⬇️ 下移')
        btn_move_down.clicked.connect(self.move_sequence_down)
        manage_layout.addWidget(btn_move_down)

        btn_delete = QPushButton('❌ 删除')
        btn_delete.clicked.connect(self.delete_sequence_operation)
        manage_layout.addWidget(btn_delete)
        sequence_layout.addLayout(manage_layout)

        # 循环次数设置
        loop_layout = QHBoxLayout()
        loop_layout.addWidget(QLabel('循环次数:'))
        self.sequence_loop_spin = QSpinBox()
        self.sequence_loop_spin.setMinimum(1)
        self.sequence_loop_spin.setMaximum(1000)
        self.sequence_loop_spin.setValue(1)
        self.sequence_loop_spin.setFixedWidth(80)
        self.sequence_loop_spin.setToolTip('设置序列循环执行的次数')
        loop_layout.addWidget(self.sequence_loop_spin)
        loop_layout.addStretch()
        sequence_layout.addLayout(loop_layout)

        # 执行控制按钮
        control_layout = QHBoxLayout()
        self.btn_start_sequence = QPushButton('▶️ 启动序列')
        self.btn_start_sequence.clicked.connect(self.start_sequence)
        self.btn_start_sequence.setStyleSheet('''
            QPushButton {
                padding: 10px;
                font-size: 10pt;
                background-color: #4CAF50;
                color: white;
                font-weight: bold;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        ''')
        control_layout.addWidget(self.btn_start_sequence)

        self.btn_stop_sequence = QPushButton('⏹️ 停止序列')
        self.btn_stop_sequence.clicked.connect(self.stop_sequence)
        self.btn_stop_sequence.setEnabled(False)
        self.btn_stop_sequence.setStyleSheet('''
            QPushButton {
                padding: 10px;
                font-size: 10pt;
            }
            QPushButton:enabled {
                background-color: #f44336;
                color: white;
                font-weight: bold;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        ''')
        control_layout.addWidget(self.btn_stop_sequence)
        sequence_layout.addLayout(control_layout)

        # 将序列内容添加到外层布局
        sequence_outer_layout.addWidget(self.sequence_content_widget)
        right_layout.addLayout(sequence_outer_layout)

        # === 保存控制区域 ===
        save_layout = QVBoxLayout()

        # 标题本身作为折叠入口，与模组控制区域一致
        save_header_layout = QHBoxLayout()
        self.btn_toggle_save = QPushButton('▶ 💾 保存控制')
        self.btn_toggle_save.clicked.connect(self.toggle_save_panel)
        save_header_layout.addWidget(self.btn_toggle_save)
        save_header_layout.addStretch()
        save_layout.addLayout(save_header_layout)

        # 保存控制内容容器
        self.save_content_widget = QGroupBox()
        self.save_content_widget.setVisible(False)
        save_content_layout = QVBoxLayout(self.save_content_widget)

        # 测试信息输入（按照命名规则顺序）
        form_layout = QVBoxLayout()

        # 1. 类型（最前面）
        type_layout = QHBoxLayout()
        type_layout.addWidget(QLabel('类型:'))
        self.type_combo = QComboBox()
        self.type_combo.addItems(['注册', '识别'])
        self.type_combo.currentIndexChanged.connect(self.on_type_changed)
        type_layout.addWidget(self.type_combo)

        # 添加保存数量输入框
        type_layout.addWidget(QLabel('  保存数量:'))
        self.save_count_spin = QSpinBox()
        self.save_count_spin.setMinimum(1)
        self.save_count_spin.setMaximum(100)
        self.save_count_spin.setValue(1)
        self.save_count_spin.setFixedWidth(80)
        self.save_count_spin.setToolTip('保存最近N组图片（每组2张）')
        type_layout.addWidget(self.save_count_spin)

        type_layout.addStretch()
        form_layout.addLayout(type_layout)

        # 2. 注册人员
        person_layout = QHBoxLayout()
        self.person_label = QLabel('注册人员:')
        person_layout.addWidget(self.person_label)
        self.person_entry = QLineEdit()
        person_layout.addWidget(self.person_entry)
        form_layout.addLayout(person_layout)

        scene_values = ['室外顺光', '室外侧光', '室外逆光', '半室外顺光', '半室外侧光', '半室外逆光', '暗室', '室内正常光']
        luma_values = ['0lux', '0-250lux', '250-1000lux', '1wlux以下', '1-3wlux', '3-5wlux', '6-8wlux', '8-12wlux', '12wlux以上']

        # 3. 注册场景
        reg_scene_layout = QHBoxLayout()
        self.reg_scene_label = QLabel('注册场景:')
        reg_scene_layout.addWidget(self.reg_scene_label)
        self.register_scene_combo = QComboBox()
        self.register_scene_combo.addItems(scene_values)
        reg_scene_layout.addWidget(self.register_scene_combo)
        form_layout.addLayout(reg_scene_layout)

        # 4. 注册亮度
        reg_luma_layout = QHBoxLayout()
        self.reg_luma_label = QLabel('注册亮度:')
        reg_luma_layout.addWidget(self.reg_luma_label)
        self.register_luma_combo = QComboBox()
        self.register_luma_combo.addItems(luma_values)
        reg_luma_layout.addWidget(self.register_luma_combo)
        form_layout.addLayout(reg_luma_layout)

        # 5. 识别场景（仅识别时启用）
        rec_scene_layout = QHBoxLayout()
        self.rec_scene_label = QLabel('识别场景:')
        rec_scene_layout.addWidget(self.rec_scene_label)
        self.recognize_scene_combo = QComboBox()
        self.recognize_scene_combo.addItems(scene_values)
        rec_scene_layout.addWidget(self.recognize_scene_combo)
        form_layout.addLayout(rec_scene_layout)

        # 6. 识别亮度（仅识别时启用）
        rec_luma_layout = QHBoxLayout()
        self.rec_luma_label = QLabel('识别亮度:')
        rec_luma_layout.addWidget(self.rec_luma_label)
        self.recognize_luma_combo = QComboBox()
        self.recognize_luma_combo.addItems(luma_values)
        rec_luma_layout.addWidget(self.recognize_luma_combo)
        form_layout.addLayout(rec_luma_layout)

        # 7. 结果（仅识别时启用）
        result_layout = QHBoxLayout()
        self.result_label = QLabel('结果:')
        result_layout.addWidget(self.result_label)
        self.result_combo = QComboBox()
        self.result_combo.addItems(['成功', '失败'])
        result_layout.addWidget(self.result_combo)
        result_layout.addStretch()
        form_layout.addLayout(result_layout)

        # 8. 备注
        remark_layout = QHBoxLayout()
        self.remark_label = QLabel('备注:')
        remark_layout.addWidget(self.remark_label)
        self.remark_entry = QLineEdit()
        self.remark_entry.setPlaceholderText('可选，如：失败原因')
        remark_layout.addWidget(self.remark_entry)
        form_layout.addLayout(remark_layout)

        save_content_layout.addLayout(form_layout)

        # 初始化联动状态
        self.on_type_changed()

        # 日志策略
        strategy_layout = QHBoxLayout()
        strategy_layout.addWidget(QLabel('日志策略:'))

        self.strategy_combo = QComboBox()
        self.strategy_combo.addItems(['全部日志', '最近N行', '从关键词开始'])
        self.strategy_combo.currentIndexChanged.connect(self.on_strategy_changed)
        strategy_layout.addWidget(self.strategy_combo)

        self.lines_spin = QSpinBox()
        self.lines_spin.setRange(1, 10000)
        self.lines_spin.setValue(100)
        self.lines_spin.setPrefix('行数: ')
        strategy_layout.addWidget(self.lines_spin)

        self.keyword_entry = QLineEdit()
        self.keyword_entry.setPlaceholderText('关键词')
        self.keyword_entry.setVisible(False)
        strategy_layout.addWidget(self.keyword_entry)

        save_content_layout.addLayout(strategy_layout)

        # 初始化日志策略显示状态
        self.on_strategy_changed(0)

        # 保存按钮
        save_btn_layout = QHBoxLayout()

        self.btn_save_current = QPushButton('💾 保存当前图片和日志')
        self.btn_save_current.clicked.connect(self.save_current_image)
        self.btn_save_current.setEnabled(False)
        self.btn_save_current.setStyleSheet('''
            QPushButton {
                padding: 10px;
                font-size: 10pt;
            }
            QPushButton:enabled {
                background-color: #2196F3;
                color: white;
                font-weight: bold;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        ''')
        save_btn_layout.addWidget(self.btn_save_current)

        btn_skip = QPushButton('⏭️ 跳过')
        btn_skip.clicked.connect(self.skip_current_image)
        btn_skip.setEnabled(False)
        btn_skip.setStyleSheet('''
            QPushButton {
                padding: 10px;
                font-size: 10pt;
            }
            QPushButton:enabled {
                background-color: #f44336;
                color: white;
                font-weight: bold;
            }
            QPushButton:disabled {
                background-color: #cccccc;
                color: #666666;
            }
        ''')
        self.btn_skip = btn_skip
        save_btn_layout.addWidget(btn_skip)

        save_content_layout.addLayout(save_btn_layout)

        # 将内容容器添加到保存布局
        save_layout.addWidget(self.save_content_widget)

        right_layout.addLayout(save_layout)

        # 将右侧widget设置到滚动区域
        right_scroll.setWidget(right_widget)
        splitter.addWidget(right_scroll)

        # 刷新模组串口列表
        self.refresh_module_ports()

        # 左右两侧都依据内容提示保持基本可用宽度；仍允许拖动折叠到0。
        left_min_width = max(520, left_widget.sizeHint().width())
        right_min_width = max(560, right_widget.sizeHint().width())
        left_widget.setMinimumWidth(left_min_width)
        right_scroll.setMinimumWidth(right_min_width)
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 3)

        main_layout.addWidget(splitter)

        # 设置快捷键
        from PySide6.QtGui import QShortcut, QKeySequence

        # Ctrl+F 打开搜索
        self.search_shortcut = QShortcut(QKeySequence('Ctrl+F'), self)
        self.search_shortcut.activated.connect(self.show_search_bar)

        # Esc 关闭搜索
        self.escape_shortcut = QShortcut(QKeySequence('Esc'), self)
        self.escape_shortcut.activated.connect(self.hide_search_bar)

    def setup_signals(self):
        """设置信号连接"""
        self.log_signal.connect(self.append_log)
        self.error_signal.connect(self.handle_error)
        self.connected_signal.connect(self.handle_connected)
        self.new_image_signal.connect(self.handle_new_image)
        self.module_response_signal.connect(self.handle_module_response)

    def load_saved_config(self):
        """加载保存的配置"""
        # 加载串口配置
        config = load_config()
        if config.get('port'):
            # 先刷新串口列表
            self.refresh_ports()
            # 然后设置保存的串口
            if config['port'] in [self.port_combo.itemText(i) for i in range(self.port_combo.count())]:
                self.port_combo.setCurrentText(config['port'])
        if config.get('baudrate'):
            self.baudrate_combo.setCurrentText(str(config['baudrate']))

        # 加载上次的测试数据
        last = _load_last_selection()
        if last.get('person'):
            self.person_entry.setText(last['person'])

    def refresh_ports(self):
        """刷新串口列表"""
        ports = [p.device for p in serial.tools.list_ports.comports()]
        current = self.port_combo.currentText()

        self.port_combo.blockSignals(True)  # 阻止信号，避免触发配置变更
        self.port_combo.clear()
        if ports:
            self.port_combo.addItems(ports)
            if current in ports:
                self.port_combo.setCurrentText(current)
        else:
            self.port_combo.addItem('无可用串口')
        self.port_combo.blockSignals(False)

    def clear_module_logs(self):
        """清空模组响应日志缓存"""
        global module_log_cache, full_log_cache, log_cache

        if hasattr(self, 'clear_log_markers'):
            self.clear_log_markers()
        print(f'[调试] 清空前 - module_log_cache长度: {len(module_log_cache)}, full_log_cache长度: {len(full_log_cache)}, log_cache长度: {len(log_cache)}')

        # 清空显示窗口
        self.log_text.clear()
        print(f'[调试] 已清空模组日志显示窗口')

        # 清空所有日志缓存
        module_log_cache.clear()
        full_log_cache.clear()
        log_cache.clear()

        print(f'[调试] 清空后 - module_log_cache长度: {len(module_log_cache)}, full_log_cache长度: {len(full_log_cache)}, log_cache长度: {len(log_cache)}')

        # 更新日志行数显示为0
        self.log_count_label.setText('日志行数: 0')
        print(f'[调试] 已更新模组日志行数显示为0')

        print('[调试] 已清空所有日志缓存')

    def clear_module_response(self):
        """清空模组响应信息显示"""
        self.module_response_text.clear()
        print('[调试] 已清空模组响应信息显示')

    def _add_exclusive_mode_menu(self, title, items, checked_key):
        """创建带勾选状态的互斥模式子菜单。"""
        menu = self.module_modes_menu.addMenu(title)
        group = QActionGroup(self)
        group.setExclusive(True)
        actions = {}
        for key, text in items:
            action = menu.addAction(text)
            action.setCheckable(True)
            action.setData(key)
            group.addAction(action)
            actions[key] = action
        actions[checked_key].setChecked(True)
        group.triggered.connect(self.update_module_mode_summary)
        return menu, group, actions

    def setup_module_modes_menu(self):
        """构建模组模式菜单，作为模式状态的唯一UI来源。"""
        _, self.operation_mode_group, self.operation_mode_actions = \
            self._add_exclusive_mode_menu(
                '操作模式', (('face', '👤 人脸模式'), ('palm', '🖐️ 手掌模式')), 'face'
            )
        _, self.raw_mode_action_group, self.raw_mode_actions = \
            self._add_exclusive_mode_menu(
                'RAW 模式', (('Y+RGB', 'Y+RGB（40% + 60%）'),
                             ('Y+IR', 'Y+IR（50% + 50%）')), 'Y+IR'
            )
        _, self.project_mode_group, self.project_mode_actions = \
            self._add_exclusive_mode_menu(
                '项目模式', (('DSM', 'DSM'), ('KDS', 'KDS')), 'DSM'
            )
        _, self.stop_condition_group, self.stop_condition_actions = \
            self._add_exclusive_mode_menu(
                '停止条件', (('none', '不停止'), ('fail', '失败停止'),
                             ('success', '成功停止')), 'none'
            )
        self.raw_mode_action_group.triggered.connect(self.on_raw_mode_changed)

        self.module_modes_menu.addSeparator()
        repeat_action = QWidgetAction(self.module_modes_menu)
        repeat_widget = QWidget(self.module_modes_menu)
        repeat_layout = QHBoxLayout(repeat_widget)
        repeat_layout.setContentsMargins(10, 4, 10, 4)
        repeat_layout.addWidget(QLabel('重复次数:'))
        self.repeat_count_spin = QSpinBox(repeat_widget)
        self.repeat_count_spin.setRange(1, 1000)
        self.repeat_count_spin.setValue(1)
        self.repeat_count_spin.setFixedWidth(90)
        self.repeat_count_spin.setToolTip('注册/识别指令的重复执行次数')
        self.repeat_count_spin.valueChanged.connect(self.update_module_mode_summary)
        repeat_layout.addWidget(self.repeat_count_spin)
        repeat_action.setDefaultWidget(repeat_widget)
        self.module_modes_menu.addAction(repeat_action)
        self.update_module_mode_summary()

    @staticmethod
    def _checked_action_key(actions):
        return next(key for key, action in actions.items() if action.isChecked())

    def update_module_mode_summary(self, checked=None):
        """同步模式按钮摘要和提示。"""
        operation = '人脸' if self.operation_mode_actions['face'].isChecked() else '手掌'
        raw = self._checked_action_key(self.raw_mode_actions)
        project = self._checked_action_key(self.project_mode_actions)
        stop_names = {'none': '不停止', 'fail': '失败停止', 'success': '成功停止'}
        stop = stop_names[self._checked_action_key(self.stop_condition_actions)]
        summary = f'{operation} · {raw} · {project} · {stop} · {self.repeat_count_spin.value()}次'
        self.btn_module_modes.setToolTip(summary)
        self.btn_module_modes.setAccessibleDescription(summary)

    def on_raw_mode_changed(self, action=None):
        """RAW模式切换"""
        self.raw_mode = self._checked_action_key(self.raw_mode_actions)
        self.update_module_mode_summary()
        print(f'[调试] RAW模式切换为: {self.raw_mode}')

    def connect_serial(self):
        """连接/断开串口"""
        print('[调试] connect_serial 被调用')

        # 如果已经连接，则断开
        if self.serial_thread and self.serial_thread.is_alive():
            print('[调试] 串口已连接，执行断开操作')
            self.disconnect_serial()
            return

        # 验证参数
        port = self.port_combo.currentText()
        if not port or port == '无可用串口':
            print('[调试] 串口无效')
            QMessageBox.warning(self, '提示', '请选择有效的串口')
            return

        try:
            baudrate = int(self.baudrate_combo.currentText())
        except ValueError:
            print('[调试] 波特率无效')
            QMessageBox.warning(self, '提示', '波特率必须是数字')
            return

        print(f'[调试] 准备连接串口: {port} @ {baudrate}')

        # 保存配置
        config = load_config()
        config['port'] = port
        config['baudrate'] = baudrate
        save_config(config)

        self.port = port
        self.baudrate = baudrate

        # 更新按钮状态
        self.btn_connect.setText('🔌 连接中...')
        self.btn_connect.setEnabled(False)
        self.set_status('● 正在连接...', '#e08a00')

        print('[调试] 启动串口线程')
        # 启动串口线程
        self.start_serial()

        print('[调试] 启动定时器')
        # 启动定时器
        self.start_timers()

        print('[调试] 启动连接超时检测（5秒）')
        # 启动连接超时检测（5秒超时）
        self.connection_timeout_timer = QTimer()
        self.connection_timeout_timer.setSingleShot(True)
        self.connection_timeout_timer.timeout.connect(self.on_connection_timeout)
        self.connection_timeout_timer.start(5000)  # 5秒超时
        print(f'[调试] 超时计时器已启动: {self.connection_timeout_timer.isActive()}')

    def disconnect_serial(self):
        """断开串口连接"""
        print('[调试] disconnect_serial 被调用')

        try:
            # 停止定时器
            if hasattr(self, 'log_timer'):
                self.log_timer.stop()
            if hasattr(self, 'error_timer'):
                self.error_timer.stop()
            if hasattr(self, 'connection_timeout_timer') and self.connection_timeout_timer:
                self.connection_timeout_timer.stop()

            # 请求线程停止并取消阻塞读取，再等待finally确认句柄已经关闭。
            control = getattr(self, 'serial_control', None)
            thread = self.serial_thread
            if control:
                control.request_stop()
            else:
                self.connected_event.clear()

            if thread and thread.is_alive():
                print('[调试] 等待串口线程释放句柄...')
                if control:
                    control.closed_event.wait(timeout=2.0)
                thread.join(timeout=0.2)

            release_failed = bool(thread and thread.is_alive()) or bool(
                control and control.close_error is not None
            )
            if release_failed:
                print('[调试] 串口线程未能释放句柄')
                self.btn_connect.setText('⌛ 正在释放...')
                self.btn_connect.setEnabled(True)
                self.set_status('● 正在释放串口', '#e08a00')
                QMessageBox.warning(
                    self, '断开未完成',
                    '日志串口仍在释放中，请稍后再试；当前不会允许模组占用该串口。'
                )
                return False

            self.serial_thread = None
            self.serial_control = None
            self.connected_event.clear()
            self.port = None
            self.baudrate = None

            # 句柄关闭后再清队列，避免工作线程在清理期间重新写入。
            for pending_queue in (self.log_queue, self.error_queue, self.send_queue):
                while True:
                    try:
                        pending_queue.get_nowait()
                    except queue.Empty:
                        break

            # 重置下载相关标志位
            self.end_download_performance('中止（断开连接）')
            self.is_downloading = False
            self.download_buffer = bytearray()
            self.download_offset = 0
            self.download_total_size = 0
            self.download_type = None

            # 更新UI
            self.btn_connect.setText('🔌 连接')
            self.btn_connect.setEnabled(True)
            self.btn_connect.setStyleSheet('QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 6px 12px; }')
            self.set_status('● 未连接', '#999999')
            self.port_combo.setEnabled(True)
            self.baudrate_combo.setEnabled(True)

            # 添加断开日志
            self.append_log('[系统] 已断开串口连接')
            print('[调试] 串口已断开')
            return True

        except Exception as e:
            print(f'[调试] 断开串口时出错: {e}')
            QMessageBox.warning(self, '断开失败', f'断开串口时出错：\n{str(e)}')
            return False


    def on_type_changed(self, index=None):
        """类型切换联动"""
        is_verify = self.type_combo.currentText() == '识别'

        # 识别场景和识别亮度只在"识别"模式下启用
        self.rec_scene_label.setEnabled(is_verify)
        self.recognize_scene_combo.setEnabled(is_verify)
        self.rec_luma_label.setEnabled(is_verify)
        self.recognize_luma_combo.setEnabled(is_verify)

        # 结果在注册和识别模式下都启用（注册也可能成功或失败）
        self.result_label.setEnabled(True)
        self.result_combo.setEnabled(True)

    def open_settings_dialog(self):
        """打开设置对话框"""
        dialog = SettingsDialog(self)

        # 加载当前配置
        config = load_config()

        # 加载上位机配置（历史记录）
        download_history = config.get('download_dir_history', [])
        dialog.download_combo.clear()
        dialog.download_combo.addItems(download_history)
        dialog.download_combo.setCurrentText(config.get('download_dir', ''))

        program_history = config.get('host_program_history', [])
        dialog.program_combo.clear()
        dialog.program_combo.addItems(program_history)
        dialog.program_combo.setCurrentText(config.get('host_program', ''))

        # 加载测试信息（历史记录）
        output_history = config.get('output_history', [])
        dialog.output_combo.clear()
        dialog.output_combo.addItems(output_history)
        dialog.output_combo.setCurrentText(config.get('output', ''))

        dialog.version_entry.setText(config.get('test_version', ''))

        if dialog.exec() == QDialog.Accepted:
            print('[调试-设置] 用户点击了应用按钮')

            # 应用新配置（保留当前的串口配置）
            new_config = load_config()
            new_config['download_dir'] = dialog.download_combo.currentText()
            new_config['output'] = dialog.output_combo.currentText()
            new_config['test_version'] = dialog.version_entry.text()
            new_config['host_program'] = dialog.program_combo.currentText()

            print(f'[调试-设置] 新配置 - 上位机程序: {new_config["host_program"]}')
            print(f'[调试-设置] 新配置 - 图片目录: {new_config["download_dir"]}')
            print(f'[调试-设置] 新配置 - 保存目录: {new_config["output"]}')
            print(f'[调试-设置] 新配置 - 测试版本: {new_config["test_version"]}')

            save_config(new_config)

            # 更新主窗口的配置
            self.download_dir = new_config['download_dir']
            self.output_base = new_config['output']
            self.test_version = new_config['test_version']
            self.host_program = new_config['host_program']

            print(f'[调试-设置] 当前 host_program 值: {self.host_program}')

            # 计算实际输出路径
            if self.test_version:
                self.output = os.path.join(self.output_base, self.test_version)
            else:
                self.output = self.output_base

            os.makedirs(self.output, exist_ok=True)

            # 如果监控已启用，重启监控
            if self.monitoring_enabled:
                print('[调试-设置] 监控已启用，重启监控')
                if self.observer:
                    self.observer.stop()
                    self.observer.join()
                self.start_observer()
                self.monitor_status_label.setText(f'图片监控：{os.path.basename(self.download_dir)}')

            # 每次应用都启动上位机程序（如果配置了）
            if self.host_program:
                print('[调试-设置] 启动上位机程序')
                self.launch_host_program()
            else:
                print('[调试-设置] 上位机程序路径为空，跳过启动')

            QMessageBox.information(self, '设置已应用', '配置已成功应用！')

    def on_strategy_changed(self, index):
        """日志策略改变"""
        self.lines_spin.setVisible(index == 1)  # 最近N行
        self.keyword_entry.setVisible(index == 2)  # 从关键词开始

    def toggle_save_panel(self):
        """折叠/展开保存控制面板"""
        expanded = self.save_content_widget.isHidden()
        self.save_content_widget.setVisible(expanded)
        self.btn_toggle_save.setText('▼ 💾 保存控制' if expanded else '▶ 💾 保存控制')

    def toggle_sequence_panel(self):
        """折叠/展开自动执行序列面板"""
        expanded = self.sequence_content_widget.isHidden()
        self.sequence_content_widget.setVisible(expanded)
        self.btn_toggle_sequence.setText('▼ 🔄 自动执行序列' if expanded else '▶ 🔄 自动执行序列')

    def check_repeat_next(self, last_success=None):
        """检查是否需要继续重复执行下一次

        Args:
            last_success: 上次操作是否成功。True=成功, False=失败, None=未知
        """
        if not self.repeat_mode:
            return

        # 防止同一次操作多次Reply触发
        if self.repeat_reply_received:
            return

        # 标记当前操作已收到Reply
        self.repeat_reply_received = True

        # 检查是否需要根据成功/失败条件停止
        should_stop = False
        stop_reason = ''

        if last_success is True and self.stop_condition_actions['success'].isChecked():
            should_stop = True
            stop_reason = '检测到成功，触发成功停止'
        elif last_success is False and self.stop_condition_actions['fail'].isChecked():
            should_stop = True
            stop_reason = '检测到失败，触发失败停止'

        if should_stop:
            # 提前停止，计算成功率
            success_rate = (self.repeat_success_count / self.repeat_current * 100) if self.repeat_current > 0 else 0
            self.append_module_log(
                f'[重复执行] {stop_reason}，已执行 {self.repeat_current}/{self.repeat_total} 次，'
                f'成功 {self.repeat_success_count} 次，'
                f'成功率 {success_rate:.2f}%',
                success=(last_success is True)
            )
            # 重置重复模式
            self.repeat_mode = False
            self.repeat_current = 0
            self.repeat_total = 1
            self.repeat_command = None
            self.repeat_success_count = 0
            self.repeat_reply_received = False
            return

        # 检查是否还有剩余次数
        if self.repeat_current < self.repeat_total:
            # 继续执行下一次
            if self.repeat_command:
                self.repeat_command()
        else:
            # 全部完成，计算成功率
            success_rate = (self.repeat_success_count / self.repeat_total * 100) if self.repeat_total > 0 else 0
            self.append_module_log(
                f'[重复执行] 已完成全部 {self.repeat_total} 次，'
                f'成功 {self.repeat_success_count} 次，'
                f'成功率 {success_rate:.2f}%',
                success=True
            )
            # 重置重复模式
            self.repeat_mode = False
            self.repeat_current = 0
            self.repeat_total = 1
            self.repeat_command = None
            self.repeat_success_count = 0
            self.repeat_reply_received = False

    def toggle_command_panel(self):
        """折叠/展开更多指令面板"""
        if self.more_commands_widget.isVisible():
            # 折叠
            self.more_commands_widget.setVisible(False)
            self.btn_toggle_commands.setText('▼ 更多指令')
        else:
            # 展开
            self.more_commands_widget.setVisible(True)
            self.btn_toggle_commands.setText('▲ 收起指令')

    def toggle_module_group(self, checked):
        """折叠/展开模组控制区域"""
        if self.module_content_widget.isVisible():
            # 折叠
            self.module_content_widget.setVisible(False)
            self.btn_toggle_module.setText('▶ 🔧 模组控制')
        else:
            # 展开
            self.module_content_widget.setVisible(True)
            self.btn_toggle_module.setText('▼ 🔧 模组控制')

    def toggle_send_group(self):
        """折叠/展开快速发送区域"""
        if self.send_content_widget.isVisible():
            # 折叠
            self.send_content_widget.setVisible(False)
            self.btn_toggle_send.setText('▶ 📤 快速发送')
        else:
            # 展开
            self.send_content_widget.setVisible(True)
            self.btn_toggle_send.setText('▼ 📤 快速发送')

    def load_quick_commands(self):
        """加载快捷命令"""
        try:
            config = load_config()
            self.quick_commands = config.get('quick_commands', [])
            self.refresh_quick_command_buttons()
        except Exception as e:
            print(f'[调试] 加载快捷命令失败: {e}')
            self.quick_commands = []

    def save_quick_commands(self):
        """保存快捷命令"""
        try:
            config = load_config()
            config['quick_commands'] = self.quick_commands
            save_config(config)
        except Exception as e:
            print(f'[调试] 保存快捷命令失败: {e}')

    def add_quick_command(self):
        """添加快捷命令"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox

        dialog = QDialog(self)
        dialog.setWindowTitle('添加快捷命令')
        dialog.setMinimumWidth(400)

        layout = QVBoxLayout(dialog)

        # 名称输入
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel('按钮名称:'))
        name_input = QLineEdit()
        name_input.setPlaceholderText('例如: 复位、查询状态')
        name_layout.addWidget(name_input)
        layout.addLayout(name_layout)

        # 命令输入
        cmd_layout = QHBoxLayout()
        cmd_layout.addWidget(QLabel('发送内容:'))
        cmd_input = QLineEdit()
        cmd_input.setPlaceholderText('例如: reset 或 AA 55 01 02')
        cmd_layout.addWidget(cmd_input)
        layout.addLayout(cmd_layout)

        # 类型选择
        type_layout = QHBoxLayout()
        type_layout.addWidget(QLabel('发送类型:'))
        type_combo = QComboBox()
        type_combo.addItems(['文本', 'HEX', 'BIN'])
        type_layout.addWidget(type_combo)
        type_layout.addStretch()
        layout.addLayout(type_layout)

        # 按钮
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        btn_cancel = QPushButton('取消')
        btn_cancel.clicked.connect(dialog.reject)
        button_layout.addWidget(btn_cancel)

        btn_save = QPushButton('保存')
        btn_save.setStyleSheet('QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 6px 16px; }')
        btn_save.clicked.connect(dialog.accept)
        button_layout.addWidget(btn_save)

        layout.addLayout(button_layout)

        if dialog.exec() == QDialog.Accepted:
            name = name_input.text().strip()
            command = cmd_input.text().strip()
            cmd_type = type_combo.currentText()

            if not name or not command:
                QMessageBox.warning(self, '输入错误', '请输入按钮名称和发送内容！')
                return

            # 检查是否已存在同名命令
            for cmd in self.quick_commands:
                if cmd['name'] == name:
                    reply = QMessageBox.question(
                        self,
                        '重复的命令名称',
                        f'快捷命令 "{name}" 已存在，是否覆盖？',
                        QMessageBox.Yes | QMessageBox.No
                    )
                    if reply == QMessageBox.Yes:
                        cmd['command'] = command
                        cmd['type'] = cmd_type
                        self.save_quick_commands()
                        self.refresh_quick_command_buttons()
                    return

            # 添加新命令
            self.quick_commands.append({
                'name': name,
                'command': command,
                'type': cmd_type
            })
            self.save_quick_commands()
            self.refresh_quick_command_buttons()

    def refresh_quick_command_buttons(self):
        """刷新快捷命令按钮"""
        # 清除现有按钮
        while self.shortcut_commands_layout.count():
            item = self.shortcut_commands_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 添加快捷命令按钮
        for cmd in self.quick_commands:
            btn = QPushButton(f'⚡ {cmd["name"]}')
            btn.setStyleSheet('QPushButton { background-color: #2196F3; color: white; padding: 4px 10px; border-radius: 3px; }')
            btn.clicked.connect(lambda checked, c=cmd: self.execute_quick_command(c))

            # 右键菜单：删除
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(lambda pos, button=btn, command=cmd: self.show_quick_command_menu(button, command))

            self.shortcut_commands_layout.addWidget(btn)

        # 添加弹性空间
        self.shortcut_commands_layout.addStretch()

    def show_quick_command_menu(self, button, command):
        """显示快捷命令右键菜单"""
        menu = QMenu(self)
        delete_action = menu.addAction('🗑️ 删除')
        action = menu.exec_(button.mapToGlobal(button.rect().center()))

        if action == delete_action:
            reply = QMessageBox.question(
                self,
                '确认删除',
                f'确定要删除快捷命令 "{command["name"]}" 吗？',
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.quick_commands.remove(command)
                self.save_quick_commands()
                self.refresh_quick_command_buttons()

    def execute_quick_command(self, command):
        """执行快捷命令"""
        if not self.connected_event.is_set():
            QMessageBox.warning(self, '提示', '串口未连接')
            return

        # 设置输入框内容
        self.send_entry.setText(command['command'])

        # 设置发送类型
        if command['type'] == '文本':
            self.rb_text.setChecked(True)
        elif command['type'] == 'HEX':
            self.rb_hex.setChecked(True)
        elif command['type'] == 'BIN':
            self.rb_binary.setChecked(True)

        # 执行发送
        self.quick_send()

    def open_custom_command_dialog(self):
        """打开自定义命令发送窗口"""
        print('[调试-打开对话框] open_custom_command_dialog 被调用')
        print(f'[调试-打开对话框] module_connected: {self.module_connected}')

        if not self.module_connected:
            QMessageBox.warning(self, '未连接', '请先连接模组！')
            return

        # 创建或显示对话框
        print(f'[调试-打开对话框] custom_command_dialog is None: {self.custom_command_dialog is None}')
        if self.custom_command_dialog is None:
            print('[调试-打开对话框] 正在创建新的对话框')
            self.custom_command_dialog = CustomCommandDialog(self)
            # 连接信号
            self.custom_command_data_signal.connect(self.custom_command_dialog.receive_data)
            print(f'[调试-打开对话框] 对话框创建完成并已连接信号: {self.custom_command_dialog}')

        print('[调试-打开对话框] 正在显示对话框')
        self.custom_command_dialog.show()
        self.custom_command_dialog.raise_()
        self.custom_command_dialog.activateWindow()
        print('[调试-打开对话框] 对话框已显示')

    def on_response_text_clicked(self, event):
        """响应信息区域点击事件，同步到串口日志"""
        from PySide6.QtWidgets import QTextEdit
        from PySide6.QtGui import QTextCursor
        from datetime import datetime
        import re

        # 获取点击位置的光标（使用position()替代deprecated的pos()）
        cursor = self.module_response_text.cursorForPosition(event.position().toPoint())
        cursor.select(QTextCursor.LineUnderCursor)
        line_text = cursor.selectedText()

        print(f'[调试] 点击的响应行: {line_text[:100]}...')

        # 解析时间戳 [HH:MM:SS.mmm]
        time_pattern = r'\[(\d{2}:\d{2}:\d{2}\.\d{3})\]'
        match = re.search(time_pattern, line_text)

        if match:
            timestamp_str = match.group(1)
            print(f'[调试] 解析到时间戳: {timestamp_str}')
            try:
                # 解析时间戳
                response_time = datetime.strptime(timestamp_str, '%H:%M:%S.%f')

                # 在串口日志中查找最接近的时间戳
                self.find_and_highlight_log(response_time)
            except Exception as e:
                print(f'解析时间戳失败: {e}')
        else:
            print(f'[调试] 该行没有找到时间戳格式 [HH:MM:SS.mmm]')

        # 调用原始的鼠标按下事件
        QTextEdit.mousePressEvent(self.module_response_text, event)

    def find_and_highlight_log(self, target_time):
        """在串口日志中查找并高亮最接近的时间戳行"""
        from datetime import datetime
        from PySide6.QtGui import QTextCursor, QTextCharFormat, QColor
        import re

        # 获取串口日志的所有文本
        log_text = self.log_text.toPlainText()
        lines = log_text.split('\n')

        # 查找最接近的时间戳
        closest_line_index = -1
        min_time_diff = float('inf')
        closest_timestamp = None
        closest_line_text = None

        # 匹配时间戳格式：可能是 [HH:MM:SS.mmm] 或者日志中包含日期的格式
        # 尝试提取第二个时间戳字段（如果日志格式是：日期 时间戳 内容）
        time_pattern = r'(\d{2}:\d{2}:\d{2}\.\d{3})'

        print(f'[调试] 目标时间: {target_time.strftime("%H:%M:%S.%f")[:-3]}')
        print(f'[调试] 总共 {len(lines)} 行日志')

        for i, line in enumerate(lines):
            # 查找所有时间戳
            matches = re.findall(time_pattern, line)

            # 如果有多个时间戳，尝试使用第二个（可能是实际的时分秒）
            # 如果只有一个，就使用第一个
            if matches:
                timestamp_str = matches[-1] if len(matches) > 1 else matches[0]
                try:
                    log_time = datetime.strptime(timestamp_str, '%H:%M:%S.%f')
                    # 计算时间差（秒）
                    time_diff = abs((log_time.hour * 3600 + log_time.minute * 60 + log_time.second + log_time.microsecond / 1000000) -
                                   (target_time.hour * 3600 + target_time.minute * 60 + target_time.second + target_time.microsecond / 1000000))

                    if time_diff < min_time_diff:
                        min_time_diff = time_diff
                        closest_line_index = i
                        closest_timestamp = timestamp_str
                        closest_line_text = line
                except:
                    continue

        # 调试信息
        if closest_line_index >= 0:
            print(f'[调试] 找到最接近的日志: 行{closest_line_index}, 时间戳={closest_timestamp}, 时间差={min_time_diff:.3f}秒')
            print(f'[调试] 日志内容: {closest_line_text[:100]}...')
        else:
            print('[调试] 未找到匹配的日志行')
            return

        # 高亮显示找到的行 - 使用文本块定位
        if closest_line_index >= 0:
            # 获取文档
            document = self.log_text.document()

            # 使用QTextDocument的findBlockByLineNumber获取准确的文本块
            block = document.findBlockByLineNumber(closest_line_index)

            if block.isValid():
                print(f'[调试] 找到的文本块内容: {block.text()[:100]}...')

                # 创建光标并定位到该块
                cursor = QTextCursor(block)

                # 选中整个块
                cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)

                # 应用橙色高亮
                highlight_format = QTextCharFormat()
                highlight_format.setBackground(QColor('#FF6B35'))  # 橙红色背景
                highlight_format.setForeground(QColor('#FFFFFF'))  # 白色文字
                cursor.mergeCharFormat(highlight_format)

                # 移动视图到该位置
                self.log_text.setTextCursor(cursor)
                self.log_text.ensureCursorVisible()
            else:
                print(f'[调试] 文本块无效，行索引={closest_line_index}')

    def toggle_monitoring(self):
        """切换图片监控"""
        if self.monitoring_enabled:
            # 停止监控
            if self.observer:
                self.observer.stop()
                self.observer.join()
                self.observer = None

            self.monitoring_enabled = False
            self.action_monitor.setText('👁️ 启用图片监控')
            self.monitor_status_label.setText('图片监控：未启用')
            self.monitor_status_label.setStyleSheet('font-size: 9pt; color: #666666;')
        else:
            # 加载配置
            config = load_config()

            # 验证参数
            download_dir = config.get('download_dir', '')
            output_dir = config.get('output', '')
            test_version = config.get('test_version', '')

            if not download_dir:
                QMessageBox.warning(self, '提示', '请先在设置中配置上位机图片目录')
                self.open_settings_dialog()
                return

            if not output_dir:
                QMessageBox.warning(self, '提示', '请先在设置中配置保存目录')
                self.open_settings_dialog()
                return

            if not test_version:
                QMessageBox.warning(self, '提示', '请先在设置中配置测试版本')
                self.open_settings_dialog()
                return

            # 保存配置
            self.download_dir = download_dir
            self.output_base = output_dir
            self.test_version = test_version
            self.host_program = config.get('host_program', '')

            if self.test_version:
                self.output = os.path.join(self.output_base, self.test_version)
            else:
                self.output = self.output_base

            os.makedirs(self.output, exist_ok=True)

            # 启动监控
            self.start_observer()

            self.monitoring_enabled = True
            self.action_monitor.setText('👁️ 停止图片监控')
            self.monitor_status_label.setText(f'图片监控：{os.path.basename(download_dir)}')
            self.monitor_status_label.setStyleSheet('font-size: 9pt; color: #2e8b57;')

            # 启动上位机程序
            self.launch_host_program()

    def marker_by_id(self, marker_id):
        return next((marker for marker in self.log_markers
                     if marker.marker_id == marker_id), None)

    def show_log_marker_window(self, selected_marker_id=None):
        """显示非模态打点窗口，首次停靠在日志区右上角。"""
        first_show = self.log_marker_window is None
        if first_show:
            self.log_marker_window = LogMarkerWindow(self)
        self.log_marker_window.refresh(selected_marker_id)
        if first_show:
            top_right = self.log_text.mapToGlobal(self.log_text.rect().topRight())
            x = max(0, top_right.x() - self.log_marker_window.width())
            self.log_marker_window.move(x, top_right.y())
        self.log_marker_window.show()
        self.log_marker_window.raise_()
        self.log_marker_window.activateWindow()

    def add_log_marker(self, log_index):
        """为完整日志绝对索引创建打点。"""
        with log_cache_lock:
            if not full_log_cache:
                QMessageBox.information(self, '提示', '当前还没有产生任何日志')
                return None
            log_index = max(0, min(int(log_index), len(full_log_cache) - 1))
            log_count = len(full_log_cache)
            line_text = full_log_cache[log_index]
        marker_id = self.next_log_marker_id
        marker = LogMarker(
            marker_id=marker_id,
            name=f'记录{marker_id}',
            created_at=datetime.now(),
            log_index=log_index,
            log_count=log_count,
            line_text=line_text,
        )
        self.next_log_marker_id += 1
        self.log_markers.append(marker)
        self.show_log_marker_window(marker.marker_id)
        return marker

    def add_current_log_marker(self):
        """绑定点击瞬间最新采集到完整缓存的日志行。"""
        with log_cache_lock:
            index = len(full_log_cache) - 1
        return self.add_log_marker(index)

    def visible_log_block_count(self):
        """返回文档内真实日志块数量，排除末尾空块。"""
        document = self.log_text.document()
        count = document.blockCount()
        last = document.lastBlock()
        if last.isValid() and not last.text():
            count -= 1
        return max(0, count)

    def log_index_at_position(self, position):
        """把日志控件坐标映射到完整缓存绝对索引。"""
        block_count = self.visible_log_block_count()
        if block_count <= 0:
            return None
        cursor = self.log_text.cursorForPosition(position)
        block_number = min(cursor.blockNumber(), block_count - 1)
        block = self.log_text.document().findBlockByNumber(block_number)
        data = block.userData() if block.isValid() else None
        if isinstance(data, LogBlockData) and data.log_index is not None:
            return data.log_index if 0 <= data.log_index < len(full_log_cache) else None
        return None

    def show_log_context_menu(self, position):
        menu = self.log_text.createStandardContextMenu()
        menu.addSeparator()
        marker_action = menu.addAction('📍 在此行打点')
        index = self.log_index_at_position(position)
        marker_action.setEnabled(index is not None)
        if index is not None:
            marker_action.triggered.connect(
                lambda checked=False, line_index=index: self.add_log_marker(line_index)
            )
        menu.exec(self.log_text.mapToGlobal(position))

    def delete_log_marker(self, marker_id):
        marker = self.marker_by_id(marker_id)
        if marker is None:
            return
        self.log_markers.remove(marker)
        if self.log_history_marker_id == marker_id:
            self.return_to_live_logs()
        if self.log_marker_window:
            self.log_marker_window.refresh()

    def clear_log_markers(self):
        self.log_markers.clear()
        self.next_log_marker_id = 1
        self.log_history_marker_id = None
        self.log_history_start = None
        self.log_history_target_row = None
        self.log_marker_selection = None
        if hasattr(self, 'log_history_bar'):
            self.log_history_bar.setVisible(False)
        if self.log_marker_window:
            self.log_marker_window.refresh()

    def show_marker_context(self, marker):
        """主日志切换到打点前后各100行，并高亮目标行。"""
        with log_cache_lock:
            logs_snapshot = list(full_log_cache)
        if not logs_snapshot or marker.log_index >= len(logs_snapshot):
            QMessageBox.warning(self, '提示', '该打点对应的日志已经不可用')
            return
        start = max(0, marker.log_index - 100)
        end = min(len(logs_snapshot), marker.log_index + 101)
        self.log_history_marker_id = marker.marker_id
        self.log_history_start = start
        self.log_history_target_row = marker.log_index - start
        self.log_history_label.setText(
            f'正在查看第 {marker.line_number} 行附近的历史日志（{marker.name}）'
        )
        self.log_history_bar.setVisible(True)
        self.render_log_snapshot(logs_snapshot[start:end], self.log_history_target_row,
                                 start_index=start)
        if self.log_marker_window:
            self.log_marker_window.update_history_state()

    def render_log_snapshot(self, lines, target_row=None, start_index=None):
        """重绘一个日志快照，必要时高亮目标整行。"""
        if start_index is not None:
            lines = [QueuedLog(start_index + offset, text)
                     for offset, text in enumerate(lines)]
        self.log_text.clear()
        self.append_logs(lines, scroll_to_end=target_row is None)
        self.log_marker_selection = None
        if target_row is not None:
            block = self.log_text.document().findBlockByNumber(target_row)
            if block.isValid():
                selection = QTextEdit.ExtraSelection()
                selection.cursor = QTextCursor(block)
                selection.cursor.movePosition(QTextCursor.EndOfBlock,
                                              QTextCursor.KeepAnchor)
                selection.format.setBackground(QColor('#FF8C42'))
                selection.format.setForeground(QColor('#FFFFFF'))
                self.log_marker_selection = selection
                self.log_text.setTextCursor(selection.cursor)
                self.log_text.ensureCursorVisible()
        self.apply_log_extra_selections()

    def apply_log_extra_selections(self, search_selections=None):
        """合并搜索与打点高亮，避免两者互相清除。"""
        if search_selections is not None:
            self.search_extra_selections = search_selections
        selections = list(getattr(self, 'search_extra_selections', []))
        if self.log_marker_selection is not None:
            selections.append(self.log_marker_selection)
        self.log_text.setExtraSelections(selections)

    def return_to_live_logs(self):
        """退出历史上下文并恢复完整缓存尾部的实时视图。"""
        self.log_history_marker_id = None
        self.log_history_start = None
        self.log_history_target_row = None
        self.log_marker_selection = None
        self.log_history_bar.setVisible(False)
        with log_cache_lock:
            total_count = len(full_log_cache)
            logs_snapshot = list(full_log_cache[-LOG_DISPLAY_MAX_LINES:])
            while not self.log_queue.empty():
                try:
                    self.log_queue.get_nowait()
                except queue.Empty:
                    break
        start_index = max(0, total_count - len(logs_snapshot))
        self.render_log_snapshot(logs_snapshot, start_index=start_index)
        if self.log_marker_window:
            self.log_marker_window.update_history_state()

    def start_serial(self):
        """启动串口线程"""
        self.connected_event.clear()
        self.log_connection_notified = False
        self.serial_control = SerialReaderControl()
        self.serial_thread = threading.Thread(
            target=serial_reader,
            args=(self.port, self.baudrate, self.error_queue, self.connected_event,
                  self.log_queue, self.send_queue, self.serial_control),
            daemon=True
        )
        self.serial_thread.start()

    def start_observer(self):
        """启动文件夹监听"""
        self.observer = Observer()
        handler = DownloadFolderHandler(self)
        self.observer.schedule(handler, self.download_dir, recursive=False)
        self.observer.start()

    def start_timers(self):
        """启动定时器"""
        self.log_timer = QTimer()
        self.log_timer.timeout.connect(self.poll_log_queue)
        self.log_timer.start(LOG_POLL_INTERVAL_MS)

        self.error_timer = QTimer()
        self.error_timer.timeout.connect(self.poll_error_queue)
        self.error_timer.start(500)

    def get_log_formats(self):
        """仅在主题变化时重建格式，避免逐行构造颜色和格式对象。"""
        if getattr(self, '_log_format_theme', None) != self.dark_mode:
            colors = LOG_HIGHLIGHT_COLORS[self.dark_mode]
            formats = {}
            for name, color in colors.items():
                if name == 'background':
                    continue
                fmt = QTextCharFormat()
                fmt.setBackground(QColor(colors['background']))
                fmt.setForeground(QColor(color))
                formats[name] = fmt
            self._log_formats = formats
            self._log_format_theme = self.dark_mode
        return self._log_formats

    def insert_log_line(self, cursor, text, formats, log_index=None):
        """使用批次共享的光标插入一行；保持原有高亮重叠规则。"""
        block = cursor.block()
        block.setUserData(LogBlockData(log_index))
        try:
            matches = []
            for color_type, pattern, priority in LOG_HIGHLIGHT_RULES:
                for match in pattern.finditer(text):
                    matches.append((match.start(), match.end(), color_type, priority))
            matches.sort(key=lambda item: item[0])

            filtered_matches = []
            last_end = 0
            for start, end, color_type, priority in matches:
                if start >= last_end:
                    filtered_matches.append((start, end, color_type, priority))
                    last_end = end
                elif (filtered_matches[-1][0] == start
                      and priority > filtered_matches[-1][3]):
                    filtered_matches[-1] = (start, end, color_type, priority)
                    last_end = end
        except Exception as e:
            # 在插入前完成解析，失败回退时不会重复已经插入的文本。
            print(f'[错误] 日志着色失败: {e}，使用简单模式')
            cursor.insertText(text + '\n', formats['text'])
            return

        pos = 0
        for start, end, color_type, _ in filtered_matches:
            if pos < start:
                cursor.insertText(text[pos:start], formats['text'])
            cursor.insertText(text[start:end], formats[color_type])
            pos = end
        cursor.insertText(text[pos:] + '\n', formats['text'])

    def append_logs(self, lines, scroll_to_end=True):
        """批量插入；迭代器可在每行渲染后检查预算，未消费的日志留在队列。"""
        lines = iter(lines)
        try:
            first_line = next(lines)
        except StopIteration:
            return

        formats = self.get_log_formats()
        cursor = QTextCursor(self.log_text.document())
        cursor.movePosition(QTextCursor.End)
        updates_enabled = self.log_text.updatesEnabled()
        self.log_text.setUpdatesEnabled(False)
        cursor.beginEditBlock()
        try:
            first_text = first_line.text if isinstance(first_line, QueuedLog) else first_line
            first_index = first_line.log_index if isinstance(first_line, QueuedLog) else None
            self.insert_log_line(cursor, first_text, formats, first_index)
            for line in lines:
                text = line.text if isinstance(line, QueuedLog) else line
                log_index = line.log_index if isinstance(line, QueuedLog) else None
                self.insert_log_line(cursor, text, formats, log_index)
        finally:
            try:
                cursor.endEditBlock()
                self.log_count_label.setText(f'日志行数: {len(full_log_cache)}')
                if scroll_to_end:
                    self.log_text.moveCursor(QTextCursor.End)
            finally:
                self.log_text.setUpdatesEnabled(updates_enabled)

    def append_log(self, text):
        """单条入口供系统提示和信号调用，复用批量渲染。"""
        self.append_logs((text,))

    def set_status(self, text, color):
        """保存日志串口状态；紧凑布局中通过连接按钮提示而不另占一行。"""
        self.log_connection_status = text
        if hasattr(self, 'btn_connect'):
            self.btn_connect.setToolTip(f'日志串口状态：{text.lstrip("● ")}')
            self.btn_connect.setAccessibleDescription(text)

    def poll_log_queue(self):
        """独立检查串口连接状态，再处理日志；无日志也能完成连接。"""
        if self.connected_event.is_set() and not self.log_connection_notified:
            self.log_connection_notified = True
            self.connected_signal.emit()

        # 历史上下文保持稳定；实时数据仍已进入完整缓存和原队列。
        if getattr(self, 'log_history_marker_id', None) is not None:
            return

        def pending_lines():
            started = time.perf_counter()
            for index in range(LOG_BATCH_MAX_LINES):
                # yield返回后已完成该行渲染，预算包含解析/插入耗时。
                if index and time.perf_counter() - started >= LOG_BATCH_BUDGET_SECONDS:
                    break
                try:
                    yield self.log_queue.get_nowait()
                except queue.Empty:
                    break

        self.append_logs(pending_lines())

    def poll_error_queue(self):
        """轮询错误队列"""
        try:
            msg = self.error_queue.get_nowait()
            self.error_signal.emit(msg)
        except queue.Empty:
            pass

    def on_connection_timeout(self):
        """连接超时处理"""
        print('[调试] on_connection_timeout 被调用')
        print(f'[调试] connected_event 状态: {self.connected_event.is_set()}')

        # 如果还没有连接成功
        if not self.connected_event.is_set():
            print('[调试] 连接超时，显示超时提示')
            self.set_status('● 连接超时', '#c0392b')
            self.append_log(f'[系统] 串口连接超时：{self.port}')

            # 恢复按钮和配置
            self.btn_connect.setText('🔌 连接')
            self.btn_connect.setEnabled(True)
            self.btn_connect.setStyleSheet('QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 6px 12px; }')
            self.port_combo.setEnabled(True)
            self.baudrate_combo.setEnabled(True)

            reply = QMessageBox.question(
                self,
                '连接超时',
                f'串口 {self.port} 连接超时\n\n可能原因：\n- 串口不存在或被占用\n- 串口权限不足\n- 串口线缆未连接\n\n是否重试连接？',
                QMessageBox.Yes | QMessageBox.No
            )

            if reply == QMessageBox.Yes:
                print('[调试] 用户选择重试连接')
                self.connect_serial()
            else:
                print('[调试] 用户取消重试')
        else:
            print('[调试] 连接已成功，忽略超时')

    def handle_connected(self):
        """处理连接成功"""
        print('[调试] handle_connected 被调用')

        # 停止超时计时器
        if self.connection_timeout_timer and self.connection_timeout_timer.isActive():
            print('[调试] 停止超时计时器')
            self.connection_timeout_timer.stop()
        else:
            print(f'[调试] 超时计时器状态: {self.connection_timeout_timer.isActive() if self.connection_timeout_timer else "不存在"}')

        self.set_status('● 已连接', '#2e8b57')
        self.btn_connect.setText('🔌 断开连接')
        self.btn_connect.setEnabled(True)
        self.btn_connect.setStyleSheet('QPushButton { background-color: #e74c3c; color: white; font-weight: bold; padding: 6px 12px; }')
        # 连接成功后禁用串口和波特率选择
        self.port_combo.setEnabled(False)
        self.baudrate_combo.setEnabled(False)
        print('[调试] 连接成功处理完成')

    def handle_error(self, msg):
        """处理错误"""
        if self.connection_timeout_timer:
            self.connection_timeout_timer.stop()
        self.connected_event.clear()
        self.set_status('● 已断开', '#c0392b')
        self.append_log(f'[系统] {msg}')

        # 恢复按钮和配置
        self.btn_connect.setText('🔌 连接')
        self.btn_connect.setEnabled(True)
        self.btn_connect.setStyleSheet('QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 6px 12px; }')
        self.port_combo.setEnabled(True)
        self.baudrate_combo.setEnabled(True)

        reply = QMessageBox.question(
            self,
            '串口已断开',
            msg + '\n\n是否重试连接？',
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            self.connect_serial()

    def handle_new_image(self, folder_path):
        """处理新图片"""
        # 创建新的图片数据
        new_image_data = ImageData(folder_path)

        # 添加到历史记录
        self.image_history.insert(0, new_image_data)  # 插入到最前面

        # 限制历史记录数量
        if len(self.image_history) > self.max_history:
            self.image_history = self.image_history[:self.max_history]

        # 更新历史下拉框
        self.update_history_combo()

        # 设置为当前图片
        self.current_image_data = new_image_data

        # 自动选择"当前图片"
        self.history_combo.blockSignals(True)
        self.history_combo.setCurrentIndex(0)
        self.history_combo.blockSignals(False)

        # 显示图片
        self.display_images(folder_path)

        # 启用保存按钮
        self.btn_save_current.setEnabled(True)
        self.btn_skip.setEnabled(True)

        # 更新信息
        self.image_info_label.setText(f'路径: {folder_path}\n检测时间: {self.current_image_data.timestamp.strftime("%Y-%m-%d %H:%M:%S")}')

    def display_images(self, folder_path):
        """显示图片（水平排列）"""
        # 查找图片文件
        image_files = []
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                if file.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif')):
                    image_files.append(os.path.join(root, file))

        self.display_image_previews(image_files[:10])

    def display_image_previews(self, image_files):
        """统一缩略图和双击入口，每次查看器使用当前组的图片快照"""
        while self.image_layout.count():
            item = self.image_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not image_files:
            label = QLabel('该文件夹中没有找到图片文件')
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet('color: #999999; padding: 20px;')
            self.image_layout.addWidget(label, 1)
            return

        preview_paths = []
        # 保留实际显示的图片及顺序，排除无法加载的文件
        valid_images = []
        for img_path in image_files:
            try:
                pixmap = QPixmap(img_path)
                if not pixmap.isNull():
                    valid_images.append((img_path, pixmap))
            except Exception as e:
                print(f'加载图片失败: {img_path}, {e}')

        if not valid_images:
            label = QLabel('该文件夹中没有找到可显示的图片文件')
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet('color: #999999; padding: 20px;')
            self.image_layout.addWidget(label, 1)
            return

        max_columns = min(len(valid_images), 2)
        preview_container = QWidget()
        preview_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        preview_layout = QHBoxLayout(preview_container)
        preview_layout.setContentsMargins(4, 4, 4, 4)
        preview_layout.setSpacing(12)
        preview_layout.setAlignment(Qt.AlignCenter)
        for image_index, (img_path, pixmap) in enumerate(valid_images[:10]):
            container_width = getattr(getattr(self, 'image_container', None), 'width', lambda: 800)()
            container_height = getattr(getattr(self, 'image_container', None), 'height', lambda: 500)()
            available_width = max(180, container_width // max_columns - 30)
            available_height = max(180, container_height - 70)
            scaled_pixmap = pixmap.scaled(available_width, available_height,
                                          Qt.KeepAspectRatio, Qt.SmoothTransformation)

            img_label = QLabel()
            img_label.setPixmap(scaled_pixmap)
            img_label.setAlignment(Qt.AlignCenter)
            img_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            img_label.setProperty('source_pixmap', pixmap)
            img_label.setStyleSheet('border: 1px solid #ddd; padding: 5px; background: white;')
            img_label.setCursor(Qt.PointingHandCursor)
            img_label.setToolTip('双击查看大图')
            img_label.setProperty('image_path', img_path)
            img_label.mouseDoubleClickEvent = (
                lambda event, path=img_path: self.open_image_viewer(path, preview_paths)
            )
            preview_paths.append(img_path)

            v_container = QWidget()
            v_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            v_layout = QVBoxLayout(v_container)
            v_layout.setContentsMargins(0, 0, 0, 0)
            v_layout.setAlignment(Qt.AlignCenter)
            v_layout.addWidget(img_label, 1, Qt.AlignCenter)
            name_label = QLabel(os.path.basename(img_path))
            name_label.setAlignment(Qt.AlignCenter)
            name_label.setStyleSheet('color: #666666; font-size: 9pt;')
            v_layout.addWidget(name_label, 0, Qt.AlignCenter)
            preview_layout.addWidget(v_container, 1)

            if image_index == 0 and len(valid_images) >= 2:
                divider = QFrame()
                divider.setFrameShape(QFrame.VLine)
                divider.setFrameShadow(QFrame.Sunken)
                divider.setLineWidth(1)
                divider.setStyleSheet('color: #999999;')
                preview_layout.insertWidget(preview_layout.count() - 1, divider)

        h_container = preview_container
        self.image_layout.addWidget(h_container, 1)

    def _image_path_value(self):
        return self.output or self.output_base

    def _copy_path(self, path, label):
        if not path:
            QMessageBox.information(self, '提示', f'{label}尚未配置，请先在参数设置中配置。')
            return
        QApplication.clipboard().setText(os.path.abspath(path))
        self.append_log(f'[系统] 已复制{label}: {path}')

    def _open_directory_path(self, path, label):
        if not path:
            QMessageBox.information(self, '提示', f'{label}尚未配置，请先在参数设置中配置。')
            return
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            QMessageBox.warning(self, '提示', f'{label}不存在或尚未创建：\n{path}')
            return
        try:
            if sys.platform == 'win32':
                os.startfile(path)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except OSError as error:
            QMessageBox.critical(self, '错误', f'打开{label}失败：\n{error}')

    def copy_image_output_path(self):
        self._copy_path(self._image_path_value(), '图片保存路径')

    def open_image_output_path(self):
        self._open_directory_path(self._image_path_value(), '图片保存目录')

    def _require_monitoring(self):
        if not self.monitoring_enabled:
            QMessageBox.information(self, '提示', '请先启动图片监控目录！')
            return False
        return True

    def copy_image_monitor_path(self):
        if self._require_monitoring():
            self._copy_path(self.download_dir, '图片监控目录')

    def open_image_monitor_path(self):
        if self._require_monitoring():
            self._open_directory_path(self.download_dir, '图片监控目录')

    def open_image_viewer(self, image_path, image_paths=None):
        """打开图片查看器窗口"""
        try:
            viewer = ImageViewerDialog(image_path, self, image_paths=image_paths)
            try:
                viewer.exec()
            finally:
                viewer.deleteLater()
        except Exception as e:
            QMessageBox.warning(self, '错误', f'打开图片查看器失败：\n{str(e)}')

    def display_downloaded_images(self, image1_path, image2_path):
        """显示下载的两张图片（水平并排）"""
        self.display_image_previews([path for path in (image1_path, image2_path) if path])

        # 启用保存按钮
        self.btn_save_current.setEnabled(True)
        if hasattr(self, 'btn_skip'):
            self.btn_skip.setEnabled(True)

        # 更新图片信息
        if self.current_image_data:
            self.image_info_label.setText(f'路径: {self.current_image_data.folder_path}\n下载时间: {self.current_image_data.timestamp}')

    def display_raw_placeholder(self):
        """显示RAW图的占位符（无法预览）"""
        # 清空之前的图片
        while self.image_layout.count():
            item = self.image_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 显示提示信息
        placeholder_label = QLabel(
            '📁 RAW图已下载\n\n'
            'RAW格式无法预览\n'
            '点击"保存当前图片和日志"可保存到目标目录\n\n'
            f'灰度图: {os.path.basename(self.current_image_data.ir_path)}\n'
            f'NV12图: {os.path.basename(self.current_image_data.rgb_path)}'
        )
        placeholder_label.setAlignment(Qt.AlignCenter)
        placeholder_label.setStyleSheet(
            'color: #666666; '
            'font-size: 11pt; '
            'padding: 40px; '
            'border: 2px dashed #ddd; '
            'background: #f9f9f9; '
            'border-radius: 5px;'
        )
        self.image_layout.addWidget(placeholder_label)

        # 启用保存按钮
        self.btn_save_current.setEnabled(True)
        if hasattr(self, 'btn_skip'):
            self.btn_skip.setEnabled(True)

        # 更新图片信息
        if self.current_image_data:
            self.image_info_label.setText(f'路径: {self.current_image_data.folder_path}\n下载时间: {self.current_image_data.timestamp}')

    def update_history_combo(self):
        """更新历史图片下拉框"""
        self.history_combo.blockSignals(True)
        self.history_combo.clear()

        # 添加"当前图片"选项
        self.history_combo.addItem('当前图片')

        # 添加历史记录
        for i, img_data in enumerate(self.image_history):
            time_str = img_data.timestamp.strftime('%H:%M:%S')
            # 使用 display_name 属性，如果没有则使用 folder_path 的文件夹名
            if hasattr(img_data, 'display_name'):
                display_name = img_data.display_name
            else:
                display_name = os.path.basename(img_data.folder_path) if hasattr(img_data, 'folder_path') else '未知'
            self.history_combo.addItem(f'[{time_str}] {display_name}')

        self.history_combo.blockSignals(False)

    def on_history_changed(self, index):
        """历史图片选择改变"""
        if index == 0:
            # 当前图片 - 使用最新的历史记录
            if self.image_history:
                self.current_image_data = self.image_history[0]
                # 根据是否有 folder_path 决定显示方式
                if hasattr(self.current_image_data, 'folder_path') and self.current_image_data.folder_path:
                    self.display_images(self.current_image_data.folder_path)
                elif self.current_image_data.ir_path or self.current_image_data.rgb_path:
                    self.display_downloaded_images(
                        self.current_image_data.ir_path,
                        self.current_image_data.rgb_path
                    )
        elif index > 0:
            # 历史图片
            history_index = index - 1
            if history_index < len(self.image_history):
                self.current_image_data = self.image_history[history_index]
                # 根据是否有 folder_path 决定显示方式
                if hasattr(self.current_image_data, 'folder_path') and self.current_image_data.folder_path:
                    self.display_images(self.current_image_data.folder_path)
                elif self.current_image_data.ir_path or self.current_image_data.rgb_path:
                    self.display_downloaded_images(
                        self.current_image_data.ir_path,
                        self.current_image_data.rgb_path
                    )

    def clear_image_history(self):
        """清空历史图片"""
        if not self.image_history:
            QMessageBox.information(self, '提示', '历史记录为空')
            return

        reply = QMessageBox.question(
            self,
            '确认清空',
            f'确定要清空所有历史记录吗？\n共 {len(self.image_history)} 条记录',
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            self.image_history.clear()
            self.current_image_data = None
            self.update_history_combo()

            # 清空图片显示
            while self.image_layout.count():
                item = self.image_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

            self.no_image_label = QLabel('历史记录已清空\n\n等待新图片...')
            self.no_image_label.setAlignment(Qt.AlignCenter)
            self.no_image_label.setStyleSheet('color: #999999; font-size: 12pt; padding: 50px;')
            self.image_layout.addWidget(self.no_image_label)

            self.image_info_label.setText('路径: 无')
            self.btn_save_current.setEnabled(False)
            self.btn_skip.setEnabled(False)

    def save_current_image(self):
        """保存当前图片和日志"""
        if not self.current_image_data:
            QMessageBox.warning(self, '提示', '没有待保存的图片')
            return

        # 验证输入
        person = self.person_entry.text().strip()
        if not person:
            QMessageBox.warning(self, '提示', '请输入注册人员')
            return

        # 确保输出目录存在
        if not self.output:
            # 如果output未设置，使用output_base
            if hasattr(self, 'output_base') and self.output_base:
                self.output = self.output_base
            else:
                # 如果连output_base都没有，使用当前目录下的output文件夹
                self.output = os.path.join(os.getcwd(), 'output')
                os.makedirs(self.output, exist_ok=True)

        # 获取保存数量
        save_count = self.save_count_spin.value()

        # 获取要保存的图片列表（从历史记录中取最近N组）
        images_to_save = self.image_history[:save_count] if len(self.image_history) >= save_count else self.image_history[:]

        if not images_to_save:
            QMessageBox.warning(self, '提示', '没有可保存的图片')
            return

        # 构建文件名（顺序：人员_注册场景_注册亮度_识别场景_识别亮度_结果_备注_类型）
        register_scene = self.register_scene_combo.currentText()
        register_luma = self.register_luma_combo.currentText()
        test_type = self.type_combo.currentText()
        remark = self.remark_entry.text().strip()

        name = f'{person}_{register_scene}_{register_luma}'

        if test_type == '识别':
            recognize_scene = self.recognize_scene_combo.currentText()
            recognize_luma = self.recognize_luma_combo.currentText()
            test_result = self.result_combo.currentText()
            name += f'_{recognize_scene}_{recognize_luma}_{test_result}'

        if remark:
            name += f'_{remark}'

        name += f'_{test_type}'

        # 目标文件夹
        dst_folder = os.path.join(self.output, name)

        # 防止重名
        if os.path.exists(dst_folder):
            dst_folder += '_' + datetime.now().strftime('%H%M%S')

        os.makedirs(dst_folder, exist_ok=True)

        # 复制所有要保存的图片
        try:
            saved_count = 0
            for idx, img_data in enumerate(images_to_save):
                # 为每组图片添加序号（从1开始，1是最新的）
                group_num = idx + 1

                # 检查是否有 image_files 属性（文件夹监控的图片）
                if hasattr(img_data, 'image_files') and img_data.image_files:
                    # 保存文件夹中的所有图片
                    for img_file in img_data.image_files:
                        if os.path.exists(img_file):
                            file_name = f'group{group_num}_' + os.path.basename(img_file)
                            shutil.copy2(img_file, os.path.join(dst_folder, file_name))
                            saved_count += 1
                else:
                    # 旧的保存逻辑：只保存 ir_path 和 rgb_path
                    # 保存IR图片
                    if img_data.ir_path and os.path.exists(img_data.ir_path):
                        ir_name = f'group{group_num}_' + os.path.basename(img_data.ir_path)
                        shutil.copy2(img_data.ir_path, os.path.join(dst_folder, ir_name))
                        saved_count += 1

                    # 保存RGB图片
                    if img_data.rgb_path and os.path.exists(img_data.rgb_path):
                        rgb_name = f'group{group_num}_' + os.path.basename(img_data.rgb_path)
                        shutil.copy2(img_data.rgb_path, os.path.join(dst_folder, rgb_name))
                        saved_count += 1

        except Exception as e:
            QMessageBox.critical(self, '错误', f'复制文件失败:\n{e}')
            return

        # 保存日志
        if full_log_cache:
            strategy_index = self.strategy_combo.currentIndex()

            if strategy_index == 0:  # 全部日志
                logs_to_save = extract_logs('all')
            elif strategy_index == 1:  # 最近N行
                logs_to_save = extract_logs('recent_n', self.lines_spin.value())
            else:  # 从关键词开始
                keyword = self.keyword_entry.text().strip()
                if not keyword:
                    QMessageBox.warning(self, '提示', '请输入关键词')
                    return
                logs_to_save = extract_logs('from_keyword', keyword)

            if logs_to_save:
                log_file = os.path.join(dst_folder, 'log.txt')
                with open(log_file, 'w', encoding='utf-8') as f:
                    for line in logs_to_save:
                        f.write(line + "\n")

        # 保存测试数据记录
        _save_last_selection({
            'person': person,
            'register_scene': register_scene,
            'register_luma': register_luma,
            'recognize_scene': self.recognize_scene_combo.currentText(),
            'recognize_luma': self.recognize_luma_combo.currentText(),
            'test_result': self.result_combo.currentText(),
            'test_type': test_type,
            'remark': remark,
        })

        QMessageBox.information(
            self,
            '保存成功',
            f'已保存 {len(images_to_save)} 组图片（共 {saved_count} 个文件）到:\n{dst_folder}'
        )

        # 清空当前图片
        self.current_image_data = None
        self.btn_save_current.setEnabled(False)
        self.btn_skip.setEnabled(False)

        # 清空图片显示
        while self.image_layout.count():
            item = self.image_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.no_image_label = QLabel('等待下一张图片...')
        self.no_image_label.setAlignment(Qt.AlignCenter)
        self.no_image_label.setStyleSheet('color: #999999; font-size: 12pt; padding: 50px;')
        self.image_layout.addWidget(self.no_image_label)

        self.image_info_label.setText('路径: 无')

    def skip_current_image(self):
        """跳过当前图片"""
        if not self.current_image_data:
            return

        reply = QMessageBox.question(
            self,
            '确认跳过',
            '确定要跳过这张图片吗？',
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            self.current_image_data = None
            self.btn_save_current.setEnabled(False)
            self.btn_skip.setEnabled(False)

            # 清空图片显示
            while self.image_layout.count():
                item = self.image_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

            self.no_image_label = QLabel('等待下一张图片...')
            self.no_image_label.setAlignment(Qt.AlignCenter)
            self.no_image_label.setStyleSheet('color: #999999; font-size: 12pt; padding: 50px;')
            self.image_layout.addWidget(self.no_image_label)

            self.image_info_label.setText('路径: 无')

    def show_full_log_viewer_menu(self):
        """按当前安装情况展示完整日志的外部查看方式。"""
        if not full_log_cache:
            QMessageBox.information(self, '提示', '当前还没有产生任何日志')
            return
        try:
            viewers = discover_text_viewers()
        except Exception as error:
            viewers = []
            QMessageBox.warning(self, '提示', f'读取本机文本编辑器列表失败：\n{error}')

        menu = QMenu(self)
        for viewer in viewers:
            action = menu.addAction(viewer.name)
            action.setToolTip(viewer.executable)
            action.triggered.connect(
                lambda checked=False, selected=viewer: self.open_full_log_with_viewer(selected)
            )
        if viewers:
            menu.addSeparator()
        open_with_action = menu.addAction('选择其他程序（系统“打开方式…”）')
        open_with_action.triggered.connect(self.open_full_log_with_system_dialog)
        folder_action = menu.addAction('导出临时文件并打开所在位置')
        folder_action.triggered.connect(self.reveal_full_log_snapshot)
        menu.popup(self.btn_more_log_viewers.mapToGlobal(
            self.btn_more_log_viewers.rect().bottomLeft()
        ))
        self._full_log_viewer_menu = menu

    def _create_full_log_snapshot_or_warn(self):
        try:
            return create_full_log_snapshot()
        except ValueError as error:
            QMessageBox.information(self, '提示', str(error))
        except OSError as error:
            QMessageBox.critical(self, '错误', f'导出完整日志失败：\n{error}')
        return None

    def open_full_log_with_viewer(self, viewer):
        """生成快照后用选定的本地编辑器打开。"""
        path = self._create_full_log_snapshot_or_warn()
        if path is None:
            return
        try:
            subprocess.Popen(viewer_command(viewer, path), shell=False)
        except (OSError, ValueError) as error:
            QMessageBox.critical(
                self, '错误', f'无法使用 {viewer.name} 打开完整日志：\n{error}'
            )

    def open_full_log_with_system_dialog(self):
        """生成快照后交给Windows原生“打开方式”选择器。"""
        path = self._create_full_log_snapshot_or_warn()
        if path is None:
            return
        try:
            show_windows_open_with(path, int(self.winId()))
        except OSError as error:
            QMessageBox.critical(self, '错误', f'无法显示系统“打开方式”：\n{error}')

    def reveal_full_log_snapshot(self):
        """生成快照并在资源管理器中选中文件。"""
        path = self._create_full_log_snapshot_or_warn()
        if path is None:
            return
        try:
            if sys.platform == 'win32':
                subprocess.Popen(['explorer.exe', '/select,', str(path)], shell=False)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', '-R', str(path)])
            else:
                subprocess.Popen(['xdg-open', str(path.parent)])
        except OSError as error:
            QMessageBox.critical(self, '错误', f'无法打开日志所在位置：\n{error}')

    def save_log_only(self):
        """保存全部日志或从选定打点开始的日志，并追加适用打点信息。"""
        if not full_log_cache:
            QMessageBox.information(self, '提示', '当前还没有产生任何日志')
            return

        dialog = SaveLogDialog(self.log_markers, self)
        if dialog.exec() != QDialog.Accepted:
            return
        selected_marker = self.marker_by_id(dialog.selected_marker_id())
        start_index = selected_marker.log_index if selected_marker else 0
        with log_cache_lock:
            logs_snapshot = list(full_log_cache)
        if start_index >= len(logs_snapshot):
            QMessageBox.warning(self, '提示', '选定打点对应的日志已经不可用')
            return

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        remark = dialog.remark()
        name = f'{remark}_{timestamp}' if remark else timestamp
        dst_folder = os.path.join(self.output if self.output else './result', name)
        if os.path.exists(dst_folder):
            dst_folder += '_' + datetime.now().strftime('%H%M%S')

        start_marker_id = selected_marker.marker_id if selected_marker else None
        text = build_marked_log_text(
            logs_snapshot, list(self.log_markers), start_index, start_marker_id
        )
        try:
            os.makedirs(dst_folder, exist_ok=True)
            log_file = os.path.join(dst_folder, 'log.txt')
            with open(log_file, 'w', encoding='utf-8', newline='') as stream:
                stream.write(text)
        except OSError as error:
            QMessageBox.critical(self, '保存失败', f'写入日志失败：\n{error}')
            return

        saved_count = len(logs_snapshot) - start_index
        QMessageBox.information(
            self, '保存成功', f'已保存 {saved_count} 行日志至:\n{dst_folder}'
        )

    def quick_send(self):
        """快速发送"""
        if not self.connected_event.is_set():
            QMessageBox.warning(self, '提示', '串口未连接')
            return

        content = self.send_entry.text().strip()
        if not content:
            return

        try:
            if self.rb_text.isChecked():
                data = content.encode('utf-8')
                if self.cb_add_newline.isChecked():
                    data += b'\r\n'
            elif self.rb_hex.isChecked():
                data = self.parse_hex(content)
            else:  # binary
                data = self.parse_binary(content)

            self.send_queue.put(data)
            self.send_entry.clear()

        except ValueError as e:
            QMessageBox.critical(self, '格式错误', str(e))
        except Exception as e:
            QMessageBox.critical(self, '发送失败', str(e))

    def parse_hex(self, text):
        """解析16进制字符串"""
        hex_str = text.replace(' ', '')
        if not all(c in '0123456789abcdefABCDEF' for c in hex_str):
            raise ValueError('16进制格式错误：只能包含 0-9 和 A-F')
        if len(hex_str) % 2 != 0:
            raise ValueError('16进制格式错误：字符数必须是偶数')
        return bytes.fromhex(hex_str)

    def parse_binary(self, text):
        """解析二进制字符串"""
        bin_str = text.replace(' ', '')
        if not all(c in '01' for c in bin_str):
            raise ValueError('二进制格式错误：只能包含 0 和 1')
        if len(bin_str) % 8 != 0:
            raise ValueError('二进制格式错误：长度必须是8的倍数')
        byte_list = []
        for i in range(0, len(bin_str), 8):
            byte_list.append(int(bin_str[i:i+8], 2))
        return bytes(byte_list)

    def open_search(self):
        """打开搜索窗口"""
        from log_search_pyside6 import LogSearchWindow
        search_window = LogSearchWindow(self, self.log_text)
        search_window.exec()

    def open_output_directory(self):
        """打开日志和图片保存目录"""
        if not self.output or not os.path.exists(self.output):
            QMessageBox.warning(self, '提示', '保存目录尚未配置或不存在\n\n请先在设置中配置保存目录和测试版本')
            return

        try:
            # Windows 使用 explorer 打开目录
            if sys.platform == 'win32':
                os.startfile(self.output)
            elif sys.platform == 'darwin':  # macOS
                subprocess.Popen(['open', self.output])
            else:  # Linux
                subprocess.Popen(['xdg-open', self.output])

            print(f'[调试] 打开保存目录: {self.output}')
        except Exception as e:
            QMessageBox.critical(self, '错误', f'打开目录失败:\n{e}')

    def toggle_theme(self):
        """切换深色/浅色主题"""
        self.dark_mode = not self.dark_mode

        if self.dark_mode:
            self.apply_dark_theme()
            self.action_theme.setText('☀️ 切换至浅色模式')
        else:
            self.apply_light_theme()
            self.action_theme.setText('🌙 切换至深色模式')

        # 重新渲染所有日志以应用新主题的颜色
        self.rerender_all_logs()

    def rerender_all_logs(self):
        """重新渲染所有日志（切换主题时调用）"""
        if getattr(self, 'log_history_marker_id', None) is not None:
            marker = self.marker_by_id(self.log_history_marker_id)
            if marker is not None:
                with log_cache_lock:
                    logs_snapshot = list(full_log_cache)
                start = max(0, marker.log_index - 100)
                end = min(len(logs_snapshot), marker.log_index + 101)
                self.log_history_start = start
                self.log_history_target_row = marker.log_index - start
                self.render_log_snapshot(logs_snapshot[start:end],
                                         self.log_history_target_row,
                                         start_index=start)
        else:
            # 只重绘已显示的逻辑行，同时保留每块绑定的完整缓存绝对索引。
            logs_to_rerender = []
            block = self.log_text.document().firstBlock()
            while block.isValid():
                if block.text() or block.next().isValid():
                    data = block.userData()
                    index = data.log_index if isinstance(data, LogBlockData) else None
                    logs_to_rerender.append(QueuedLog(index, block.text()))
                block = block.next()
            self.log_text.clear()
            self.append_logs(logs_to_rerender)

        # 重新渲染模组响应日志
        module_logs_to_rerender = []
        for log_item in module_log_cache:
            module_logs_to_rerender.append(log_item)

        # 清空模组日志显示
        self.module_response_text.clear()

        # 重新渲染每一行模组日志
        for text, success, error in module_logs_to_rerender:
            # 直接渲染，不再保存到缓存（避免重复）
            timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            log = f'[{timestamp}] {text}'

            # 设置颜色
            if success:
                color = '#2e8b57'  # 绿色
            elif error:
                color = '#c0392b'  # 红色
            else:
                color = '#333333' if not self.dark_mode else '#e0e0e0'  # 默认颜色

            # 设置背景色
            bg_color = '#1e1e1e' if self.dark_mode else 'white'

            # 追加到文本框
            cursor = self.module_response_text.textCursor()
            cursor.movePosition(QTextCursor.End)

            format = cursor.charFormat()
            format.setForeground(QColor(color))
            format.setBackground(QColor(bg_color))
            cursor.setCharFormat(format)
            cursor.insertText(log + '\n')

        self.module_response_text.moveCursor(QTextCursor.End)

    def update_theme_icons(self, dark_mode):
        """刷新按钮使用矢量图标，不受系统emoji字体和运行目录影响。"""
        icon = QIcon(':/theme/refresh_dark.svg' if dark_mode else ':/theme/refresh_light.svg')
        for name in ('refreshPortsButton', 'refreshModulePortsButton'):
            button = self.findChild(QPushButton, name)
            if button is not None:
                button.setIcon(icon)

    def apply_dark_theme(self):
        """应用深色主题"""
        # 主窗口样式
        self.setStyleSheet('''
            QMainWindow, QWidget {
                background-color: #2b2b2b;
                color: #e0e0e0;
            }
            QGroupBox {
                background-color: #353535;
                border: 1px solid #555555;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
                color: #e0e0e0;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: #e0e0e0;
            }
            QPushButton {
                background-color: #404040;
                color: #e0e0e0;
                border: 1px solid #555555;
                padding: 6px 12px;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #4a4a4a;
                border: 1px solid #666666;
            }
            QPushButton:pressed {
                background-color: #353535;
            }
            QPushButton[compactButton="true"] {
                padding: 4px 2px;
                min-width: 24px;
            }
            QPushButton:disabled {
                color: #777777;
            }
            QMenu {
                background-color: #353535;
                color: #e0e0e0;
                border: 1px solid #555555;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 28px 6px 24px;
                border-radius: 2px;
            }
            QMenu::item:selected {
                background-color: #0078d7;
                color: white;
            }
            QMenu::item:disabled {
                color: #777777;
            }
            QMenu::separator {
                height: 1px;
                background-color: #555555;
                margin: 4px 8px;
            }
            QLineEdit, QComboBox, QSpinBox {
                background-color: #353535;
                color: #e0e0e0;
                border: 1px solid #555555;
                padding: 4px;
                border-radius: 3px;
            }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
                border: 1px solid #0078d7;
            }
            QTextEdit {
                background-color: #1e1e1e;
                color: #e0e0e0;
                border: 1px solid #555555;
                border-radius: 3px;
            }
            QLabel {
                color: #e0e0e0;
            }
            QCheckBox, QRadioButton {
                color: #e0e0e0;
            }
            QScrollArea {
                background-color: #1e1e1e;
                border: 1px solid #555555;
            }
            QScrollArea > QWidget > QWidget {
                background-color: #1e1e1e;
            }
            QScrollArea QLabel {
                background-color: transparent;
            }
            QComboBox, QSpinBox {
                padding: 4px 28px 4px 6px;
                min-height: 18px;
            }
            QComboBox QLineEdit, QSpinBox QLineEdit {
                background: transparent;
                border: none;
                padding: 0;
            }
            QComboBox:disabled, QSpinBox:disabled, QLineEdit:disabled {
                color: #777777;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 24px;
                border: none;
                border-left: 1px solid #555555;
                background-color: #404040;
                border-top-right-radius: 3px;
                border-bottom-right-radius: 3px;
            }
            QComboBox::drop-down:hover, QSpinBox::up-button:hover, QSpinBox::down-button:hover {
                background-color: #4a4a4a;
            }
            QComboBox::down-arrow {
                image: url(:/theme/down.svg);
                width: 12px;
                height: 8px;
            }
            QComboBox::down-arrow:disabled {
                image: url(:/theme/down_disabled.svg);
            }
            QComboBox QAbstractItemView {
                background-color: #353535;
                color: #e0e0e0;
                border: 1px solid #555555;
                selection-background-color: #0078d7;
                selection-color: white;
                outline: none;
            }
            QSpinBox::up-button, QSpinBox::down-button {
                subcontrol-origin: border;
                width: 22px;
                background-color: #404040;
                border: 1px solid #555555;
            }
            QSpinBox::up-button {
                subcontrol-position: top right;
                border-top-right-radius: 3px;
            }
            QSpinBox::down-button {
                subcontrol-position: bottom right;
                border-bottom-right-radius: 3px;
            }
            QSpinBox::up-arrow {
                image: url(:/theme/up.svg);
                width: 10px;
                height: 6px;
            }
            QSpinBox::down-arrow {
                image: url(:/theme/down.svg);
                width: 10px;
                height: 6px;
            }
            QSpinBox::up-arrow:disabled, QSpinBox::up-arrow:off {
                image: url(:/theme/up_disabled.svg);
            }
            QSpinBox::down-arrow:disabled, QSpinBox::down-arrow:off {
                image: url(:/theme/down_disabled.svg);
            }
        ''')

        self.update_theme_icons(True)

        # 日志文本特殊处理
        self.log_text.setStyleSheet('''
            QTextEdit {
                background-color: #1e1e1e;
                color: #e0e0e0;
                border: 1px solid #555555;
            }
        ''')

        # 搜索栏样式
        self.search_bar.setStyleSheet('QWidget#logSearchBar { background-color: #353535; border: 1px solid #555555; }')

        # 状态标签保持原有颜色逻辑，只调整默认色
        # 其他动态颜色（连接状态等）保持不变

    def apply_light_theme(self):
        """应用浅色主题"""
        # 清除所有自定义样式，恢复默认
        self.setStyleSheet('')
        self.update_theme_icons(False)
        self.log_text.setStyleSheet('')
        self.search_bar.setStyleSheet('QWidget#logSearchBar { background-color: #f0f0f0; border: 1px solid #ccc; }')

    def launch_host_program(self):
        """启动上位机程序"""
        if not self.host_program:
            print('[调试-启动] 上位机程序路径为空，取消启动')
            return

        print(f'[调试-启动] 启动上位机程序: {self.host_program}')

        try:
            subprocess.Popen(
                self.host_program,
                cwd=os.path.dirname(self.host_program) or None,
            )
            self.append_log(f'[系统] 已启动上位机程序: {self.host_program}')
            print(f'[调试-启动] 启动成功')
        except Exception as e:
            self.append_log(f'[系统] 启动上位机程序失败: {e}')
            print(f'[调试-启动] 启动失败: {e}')

    def show_search_bar(self):
        """显示搜索栏"""
        print('[调试-搜索] show_search_bar 被调用')

        # 获取选中的文本
        cursor = self.log_text.textCursor()
        selected_text = cursor.selectedText()
        print(f'[调试-搜索] 选中的文本: "{selected_text}"')

        self.search_bar.setVisible(True)
        self.search_input.setFocus()

        # 如果有选中的文本，自动填充到搜索框
        if selected_text:
            self.search_input.setText(selected_text)
            self.search_input.selectAll()
            print(f'[调试-搜索] 已填充到搜索框: "{selected_text}"')

        # 触发搜索
        if self.search_input.text():
            print(f'[调试-搜索] 准备触发搜索，搜索内容: "{self.search_input.text()}"')
            self.on_search_text_changed()

    def hide_search_bar(self):
        """隐藏搜索栏"""
        print('[调试-搜索] hide_search_bar 被调用')
        self.search_bar.setVisible(False)
        self.clear_search_highlights()
        self.log_text.setFocus()

    def on_search_text_changed(self):
        """搜索文本改变"""
        search_text = self.search_input.text()
        print(f'[调试-搜索] on_search_text_changed 被调用，搜索文本: "{search_text}"')

        # 清除之前的高亮
        self.clear_search_highlights()
        self.search_matches = []
        self.current_match_index = -1

        if not search_text:
            self.search_result_label.setText('0/0')
            print('[调试-搜索] 搜索文本为空，返回')
            return

        # 执行搜索
        print('[调试-搜索] 开始查找所有匹配项')
        self.find_all_matches(search_text)
        print(f'[调试-搜索] 找到 {len(self.search_matches)} 个匹配项')

        # 更新结果计数
        if self.search_matches:
            self.current_match_index = 0
            self.search_result_label.setText(f'1/{len(self.search_matches)}')
            self.highlight_current_match()
            print(f'[调试-搜索] 显示第一个匹配项')
        else:
            self.search_result_label.setText('0/0')
            print('[调试-搜索] 未找到匹配项')

    def find_all_matches(self, search_text):
        """查找所有匹配项（使用 ExtraSelections）"""
        print(f'[调试-搜索] find_all_matches 开始，搜索: "{search_text}"')
        document = self.log_text.document()

        # 设置查找标志
        flags = QTextDocument.FindFlag(0)
        if self.cb_case_sensitive.isChecked():
            flags |= QTextDocument.FindCaseSensitively
            print('[调试-搜索] 启用区分大小写')
        if self.cb_whole_word.isChecked():
            flags |= QTextDocument.FindWholeWords
            print('[调试-搜索] 启用全字匹配')

        # 查找所有匹配并创建 ExtraSelections
        cursor = QTextCursor(document)
        match_count = 0
        extra_selections = []

        while True:
            cursor = document.find(search_text, cursor, flags)
            if cursor.isNull():
                break

            self.search_matches.append(cursor.position() - len(search_text))
            match_count += 1

            # 创建高亮选区（不修改原始文本格式）
            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor
            selection.format.setBackground(QColor('#ffff99'))  # 黄色背景
            extra_selections.append(selection)

        # 应用所有高亮
        self.apply_log_extra_selections(extra_selections)

        print(f'[调试-搜索] find_all_matches 完成，共找到 {match_count} 个匹配项')

    def clear_search_highlights(self):
        """清除搜索高亮但保留打点目标高亮。"""
        self.search_extra_selections = []
        self.apply_log_extra_selections()

    def highlight_current_match(self):
        """高亮当前匹配项（使用 ExtraSelections）"""
        print(f'[调试-搜索] highlight_current_match 被调用')
        print(f'[调试-搜索] 匹配项总数: {len(self.search_matches)}')
        print(f'[调试-搜索] 当前索引: {self.current_match_index}')

        if not self.search_matches or self.current_match_index < 0:
            print('[调试-搜索] 没有匹配项或索引无效，返回')
            return

        search_text = self.search_input.text()
        document = self.log_text.document()
        extra_selections = []

        # 为所有匹配项添加黄色高亮
        for i, pos in enumerate(self.search_matches):
            cursor = QTextCursor(document)
            cursor.setPosition(pos)
            cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, len(search_text))

            selection = QTextEdit.ExtraSelection()
            selection.cursor = cursor

            # 当前匹配项用橙色，其他用黄色
            if i == self.current_match_index:
                selection.format.setBackground(QColor('#FF6B35'))  # 鲜艳的橙红色，对比度高
            else:
                selection.format.setBackground(QColor('#ffff99'))  # 黄色

            extra_selections.append(selection)

        # 应用所有高亮
        self.apply_log_extra_selections(extra_selections)

        # 移动光标到当前匹配位置
        match_position = self.search_matches[self.current_match_index]
        print(f'[调试-搜索] 匹配位置: {match_position}')

        cursor = self.log_text.textCursor()
        cursor.setPosition(match_position)
        cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, len(search_text))
        self.log_text.setTextCursor(cursor)
        self.log_text.ensureCursorVisible()

        # 强制让日志窗口获得焦点，确保选中颜色显示正确
        self.log_text.setFocus()

        print('[调试-搜索] 已跳转到匹配位置')

    def find_next(self):
        """查找下一个"""
        print(f'[调试-搜索] find_next 被调用')
        print(f'[调试-搜索] 当前匹配项数量: {len(self.search_matches)}')
        print(f'[调试-搜索] 当前索引（跳转前）: {self.current_match_index}')

        if not self.search_matches:
            print('[调试-搜索] 没有匹配项，返回')
            return

        # 先清除当前高亮的橙色标记，重新应用黄色
        self.refresh_highlights()

        # 移动到下一个
        self.current_match_index = (self.current_match_index + 1) % len(self.search_matches)
        print(f'[调试-搜索] 当前索引（跳转后）: {self.current_match_index}')
        self.search_result_label.setText(f'{self.current_match_index + 1}/{len(self.search_matches)}')
        self.highlight_current_match()

    def find_previous(self):
        """查找上一个"""
        print(f'[调试-搜索] find_previous 被调用')
        print(f'[调试-搜索] 当前匹配项数量: {len(self.search_matches)}')
        print(f'[调试-搜索] 当前索引（跳转前）: {self.current_match_index}')

        if not self.search_matches:
            print('[调试-搜索] 没有匹配项，返回')
            return

        # 先清除当前高亮的橙色标记，重新应用黄色
        self.refresh_highlights()

        # 移动到上一个
        self.current_match_index = (self.current_match_index - 1) % len(self.search_matches)
        print(f'[调试-搜索] 当前索引（跳转后）: {self.current_match_index}')
        self.search_result_label.setText(f'{self.current_match_index + 1}/{len(self.search_matches)}')
        self.highlight_current_match()

    def refresh_highlights(self):
        """刷新高亮显示（重新应用所有黄色高亮）"""
        print('[调试-搜索] refresh_highlights 被调用')

        # 直接调用 highlight_current_match，它会处理所有高亮
        self.highlight_current_match()

    # ==============================
    # 模组控制相关方法
    # ==============================

    def refresh_module_ports(self):
        """刷新模组串口列表"""
        ports = [p.device for p in serial.tools.list_ports.comports()]
        current = self.module_port_combo.currentText()

        self.module_port_combo.blockSignals(True)
        self.module_port_combo.clear()
        if ports:
            self.module_port_combo.addItems(ports)
            if current in ports:
                self.module_port_combo.setCurrentText(current)
        else:
            self.module_port_combo.addItem('无可用串口')
        self.module_port_combo.blockSignals(False)

    def is_log_port_active(self, port=None):
        """判断日志串口是否仍实际打开或处于关闭中的受保护阶段。"""
        active_port = getattr(self, 'port', None)
        if port is not None and port != active_port:
            return False
        control = getattr(self, 'serial_control', None)
        thread = getattr(self, 'serial_thread', None)
        if control is not None:
            return control.is_active() or bool(thread and thread.is_alive())
        return bool(thread and thread.is_alive()) or self.connected_event.is_set()

    def connect_module(self):
        """连接模组串口"""
        if self.module_connected:
            # 断开连接
            self.disconnect_module()
            return

        # 验证参数
        port = self.module_port_combo.currentText()
        if not port or port == '无可用串口':
            QMessageBox.warning(self, '提示', '请选择有效的模组串口')
            return

        try:
            baudrate = int(self.module_baudrate_combo.currentText())
        except ValueError:
            QMessageBox.warning(self, '提示', '波特率必须是数字')
            return

        # 仅在日志串口仍实际打开或正在释放时阻止同端口复用。
        if self.is_log_port_active(port):
            QMessageBox.warning(self, '提示', '该串口已被日志串口使用，请先断开日志串口')
            return

        try:
            # 打开串口
            self.module_serial = serial.Serial(
                port=port,
                baudrate=baudrate,
                timeout=1,
                write_timeout=0.5
            )

            self.module_port = port
            self.module_baudrate = baudrate
            self.module_connected = True

            # 启动接收线程
            self.module_receive_thread = threading.Thread(
                target=self.module_receive_worker,
                daemon=True
            )
            self.module_receive_thread.start()

            # 启动响应轮询定时器
            self.module_response_timer = QTimer(self)
            self.module_response_timer.setTimerType(Qt.PreciseTimer)
            self.module_response_timer.timeout.connect(self.poll_module_response)
            self.module_response_timer.start(100)

            # 更新UI
            self.module_status_label.setText('● 已连接')
            self.module_status_label.setStyleSheet('font-size: 10pt; font-weight: bold; color: #2e8b57;')
            self.btn_module_connect.setText('🔌 断开模组')
            self.btn_module_connect.setStyleSheet('QPushButton { background-color: #c0392b; color: white; font-weight: bold; padding: 6px 12px; }')
            self.module_port_combo.setEnabled(False)
            self.module_baudrate_combo.setEnabled(False)

            # 启用功能按钮
            self.btn_get_version.setEnabled(True)
            self.btn_get_all_user_ids.setEnabled(True)
            self.btn_register.setEnabled(True)
            self.btn_face_recognition.setEnabled(True)
            self.btn_palm_register.setEnabled(True)
            self.btn_palm_recognition.setEnabled(True)
            self.btn_download_image.setEnabled(True)
            self.btn_download_raw.setEnabled(True)
            self.btn_delete_user.setEnabled(True)
            self.btn_delete_all.setEnabled(True)
            self.btn_restart_module.setEnabled(True)
            self.btn_standby.setEnabled(True)
            self.btn_enter_demo.setEnabled(True)
            self.btn_exit_demo.setEnabled(True)
            self.btn_enter_debug.setEnabled(True)
            self.btn_exit_debug.setEnabled(True)
            self.btn_custom_command.setEnabled(True)
            self.btn_ota.setEnabled(True)

            self.append_module_log(f'[模组] 已连接到 {port} @ {baudrate}bps')

        except Exception as e:
            QMessageBox.critical(self, '连接失败', f'无法连接到模组串口:\n{e}')
            self.module_serial = None
            self.module_connected = False

    def disconnect_module(self):
        """断开模组串口"""
        try:
            if self.module_serial:
                self.module_serial.close()
                self.module_serial = None

            self.module_connected = False

            # 停止响应轮询
            if hasattr(self, 'module_response_timer'):
                self.module_response_timer.stop()

            # 重置下载相关标志位（防止下载卡死后重新连接无法下载）
            if getattr(self, 'ota_in_progress', False) or getattr(self, 'ota_perf', None):
                self.finish_ota('中止（断开模组）')
            self.end_download_performance('中止（断开模组）')
            self.is_downloading = False
            self.download_buffer = bytearray()
            self.download_offset = 0
            self.download_total_size = 0
            self.download_type = None
            print('[调试] 断开连接时已重置下载标志位')

            # 更新UI
            self.module_status_label.setText('● 未连接')
            self.module_status_label.setStyleSheet('font-size: 10pt; font-weight: bold; color: #999999;')
            self.btn_module_connect.setText('🔌 连接模组')
            self.btn_module_connect.setStyleSheet('QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 6px 12px; }')
            self.module_port_combo.setEnabled(True)
            self.module_baudrate_combo.setEnabled(True)

            # 禁用功能按钮
            self.btn_get_version.setEnabled(False)
            self.btn_get_all_user_ids.setEnabled(False)
            self.btn_register.setEnabled(False)
            self.btn_face_recognition.setEnabled(False)
            self.btn_palm_register.setEnabled(False)
            self.btn_palm_recognition.setEnabled(False)
            self.btn_download_image.setEnabled(False)
            self.btn_download_raw.setEnabled(False)
            self.btn_delete_user.setEnabled(False)
            self.btn_delete_all.setEnabled(False)
            self.btn_restart_module.setEnabled(False)
            self.btn_standby.setEnabled(False)
            self.btn_enter_demo.setEnabled(False)
            self.btn_exit_demo.setEnabled(False)
            self.btn_enter_debug.setEnabled(False)
            self.btn_exit_debug.setEnabled(False)
            self.btn_custom_command.setEnabled(False)
            self.btn_ota.setEnabled(False)

            self.append_module_log('[模组] 已断开连接')

        except Exception as e:
            print(f'[模组] 断开连接失败: {e}')

    def module_receive_worker(self):
        """模组串口接收线程（优化版 - 使用阻塞读取）"""
        # 设置读取超时为500ms，避免永久阻塞
        if self.module_serial:
            self.module_serial.timeout = 0.5

        while self.module_connected and self.module_serial:
            try:
                # 阻塞读取头部5字节（同步头2 + 消息类型1 + 长度2）
                # pyserial会在有数据时立即返回，或超时后返回实际读到的字节
                header = self.module_serial.read(5)
                if len(header) < 5:
                    if header:
                        self.module_response_queue.put(('receive_stat', '头部短读', time.perf_counter()))
                    continue  # 无数据的空闲超时不计入错误

                sync = header[0:2]
                if sync != b'\xEF\xAA':
                    self.module_response_queue.put(('receive_stat', '同步头不匹配', time.perf_counter()))
                    continue

                msg_type = header[2:3]
                data_size_bytes = header[3:5]
                data_size = int.from_bytes(data_size_bytes, byteorder='big')

                # 阻塞读取数据和校验和（data_size + 1字节）
                # 不需要手动轮询in_waiting，read()会高效等待数据到达
                tail = self.module_serial.read(data_size + 1)
                received_at = time.perf_counter()
                if len(tail) < data_size + 1:
                    self.module_response_queue.put(('receive_stat', '包体短读', received_at))
                    continue  # 数据不完整，重新读取

                data = tail[0:data_size]
                checksum = tail[data_size:data_size + 1]

                # 快速校验和计算
                calc_checksum = msg_type[0] ^ data_size_bytes[0] ^ data_size_bytes[1]
                for b in data:
                    calc_checksum ^= b

                if calc_checksum != checksum[0]:
                    self.module_response_queue.put(('receive_stat', '校验失败', received_at))
                    self.module_response_queue.put(('error', b'Checksum error'))
                    continue

                # 自定义命令对话框（只在需要时构造数据包）
                if self.custom_command_dialog and self.custom_command_dialog.isVisible():
                    raw_packet = sync + msg_type + data_size_bytes + data + checksum
                    self.custom_command_data_signal.emit(raw_packet)

                # 根据消息类型处理
                msg_type_val = msg_type[0]
                if msg_type_val == 0x00:  # Reply消息
                    if data_size >= 1:
                        msg_id = data[0]
                        result = data[1] if data_size >= 2 else 0xFF
                        payload = data[2:] if data_size > 2 else b''
                        self.module_response_queue.put(('reply', msg_id, result, payload,
                                                        (received_at, time.perf_counter())))

                elif msg_type_val == 0x01:  # Note消息
                    self.module_response_queue.put(('note', data))

                elif msg_type_val == 0x02:  # 图片数据消息
                    self.module_response_queue.put(('image_data', data_size, data,
                                                    (received_at, time.perf_counter())))

                else:
                    self.module_response_queue.put(('error', f'Unknown message type: 0x{msg_type_val:02X}'.encode()))

            except Exception as e:
                if self.module_connected:
                    self.module_response_queue.put(('error', f'Receive error: {e}'.encode()))
                break

    def poll_module_response(self):
        """轮询模组响应队列"""
        try:
            while not self.module_response_queue.empty():
                response = self.module_response_queue.get_nowait()

                if response[0] == 'receive_stat':
                    perf = getattr(self, 'download_perf', None)
                    if perf and response[2] >= perf.started:
                        perf.count(response[1])
                    ota_perf = getattr(self, 'ota_perf', None)
                    if ota_perf and response[2] >= ota_perf.started:
                        ota_perf.count(response[1])
                elif response[0] == 'error':
                    if response[1].startswith(b'Receive error:') and getattr(self, 'download_perf', None):
                        self.is_downloading = False
                        self.end_download_performance('中止（串口接收异常）')
                    if response[1].startswith(b'Receive error:') and getattr(self, 'ota_perf', None):
                        self.finish_ota('中止（串口接收异常）')
                    error_msg = response[1].decode('utf-8', errors='ignore')
                    self.append_module_log(f'[错误] {error_msg}', error=True)
                elif response[0] == 'reply':
                    # Reply消息: ('reply', msg_id, result, payload)
                    _, msg_id, result, payload = response[:4]
                    if msg_id == 0x44 and len(response) > 4 and getattr(self, 'ota_perf', None):
                        self.record_ota_ack_timing(response[4])
                    # 优化：下载期间减少打印
                    # if not self.is_downloading or msg_id not in [0x18, 0x51]:
                    #     print(f'[调试-Reply] msg_id=0x{msg_id:02X}, result=0x{result:02X}, payload长度={len(payload)}')
                    self.module_response_signal.emit(f'0x{msg_id:02X}', ('reply', result, payload))
                elif response[0] == 'note':
                    # Note消息: ('note', data)
                    _, data = response
                    # if not self.is_downloading:
                    #     print(f'[调试-Note] data长度={len(data)}, 前4字节={data[:4].hex().upper() if len(data) >= 4 else data.hex().upper()}')
                    self.module_response_signal.emit('note', ('note', 0, data))
                elif response[0] == 'image_data':
                    _, data_size, img_data, timing = response
                    self.handle_image_data(data_size, img_data, timing)
        except queue.Empty:
            pass

    def handle_module_response(self, msg_id, data):
        """处理模组响应"""
        msg_type, result, payload = data

        # 调试打印
        # print(f'[调试-处理响应] msg_id={msg_id}, msg_type={msg_type}, result={result if msg_type == "reply" else "N/A"}')

        # 处理Note消息
        if msg_id == 'note':
            self.handle_note_message(payload)
            return

        # 处理Reply消息
        if msg_type != 'reply':
            return

        # 清除超时定时器（收到响应说明指令成功）
        if self.command_timeout_timer and self.command_timeout_timer.isActive():
            self.command_timeout_timer.stop()

        # 清除待响应指令记录
        if self.pending_command:
            pending_msg_id = self.pending_command[0]
            # 检查响应的msg_id是否匹配待响应的指令
            try:
                current_msg_id = int(msg_id, 16)
                if current_msg_id == pending_msg_id:
                    self.pending_command = None
            except:
                pass

        if (getattr(self, 'download_perf', None) and result != 0x00
                and msg_id in ('0x14', '0x15', '0x51')):
            self.is_downloading = False
            self.end_download_performance(f'中止（{msg_id} 返回失败）')

        if msg_id == '0x51' and result != 0x00 and getattr(self, 'ota_perf', None):
            self.finish_ota('中止（设置波特率失败）')
        if msg_id in ('0x40', '0x43'):
            expected_stage = 2 if msg_id == '0x40' else 3
            if not self.ota_in_progress or self.ota_stage != expected_stage:
                return

        # 计算时长
        elapsed_time = self.get_command_elapsed_time(msg_id)

        if msg_id == '0x30':  # 获取版本号
            if result == 0x00:
                # 成功，解析版本号

                version = payload[:-1].decode('utf-8', errors='ignore').rstrip('\x00')
                self.append_module_log(f'[版本号] {version} {elapsed_time}', success=True)
                # 检查是否需要执行序列的下一步
                self.check_sequence_next('获取版本号')

            else:
                self.append_module_log(f'[错误] 获取版本号失败，结果码: 0x{result:02X} {elapsed_time}', error=True)
                self.check_sequence_next('获取版本号')

        elif msg_id == '0x24':  # 获取所有用户ID
            if result == 0x00:
                # 成功，解析用户ID列表
                # payload格式：第1个字节是用户数量，后面每2个字节是一个用户ID（大端序）
                if len(payload) >= 1:
                    user_count = payload[0]  # 第一个字节是用户数量

                    # 检查数据长度是否足够
                    expected_length = 1 + user_count * 2
                    if len(payload) >= expected_length:
                        user_ids = []
                        # 从第2个字节开始读取用户ID
                        for i in range(user_count):
                            offset = 1 + i * 2  # 跳过第一个字节（用户数量）
                            user_id = int.from_bytes(payload[offset:offset+2], byteorder='big')
                            user_ids.append(user_id)

                        if user_ids:
                            user_ids_str = ', '.join(str(uid) for uid in user_ids)
                            self.append_module_log(f'共查询到 {user_count} 个ID，分别是 {user_ids_str} {elapsed_time}', success=True)
                        else:
                            self.append_module_log(f'共查询到 0 个ID {elapsed_time}', success=True)
                    else:
                        self.append_module_log(f'[错误] 用户ID数据长度不足，期望 {expected_length} 字节，实际 {len(payload)} 字节 {elapsed_time}', error=True)
                else:
                    self.append_module_log(f'[错误] 用户ID数据格式错误，缺少用户数量字段 {elapsed_time}', error=True)
            else:
                self.append_module_log(f'[错误] 获取用户ID失败，结果码: 0x{result:02X} {elapsed_time}', error=True)
            # 检查是否需要执行序列的下一步
            self.check_sequence_next('获取所有用户ID')

        elif msg_id == '0x1D':  # 单帧注册
            if result == 0x00:
                # 注册成功，解析用户ID（紧跟result后面的2字节）
                if len(payload) >= 2:
                    user_id = int.from_bytes(payload[:2], byteorder='big')
                    self.append_module_log(f'注册成功，用户ID为 {user_id} {elapsed_time}', success=True)
                else:
                    self.append_module_log(f'注册成功 {elapsed_time}', success=True)

                # 统计成功次数
                if self.repeat_mode:
                    self.repeat_success_count += 1

                # 检查是否需要继续重复执行（传递成功状态）
                self.check_repeat_next(last_success=True)

                # 检查是否需要执行序列的下一步
                self.check_sequence_next('人脸注册')
            else:
                error_messages = {
                    0x01: '模组拒绝此命令',
                    0x04: 'Camera open fail',
                    0x08: '无人脸录入/无该用户',
                    0x09: '超出最大注册用户数量',
                    0x0C: '活体检测失败',
                    0x0D: '超时',
                    0x10: '验证失败',
                }
                error_msg = error_messages.get(result, f'未知错误 (0x{result:02X})')
                self.append_module_log(f'注册失败: {error_msg} {elapsed_time}', error=True)

                # 检查是否需要继续重复执行（传递失败状态）
                self.check_repeat_next(last_success=False)

                # 注册失败也触发序列下一步（可根据需求修改）
                self.check_sequence_next('人脸注册')

        elif msg_id == '0x12':  # 人脸识别
            if result == 0x00:
                # 识别成功，解析用户ID（紧跟result后面的2字节）
                if len(payload) >= 2:
                    user_id = int.from_bytes(payload[:2], byteorder='big')
                    self.append_module_log(f'识别成功，用户ID为 {user_id} {elapsed_time}', success=True)
                else:
                    self.append_module_log(f'识别成功 {elapsed_time}', success=True)

                # 统计成功次数
                if self.repeat_mode:
                    self.repeat_success_count += 1

                # 检查是否需要继续重复执行（传递成功状态）
                self.check_repeat_next(last_success=True)

                # 检查是否需要执行序列的下一步
                self.check_sequence_next('识别D')
            elif result == 0x23:
                self.append_module_log(f'palm switch {elapsed_time}', success=True)
                # palm switch不触发重复逻辑
            else:
                error_messages = {
                    0x01: '模组拒绝此命令',
                    0x04: 'Camera open fail',
                    0x08: '无人脸录入',
                    0x09: '超出最大注册用户数量',
                    0x0C: '活体检测失败',
                    0x0D: '超时',
                    0x10: '验证失败',
                    0x17: '无手掌录入用户',
                    0x19: '超时',
                    0x22: '人脸高度相似',
                    # 0x23: 'palm modle switch',
                    0x24: '手掌对比失败',
                }
                error_msg = error_messages.get(result, f'未知错误 (0x{result:02X})')
                self.append_module_log(f'识别失败: {error_msg} {elapsed_time}', error=True)

                # 检查是否需要继续重复执行（传递失败状态）
                self.check_repeat_next(last_success=False)

                # 识别失败也触发序列下一步
                self.check_sequence_next('识别D')

        elif msg_id == '0x51':  # 设置波特率
            # 添加调试信息
            print(f'[调试-0x51] result=0x{result:02X}, payload长度={len(payload)}, payload={payload.hex().upper() if payload else "空"}')

            if result == 0x00:
                # 设置成功，payload第一个字节是波特率代码
                baudrate_map = {
                    0x01: 115200,
                    0x02: 230400,
                    0x03: 460800,
                    0x04: 1500000,
                }

                # 根据你的描述，data部分的第二个字节是结果，那么payload应该是result之后的数据
                # 但可能payload为空，我们需要从发送的命令中获取波特率代码
                if len(payload) >= 1:
                    baudrate_code = payload[0]
                    print(f'[调试-0x51] 从payload读取波特率代码: 0x{baudrate_code:02X}')
                else:
                    # payload为空，从当前下载状态推断
                    print(f'[调试-0x51] payload为空，从下载状态推断')
                    print(f'[调试-0x51] is_downloading={self.is_downloading}, download_offset={self.download_offset}, download_total_size={self.download_total_size}')
                    baudrate_code = None

                    # 如果已经完成下载（download_offset >= download_total_size），说明是恢复波特率
                    if self.download_offset >= self.download_total_size and self.download_total_size > 0:
                        baudrate_code = 0x01
                        print(f'[调试-0x51] 下载已完成，推断为恢复标准波特率: 0x01')
                    # 如果还没开始下载，应该是设置高速波特率
                    elif self.download_offset == 0 and self.download_total_size == 0:
                        baudrate_code = 0x04
                        print(f'[调试-0x51] 尚未开始下载，推断为高速波特率设置: 0x04')
                    else:
                        # 其他情况，根据is_downloading判断
                        if self.is_downloading or self.download_offset > 0:
                            baudrate_code = 0x01
                            print(f'[调试-0x51] 正在下载或下载中断，推断为恢复标准波特率: 0x01')
                        else:
                            baudrate_code = 0x04
                            print(f'[调试-0x51] 默认推断为高速波特率设置: 0x04')

                if baudrate_code is not None:
                    baudrate = baudrate_map.get(baudrate_code, baudrate_code)
                    self.append_module_log(f'设置波特率成功！{elapsed_time}', success=True)
                    print(f'[调试-0x51] 推断波特率: {baudrate}')
                    print(f'[调试-0x51] ota_in_progress={self.ota_in_progress}, ota_stage={self.ota_stage}')
                    print(f'[调试-0x51] is_standby_restoring={getattr(self, "is_standby_restoring", False)}')

                    # 判断是OTA升级还是图片下载
                    if self.ota_in_progress and self.ota_stage == 1:
                        # OTA升级流程：使用保存的目标波特率，而不是推断的波特率
                        actual_baudrate = getattr(self, 'ota_target_baudrate', baudrate)
                        print(f'[调试-0x51] OTA升级流程，切换波特率到 {actual_baudrate}（目标波特率）')
                        if self.module_serial:
                            try:
                                self.module_serial.baudrate = actual_baudrate
                                self.append_module_log(f'[OTA] 串口波特率已切换到 {actual_baudrate}')
                                # 进入下一阶段：发送0x40进入OTA状态
                                self.ota_stage = 2
                                self.schedule_ota_step(100, 2, self.enter_ota_mode)
                            except Exception as e:
                                self.append_module_log(f'[OTA] 切换波特率失败: {e}', error=True)
                                self.finish_ota('中止（切换波特率失败）')
                    elif self.ota_in_progress and self.ota_stage > 1:
                        # OTA升级过程中（stage > 1），忽略其他0x51响应
                        print(f'[调试-0x51] OTA升级过程中，忽略0x51响应（stage={self.ota_stage}）')
                    elif getattr(self, 'is_standby_restoring', False):
                        # 待机恢复波特率，不触发任何操作
                        print(f'[调试-0x51] 待机恢复波特率，不触发图片下载')
                        self.is_standby_restoring = False
                    elif baudrate == 1500000 and self.module_serial:
                        # 图片下载流程
                        print(f'[调试-0x51] 图片下载流程，开始切换串口波特率...')
                        try:
                            self.module_serial.baudrate = baudrate
                            if getattr(self, 'download_perf', None):
                                self.download_perf.mark('high_baud')
                            print(f'[调试-0x51] 串口波特率切换成功')
                            self.append_module_log(f'串口波特率已切换到 {baudrate}')

                            # 延迟30ms后再发送下一个指令，等待模组稳定
                            QTimer.singleShot(30, self.send_get_image_size_command)

                        except Exception as e:
                            print(f'[调试-0x51] 切换波特率异常: {e}')
                            self.append_module_log(f'[错误] 切换波特率失败: {e}', error=True)
                            self.is_downloading = False
                            self.end_download_performance('中止（切换波特率失败）')
                    elif baudrate == 115200 and self.module_serial:
                        # 恢复标准波特率
                        print(f'[调试-0x51] 恢复标准波特率')
                        try:
                            self.module_serial.baudrate = baudrate
                        except Exception as e:
                            self.end_download_performance('中止（恢复波特率失败）')
                            self.append_module_log(f'[错误] 恢复波特率失败: {e}', error=True)
                            return
                        self.append_module_log(f'串口波特率已恢复到 {baudrate}')
                        perf = getattr(self, 'download_perf', None)
                        if perf:
                            perf.mark('restored')
                            outcome = '完成' if 'preview' in perf.stages else '中止（传输未完成）'
                            self.end_download_performance(outcome)

                        # 判断是否是待机恢复波特率
                        if getattr(self, 'is_standby_restoring', False):
                            print(f'[调试-0x51] 这是待机恢复波特率，不触发图片下载')
                            self.is_standby_restoring = False  # 重置标志
                        else:
                            # 图片下载流程完成
                            self.append_module_log('[完成] 图片下载流程完成！', success=True)

                            # 检查是否需要执行序列的下一步
                            if self.download_type == 'jpeg':
                                self.check_sequence_next('下载JPEG')
                            elif self.download_type == 'raw':
                                self.check_sequence_next('下载RAW')
                    else:
                        print(f'[调试-0x51] 波特率={baudrate}, 不执行切换逻辑')
                else:
                    self.append_module_log(f'设置波特率成功（无法确定波特率值） {elapsed_time}', success=True)
            else:
                self.append_module_log(f'设置波特率失败 {elapsed_time}', error=True)

        elif msg_id == '0x14':  # 获取JPEG图片大小
            if result == 0x00:
                # 成功，解析图片大小
                # payload后8个字节：前4字节是第一张图片大小，后4字节是第二张图片大小
                if len(payload) >= 8:
                    self.image1_size = int.from_bytes(payload[:4], byteorder='big')
                    self.image2_size = int.from_bytes(payload[4:8], byteorder='big')
                    total_size = self.image1_size + self.image2_size
                    self.append_module_log(
                        f'获取JPEG大小成功：第一张 {self.image1_size} 字节，第二张 {self.image2_size} 字节，'
                        f'总计 {total_size} 字节 {elapsed_time}',
                        success=True
                    )
                    # 开始下载图片
                    self.start_image_download()
                else:
                    self.append_module_log(f'[错误] 图片大小数据长度不足 {elapsed_time}', error=True)
                    self.end_download_performance('中止（JPEG大小回复不完整）')
            else:
                self.append_module_log(f'[错误] 获取图片大小失败 {elapsed_time}', error=True)

        elif msg_id == '0x15':  # 获取RAW图片大小
            if result == 0x00:
                # 成功，解析RAW图总大小
                # payload是4个字节：表示两张图片的总大小
                if len(payload) >= 4:
                    total_size = int.from_bytes(payload[:4], byteorder='big')
                    # 根据RAW模式计算两张图的大小
                    if self.raw_mode == 'Y+RGB':
                        # Y+RGB模式：第一张图40%，第二张图60%
                        self.image1_size = int(total_size * 0.4)
                        self.image2_size = total_size - self.image1_size
                        mode_desc = 'Y+RGB模式(40%+60%)'
                    else:  # Y+IR
                        # Y+IR模式：各50%
                        self.image1_size = total_size // 2
                        self.image2_size = total_size - self.image1_size
                        mode_desc = 'Y+IR模式(50%+50%)'

                    self.append_module_log(
                        f'获取RAW图大小成功 [{mode_desc}]：总大小 {total_size} 字节，'
                        f'第一张 {self.image1_size} 字节，第二张 {self.image2_size} 字节 {elapsed_time}',
                        success=True
                    )
                    # 开始下载图片
                    self.start_image_download()
                else:
                    self.append_module_log(f'[错误] RAW图大小数据长度不足 {elapsed_time}', error=True)
                    self.end_download_performance('中止（RAW大小回复不完整）')
            else:
                self.append_module_log(f'[错误] 获取RAW图大小失败 {elapsed_time}', error=True)

        elif msg_id == '0x18':  # 图片上传指令的回复（错误情况）
            # 在正常传输过程中收到reply消息说明出错了，需要重传
            print(f'[调试-0x18] 收到回复消息，传输出错，result=0x{result:02X}')
            self.append_module_log(f'[警告] 图片传输出错，1秒后重传... (错误码: 0x{result:02X})')

            # 停止之前的重传定时器
            if self.retry_timer:
                self.retry_timer.stop()

            # 1秒后重传最后一次的上传指令
            self.retry_timer = QTimer()
            self.retry_timer.setSingleShot(True)
            self.retry_timer.timeout.connect(self.retry_last_upload)
            self.retry_timer.start(1000)  # 1秒后重传

        elif msg_id == '0x62':  # 手掌注册
            if result == 0x00:
                # 注册成功，payload是用户ID（2字节）
                if len(payload) >= 2:
                    user_id = int.from_bytes(payload[:2], byteorder='big')
                    self.append_module_log(f'手掌注册成功，用户ID为 {user_id} {elapsed_time}', success=True)
                else:
                    self.append_module_log(f'手掌注册成功 {elapsed_time}', success=True)

                # 统计成功次数
                if self.repeat_mode:
                    self.repeat_success_count += 1

                # 检查是否需要继续重复执行（传递成功状态）
                self.check_repeat_next(last_success=True)

                # 检查是否需要执行序列的下一步
                self.check_sequence_next('手掌注册')
            else:
                # 注册失败（错误码从十进制转换为十六进制）
                error_messages = {
                    0x17: '没有手掌用户',  # 23
                    0x18: '超出最大注册用户数量',  # 24
                    0x19: '解锁超时',  # 25
                    0x23: '掌模型转换失败',  # 35
                    0x24: '手掌对比失败',  # 36
                }
                error_msg = error_messages.get(result, f'未知错误 (0x{result:02X})')
                self.append_module_log(f'手掌注册失败: {error_msg} {elapsed_time}', error=True)

                # 检查是否需要继续重复执行（传递失败状态）
                self.check_repeat_next(last_success=False)

                # 注册失败也触发序列下一步
                self.check_sequence_next('手掌注册')

        elif msg_id == '0x63':  # 手掌识别
            if result == 0x00:
                # 识别成功，payload包含36字节：[用户ID 2字节][用户名 32字节][是否管理员 1字节][解锁状态 1字节]
                if len(payload) >= 36:
                    user_id = int.from_bytes(payload[:2], byteorder='big')
                    # 用户名：32字节，去除末尾的0x00
                    username_bytes = payload[2:34]
                    username = username_bytes.rstrip(b'\x00').decode('utf-8', errors='ignore')
                    is_admin = payload[34]
                    unlock_status = payload[35]

                    self.append_module_log(
                        f'手掌识别成功：用户ID={user_id}, 用户名={username}, '
                        f'管理员={is_admin}, 解锁状态={unlock_status} {elapsed_time}',
                        success=True
                    )
                else:

                    self.append_module_log(f'手掌识别成功 {elapsed_time}', success=True)

                # 统计成功次数
                if self.repeat_mode:
                    self.repeat_success_count += 1

                # 检查是否需要继续重复执行（传递成功状态）
                self.check_repeat_next(last_success=True)

                # 检查是否需要执行序列的下一步
                self.check_sequence_next('识别K')
            elif result == 0x23:
                self.append_module_log(f'palm switch {elapsed_time}', success=True)
                # palm switch不触发重复逻辑
            else:
                # 识别失败（错误码从十进制转换为十六进制）
                error_messages = {
                    # 0x17: '没有手掌用户',  # 23
                    0x18: '超出最大注册用户数量',  # 24
                    0x19: '解锁超时',  # 25
                    0x23: '掌模型转换失败',  # 35
                    # 0x24: '手掌对比失败',  # 36
                    0x01: '模组拒绝此命令',
                    0x04: 'Camera open fail',
                    0x08: '无人脸录入',
                    0x09: '超出最大注册用户数量',
                    0x0C: '活体检测失败',
                    0x0D: '超时',
                    0x10: '验证失败',
                    0x17: '无手掌录入用户',
                    0x19: '超时',
                    0x22: '人脸高度相似',
                    # 0x23: 'palm modle switch',
                    0x24: '手掌对比失败',
                }
                error_msg = error_messages.get(result, f'未知错误 (0x{result:02X})')
                self.append_module_log(f'手掌识别失败: {error_msg} {elapsed_time}', error=True)

                # 检查是否需要继续重复执行（传递失败状态）
                self.check_repeat_next(last_success=False)

                # 识别失败也触发序列下一步
                self.check_sequence_next('识别K')

        elif msg_id == '0x64':  # 手掌获取已注册用户列表
            if result == 0x00:
                # 成功，payload格式：[用户数量 2字节][用户ID1 2字节][用户ID2 2字节]...
                if len(payload) >= 2:
                    # 用户数量：前2字节，小端序
                    user_count = int.from_bytes(payload[:2], byteorder='little')

                    # 读取所有用户ID（只读取user_count个，使用小端序）
                    user_ids = []
                    for i in range(user_count):
                        offset = 2 + i * 2
                        if offset + 2 <= len(payload):
                            user_id = int.from_bytes(payload[offset:offset+2], byteorder='little')
                            # 只添加非0的用户ID
                            if user_id != 0:
                                user_ids.append(user_id)

                    # 显示结果
                    if user_ids:
                        user_ids_str = ', '.join(str(uid) for uid in user_ids)
                        self.append_module_log(
                            f'已注册手掌用户数量: {len(user_ids)}, 用户ID: [{user_ids_str}] {elapsed_time}',
                            success=True
                        )
                    else:
                        self.append_module_log(
                            f'已注册手掌用户数量: 0 {elapsed_time}',
                            success=True
                        )
                else:
                    self.append_module_log(f'获取已注册用户列表成功，但数据长度不足 {elapsed_time}', error=True)
            else:
                self.append_module_log(f'获取已注册用户列表失败 {elapsed_time}', error=True)

        elif msg_id == '0x20':  # 人脸删除指定用户ID
            if result == 0x00:
                self.append_module_log(f'[人脸模式] 删除用户成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[人脸模式] 删除用户失败，结果码: 0x{result:02X} {elapsed_time}', error=True)
            # 检查是否需要执行序列的下一步
            self.check_sequence_next('删除指定用户ID')

        elif msg_id == '0x21':  # 人脸删除所有用户
            if result == 0x00:
                self.append_module_log(f'[人脸模式] 删除所有用户成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[人脸模式] 删除所有用户失败，结果码: 0x{result:02X} {elapsed_time}', error=True)
            # 检查是否需要执行序列的下一步
            self.check_sequence_next('删除所有用户')

        elif msg_id == '0x65':  # 手掌删除指定用户ID
            if result == 0x00:
                self.append_module_log(f'[手掌模式] 删除用户成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[手掌模式] 删除用户失败，结果码: 0x{result:02X} {elapsed_time}', error=True)
            # 检查是否需要执行序列的下一步
            self.check_sequence_next('删除指定用户ID')

        elif msg_id == '0x66':  # 手掌删除所有用户
            if result == 0x00:
                self.append_module_log(f'[手掌模式] 删除所有用户成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[手掌模式] 删除所有用户失败，结果码: 0x{result:02X} {elapsed_time}', error=True)
            # 检查是否需要执行序列的下一步
            self.check_sequence_next('删除所有用户')

        elif msg_id == '0x55':  # 重启模组
            if result == 0x00:
                self.append_module_log(f'[重启模组] 重启成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[重启模组] 重启失败，结果码: 0x{result:02X} {elapsed_time}', error=True)
            # 检查是否需要执行序列的下一步
            self.check_sequence_next('重启模组')

        elif msg_id == '0x10':  # 待机
            if result == 0x00:
                self.append_module_log(f'[待机] 待机成功 {elapsed_time}', success=True)
                # 检查是否需要执行序列的下一步
                self.check_sequence_next('待机')
            else:
                self.append_module_log(f'[待机] 待机失败，结果码: 0x{result:02X} {elapsed_time}', error=True)
                # 失败也触发下一步
                self.check_sequence_next('待机')

        elif msg_id == '0x80':  # KDS手掌注册
            if result == 0x00:
                # 注册成功，解析用户ID（紧跟result后面的2字节）
                if len(payload) >= 2:
                    user_id = int.from_bytes(payload[:2], byteorder='big')
                    self.append_module_log(f'[KDS手掌注册] 注册成功，用户ID为 {user_id} {elapsed_time}', success=True)
                else:
                    self.append_module_log(f'[KDS手掌注册] 注册成功 {elapsed_time}', success=True)

                # 统计成功次数
                if self.repeat_mode:
                    self.repeat_success_count += 1
            else:
                self.append_module_log(f'[KDS手掌注册] 注册失败，结果码: 0x{result:02X} {elapsed_time}', error=True)

            # 检查是否需要继续重复执行
            self.check_repeat_next()

        elif msg_id == '0x81':  # KDS手掌识别
            if result == 0x00:
                # 识别成功，解析用户ID（紧跟result后面的2字节）
                if len(payload) >= 2:
                    user_id = int.from_bytes(payload[:2], byteorder='big')
                    self.append_module_log(f'[KDS手掌识别] 识别成功，用户ID为 {user_id} {elapsed_time}', success=True)
                else:
                    self.append_module_log(f'[KDS手掌识别] 识别成功 {elapsed_time}', success=True)

                # 统计成功次数
                if self.repeat_mode:
                    self.repeat_success_count += 1
            elif result == 0x23:
                self.append_module_log(f'palm switch {elapsed_time}', success=True)
            else:
                error_messages = {
                    0x01: '模组拒绝此命令',
                    0x04: 'Camera open fail',
                    0x08: '无人脸录入',
                    0x09: '超出最大注册用户数量',
                    0x0C: '活体检测失败',
                    0x0D: '超时',
                    0x10: '验证失败',
                    0x17: '无手掌录入用户',
                    0x19: '超时',
                    0x22: '人脸高度相似',
                    # 0x23: 'palm modle switch',
                    0x24: '手掌对比失败',
                }
                error_msg = error_messages.get(result, f'未知错误 (0x{result:02X})')
                self.append_module_log(f'识别失败: {error_msg} {elapsed_time}', error=True)
                # self.append_module_log(f'[KDS手掌识别] 识别失败，结果码: 0x{result:02X} {elapsed_time}', error=True)

            # 检查是否需要继续重复执行
            if result != 0x23:
                self.check_repeat_next()

        elif msg_id == '0x82':  # KDS删除所有用户
            if result == 0x00:
                self.append_module_log(f'[KDS手掌模式] 删除所有用户成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[KDS手掌模式] 删除所有用户失败，结果码: 0x{result:02X} {elapsed_time}', error=True)

        elif msg_id == '0x83':  # KDS删除指定用户ID
            if result == 0x00:
                self.append_module_log(f'[KDS手掌模式] 删除用户成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[KDS手掌模式] 删除用户失败，结果码: 0x{result:02X} {elapsed_time}', error=True)

        elif msg_id == '0x84':  # KDS获取所有用户ID
            if result == 0x00:
                if len(payload) >= 2:
                    # 用户数量：前2字节，小端序
                    user_count = int.from_bytes(payload[:2], byteorder='little')

                    # 读取所有用户ID（只读取user_count个，使用小端序）
                    user_ids = []
                    for i in range(user_count):
                        offset = 2 + i * 2
                        if offset + 2 <= len(payload):
                            user_id = int.from_bytes(payload[offset:offset+2], byteorder='little')
                            # 只添加非0的用户ID
                            if user_id != 0:
                                user_ids.append(user_id)

                    # 显示结果
                    if user_ids:
                        user_ids_str = ', '.join(str(uid) for uid in user_ids)
                        self.append_module_log(
                            f'[KDS] 已注册手掌用户数量: {len(user_ids)}, 用户ID: [{user_ids_str}] {elapsed_time}',
                            success=True
                        )
                    else:
                        self.append_module_log(
                            f'[KDS] 已注册手掌用户数量: 0 {elapsed_time}',
                            success=True
                        )
                else:
                    self.append_module_log(f'[KDS] 获取已注册用户列表成功，但数据长度不足 {elapsed_time}', error=True)
            else:
                self.append_module_log(f'[KDS] 获取已注册用户列表失败 {elapsed_time}', error=True)

        elif msg_id == '0xFE':  # 演示模式
            if result == 0x00:
                self.append_module_log(f'[演示模式] 操作成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[演示模式] 操作失败，结果码: 0x{result:02X} {elapsed_time}', error=True)
            # 检查是否需要执行序列的下一步（进入演示和退出演示都使用同一个msg_id）
            self.check_sequence_next('进入演示')
            self.check_sequence_next('退出演示')

        elif msg_id == '0xF0':  # Debug模式
            if result == 0x00:
                self.append_module_log(f'[Debug模式] 操作成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[Debug模式] 操作失败，结果码: 0x{result:02X} {elapsed_time}', error=True)
            # 检查是否需要执行序列的下一步（进入Debug和退出Debug都使用同一个msg_id）
            self.check_sequence_next('进入Debug')
            self.check_sequence_next('退出Debug')

        elif msg_id == '0x40':  # 进入OTA状态
            print(f'[OTA调试] 收到0x40响应: result=0x{result:02X}, payload长度={len(payload)}')
            if len(payload) > 0:
                print(f'[OTA调试] payload: {payload.hex().upper()}')
            if result == 0x00:
                self.append_module_log(f'[OTA] 进入OTA状态成功 {elapsed_time}', success=True)
                # 进入下一阶段：发送OTA header
                if self.ota_in_progress and self.ota_stage == 2:
                    self.ota_stage = 3
                    self.schedule_ota_step(100, 3, self.send_ota_header)
            else:
                self.append_module_log(f'[OTA] 进入OTA状态失败，结果码: 0x{result:02X} {elapsed_time}', error=True)
                self.finish_ota('中止（进入OTA失败）')

        elif msg_id == '0x43':  # OTA header
            print(f'[OTA调试] 收到0x43响应: result=0x{result:02X}, payload长度={len(payload)}')
            if len(payload) > 0:
                print(f'[OTA调试] payload: {payload.hex().upper()}')
            if result == 0x00:
                self.append_module_log(f'[OTA] OTA header发送成功 {elapsed_time}', success=True)
                # 进入下一阶段：发送固件包
                if self.ota_in_progress and self.ota_stage == 3:
                    self.ota_stage = 4
                    self.ota_current_packet = 0
                    self.set_download_polling(False)
                    self.schedule_ota_step(100, 4, self.send_ota_packet)
            else:
                self.append_module_log(f'[OTA] OTA header发送失败，结果码: 0x{result:02X} {elapsed_time}', error=True)
                self.finish_ota('中止（header失败）')

        elif msg_id == '0x44':  # OTA固件包传输
            if not (self.ota_in_progress and self.ota_stage == 4 and self.ota_waiting_ack):
                return
            self.ota_waiting_ack = False
            perf = getattr(self, 'ota_perf', None)
            if perf:
                perf.handled_at = time.perf_counter()

            if result == 0x00:
                # 停止超时重传定时器
                if self.ota_retry_timer:
                    self.ota_retry_timer.stop()
                    self.ota_retry_timer = None

                # 传输成功，重置重传次数
                self.ota_retry_count = 0

                # 继续发送下一包
                if self.ota_in_progress and self.ota_stage == 4:
                    if perf:
                        perf.packets += 1
                        perf.bytes_received += min(self.ota_packet_size,
                            len(self.ota_file_data) - self.ota_current_packet * self.ota_packet_size)
                        perf.request_retried = False
                    self.ota_current_packet += 1
                    progress = (self.ota_current_packet / self.ota_total_packets) * 100

                    # 进度限频，最后一包必输出
                    now = time.perf_counter()
                    if (self.ota_current_packet >= self.ota_total_packets
                            or (perf and now - perf.last_progress >= 1.0)):
                        if perf:
                            perf.last_progress = now
                        self.append_module_log(f'[OTA] 传输进度: {self.ota_current_packet}/{self.ota_total_packets} ({progress:.1f}%)')

                    if self.ota_current_packet < self.ota_total_packets:
                        # 继续发送下一包
                        self.schedule_ota_step(10, 4, self.send_ota_packet)
                    else:
                        # 所有包发送完成，等待烧录
                        self.append_module_log('[OTA] 所有固件包发送完成，等待模组烧录...', success=True)
                        self.ota_stage = 5
                        self.set_download_polling(False)
                        if perf:
                            perf.mark('received')
            else:
                self.append_module_log(f'[OTA] 固件包传输失败，包序号: {self.ota_current_packet}, 结果码: 0x{result:02X}', error=True)
                # 停止超时重传定时器
                if self.ota_retry_timer:
                    self.ota_retry_timer.stop()
                    self.ota_retry_timer = None
                # 传输失败，尝试重传
                if self.ota_retry_count < 3:
                    self.ota_retry_count += 1
                    if perf:
                        perf.count('错误ACK重传')
                        perf.request_retried = True
                    self.append_module_log(f'[OTA] 第{self.ota_retry_count}次重传包序号: {self.ota_current_packet}')
                    self.schedule_ota_step(100, 4, self.send_ota_packet)
                else:
                    self.append_module_log(f'[OTA] 包序号{self.ota_current_packet}重传3次后仍失败，终止OTA升级', error=True)
                    self.finish_ota('中止（错误ACK重试耗尽）')

    def handle_note_message(self, data):
        """处理Note消息"""
        # 特殊处理：重启模组的Note消息（datasize=1，只有1字节）
        if len(data) == 1:
            # 重启模组返回的Note消息，data部分只有1字节
            result = data[0]
            if result == 0x00:
                self.append_module_log('[重启模组] 重启成功', success=True)
            else:
                self.append_module_log(f'[重启模组] 重启失败，结果码: 0x{result:02X}', error=True)
            return

        # Note消息格式：第1字节是NID，标识消息类型
        if len(data) >= 2:
            nid = data[0]

            # OTA过程结束通知（data[0]=0x03, data[1]=0x00表示OTA结束）
            if nid == 0x03:
                if not self.ota_in_progress or self.ota_stage != 5:
                    return
                status = data[1] if len(data) >= 2 else 0xFF
                print(f'[OTA调试] 收到OTA结束Note消息: nid=0x{nid:02X}, status=0x{status:02X}')
                hex_data = ' '.join([f'{b:02X}' for b in data])
                print(f'[OTA调试] Note完整数据: {hex_data}')
                if status == 0x00:
                    self.append_module_log('[OTA] 烧录完成，等待模组重启！', success=True)
                    if getattr(self, 'ota_perf', None):
                        self.ota_perf.mark('burned')
                    # 等待模组重启并发送ready消息
                    self.ota_stage = 6
                else:
                    self.append_module_log(f'[OTA] 烧录失败，状态码: 0x{status:02X}', error=True)
                    self.finish_ota('中止（模组烧录失败）')
                return

            # Ready消息（data[0]=0x00, data[1]=0x00表示模组ready）
            if nid == 0x00 and len(data) >= 2:
                status = data[1]
                print(f'[OTA调试] 收到Ready消息: nid=0x{nid:02X}, status=0x{status:02X}')
                hex_data = ' '.join([f'{b:02X}' for b in data])
                print(f'[OTA调试] Note完整数据: {hex_data}')
                if status == 0x00:
                    if self.ota_in_progress and self.ota_stage == 6:
                        self.append_module_log('[OTA] 模组重启成功，OTA升级完成！', success=True)
                        if getattr(self, 'ota_perf', None):
                            self.ota_perf.mark('ready')
                        self.finish_ota('升级完成')
                    else:
                        self.append_module_log('[模组] Ready消息收到', success=True)
                elif self.ota_in_progress and self.ota_stage == 6:
                    self.append_module_log(f'[OTA] 模组重启失败，状态码: 0x{status:02X}', error=True)
                    self.finish_ota('中止（模组重启失败）')
                return

            # 判断是人脸还是手掌
            if nid == 0x01:  # 人脸Note消息
                # 第1字节是固定字段0x01，第2字节是第一个状态信息
                status = data[1]

                status_messages = {
                    0: '人脸正常',
                    1: '未检测到人脸',
                    2: '人脸太靠上，请向下移动',
                    3: '人脸太靠下，请向上移动',
                    4: '人脸太靠右，请向左移动',
                    5: '人脸太靠左，请向右移动',
                    6: '人脸太远，请靠近',
                    7: '人脸太近，请远离',
                    8: '眉毛遮挡/检测到多人',
                    9: '眼睛遮挡',
                    10: '脸部遮挡',
                    11: '人脸方向错误',
                    12: '闭眼模式检测到睁眼/非活体',
                    13: '闭眼状态',
                    14: '闭眼模式无法判断睁眼闭眼',
                    15: '目标模糊',
                    16: '对比失败',
                    17: '人脸质量过低',
                    18: '2D活体失败',
                    19: '3D活体失败',
                    20: '偏转角度过大',
                    21: '偏转角度过大',
                    22: '偏转角度过大',
                    23: '偏转角度过大',
                    24: '高相似度对比失败',
                    25: '正脸',
                }

                status_msg = status_messages.get(status, f'未知状态 ({status})')

                # 根据当前命令类型显示不同的状态前缀
                if self.current_command_type == 0x1D:
                    self.append_module_log(f'[注册状态] {status_msg}')
                elif self.current_command_type == 0x12:
                    self.append_module_log(f'[识别状态] {status_msg}')
                else:
                    self.append_module_log(f'[状态] {status_msg}')

            elif nid == 0x04:  # 手掌Note消息（DSM模式）
                # 第1字节是0x04，第2字节是状态信息
                status = data[1]

                palm_status_messages = {
                    0x28: '手掌正常',  # 40
                    0x29: '未检测到手掌',  # 41
                    0x2A: '关键点置信度过低',  # 42
                    0x2B: '关键点置信度过低',  # 43
                    0x2C: '角度不符合',  # 44
                    0x2D: '角度不符合',  # 45
                    0x2E: '遮挡',  # 46
                    0x2F: '模糊',  # 47
                    0x30: '手背',  # 48
                    0x31: '异常手势',  # 49
                    0x32: '过曝',  # 50
                    0x33: '欠曝',  # 51
                    0x34: '遮挡',  # 52
                    0x35: '模糊',  # 53
                    0x36: '手掌有水',  # 54
                    0x38: '太近',  # 56
                    0x39: '太远',  # 57
                    0x3A: '活体不过',  # 58
                    0x3B: '对比失败',  # 59
                    0x3C: '手掌反光',  # 60
                    0x3D: '左边出框',  # 61
                    0x3E: '右边出框',  # 62
                    0x3F: '上边出框',  # 63
                    0x40: '下边出框',  # 64
                    0x41: '左上角出框',  # 65
                    0x42: '右上角出框',  # 66
                    0x43: '左下角出框',  # 67
                    0x44: '右下角出框',  # 68
                    0x50: '手掌移动，请保持稳定',  # 80
                }

                status_msg = palm_status_messages.get(status, f'未知错误 (0x{status:02X})')
                self.append_module_log(f'[手掌状态] {status_msg}')

            elif nid == 0x08:  # 手掌Note消息（KDS模式）
                # 第1字节是0x05，第2字节是状态信息
                status = data[1]

                # KDS模式的状态码（十进制转十六进制）
                kds_palm_status_messages = {
                    0x00: '正常',  # 0
                    0x01: '未检测到手掌',  # 1
                    0x02: '太靠近边缘',  # 2
                    0x03: '太靠近边缘',  # 3
                    0x04: '太靠近边缘',  # 4
                    0x05: '太靠近边缘',  # 5
                    0x06: '太远',  # 6
                    0x07: '太近',  # 7
                    0x0C: '活体不过',  # 12
                    0x10: '对比失败',  # 16
                    0x11: '手掌模糊',  # 17
                    0x2A: '关键点置信度过低',  # 42
                    0x2B: '关键点置信度过低',  # 43
                    0x2C: '角度不符合',  # 44
                    0x2D: '角度不符合',  # 45
                    0x2E: '遮挡',  # 46
                    0x30: '手背',  # 48
                    0x31: '异常手势',  # 49
                    0x34: '遮挡',  # 52
                    0x36: '手掌有水',  # 54
                    0x3C: '手掌反光',  # 60
                    0x41: '左上角出框',  # 65
                    0x42: '右上角出框',  # 66
                    0x43: '左下角出框',  # 67
                    0x44: '右下角出框',  # 68
                    0x50: '手掌移动，请保持稳定',  # 80


                }

                status_msg = kds_palm_status_messages.get(status, f'未知错误 (0x{status:02X})')
                self.append_module_log(f'[KDS手掌状态] {status_msg}')

            else:
                self.append_module_log(f'[Note消息] 未知NID: 0x{nid:02X}')
        else:
            self.append_module_log('[错误] Note消息数据长度不足', error=True)

    def get_command_elapsed_time(self, msg_id):
        """获取指令执行时长

        Args:
            msg_id: 消息ID字符串（如'0x1D'）

        Returns:
            格式化的时长字符串（如'(耗时: 1.23s)'）
        """
        # 从msg_id字符串中提取数字部分
        try:
            msg_id_int = int(msg_id, 16)
            if msg_id_int in self.module_command_start_time:
                elapsed = time.time() - self.module_command_start_time[msg_id_int]
                # 清除记录
                del self.module_command_start_time[msg_id_int]
                return f'(耗时: {elapsed:.2f}s)'
        except (ValueError, KeyError):
            pass

        return ''

    def send_module_command(self, msg_id, data=b''):
        """发送模组指令

        Args:
            msg_id: 消息ID（字节）
            data: 数据部分（字节串）
        """
        if not self.module_connected or not self.module_serial:
            if getattr(self, 'download_perf', None):
                self.is_downloading = False
                self.end_download_performance('中止（模组未连接）')
            if getattr(self, 'ota_perf', None):
                self.finish_ota('中止（模组未连接）')
            QMessageBox.warning(self, '提示', '模组串口未连接')
            return False

        try:
            ota_perf = getattr(self, 'ota_perf', None) if msg_id == 0x44 else None
            build_started = time.perf_counter() if ota_perf else None
            # 构建消息
            sync = b'\xEF\xAA'
            msg_id_byte = bytes([msg_id])
            data_size = len(data).to_bytes(2, byteorder='big')

            # 计算校验和（不包括同步字段）
            checksum = msg_id
            checksum ^= data_size[0]
            checksum ^= data_size[1]
            for b in data:
                checksum ^= b

            # 完整消息
            message = sync + msg_id_byte + data_size + data + bytes([checksum])

            # # 打印调试信息（对OTA相关指令）
            # if msg_id in [0x40, 0x43, 0x44, 0x51]:
            #     hex_msg = ' '.join([f'{b:02X}' for b in message])
            #     print(f'[发送指令] MID=0x{msg_id:02X}, 完整消息: {hex_msg}')

            # 保留 flush，先测量它是否造成逐包等待
            perf = getattr(self, 'download_perf', None) if msg_id == 0x18 else None
            perf = ota_perf or perf
            write_started = time.perf_counter()
            if ota_perf:
                ota_perf.add('协议封装/校验', write_started - build_started)
            self.module_serial.write(message)
            written_at = time.perf_counter()
            self.module_serial.flush()
            flushed_at = time.perf_counter()
            if perf:
                perf.add('串口write', written_at - write_started)
                perf.add('串口flush', flushed_at - written_at)

            # 记录发送时间
            self.module_command_start_time[msg_id] = time.time()

            # 记录当前命令类型（用于Note消息识别）
            self.current_command_type = msg_id

            # 为关键指令启动超时重传定时器（0x14获取JPEG大小、0x15获取RAW大小、0x51设置波特率）
            if msg_id in [0x14, 0x15, 0x51]:
                # 停止之前的超时定时器
                if self.command_timeout_timer:
                    self.command_timeout_timer.stop()

                # 记录待响应的指令
                if not self.pending_command or self.pending_command[0] != msg_id:
                    self.pending_command = (msg_id, data, 0)  # (msg_id, data, retry_count)

                # 启动超时定时器（5秒超时）
                self.command_timeout_timer = QTimer()
                self.command_timeout_timer.setSingleShot(True)
                self.command_timeout_timer.timeout.connect(self.on_command_timeout)
                self.command_timeout_timer.start(5000)

            # 记录日志
            # hex_str = ' '.join(f'{b:02X}' for b in message)
            # self.append_module_log(f'[发送] {msg_id_byte}')

            return True

        except Exception as e:
            if getattr(self, 'download_perf', None):
                self.is_downloading = False
                self.end_download_performance('中止（发送失败）')
            if getattr(self, 'ota_perf', None):
                self.finish_ota('中止（发送失败）')
            QMessageBox.critical(self, '发送失败', f'发送指令失败:\n{e}')
            return False

    def on_command_timeout(self):
        """指令超时处理"""
        if not self.pending_command:
            return

        msg_id, data, retry_count = self.pending_command

        # 最多重试3次
        if retry_count < 3:
            retry_count += 1
            if getattr(self, 'download_perf', None):
                self.download_perf.count(f'命令0x{msg_id:02X}超时重传')
            if getattr(self, 'ota_perf', None):
                self.ota_perf.count(f'命令0x{msg_id:02X}超时重传')
            self.pending_command = (msg_id, data, retry_count)

            msg_name_map = {
                0x14: '获取JPEG大小',
                0x15: '获取RAW大小',
                0x51: '设置波特率'
            }
            msg_name = msg_name_map.get(msg_id, f'0x{msg_id:02X}')

            self.append_module_log(f'[超时重传] {msg_name}指令无响应，第{retry_count}次重试...', error=True)
            print(f'[调试-超时] 指令0x{msg_id:02X}超时，重试次数={retry_count}')

            # 重新发送指令
            self.send_module_command(msg_id, data)
        else:
            # 重试次数用尽
            msg_name_map = {
                0x14: '获取JPEG大小',
                0x15: '获取RAW大小',
                0x51: '设置波特率'
            }
            msg_name = msg_name_map.get(msg_id, f'0x{msg_id:02X}')

            self.append_module_log(f'[错误] {msg_name}指令重试3次后仍无响应，请检查模组连接', error=True)
            self.pending_command = None

            if getattr(self, 'download_perf', None):
                self.is_downloading = False
                self.end_download_performance('中止（命令超时重试耗尽）')

            if getattr(self, 'ota_perf', None):
                self.finish_ota('中止（命令超时重试耗尽）')

            # 清理下载状态
            if msg_id in [0x14, 0x15]:
                self.is_downloading = False
                self.download_buffer = bytearray()
                self.download_offset = 0
                self.download_total_size = 0

    def get_module_version(self):
        """获取模组版本号"""
        # 发送0x30指令获取版本号
        self.send_module_command(0x30)
        self.append_module_log('[获取版本号] 已发送指令，等待响应...')

    def get_all_user_ids(self):
        """获取所有用户ID"""
        # 根据模式选择发送不同的指令
        if self.operation_mode_actions['palm'].isChecked():
            # 手掌模式：根据项目模式选择命令
            if self.project_mode_actions['KDS'].isChecked():
                cmd = 0x84  # KDS模式
                mode_name = 'KDS手掌模式'
            else:
                cmd = 0x64  # DSM模式
                mode_name = 'DSM手掌模式'
            self.send_module_command(cmd)
            self.append_module_log(f'[{mode_name}] 已发送获取已注册用户列表指令，等待响应...')
        else:
            # 人脸模式：发送0x24指令获取所有用户ID
            self.send_module_command(0x24)
            self.append_module_log('[人脸模式] 已发送获取所有用户ID指令，等待响应...')

    def register_single_frame(self):
        """单帧注册（支持重复执行）"""
        # 获取重复次数
        repeat_count = self.repeat_count_spin.value()

        if repeat_count > 1 and not self.repeat_mode:
            # 开始重复模式
            self.repeat_mode = True
            self.repeat_current = 0
            self.repeat_total = repeat_count
            self.repeat_command = self._do_register_single_frame
            self.repeat_success_count = 0  # 重置成功计数
            self.append_module_log(f'[重复注册] 开始执行，共 {repeat_count} 次')

        # 执行第一次
        self._do_register_single_frame()

    def _do_register_single_frame(self):
        """执行单次注册"""
        if self.repeat_mode:
            self.repeat_current += 1
            self.repeat_reply_received = False  # 重置Reply接收标志
            self.append_module_log(f'[单帧注册] 第 {self.repeat_current}/{self.repeat_total} 次')

        # 固定的注册用户信息（35字节）
        user_data = b'\x00tester' + b'\x00' * 27 + b'\x05'  # "tester" + 27个0x00 + 0x05
        self.send_module_command(0x1D, user_data)

        if not self.repeat_mode:
            self.append_module_log('[单帧注册] 已发送注册指令，等待响应...')

    def face_recognition(self):
        """人脸识别（支持重复执行）"""
        # 获取重复次数
        repeat_count = self.repeat_count_spin.value()

        if repeat_count > 1 and not self.repeat_mode:
            # 开始重复模式
            self.repeat_mode = True
            self.repeat_current = 0
            self.repeat_total = repeat_count
            self.repeat_command = self._do_face_recognition
            self.repeat_success_count = 0  # 重置成功计数
            self.append_module_log(f'[重复识别] 开始执行，共 {repeat_count} 次')

        # 执行第一次
        self._do_face_recognition()

    def _do_face_recognition(self):
        """执行单次人脸识别"""
        if self.repeat_mode:
            self.repeat_current += 1
            self.repeat_reply_received = False  # 重置Reply接收标志
            self.append_module_log(f'[人脸识别] 第 {self.repeat_current}/{self.repeat_total} 次')

        # 发送识别指令
        data = b'\x00\x05'
        self.send_module_command(0x12, data)

        if not self.repeat_mode:
            self.append_module_log('[人脸识别] 已发送识别指令，等待响应...')

    def palm_register(self):
        """手掌注册（支持重复执行）"""
        # 获取重复次数
        repeat_count = self.repeat_count_spin.value()

        if repeat_count > 1 and not self.repeat_mode:
            # 开始重复模式
            self.repeat_mode = True
            self.repeat_current = 0
            self.repeat_total = repeat_count
            self.repeat_command = self._do_palm_register
            self.repeat_success_count = 0  # 重置成功计数
            self.append_module_log(f'[重复注册] 开始执行，共 {repeat_count} 次')

        # 执行第一次
        self._do_palm_register()

    def _do_palm_register(self):
        """执行单次手掌注册"""
        if self.repeat_mode:
            self.repeat_current += 1
            self.repeat_reply_received = False  # 重置Reply接收标志

        # 根据项目模式选择命令ID
        if self.project_mode_actions['KDS'].isChecked():
            cmd = 0x80  # KDS模式
            mode_name = 'KDS手掌注册'
        else:
            cmd = 0x62  # DSM模式
            mode_name = 'DSM手掌注册'

        if self.repeat_mode:
            self.append_module_log(f'[{mode_name}] 第 {self.repeat_current}/{self.repeat_total} 次')

        # 发送手掌注册指令
        user_data = b'\x00tester' + b'\x00' * 27 + b'\x05'
        self.send_module_command(cmd, user_data)

        if not self.repeat_mode:
            self.append_module_log(f'[{mode_name}] 已发送注册指令，等待响应...')

    def palm_recognition(self):
        """手掌识别（支持重复执行）"""
        # 获取重复次数
        repeat_count = self.repeat_count_spin.value()

        if repeat_count > 1 and not self.repeat_mode:
            # 开始重复模式
            self.repeat_mode = True
            self.repeat_current = 0
            self.repeat_total = repeat_count
            self.repeat_command = self._do_palm_recognition
            self.repeat_success_count = 0  # 重置成功计数
            self.append_module_log(f'[重复识别] 开始执行，共 {repeat_count} 次')

        # 执行第一次
        self._do_palm_recognition()

    def _do_palm_recognition(self):
        """执行单次手掌识别"""
        if self.repeat_mode:
            self.repeat_current += 1
            self.repeat_reply_received = False  # 重置Reply接收标志

        # 根据项目模式选择命令ID
        if self.project_mode_actions['KDS'].isChecked():
            cmd = 0x81  # KDS模式
            mode_name = 'KDS手掌识别'
        else:
            cmd = 0x63  # DSM模式
            mode_name = 'DSM手掌识别'

        if self.repeat_mode:
            self.append_module_log(f'[{mode_name}] 第 {self.repeat_current}/{self.repeat_total} 次')

        # 发送手掌识别指令
        data = b'\x00\x05'
        self.send_module_command(cmd, data)

        if not self.repeat_mode:
            self.append_module_log(f'[{mode_name}] 已发送识别指令，等待响应...')

    def delete_user_by_id(self):
        """删除指定用户ID"""
        from PySide6.QtWidgets import QInputDialog

        # 弹出输入框让用户输入要删除的ID
        # getInt(parent, title, label, value, minValue, maxValue, step)
        user_id, ok = QInputDialog.getInt(
            self,
            '删除用户',
            '请输入要删除的用户ID:',
            1,      # value (默认值)
            0,      # minValue
            65535,  # maxValue
            1       # step
        )

        if not ok:
            return

        # 根据模式选择发送不同的指令
        if self.operation_mode_actions['palm'].isChecked():
            # 手掌模式：根据项目模式选择命令
            if self.project_mode_actions['KDS'].isChecked():
                cmd = 0x83  # KDS模式
                mode_name = 'KDS手掌模式'
            else:
                cmd = 0x65  # DSM模式
                mode_name = 'DSM手掌模式'
        else:
            # 人脸模式：发送0x20指令删除指定ID
            cmd = 0x20
            mode_name = '人脸模式'

        # 用户ID转为2字节大端序
        data = user_id.to_bytes(2, byteorder='big')
        self.send_module_command(cmd, data)
        self.append_module_log(f'[{mode_name}] 已发送删除用户ID {user_id} 的指令，等待响应...')

    def delete_all_users(self):
        """删除所有用户"""
        from PySide6.QtWidgets import QMessageBox

        # 确认对话框
        reply = QMessageBox.question(
            self,
            '确认删除',
            '确定要删除所有用户吗？此操作不可恢复！',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        # 根据模式选择发送不同的指令
        if self.operation_mode_actions['palm'].isChecked():
            # 手掌模式：根据项目模式选择命令
            if self.project_mode_actions['KDS'].isChecked():
                cmd = 0x82  # KDS模式
                mode_name = 'KDS手掌模式'
            else:
                cmd = 0x66  # DSM模式
                mode_name = 'DSM手掌模式'
        else:
            # 人脸模式：发送0x21指令删除所有用户
            cmd = 0x21
            mode_name = '人脸模式'

        # 删除所有用户没有数据部分
        self.send_module_command(cmd)
        self.append_module_log(f'[{mode_name}] 已发送删除所有用户的指令，等待响应...')

    def restart_module(self):
        """重启模组"""
        from PySide6.QtWidgets import QMessageBox

        # 确认对话框
        reply = QMessageBox.question(
            self,
            '确认重启',
            '确定要重启模组吗？',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply != QMessageBox.Yes:
            return

        # 发送重启指令：0x55
        self.send_module_command(0x55)
        self.append_module_log('[重启模组] 已发送重启指令，等待响应...')

    def standby_module(self):
        """待机模组"""
        # 停止OTA升级流程
        if self.ota_in_progress:
            self.append_module_log('[待机] 停止OTA升级流程...')
            self.finish_ota('中止（进入待机）')
            self.ota_current_packet = 0
            self.ota_retry_count = 0
            if self.ota_retry_timer:
                self.ota_retry_timer.stop()
                self.ota_retry_timer = None

        # 先恢复波特率到115200并停止当前所有操作
        if self.module_serial and self.module_serial.baudrate != 115200:
            try:
                self.append_module_log('[待机] 正在恢复波特率到115200...')
                # 设置待机恢复标志
                self.is_standby_restoring = True
                # 发送0x51指令设置波特率为115200
                self.send_module_command(0x51, b'\x01')  # 0x01 = 115200
                # 立即停止所有下载操作
                self.end_download_performance('中止（进入待机）')
                self.is_downloading = False
                if self.retry_timer:
                    self.retry_timer.stop()
                if self.command_timeout_timer:
                    self.command_timeout_timer.stop()
                # 切换串口波特率
                self.module_serial.baudrate = 115200
                self.append_module_log('[待机] 波特率已恢复到115200')
            except Exception as e:
                self.append_module_log(f'[警告] 恢复波特率失败: {e}', error=True)

        # 停止所有重复循环操作
        if self.repeat_mode:
            self.append_module_log('[待机] 停止重复执行...')
            self.repeat_mode = False
            self.repeat_current = 0
            self.repeat_total = 1
            self.repeat_command = None
            self.repeat_success_count = 0
            self.repeat_reply_received = False
            # 更新UI
            if hasattr(self, 'btn_stop_repeat'):
                self.btn_stop_repeat.setEnabled(False)

        # 延迟10ms后发送待机指令
        self.append_module_log('[待机] 准备发送待机指令...')
        QTimer.singleShot(10, self._send_standby_command)

    def _send_standby_command(self):
        """延迟发送待机指令"""
        self.send_module_command(0x10)
        self.append_module_log('[待机] 已发送待机指令，等待响应...')

    def enter_demo_mode(self):
        """进入演示模式"""
        # 发送进入演示指令：0xFE, data=0x01
        self.send_module_command(0xFE, b'\x01')
        self.append_module_log('[演示模式] 已发送进入演示模式指令，等待响应...')

    def exit_demo_mode(self):
        """退出演示模式"""
        # 发送退出演示指令：0xFE, data=0x00
        self.send_module_command(0xFE, b'\x00')
        self.append_module_log('[演示模式] 已发送退出演示模式指令，等待响应...')

    def enter_debug_mode(self):
        """进入Debug模式"""
        # 发送进入Debug指令：0xF0, data=0x01
        self.send_module_command(0xF0, b'\x01')
        self.append_module_log('[Debug模式] 已发送进入Debug模式指令，等待响应...')

    def exit_debug_mode(self):
        """退出Debug模式"""
        # 发送退出Debug指令：0xF0, data=0x00
        self.send_module_command(0xF0, b'\x00')
        self.append_module_log('[Debug模式] 已发送退出Debug模式指令，等待响应...')

    def send_get_image_size_command(self):
        """发送获取图片大小指令（在波特率切换后延迟调用）"""
        # 步骤2: 根据下载类型发送获取图片大小指令
        if self.download_type == 'raw':
            self.append_module_log('发送获取RAW图大小指令')
            print(f'[调试-0x51] 准备发送0x15指令')
            self.send_module_command(0x15)
            print(f'[调试-0x51] 已发送0x15指令')
        else:  # jpeg
            self.append_module_log('发送获取JPEG大小指令')
            print(f'[调试-0x51] 准备发送0x14指令')
            self.send_module_command(0x14)
            print(f'[调试-0x51] 已发送0x14指令')

    def set_download_polling(self, active):
        timer = getattr(self, 'module_response_timer', None)
        if timer is not None:
            ota_active = getattr(self, 'ota_in_progress', False) and self.ota_stage == 4
            timer.setInterval(5 if active or ota_active else 100)

    def begin_download_performance(self, image_type):
        if getattr(self, 'download_perf', None):
            self.end_download_performance('中止（开始新的下载）')
        self.download_perf = DownloadPerformance(image_type)

    def end_download_performance(self, outcome):
        self.set_download_polling(False)
        if self.retry_timer:
            self.retry_timer.stop()
        perf = getattr(self, 'download_perf', None)
        self.download_perf = None
        if perf:
            self.is_downloading = False
            if self.command_timeout_timer:
                self.command_timeout_timer.stop()
            self.pending_command = None
            if outcome.startswith('中止'):
                self.append_module_log(f'[下载] {outcome}', error=True)

    def download_image(self):
        """下载JPEG图片（完整流程）"""
        if (self.is_downloading or getattr(self, 'download_perf', None)
                or getattr(self, 'ota_in_progress', False)):
            self.append_module_log('[警告] 图片下载或OTA正在进行中，请勿重复操作', error=True)
            return

        # 重置所有下载状态，准备新的下载
        self.download_buffer = bytearray()
        self.download_offset = 0
        self.download_total_size = 0
        self.image1_size = 0
        self.image2_size = 0
        self.last_upload_command = None
        self.download_type = 'jpeg'  # 设置下载类型为JPEG
        if self.retry_timer:
            self.retry_timer.stop()
            self.retry_timer = None

        self.begin_download_performance('jpeg')
        self.append_module_log('[下载JPEG] 开始下载流程...')

        # 步骤1: 设置高速波特率 1500000
        self.append_module_log('设置波特率为 1500000')
        self.send_module_command(0x51, b'\x04')  # 0x04 = 1500000

    def download_raw_image(self):
        """下载RAW图片（完整流程）"""
        if (self.is_downloading or getattr(self, 'download_perf', None)
                or getattr(self, 'ota_in_progress', False)):
            self.append_module_log('[警告] 图片下载或OTA正在进行中，请勿重复操作', error=True)
            return

        # 重置所有下载状态，准备新的下载
        self.download_buffer = bytearray()
        self.download_offset = 0
        self.download_total_size = 0
        self.image1_size = 0
        self.image2_size = 0
        self.last_upload_command = None
        self.download_type = 'raw'  # 设置下载类型为RAW
        if self.retry_timer:
            self.retry_timer.stop()
            self.retry_timer = None

        self.begin_download_performance('raw')
        self.append_module_log('[下载RAW] 开始下载流程...')

        # 步骤1: 设置高速波特率 1500000
        self.append_module_log('设置波特率为 1500000')
        self.send_module_command(0x51, b'\x04')  # 0x04 = 1500000

    def start_image_download(self):
        """开始图片下载传输"""
        # 初始化下载状态
        self.download_buffer = bytearray()
        self.download_offset = 0
        self.download_total_size = self.image1_size + self.image2_size
        self.is_downloading = True

        if not getattr(self, 'download_perf', None):
            self.begin_download_performance(self.download_type)
        self.download_perf.mark('transfer')
        self.download_perf.baudrate = getattr(self.module_serial, 'baudrate', 1500000)
        self.set_download_polling(True)
        if self.download_total_size <= 0:
            self.is_downloading = False
            self.end_download_performance('中止（图片大小为零）')
            return

        self.append_module_log(f'开始下载图片数据，总大小 {self.download_total_size} 字节')

        # 发送第一个上传请求
        self.send_image_upload_request(0, min(4000, self.download_total_size))

    def send_image_upload_request(self, offset, size):
        """发送图片上传请求

        Args:
            offset: 偏移量（4字节大端序）
            size: 请求的数据大小（4字节大端序）
        """
        # 构建指令: EF AA 18 00 08 [offset 4字节] [size 4字节] [checksum]
        data = offset.to_bytes(4, byteorder='big') + size.to_bytes(4, byteorder='big')

        # 记录最后一次的上传指令，用于重传
        self.last_upload_command = (offset, size)
        perf = getattr(self, 'download_perf', None)
        if perf:
            now = time.perf_counter()
            if perf.handled_at is not None:
                perf.add('GUI处理至下次请求', now - perf.handled_at)
                perf.handled_at = None
            perf.request_started = now

        if not self.send_module_command(0x18, data):
            return

        # 停止之前的重传定时器
        if self.retry_timer:
            self.retry_timer.stop()

        # 启动超时重传定时器（3秒超时）
        self.retry_timer = QTimer()
        self.retry_timer.setSingleShot(True)
        self.retry_timer.timeout.connect(self.on_upload_timeout)
        self.retry_timer.start(3000)  # 3秒后如果还没收到数据就重传

    def on_upload_timeout(self):
        """上传请求超时，触发重传"""
        if self.last_upload_command and self.is_downloading:
            offset, size = self.last_upload_command
            if getattr(self, 'download_perf', None):
                self.download_perf.count('上传超时重传')
                self.download_perf.request_retried = True
            self.append_module_log(f'[超时重传] 未收到响应，重新发送请求，偏移量={offset}, 大小={size}', error=True)
            self.send_image_upload_request(offset, size)

    def retry_last_upload(self):
        """重传最后一次的上传指令"""
        if self.last_upload_command and self.is_downloading:
            offset, size = self.last_upload_command
            if getattr(self, 'download_perf', None):
                self.download_perf.count('设备错误重传')
                self.download_perf.request_retried = True
            self.append_module_log(f'[重传] 重新发送上传请求，偏移量={offset}, 大小={size}')
            self.send_image_upload_request(offset, size)
        else:
            print('[调试-重传] 没有可重传的指令或未在下载中')

    def handle_image_data(self, data_size, img_data, timing=None):
        """处理图片包；收包线程只传递时间戳，统计在主线程聚合。"""
        if not self.is_downloading:
            return
        now = time.perf_counter()
        perf = getattr(self, 'download_perf', None)
        if perf:
            perf.handled_at = now
            perf.packets += 1
            perf.bytes_received += data_size
            if timing:
                received, queued = timing
                perf.add('收包后校验/入队准备', queued - received)
                perf.add('队列等待GUI', now - queued)
                if perf.request_started is not None and received >= perf.request_started:
                    name = '重传请求至完整包' if perf.request_retried else '请求至完整包'
                    perf.add(name, received - perf.request_started)
            perf.request_retried = False

        if self.retry_timer:
            self.retry_timer.stop()
        before_append = time.perf_counter()
        self.download_buffer.extend(img_data)
        self.download_offset += data_size
        if perf:
            perf.add('缓冲拼接', time.perf_counter() - before_append)

        complete = self.download_offset >= self.download_total_size
        if complete or (perf and now - perf.last_progress >= 1.0):
            progress = self.download_offset / self.download_total_size * 100
            self.append_module_log(f'[下载进度] {self.download_offset}/{self.download_total_size} ({progress:.1f}%)')
            if perf:
                perf.last_progress = now

        if complete:
            if perf:
                perf.mark('received')
            self.set_download_polling(False)
            self.finish_image_download()
        else:
            remaining = self.download_total_size - self.download_offset
            self.send_image_upload_request(self.download_offset, min(4000, remaining))

    def finish_image_download(self):
        """完成图片下载，分离并保存两张图片，并显示到预览区"""
        self.append_module_log('[下载完成] 开始处理图片数据...')

        try:
            # 分离两张图片
            image1_data = bytes(self.download_buffer[:self.image1_size])
            image2_data = bytes(self.download_buffer[self.image1_size:self.image1_size + self.image2_size])
            perf = getattr(self, 'download_perf', None)
            if perf:
                perf.mark('split')

            # 生成时间戳
            timestamp_dt = datetime.now()  # datetime 对象
            timestamp_str = timestamp_dt.strftime('%Y%m%d_%H%M%S')  # 字符串格式用于文件名

            # 根据下载类型处理文件
            if self.download_type == 'raw':
                # 为RAW图创建独立的子文件夹
                display_name = f'RAW_{timestamp_str}'
                temp_dir = os.path.join(os.getcwd(), 'temp_downloaded', display_name)
                os.makedirs(temp_dir, exist_ok=True)

                # 保存RAW图到子文件夹
                temp_image1_path = os.path.join(temp_dir, f'raw_gray_{timestamp_str}.raw')
                temp_image2_path = os.path.join(temp_dir, f'raw_nv12_{timestamp_str}.raw')

                with open(temp_image1_path, 'wb') as f:
                    f.write(image1_data)
                with open(temp_image2_path, 'wb') as f:
                    f.write(image2_data)
                if perf:
                    perf.mark('saved')

                self.append_module_log(f'[下载完成] RAW图已下载（灰度图 + NV12）', success=True)

                # 创建一个简单的ImageData对象
                class DownloadedImageData:
                    def __init__(self, folder_path, ir_path, rgb_path, timestamp, display_name):
                        self.folder_path = folder_path
                        self.ir_path = ir_path
                        self.rgb_path = rgb_path
                        self.timestamp = timestamp
                        self.display_name = display_name

                new_image = DownloadedImageData(
                    folder_path=temp_dir,
                    ir_path=temp_image1_path,
                    rgb_path=temp_image2_path,
                    timestamp=timestamp_dt,
                    display_name=display_name
                )

                # 添加到历史记录
                self.image_history.insert(0, new_image)
                if len(self.image_history) > 100:
                    self.image_history.pop()

                self.current_image_data = new_image

                # 显示RAW图提示信息
                self.display_raw_placeholder()

                # 更新历史记录下拉框
                self.update_history_combo()

            else:  # jpeg
                # 为JPEG图创建独立的子文件夹
                display_name = f'JPEG_{timestamp_str}'
                temp_dir = os.path.join(os.getcwd(), 'temp_downloaded', display_name)
                os.makedirs(temp_dir, exist_ok=True)

                # 保存到子文件夹
                temp_image1_path = os.path.join(temp_dir, f'image1_{timestamp_str}.jpg')
                temp_image2_path = os.path.join(temp_dir, f'image2_{timestamp_str}.jpg')

                with open(temp_image1_path, 'wb') as f:
                    f.write(image1_data)
                with open(temp_image2_path, 'wb') as f:
                    f.write(image2_data)
                if perf:
                    perf.mark('saved')

                self.append_module_log(f'[下载完成] 图片已加载到预览区', success=True)

                # 创建一个简单的ImageData对象
                class DownloadedImageData:
                    def __init__(self, folder_path, ir_path, rgb_path, timestamp, display_name):
                        self.folder_path = folder_path
                        self.ir_path = ir_path
                        self.rgb_path = rgb_path
                        self.timestamp = timestamp
                        self.display_name = display_name

                new_image = DownloadedImageData(
                    folder_path=temp_dir,
                    ir_path=temp_image1_path,
                    rgb_path=temp_image2_path,
                    timestamp=timestamp_dt,
                    display_name=display_name
                )

                # 添加到历史记录并显示
                self.image_history.insert(0, new_image)
                if len(self.image_history) > 100:  # 限制历史记录数量
                    self.image_history.pop()

                self.current_image_data = new_image

                # 直接显示这两张图片，而不是扫描整个文件夹
                self.display_downloaded_images(temp_image1_path, temp_image2_path)

                # 更新历史记录下拉框
                self.update_history_combo()

            if perf:
                perf.mark('preview')
            self.set_download_polling(False)
            # 步骤4: 恢复标准波特率 115200
            self.append_module_log('恢复波特率为 115200')

            # 重置下载状态（在发送恢复波特率指令之前）
            self.is_downloading = False
            self.download_buffer = bytearray()
            # 注意：不要重置download_offset和download_total_size，用于判断是恢复波特率

            self.send_module_command(0x51, b'\x01')  # 0x01 = 115200
            if getattr(self, 'download_perf', None):
                self.download_perf.mark('restore_sent')

        except Exception as e:
            self.append_module_log(f'[错误] 处理图片失败: {e}', error=True)
            self.is_downloading = False
            self.end_download_performance('中止（图片处理失败）')

    def append_module_log(self, text, success=False, error=False):
        """添加模组日志"""
        # 保存到缓存
        module_log_cache.append((text, success, error))

        timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        log = f'[{timestamp}] {text}'

        # 设置颜色
        if success:
            color = '#00CC00'  # 亮绿色
        elif error:
            color = '#FF0000'  # 亮红色
        else:
            color = '#333333' if not self.dark_mode else '#e0e0e0'  # 默认颜色

        # 设置背景色
        bg_color = '#1e1e1e' if self.dark_mode else 'white'

        # 追加到文本框
        cursor = self.module_response_text.textCursor()
        cursor.movePosition(QTextCursor.End)

        format = cursor.charFormat()
        format.setForeground(QColor(color))
        format.setBackground(QColor(bg_color))  # 设置背景色
        cursor.setCharFormat(format)
        cursor.insertText(log + '\n')

        self.module_response_text.moveCursor(QTextCursor.End)

    # === 自动执行序列相关方法 ===
    def add_sequence_operation(self):
        """添加操作到序列"""
        operation = self.sequence_operation_combo.currentText()
        self.sequence_list.append(operation)
        self.sequence_list_widget.addItem(operation)
        self.append_module_log(f'[序列] 已添加操作: {operation}')

    def move_sequence_up(self):
        """上移序列中的操作"""
        current_row = self.sequence_list_widget.currentRow()
        if current_row > 0:
            # 交换列表中的元素
            self.sequence_list[current_row], self.sequence_list[current_row - 1] = \
                self.sequence_list[current_row - 1], self.sequence_list[current_row]

            # 更新显示
            item = self.sequence_list_widget.takeItem(current_row)
            self.sequence_list_widget.insertItem(current_row - 1, item)
            self.sequence_list_widget.setCurrentRow(current_row - 1)

    def move_sequence_down(self):
        """下移序列中的操作"""
        current_row = self.sequence_list_widget.currentRow()
        if current_row < self.sequence_list_widget.count() - 1 and current_row >= 0:
            # 交换列表中的元素
            self.sequence_list[current_row], self.sequence_list[current_row + 1] = \
                self.sequence_list[current_row + 1], self.sequence_list[current_row]

            # 更新显示
            item = self.sequence_list_widget.takeItem(current_row)
            self.sequence_list_widget.insertItem(current_row + 1, item)
            self.sequence_list_widget.setCurrentRow(current_row + 1)

    def delete_sequence_operation(self):
        """删除序列中的操作"""
        current_row = self.sequence_list_widget.currentRow()
        if current_row >= 0:
            operation = self.sequence_list[current_row]
            del self.sequence_list[current_row]
            self.sequence_list_widget.takeItem(current_row)
            self.append_module_log(f'[序列] 已删除操作: {operation}')

    def start_sequence(self):
        """启动自动执行序列"""
        if not self.sequence_list:
            self.append_module_log('[序列] 序列为空，请先添加操作', error=True)
            return

        if not self.module_connected:
            self.append_module_log('[序列] 请先连接模组', error=True)
            return

        self.sequence_running = True
        self.sequence_index = 0
        self.sequence_current_loop = 1
        self.sequence_total_loops = self.sequence_loop_spin.value()
        self.btn_start_sequence.setEnabled(False)
        self.btn_stop_sequence.setEnabled(True)

        self.append_module_log(f'[序列] 开始执行序列，共 {len(self.sequence_list)} 个操作，循环 {self.sequence_total_loops} 次')
        self.execute_next_sequence_step()

    def stop_sequence(self):
        """停止自动执行序列"""
        if self.sequence_running:
            self.sequence_running = False
            self.sequence_wait_response = None
            self.btn_start_sequence.setEnabled(True)
            self.btn_stop_sequence.setEnabled(False)
            self.append_module_log(f'[序列] 已停止执行序列（已完成 {self.sequence_current_loop - 1}/{self.sequence_total_loops} 次循环）', error=True)

    def execute_next_sequence_step(self):
        """执行序列中的下一步操作"""
        if not self.sequence_running:
            return

        # 检查当前序列是否执行完成
        if self.sequence_index >= len(self.sequence_list):
            # 当前循环完成，检查是否需要继续下一轮循环
            if self.sequence_current_loop < self.sequence_total_loops:
                self.sequence_current_loop += 1
                self.sequence_index = 0
                self.append_module_log(f'[序列] 第 {self.sequence_current_loop - 1} 轮循环完成，开始第 {self.sequence_current_loop} 轮循环', success=True)
                # 延迟200ms后开始下一轮循环
                QTimer.singleShot(200, self.execute_next_sequence_step)
                return
            else:
                # 所有循环完成
                self.sequence_running = False
                self.btn_start_sequence.setEnabled(True)
                self.btn_stop_sequence.setEnabled(False)
                self.append_module_log(f'[序列] 序列执行完成，共完成 {self.sequence_total_loops} 轮循环', success=True)
                return

        operation = self.sequence_list[self.sequence_index]
        self.append_module_log(f'[序列] 第 {self.sequence_current_loop}/{self.sequence_total_loops} 轮，执行第 {self.sequence_index + 1}/{len(self.sequence_list)} 步: {operation}')

        # 设置等待响应标记
        self.sequence_wait_response = operation

        # 执行对应的操作
        if operation == '人脸注册':
            self.register_single_frame()
        elif operation == '手掌注册':
            self.palm_register()
        elif operation == '识别D':
            self.face_recognition()
        elif operation == '识别K':
            self.palm_recognition()
        elif operation == '下载JPEG':
            self.download_image()
        elif operation == '下载RAW':
            self.download_raw_image()
        elif operation == '获取版本号':
            self.get_module_version()
        elif operation == '获取所有用户ID':
            self.get_all_user_ids()
        elif operation == '删除指定用户ID':
            self.delete_user_by_id()
        elif operation == '删除所有用户':
            self.delete_all_users()
        elif operation == '重启模组':
            self.restart_module()
        elif operation == '待机':
            self.standby_module()
        elif operation == '进入演示':
            self.enter_demo_mode()
        elif operation == '退出演示':
            self.exit_demo_mode()
        elif operation == '进入Debug':
            self.enter_debug_mode()
        elif operation == '退出Debug':
            self.exit_debug_mode()

        self.sequence_index += 1

    def check_sequence_next(self, operation_type):
        """检查是否需要执行序列的下一步

        Args:
            operation_type: 完成的操作类型（'注册', '识别', '下载JPEG', '下载RAW', '待机'）
        """
        if self.sequence_running and self.sequence_wait_response == operation_type:
            self.sequence_wait_response = None
            # 延迟100ms后执行下一步，确保当前操作完全结束
            QTimer.singleShot(100, self.execute_next_sequence_step)

    # ==============================
    # OTA升级相关方法
    # ==============================

    def schedule_ota_step(self, delay, stage, callback):
        """使用可取消的延迟任务，避免中止后仍发送旧固件。"""
        if getattr(self, 'ota_step_timer', None):
            self.ota_step_timer.stop()
            self.ota_step_timer.deleteLater()
        self.ota_step_timer = QTimer(self)
        self.ota_step_timer.setSingleShot(True)
        self.ota_step_timer.setTimerType(Qt.PreciseTimer)
        self.ota_step_timer.timeout.connect(
            lambda: callback() if self.ota_in_progress and self.ota_stage == stage else None
        )
        self.ota_step_timer.start(delay)

    def finish_ota(self, outcome):
        self.ota_in_progress = False
        self.ota_stage = 0
        self.ota_waiting_ack = False
        for name in ('ota_step_timer', 'ota_retry_timer'):
            timer = getattr(self, name, None)
            if timer:
                timer.stop()
                timer.deleteLater()
                setattr(self, name, None)
        if self.pending_command and self.pending_command[0] in (0x51, 0x40, 0x43, 0x44):
            if self.command_timeout_timer:
                self.command_timeout_timer.stop()
            self.pending_command = None
        self.set_download_polling(self.is_downloading)
        perf = getattr(self, 'ota_perf', None)
        self.ota_perf = None
        if perf and outcome.startswith('中止'):
            self.append_module_log(f'[OTA] {outcome}', error=True)

    def record_ota_ack_timing(self, timing):
        perf = getattr(self, 'ota_perf', None)
        if not (perf and self.ota_in_progress and self.ota_stage == 4
                and self.ota_waiting_ack):
            return
        now = time.perf_counter()
        received, queued = timing
        if perf.request_started is not None and received >= perf.request_started:
            label = '重传发送至完整ACK' if perf.request_retried else '发送至完整ACK'
            perf.add(label, received - perf.request_started)
            perf.add('ACK校验/入队准备', queued - received)
            perf.add('ACK等待GUI', now - queued)

    def start_ota_upgrade(self):
        """启动OTA升级流程"""
        if self.ota_in_progress or self.is_downloading or getattr(self, 'download_perf', None):
            QMessageBox.warning(self, '提示', 'OTA升级或图片下载正在进行中，请勿重复操作')
            return

        # 创建OTA配置对话框
        dialog = QDialog(self)
        dialog.setWindowTitle('OTA升级配置')
        dialog.setMinimumWidth(500)

        layout = QVBoxLayout(dialog)

        # 选择OTA固件包
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel('固件包:'))
        self.ota_file_input = QLineEdit()
        self.ota_file_input.setReadOnly(True)
        file_layout.addWidget(self.ota_file_input)

        btn_browse = QPushButton('📂 浏览')
        btn_browse.clicked.connect(lambda: self.browse_ota_file(dialog))
        file_layout.addWidget(btn_browse)
        layout.addLayout(file_layout)

        # 波特率设置
        baudrate_layout = QHBoxLayout()
        baudrate_layout.addWidget(QLabel('OTA波特率:'))
        self.ota_baudrate_combo = QComboBox()
        self.ota_baudrate_combo.addItems(['115200', '230400', '460800', '1500000'])
        self.ota_baudrate_combo.setCurrentText('1500000')
        baudrate_layout.addWidget(self.ota_baudrate_combo)
        baudrate_layout.addStretch()
        layout.addLayout(baudrate_layout)

        # 包大小设置
        packet_layout = QHBoxLayout()
        packet_layout.addWidget(QLabel('单包大小(字节):'))
        self.ota_packet_size_spin = QSpinBox()
        self.ota_packet_size_spin.setMinimum(512)
        self.ota_packet_size_spin.setMaximum(8192)
        self.ota_packet_size_spin.setValue(4000)
        self.ota_packet_size_spin.setSingleStep(512)
        packet_layout.addWidget(self.ota_packet_size_spin)
        packet_layout.addStretch()
        layout.addLayout(packet_layout)

        # 提示信息
        info_label = QLabel('💡 提示：\n1. 请确保固件包文件完整\n2. 升级过程中请勿断开连接\n3. 升级时间约需1-5分钟')
        info_label.setStyleSheet('background-color: #e3f2fd; padding: 10px; border-radius: 4px;')
        layout.addWidget(info_label)

        # 按钮
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        btn_cancel = QPushButton('取消')
        btn_cancel.clicked.connect(dialog.reject)
        button_layout.addWidget(btn_cancel)

        btn_start = QPushButton('开始升级')
        btn_start.setStyleSheet('QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 8px 16px; }')
        btn_start.clicked.connect(dialog.accept)
        button_layout.addWidget(btn_start)

        layout.addLayout(button_layout)

        # 显示对话框
        if dialog.exec() == QDialog.Accepted:
            # 验证参数
            if not self.ota_file_input.text():
                QMessageBox.warning(self, '提示', '请选择固件包文件')
                return

            # 开始OTA升级
            self.execute_ota_upgrade()

    def browse_ota_file(self, parent):
        """浏览选择OTA固件包"""
        file_path, _ = QFileDialog.getOpenFileName(
            parent,
            '选择OTA固件包',
            '',
            'Bin文件 (*.bin);;所有文件 (*.*)'
        )
        if file_path:
            self.ota_file_input.setText(file_path)

    def execute_ota_upgrade(self):
        """执行OTA升级"""
        if self.ota_in_progress or self.is_downloading or getattr(self, 'download_perf', None):
            self.append_module_log('[OTA] 图片下载或OTA正在进行中，无法开始', error=True)
            return
        self.ota_perf = OtaPerformance()
        try:
            # 读取固件包
            self.ota_file_path = self.ota_file_input.text()
            with open(self.ota_file_path, 'rb') as f:
                self.ota_file_data = f.read()

            file_size = len(self.ota_file_data)
            self.ota_perf.mark('loaded')
            if not file_size:
                raise ValueError('固件包为空')
            self.append_module_log(f'[OTA] 固件包加载成功，大小: {file_size} 字节')

            # 获取配置
            self.ota_packet_size = self.ota_packet_size_spin.value()
            self.ota_total_packets = (file_size + self.ota_packet_size - 1) // self.ota_packet_size

            self.append_module_log(f'[OTA] 配置: 单包大小={self.ota_packet_size}字节, 总包数={self.ota_total_packets}')

            # 开始OTA流程
            self.ota_in_progress = True
            self.ota_stage = 1
            self.ota_current_packet = 0

            # 步骤1: 设置波特率（根据用户选择）
            baudrate_str = self.ota_baudrate_combo.currentText()
            baudrate_map = {
                '115200': (0x01, 115200),
                '230400': (0x02, 230400),
                '460800': (0x03, 460800),
                '1500000': (0x04, 1500000),
            }
            baudrate_code, baudrate_value = baudrate_map.get(baudrate_str, (0x04, 1500000))

            self.append_module_log(f'[OTA] 步骤1: 设置波特率为 {baudrate_value}')
            # 保存目标波特率，供0x51响应处理使用
            self.ota_target_baudrate = baudrate_value
            self.ota_perf.baudrate = baudrate_value
            self.ota_retry_count = 0
            self.ota_waiting_ack = False
            print(f'[OTA调试] 发送0x51指令，波特率代码: 0x{baudrate_code:02X}, 目标波特率: {baudrate_value}')
            if not self.send_module_command(0x51, bytes([baudrate_code])):
                self.finish_ota('中止（波特率指令发送失败）')

        except Exception as e:
            self.finish_ota('中止（OTA启动失败）')
            QMessageBox.critical(self, '错误', f'OTA升级失败:\n{e}')

    def switch_baudrate_for_ota(self, baudrate):
        """切换串口波特率（用于OTA）"""
        try:
            if self.module_serial:
                self.module_serial.baudrate = baudrate
                self.append_module_log(f'[OTA] 串口波特率已切换到 {baudrate}')

                # 步骤2: 发送0x40进入OTA状态
                self.ota_stage = 2
                self.schedule_ota_step(100, 2, self.enter_ota_mode)
        except Exception as e:
            self.append_module_log(f'[OTA] 切换波特率失败: {e}', error=True)
            self.finish_ota('中止（切换波特率失败）')

    def enter_ota_mode(self):
        """进入OTA模式"""
        if not self.ota_in_progress or self.ota_stage != 2:
            return
        self.append_module_log('[OTA] 步骤2: 进入OTA状态')
        # 发送0x40指令进入OTA状态
        print(f'[OTA调试] 发送0x40指令: EF AA 40 00 00 40')
        if not self.send_module_command(0x40):
            self.finish_ota('中止（进入OTA指令发送失败）')

    def send_ota_header(self):
        """发送OTA header"""
        if not self.ota_in_progress or self.ota_stage != 3:
            return
        try:
            import hashlib

            self.append_module_log('[OTA] 步骤3: 发送OTA header')

            file_size = len(self.ota_file_data)
            packet_count = self.ota_total_packets
            packet_size = self.ota_packet_size

            # 计算MD5
            md5_hash = hashlib.md5(self.ota_file_data).hexdigest()
            # MD5要转换成32字节的ASCII字符串，而不是16字节的二进制
            md5_bytes = md5_hash.encode('ascii')  # 32字节的ASCII字符串

            self.append_module_log(f'[OTA] 文件大小: {file_size}, 包数量: {packet_count}, 单包大小: {packet_size}')
            self.append_module_log(f'[OTA] MD5: {md5_hash}')

            # 打印MD5详细信息
            print(f'[OTA调试] MD5字符串: {md5_hash}')
            print(f'[OTA调试] MD5字节数: {len(md5_bytes)}')
            md5_hex = ' '.join([f'{b:02X}' for b in md5_bytes])
            print(f'[OTA调试] MD5十六进制(ASCII): {md5_hex}')

            # 构建header数据
            # 包大小（4字节，大端序）
            data = file_size.to_bytes(4, byteorder='big')
            print(f'[OTA调试] 文件大小(4字节): {" ".join([f"{b:02X}" for b in data])} = {file_size}')

            # 分包数量（4字节，大端序）
            data += packet_count.to_bytes(4, byteorder='big')
            print(f'[OTA调试] 包数量(4字节): {" ".join([f"{b:02X}" for b in packet_count.to_bytes(4, byteorder="big")])} = {packet_count}')

            # 单包大小（2字节，大端序）
            data += packet_size.to_bytes(2, byteorder='big')
            print(f'[OTA调试] 单包大小(2字节): {" ".join([f"{b:02X}" for b in packet_size.to_bytes(2, byteorder="big")])} = {packet_size}')

            # 文件校验位（32字节，MD5的ASCII字符串形式）
            data += md5_bytes

            # 打印完整header
            hex_data = ' '.join([f'{b:02X}' for b in data])
            print(f'[OTA调试] 完整header数据(42字节): {hex_data}')
            print(f'[OTA调试] Header数据长度: {len(data)}字节')

            # 发送0x43指令
            if not self.send_module_command(0x43, data):
                self.finish_ota('中止（header发送失败）')

        except Exception as e:
            self.append_module_log(f'[OTA] 发送header失败: {e}', error=True)
            self.finish_ota('中止（header发送失败）')

    def send_ota_packet(self):
        """发送OTA固件包"""
        try:
            if (not self.ota_in_progress or self.ota_stage != 4
                    or self.ota_current_packet >= self.ota_total_packets or self.ota_waiting_ack):
                return
            perf = getattr(self, 'ota_perf', None)
            build_started = time.perf_counter()
            if perf:
                if 'transfer' not in perf.stages:
                    perf.mark('transfer')
                if perf.handled_at is not None:
                    perf.add('ACK处理至下一包（含间隔）', build_started - perf.handled_at)
                    perf.handled_at = None

            # 计算当前包的偏移和大小
            offset = self.ota_current_packet * self.ota_packet_size
            remaining = len(self.ota_file_data) - offset
            current_packet_size = min(self.ota_packet_size, remaining)

            # 提取当前包的数据
            packet_data = self.ota_file_data[offset:offset + current_packet_size]

            # 构建44指令的数据部分
            # 包序（2字节，大端序）
            data = self.ota_current_packet.to_bytes(2, byteorder='big')
            # 包长（2字节，大端序）
            data += current_packet_size.to_bytes(2, byteorder='big')
            # 包内容
            data += packet_data

            if perf:
                perf.add('固件分包构造', time.perf_counter() - build_started)
                perf.request_started = time.perf_counter()
            self.ota_waiting_ack = True
            # 发送失败后不启动本包超时重传
            if not self.send_module_command(0x44, data):
                self.finish_ota('中止（固件包发送失败）')
                return

            # 停止之前的超时定时器
            if self.ota_retry_timer:
                self.ota_retry_timer.stop()

            # 启动超时重传定时器（3秒超时）
            self.ota_retry_timer = QTimer()
            self.ota_retry_timer.setSingleShot(True)
            self.ota_retry_timer.timeout.connect(self.on_ota_packet_timeout)
            self.ota_retry_timer.start(3000)  # 3秒超时

        except Exception as e:
            self.append_module_log(f'[OTA] 发送固件包失败: {e}', error=True)
            self.finish_ota('中止（固件包发送异常）')

    def on_ota_packet_timeout(self):
        """OTA固件包传输超时处理"""
        if not self.ota_in_progress or self.ota_stage != 4:
            return

        self.ota_waiting_ack = False
        if self.ota_retry_count < 3:
            self.ota_retry_count += 1
            if getattr(self, 'ota_perf', None):
                self.ota_perf.count('ACK超时重传')
                self.ota_perf.request_retried = True
            self.append_module_log(f'[OTA] 包序号{self.ota_current_packet}超时，第{self.ota_retry_count}次重传...', error=True)
            self.send_ota_packet()
        else:
            self.append_module_log(f'[OTA] 包序号{self.ota_current_packet}重传3次后仍超时，终止OTA升级', error=True)
            self.finish_ota('中止（ACK超时重试耗尽）')

    def closeEvent(self, event):
        """关闭事件"""
        # 先释放日志串口，避免daemon线程依赖进程退出强制关闭句柄。
        if self.is_log_port_active():
            self.disconnect_serial()

        # 断开模组串口
        if self.module_connected:
            self.disconnect_module()

        # 停止文件监控
        if self.observer:
            self.observer.stop()
            self.observer.join()

        if self.log_marker_window:
            self.log_marker_window.setParent(None)
            self.log_marker_window.deleteLater()
            self.log_marker_window = None

        event.accept()


# ==============================
# 主函数
# ==============================

def main():
    app = QApplication(sys.argv)
    app.setApplicationName('capLG')
    app.setApplicationVersion('1.0.0.8')
    icon_name = 'ChatGPT Image 2026年9月16日 00_28_09.png'
    if getattr(sys, 'frozen', False):
        icon_path = os.path.join(sys._MEIPASS, 'resources', icon_name)
    else:
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), icon_name)
    app.setWindowIcon(QIcon(icon_path))

    # 设置应用样式
    app.setStyle('Fusion')

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
