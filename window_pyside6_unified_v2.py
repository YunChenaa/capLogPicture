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
import serial.tools.list_ports
import re

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTextEdit, QComboBox, QLineEdit, QCheckBox,
    QRadioButton, QButtonGroup, QTabWidget, QMessageBox, QFileDialog,
    QDialog, QGroupBox, QFrame, QSplitter, QScrollArea, QSpinBox, QInputDialog,
    QGridLayout, QMenu
)
from PySide6.QtCore import Qt, Signal, QTimer, QThread, QObject, QSize
from PySide6.QtGui import QFont, QTextCursor, QPalette, QColor, QPixmap, QImage, QTextDocument, QShortcut, QKeySequence, QTransform

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
module_log_cache = []  # 模组响应日志缓存（格式：[(text, success, error), ...]）

# ==============================
# 日志提取工具函数
# ==============================

def extract_logs(strategy, param=None):
    """根据策略从 full_log_cache 中提取日志"""
    if not full_log_cache:
        return []

    if strategy == 'all':
        return list(full_log_cache)
    elif strategy == 'recent_n':
        n = int(param) if param else 100
        return list(full_log_cache[-n:]) if n > 0 else []
    elif strategy == 'from_keyword':
        keyword = str(param) if param else ''
        if not keyword:
            return []
        for i in range(len(full_log_cache) - 1, -1, -1):
            if keyword in full_log_cache[i]:
                return list(full_log_cache[i:])
        return []
    else:
        return list(full_log_cache)

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

    def __init__(self, image_path, parent=None):
        super().__init__(parent)
        self.image_path = image_path
        self.setWindowTitle(f'图片查看器 - {os.path.basename(image_path)}')
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

        # 加载原始图片
        print(f'[调试] 正在加载图片: {image_path}')
        print(f'[调试] 文件是否存在: {os.path.exists(image_path)}')

        self.original_pixmap = QPixmap(image_path)
        print(f'[调试] QPixmap 加载结果: isNull={self.original_pixmap.isNull()}')
        print(f'[调试] 原始图片尺寸: {self.original_pixmap.width()} x {self.original_pixmap.height()}')

        if self.original_pixmap.isNull():
            QMessageBox.warning(self, '错误', '无法加载图片')
            self.reject()
            return

        self.setup_ui()

        # 延迟执行适应窗口，等待窗口完全显示后
        print(f'[调试] 将在100ms后执行 zoom_fit')
        QTimer.singleShot(100, self.zoom_fit)

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
        img_info = f'{self.original_pixmap.width()} × {self.original_pixmap.height()} px'
        file_size = os.path.getsize(self.image_path) / 1024
        info_text = f'{img_info} | {file_size:.1f} KB'
        self.info_label = QLabel(info_text)
        self.info_label.setStyleSheet('color: #666666; font-size: 9pt;')
        toolbar.addWidget(self.info_label)

        layout.addLayout(toolbar)

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

        layout.addWidget(self.scroll_area)

        # 矩形框绘制区域
        rect_group = QGroupBox('📦 绘制矩形框')
        rect_main_layout = QVBoxLayout(rect_group)

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

        layout.addWidget(rect_group)

        # 底部按钮
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        btn_close = QPushButton('关闭')
        btn_close.clicked.connect(self.accept)
        button_layout.addWidget(btn_close)

        layout.addLayout(button_layout)

    def zoom_in(self):
        """放大"""
        print(f'[调试] zoom_in 被调用')
        self.zoom_scale *= 1.25
        self.update_image()

    def zoom_out(self):
        """缩小"""
        print(f'[调试] zoom_out 被调用')
        self.zoom_scale /= 1.25
        if self.zoom_scale < 0.05:
            self.zoom_scale = 0.05
        self.update_image()

    def zoom_fit(self):
        """适应窗口"""
        print(f'[调试] zoom_fit 被调用')
        available_width = self.scroll_area.viewport().width() - 20
        available_height = self.scroll_area.viewport().height() - 20
        print(f'[调试] 可用区域大小: {available_width} x {available_height}')
        print(f'[调试] 原始图片大小: {self.original_pixmap.width()} x {self.original_pixmap.height()}')

        # 计算缩放比例
        scale_w = available_width / self.original_pixmap.width()
        scale_h = available_height / self.original_pixmap.height()
        self.zoom_scale = min(scale_w, scale_h, 1.0)
        print(f'[调试] 计算的缩放比例: scale_w={scale_w:.3f}, scale_h={scale_h:.3f}, 最终={self.zoom_scale:.3f}')

        self.update_image()

    def zoom_actual(self):
        """实际大小"""
        print(f'[调试] zoom_actual 被调用')
        self.zoom_scale = 1.0
        self.update_image()

    def rotate_left(self):
        """逆时针旋转"""
        print(f'[调试] rotate_left 被调用')
        self.rotation = (self.rotation - 90) % 360
        print(f'[调试] 旋转角度变为: {self.rotation}')
        self.update_image()

    def rotate_right(self):
        """顺时针旋转"""
        print(f'[调试] rotate_right 被调用')
        self.rotation = (self.rotation + 90) % 360
        print(f'[调试] 旋转角度变为: {self.rotation}')
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
            # 移除方括号
            text = text.replace('[', '').replace(']', '')
            # 按逗号分割
            parts = [p.strip() for p in text.split(',')]

            if len(parts) != 4:
                QMessageBox.warning(self, '解析错误', f'需要4个数字，但找到了 {len(parts)} 个\n格式: [left, top, right, bottom]')
                return

            # 转换为整数
            left = int(parts[0])
            top = int(parts[1])
            right = int(parts[2])
            bottom = int(parts[3])

            # 填充到详细输入框
            self.rect_left_input.setText(str(left))
            self.rect_top_input.setText(str(top))
            self.rect_right_input.setText(str(right))
            self.rect_bottom_input.setText(str(bottom))

            print(f'[调试] 智能解析成功: left={left}, top={top}, right={right}, bottom={bottom}')

            # 自动绘制
            self.draw_rectangle()

        except ValueError as e:
            QMessageBox.warning(self, '解析错误', f'无法解析数字：{str(e)}\n请确保输入格式正确，例如: [50, 100, 300, 400]')

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
        self.btn_add_shortcut.setToolTip('添加快捷命令')
        self.btn_add_shortcut.setStyleSheet('QPushButton { background-color: #FF9800; color: white; font-weight: bold; padding: 8px; }')
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
        self.btn_clear_log.clicked.connect(lambda: self.output_text.clear())
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

def serial_reader(port, baudrate, error_queue, connected_event=None, log_queue=None, send_queue=None):
    """串口读取线程函数"""
    ser = None
    try:
        available_ports = [p.device for p in serial.tools.list_ports.comports()]
        if port not in available_ports:
            raise serial.SerialException(f'串口 {port} 不存在或未连接')

        ser = serial.Serial(port=port, baudrate=baudrate, timeout=1, write_timeout=0.5)
        print('串口打开成功:', port, baudrate)

        if connected_event is not None:
            connected_event.set()

        while connected_event.is_set():  # 检查连接状态
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
                print(log)
                log_cache.append(log)
                full_log_cache.append(log)
                if log_queue is not None:
                    log_queue.put(log)
            else:
                time.sleep(0.01)

    except Exception as e:
        print('串口异常:', e)
        error_queue.put(f'串口 {port} 已断开或无法访问：\n{e}')
    finally:
        # 确保串口被关闭
        if ser is not None and ser.is_open:
            try:
                ser.close()
                print(f'串口 {port} 已关闭')
            except Exception as e:
                print(f'关闭串口失败: {e}')

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
# 设置对话框
# ==============================

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

        self.setWindowTitle('串口日志采集工具')
        self.resize(1400, 800)

        # 串口相关
        self.port = None
        self.baudrate = None
        self.error_queue = queue.Queue()
        self.log_queue = queue.Queue()
        self.send_queue = queue.Queue()
        self.connected_event = threading.Event()
        self.serial_thread = None

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
        self.last_upload_command = b''  # 最后一次上传指令（用于重传）
        self.retry_timer = None  # 重传定时器
        self.download_type = 'jpeg'  # 下载类型：'jpeg' 或 'raw'
        self.raw_mode = 'Y+IR'  # RAW图模式：'Y+RGB'(40%+60%) 或 'Y+IR'(50%+50%)

        # 重复执行相关
        self.repeat_mode = False  # 是否处于重复执行模式
        self.repeat_current = 0  # 当前执行次数
        self.repeat_total = 1  # 总共要执行的次数
        self.repeat_command = None  # 要重复执行的命令函数
        self.repeat_success_count = 0  # 成功次数统计
        self.repeat_reply_received = False  # 当前操作是否已收到Reply（防止多次Reply触发）

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

        btn_refresh = QPushButton('🔄')
        btn_refresh.setMaximumWidth(30)
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

        serial_config_layout.addStretch()

        left_layout.addLayout(serial_config_layout)

        # 工具栏（紧凑）
        toolbar_layout = QHBoxLayout()

        self.btn_settings = QPushButton('⚙️ 设置')
        self.btn_settings.clicked.connect(self.open_settings_dialog)
        toolbar_layout.addWidget(self.btn_settings)

        self.btn_monitor = QPushButton('👁️ 启用监控')
        self.btn_monitor.clicked.connect(self.toggle_monitoring)
        toolbar_layout.addWidget(self.btn_monitor)

        btn_save_log = QPushButton('💾 保存日志')
        btn_save_log.clicked.connect(self.save_log_only)
        toolbar_layout.addWidget(btn_save_log)

        self.btn_open_output = QPushButton('📂 打开保存目录')
        self.btn_open_output.clicked.connect(self.open_output_directory)
        toolbar_layout.addWidget(self.btn_open_output)

        self.btn_theme = QPushButton('🌙 深色模式')
        self.btn_theme.clicked.connect(self.toggle_theme)
        toolbar_layout.addWidget(self.btn_theme)

        toolbar_layout.addStretch()

        left_layout.addLayout(toolbar_layout)

        # 刷新串口列表
        self.refresh_ports()

        # 状态栏
        status_layout = QHBoxLayout()
        self.status_label = QLabel('● 未连接')
        self.status_label.setStyleSheet('font-size: 11pt; font-weight: bold; color: #999999;')
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()

        self.monitor_status_label = QLabel('监控: 未启用')
        self.monitor_status_label.setStyleSheet('font-size: 9pt; color: #666666;')
        status_layout.addWidget(self.monitor_status_label)

        left_layout.addLayout(status_layout)

        # 日志显示区
        log_group = QGroupBox('📋 串口日志')
        log_layout = QVBoxLayout(log_group)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont('Consolas', 9))
        log_layout.addWidget(self.log_text)

        # 嵌入式搜索栏（默认隐藏）
        self.search_bar = QWidget()
        search_bar_layout = QHBoxLayout(self.search_bar)
        search_bar_layout.setContentsMargins(5, 5, 5, 5)
        self.search_bar.setStyleSheet('QWidget { background-color: #f0f0f0; border: 1px solid #ccc; }')

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

        log_layout.addWidget(self.search_bar)
        self.search_bar.setVisible(False)

        # 搜索相关变量
        self.search_matches = []
        self.current_match_index = -1

        # 日志工具栏
        log_tool_layout = QHBoxLayout()

        btn_clear = QPushButton('🗑️ 清空')
        btn_clear.clicked.connect(self.log_text.clear)
        log_tool_layout.addWidget(btn_clear)

        log_tool_layout.addStretch()

        self.log_count_label = QLabel('日志行数: 0')
        self.log_count_label.setStyleSheet('color: #666666; font-size: 9pt;')
        log_tool_layout.addWidget(self.log_count_label)

        log_layout.addLayout(log_tool_layout)

        left_layout.addWidget(log_group)

        # 快速发送区（可折叠）
        send_outer_layout = QVBoxLayout()

        # 标题栏和折叠按钮
        send_header_layout = QHBoxLayout()
        self.btn_toggle_send = QPushButton('▼ 📤 快速发送')
        self.btn_toggle_send.clicked.connect(self.toggle_send_group)
        send_header_layout.addWidget(self.btn_toggle_send)
        send_header_layout.addStretch()
        send_outer_layout.addLayout(send_header_layout)

        # 可折叠的快速发送内容区域
        self.send_content_widget = QWidget()
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
        scroll_area.setMinimumHeight(300)

        self.image_container = QWidget()
        self.image_layout = QVBoxLayout(self.image_container)
        self.image_layout.setAlignment(Qt.AlignTop)

        self.no_image_label = QLabel('暂无图片\n\n启用图片监控后\n这里会显示检测到的图片')
        self.no_image_label.setAlignment(Qt.AlignCenter)
        self.no_image_label.setStyleSheet('color: #999999; font-size: 12pt; padding: 50px;')
        self.image_layout.addWidget(self.no_image_label)

        scroll_area.setWidget(self.image_container)
        preview_layout.addWidget(scroll_area)

        # 图片信息
        self.image_info_label = QLabel('路径: 无')
        self.image_info_label.setStyleSheet('color: #666666; font-size: 9pt; padding: 5px;')
        self.image_info_label.setWordWrap(True)
        preview_layout.addWidget(self.image_info_label)

        right_layout.addWidget(preview_group)

        # === 模组控制区域 ===
        module_outer_layout = QVBoxLayout()

        # 标题栏和折叠按钮
        module_header_layout = QHBoxLayout()
        self.btn_toggle_module = QPushButton('▼ 🔧 模组控制')
        self.btn_toggle_module.clicked.connect(self.toggle_module_group)
        module_header_layout.addWidget(self.btn_toggle_module)
        module_header_layout.addStretch()
        module_outer_layout.addLayout(module_header_layout)

        # 可折叠的模组控制内容区域
        self.module_content_widget = QWidget()
        module_group = QGroupBox()
        module_layout = QVBoxLayout(module_group)
        self.module_content_widget.setLayout(QVBoxLayout())
        self.module_content_widget.layout().setContentsMargins(0, 0, 0, 0)
        self.module_content_widget.layout().addWidget(module_group)

        # 模式选择（人脸/手掌）
        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel('模式选择:'))

        # 创建人脸/手掌模式按钮组
        self.mode_button_group = QButtonGroup(self)

        self.face_mode_radio = QRadioButton('👤 人脸模式')
        self.face_mode_radio.setChecked(True)  # 默认选中人脸模式
        self.mode_button_group.addButton(self.face_mode_radio)
        mode_layout.addWidget(self.face_mode_radio)

        self.palm_mode_radio = QRadioButton('🖐️ 手掌模式')
        self.mode_button_group.addButton(self.palm_mode_radio)
        mode_layout.addWidget(self.palm_mode_radio)

        mode_layout.addStretch()

        # RAW图模式选择
        mode_layout.addWidget(QLabel('RAW模式:'))

        self.raw_mode_button_group = QButtonGroup(self)

        self.raw_y_rgb_radio = QRadioButton('Y+RGB')
        self.raw_y_rgb_radio.setToolTip('第一张图40%，第二张图60%')
        self.raw_mode_button_group.addButton(self.raw_y_rgb_radio)
        mode_layout.addWidget(self.raw_y_rgb_radio)

        self.raw_y_ir_radio = QRadioButton('Y+IR')
        self.raw_y_ir_radio.setChecked(True)  # 默认选中Y+IR
        self.raw_y_ir_radio.setToolTip('第一张图50%，第二张图50%')
        self.raw_mode_button_group.addButton(self.raw_y_ir_radio)
        mode_layout.addWidget(self.raw_y_ir_radio)

        # 连接信号
        self.raw_y_rgb_radio.toggled.connect(self.on_raw_mode_changed)

        mode_layout.addWidget(QLabel('  '))  # 添加一点间距

        # 添加重复次数输入
        mode_layout.addWidget(QLabel('重复次数:'))
        self.repeat_count_spin = QSpinBox()
        self.repeat_count_spin.setMinimum(1)
        self.repeat_count_spin.setMaximum(1000)
        self.repeat_count_spin.setValue(1)
        self.repeat_count_spin.setFixedWidth(80)
        self.repeat_count_spin.setToolTip('注册/识别指令的重复执行次数')
        mode_layout.addWidget(self.repeat_count_spin)

        module_layout.addLayout(mode_layout)

        # 项目模式选择（DSM/KDS）
        project_mode_layout = QHBoxLayout()
        project_mode_layout.addWidget(QLabel('项目模式:'))

        # 创建DSM/KDS项目模式按钮组
        self.project_button_group = QButtonGroup(self)

        self.dsm_mode_radio = QRadioButton('DSM')
        self.dsm_mode_radio.setChecked(True)  # 默认选中DSM模式
        self.project_button_group.addButton(self.dsm_mode_radio)
        project_mode_layout.addWidget(self.dsm_mode_radio)

        self.kds_mode_radio = QRadioButton('KDS')
        self.project_button_group.addButton(self.kds_mode_radio)
        project_mode_layout.addWidget(self.kds_mode_radio)

        project_mode_layout.addStretch()
        module_layout.addLayout(project_mode_layout)

        # 分隔线
        line_mode = QFrame()
        line_mode.setFrameShape(QFrame.HLine)
        line_mode.setFrameShadow(QFrame.Sunken)
        module_layout.addWidget(line_mode)

        # 模组串口配置
        module_serial_layout = QHBoxLayout()
        module_serial_layout.addWidget(QLabel('模组串口:'))

        self.module_port_combo = QComboBox()
        self.module_port_combo.setMinimumWidth(100)
        module_serial_layout.addWidget(self.module_port_combo)

        btn_refresh_module = QPushButton('🔄')
        btn_refresh_module.setMaximumWidth(30)
        btn_refresh_module.setToolTip('刷新串口列表')
        btn_refresh_module.clicked.connect(self.refresh_module_ports)
        module_serial_layout.addWidget(btn_refresh_module)

        module_serial_layout.addWidget(QLabel('波特率:'))
        self.module_baudrate_combo = QComboBox()
        self.module_baudrate_combo.setEditable(True)
        self.module_baudrate_combo.addItems(['9600', '19200', '38400', '57600', '115200', '230400', '460800', '1500000', '921600'])
        self.module_baudrate_combo.setCurrentText('115200')
        self.module_baudrate_combo.setMinimumWidth(100)
        module_serial_layout.addWidget(self.module_baudrate_combo)

        self.btn_module_connect = QPushButton('🔌 连接模组')
        self.btn_module_connect.clicked.connect(self.connect_module)
        self.btn_module_connect.setStyleSheet('QPushButton { background-color: #4CAF50; color: white; font-weight: bold; padding: 6px 12px; }')
        module_serial_layout.addWidget(self.btn_module_connect)

        module_layout.addLayout(module_serial_layout)

        # 模组状态
        self.module_status_label = QLabel('● 未连接')
        self.module_status_label.setStyleSheet('font-size: 10pt; font-weight: bold; color: #999999;')
        module_layout.addWidget(self.module_status_label)

        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        module_layout.addWidget(line)

        # 指令按钮区域 - 使用网格布局，从左到右排列
        cmd_label = QLabel('模组指令:')
        cmd_label.setStyleSheet('font-weight: bold;')
        module_layout.addWidget(cmd_label)

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

        # 添加弹性空间
        more_button_grid.setColumnStretch(4, 1)

        more_commands_layout.addWidget(more_button_container)
        module_layout.addWidget(self.more_commands_widget)

        # 默认隐藏更多指令区域
        self.more_commands_widget.setVisible(False)

        # 响应显示区域
        response_label = QLabel('响应信息:')
        response_label.setStyleSheet('font-weight: bold; margin-top: 10px;')
        module_layout.addWidget(response_label)

        self.module_response_text = QTextEdit()
        self.module_response_text.setReadOnly(True)
        self.module_response_text.setFont(QFont('Consolas', 9))
        self.module_response_text.setMinimumHeight(300)
        self.module_response_text.setPlaceholderText('模组响应信息将显示在这里...')
        # 连接鼠标点击事件
        self.module_response_text.mousePressEvent = self.on_response_text_clicked
        module_layout.addWidget(self.module_response_text)

        # 将模组内容添加到折叠容器，然后添加到右侧布局
        module_outer_layout.addWidget(self.module_content_widget)
        right_layout.addLayout(module_outer_layout)

        # === 保存控制区域 ===
        save_group = QGroupBox('💾 保存控制')
        save_layout = QVBoxLayout(save_group)

        # 添加折叠/展开按钮
        save_header_layout = QHBoxLayout()
        self.btn_toggle_save = QPushButton('▼ 折叠')
        self.btn_toggle_save.setMaximumWidth(80)
        self.btn_toggle_save.clicked.connect(self.toggle_save_panel)
        save_header_layout.addWidget(self.btn_toggle_save)
        save_header_layout.addStretch()
        save_layout.addLayout(save_header_layout)

        # 保存控制内容容器
        self.save_content_widget = QWidget()
        save_content_layout = QVBoxLayout(self.save_content_widget)
        save_content_layout.setContentsMargins(0, 0, 0, 0)

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

        right_layout.addWidget(save_group)

        # 将右侧widget设置到滚动区域
        right_scroll.setWidget(right_widget)
        splitter.addWidget(right_scroll)

        # 刷新模组串口列表
        self.refresh_module_ports()

        # 设置初始比例 - 左侧占更多空间
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

    def on_raw_mode_changed(self, checked):
        """RAW模式切换"""
        if checked:
            self.raw_mode = 'Y+RGB'
        else:
            self.raw_mode = 'Y+IR'
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

            # 清空队列
            while not self.log_queue.empty():
                try:
                    self.log_queue.get_nowait()
                except:
                    pass
            while not self.error_queue.empty():
                try:
                    self.error_queue.get_nowait()
                except:
                    pass

            # 重置连接事件
            self.connected_event.clear()

            # 等待线程结束（最多等待1秒）
            if self.serial_thread and self.serial_thread.is_alive():
                print('[调试] 等待串口线程结束...')
                self.serial_thread.join(timeout=1.0)
                if self.serial_thread.is_alive():
                    print('[调试] 串口线程未能正常结束')

            self.serial_thread = None

            # 重置下载相关标志位
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

        except Exception as e:
            print(f'[调试] 断开串口时出错: {e}')
            QMessageBox.warning(self, '断开失败', f'断开串口时出错：\n{str(e)}')


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
                self.monitor_status_label.setText(f'监控: {os.path.basename(self.download_dir)}')

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
        if self.save_content_widget.isVisible():
            # 折叠
            self.save_content_widget.setVisible(False)
            self.btn_toggle_save.setText('▶ 展开')
        else:
            # 展开
            self.save_content_widget.setVisible(True)
            self.btn_toggle_save.setText('▼ 折叠')

    def check_repeat_next(self):
        """检查是否需要继续重复执行下一次"""
        if not self.repeat_mode:
            return

        # 防止同一次操作多次Reply触发
        if self.repeat_reply_received:
            return

        # 标记当前操作已收到Reply
        self.repeat_reply_received = True

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
            self.btn_monitor.setText('👁️ 启用监控')
            self.monitor_status_label.setText('监控: 未启用')
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
            self.btn_monitor.setText('👁️ 停止监控')
            self.monitor_status_label.setText(f'监控: {os.path.basename(download_dir)}')
            self.monitor_status_label.setStyleSheet('font-size: 9pt; color: #2e8b57;')

            # 启动上位机程序
            self.launch_host_program()

    def start_serial(self):
        """启动串口线程"""
        self.connected_event.clear()
        self.serial_thread = threading.Thread(
            target=serial_reader,
            args=(self.port, self.baudrate, self.error_queue, self.connected_event, self.log_queue, self.send_queue),
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
        self.log_timer.start(200)

        self.error_timer = QTimer()
        self.error_timer.timeout.connect(self.poll_error_queue)
        self.error_timer.start(500)

    def append_log(self, text):
        """追加日志（带语法高亮）"""
        try:
            import re

            # 保存当前光标位置
            cursor = self.log_text.textCursor()
            cursor.movePosition(QTextCursor.End)

            # 定义颜色方案（浅色模式和深色模式）
            if self.dark_mode:
                colors = {
                   'timestamp': "#87CEEB",      # 深蓝 - 时间戳
                    'bracket': '#216AAF',        # 蓝灰色 - 括号
                    'number': '#098658',         # 深绿 - 数字
                    'hex': '#78E22E',            # 黄绿色 - 十六进制
                    'keyword': '#87CEEB',        # 蓝色 - 关键字（成功等）
                    'keyword_err': '#EC5800',    # 深红色 - 错误关键字
                    'symbol': '#41B9EF',          # 浅灰 - 符号
                    'text': '#F8F9FA',           # 浅灰 - 普通文本
                    'upper_letter': '#FFB6C1',    # 橙色 - 全大写字母
                    'background': '#1e1e1e'
                }
            else:
                colors = {
                    'timestamp': '#0066CC',      # 深蓝 - 时间戳
                    'bracket': '#216AAF',        # 蓝灰色 - 括号
                    'number': '#098658',         # 深绿 - 数字
                    'hex': '#78E22E',            # 黄绿色 - 十六进制
                    'keyword': '#0000FF',        # 蓝色 - 关键字（成功等）
                    'keyword_err': '#A31515',    # 深红色 - 错误关键字
                    'symbol': '#41B9EF',         # 青色 - 符号
                    'text': '#000000',           # 黑色 - 普通文本
                    'upper_letter': '#EC5800',   # 橙色 - 全大写字母
                    'background': 'white'
                }

            # 解析并着色日志
            # 匹配时间戳：[2024-01-01 12:34:56.789]
            timestamp_pattern = r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]'
            # 匹配十六进制：0x1234, AA, 55, EF 等
            hex_pattern = r'\b0x[0-9A-Fa-f]+\b'
            # 匹配数字：123, 456 等
            number_pattern = r'\b\d+\b'
            # 匹配关键字
            keyword_pattern = r'\b(成功|warning|success|connected|发送|接收|下载)\b'
            keyword_pattern_err = r'\b(失败|错误|error|failed|timeout|disconnected)\b'
            # 匹配括号和符号
            bracket_pattern = r'[\[\](){}]'
            symbol_pattern = r'[,:;=<>+\-*/]'
            upper_letter_pattern = r'\b[A-Z]+\b'

            # 创建一个统一的模式，并标记类型
            patterns = [
                ('timestamp', timestamp_pattern),
                ('hex', hex_pattern),
                ('number', number_pattern),
                ('keyword', keyword_pattern),
                ('bracket', bracket_pattern),
                ('symbol', symbol_pattern),
                ('keyword_err', keyword_pattern_err),
                ('upper_letter', upper_letter_pattern),
            ]

            # 找到所有匹配项及其位置
            matches = []
            for color_type, pattern in patterns:
                # 只对关键字使用忽略大小写，其他保持大小写敏感
                if color_type in ['keyword', 'keyword_err']:
                    flags = re.IGNORECASE
                else:
                    flags = 0
                for match in re.finditer(pattern, text, flags):
                    matches.append((match.start(), match.end(), color_type))

            # 按位置排序
            matches.sort(key=lambda x: x[0])

            # 合并重叠的匹配（优先级：timestamp > keyword > keyword_err > hex > upper_letter > number > bracket > symbol）
            priority = {
                'timestamp': 8,
                'keyword': 7,
                'keyword_err': 6,
                'hex': 5,
                'upper_letter': 4,
                'number': 3,
                'bracket': 2,
                'symbol': 1
            }
            filtered_matches = []
            last_end = 0
            for start, end, color_type in matches:
                if start >= last_end:
                    filtered_matches.append((start, end, color_type))
                    last_end = end
                elif priority.get(color_type, 0) > priority.get(filtered_matches[-1][2], 0):
                    # 如果当前匹配优先级更高，替换前一个
                    if filtered_matches and filtered_matches[-1][0] == start:
                        filtered_matches[-1] = (start, end, color_type)
                        last_end = end

            # 插入带颜色的文本
            pos = 0
            for start, end, color_type in filtered_matches:
                # 插入未匹配部分（普通文本）
                if pos < start:
                    format = cursor.charFormat()
                    format.clearBackground()
                    format.setBackground(QColor(colors['background']))
                    format.setForeground(QColor(colors['text']))
                    cursor.setCharFormat(format)
                    cursor.insertText(text[pos:start])

                # 插入匹配部分（带颜色）
                format = cursor.charFormat()
                format.clearBackground()
                format.setBackground(QColor(colors['background']))
                format.setForeground(QColor(colors[color_type]))
                cursor.setCharFormat(format)
                cursor.insertText(text[start:end])

                pos = end

            # 插入剩余部分
            if pos < len(text):
                format = cursor.charFormat()
                format.clearBackground()
                format.setBackground(QColor(colors['background']))
                format.setForeground(QColor(colors['text']))
                cursor.setCharFormat(format)
                cursor.insertText(text[pos:])

            # 插入换行
            cursor.insertText('\n')

        except Exception as e:
            # 如果语法高亮失败，使用简单模式显示
            print(f'[错误] 日志着色失败: {e}，使用简单模式')
            cursor = self.log_text.textCursor()
            cursor.movePosition(QTextCursor.End)
            format = cursor.charFormat()
            format.clearBackground()
            if self.dark_mode:
                format.setBackground(QColor('#1e1e1e'))
                format.setForeground(QColor('#e0e0e0'))
            else:
                format.setBackground(QColor('white'))
                format.setForeground(QColor('black'))
            cursor.setCharFormat(format)
            cursor.insertText(text + '\n')

        # 更新日志计数
        self.log_count_label.setText(f'日志行数: {len(full_log_cache)}')

        # 限制显示行数
        document = self.log_text.document()
        if document.lineCount() > 2000:
            cursor = QTextCursor(document)
            cursor.movePosition(QTextCursor.Start)
            for _ in range(document.lineCount() - 2000):
                cursor.select(QTextCursor.LineUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()

        # 滚动到底部
        self.log_text.moveCursor(QTextCursor.End)

    def set_status(self, text, color):
        """设置状态"""
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f'font-size: 11pt; font-weight: bold; color: {color};')

    def poll_log_queue(self):
        """轮询日志队列"""
        while True:
            try:
                line = self.log_queue.get_nowait()
                self.log_signal.emit(line)
                if self.connected_event.is_set():
                    self.connected_signal.emit()
            except queue.Empty:
                break

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
        # 清空之前的图片
        while self.image_layout.count():
            item = self.image_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 查找图片文件
        image_files = []
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                if file.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.gif')):
                    image_files.append(os.path.join(root, file))

        if not image_files:
            label = QLabel('该文件夹中没有找到图片文件')
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet('color: #999999; padding: 20px;')
            self.image_layout.addWidget(label)
            return

        # 创建水平布局容器
        h_layout = QHBoxLayout()
        h_layout.setSpacing(10)
        h_layout.setAlignment(Qt.AlignCenter)

        # 显示图片（最多10张，水平排列）
        for img_path in image_files[:10]:
            try:
                pixmap = QPixmap(img_path)
                if not pixmap.isNull():
                    # 缩放图片
                    scaled_pixmap = pixmap.scaled(250, 250, Qt.KeepAspectRatio, Qt.SmoothTransformation)

                    img_label = QLabel()
                    img_label.setPixmap(scaled_pixmap)
                    img_label.setAlignment(Qt.AlignCenter)
                    img_label.setStyleSheet('border: 1px solid #ddd; padding: 5px; background: white;')

                    # 设置为可点击
                    img_label.setCursor(Qt.PointingHandCursor)
                    img_label.setToolTip('双击查看大图')

                    # 保存图片路径到标签
                    img_label.setProperty('image_path', img_path)

                    # 双击事件
                    img_label.mouseDoubleClickEvent = lambda event, path=img_path: self.open_image_viewer(path)

                    # 创建垂直容器（图片+文件名）
                    v_container = QWidget()
                    v_layout = QVBoxLayout(v_container)
                    v_layout.setContentsMargins(0, 0, 0, 0)
                    v_layout.addWidget(img_label)

                    # 文件名
                    name_label = QLabel(os.path.basename(img_path))
                    name_label.setAlignment(Qt.AlignCenter)
                    name_label.setStyleSheet('color: #666666; font-size: 9pt;')
                    v_layout.addWidget(name_label)

                    h_layout.addWidget(v_container)
            except Exception as e:
                print(f'加载图片失败: {img_path}, {e}')

        # 添加水平布局到主布局
        h_layout.addStretch()
        h_container = QWidget()
        h_container.setLayout(h_layout)
        self.image_layout.addWidget(h_container)

    def open_image_viewer(self, image_path):
        """打开图片查看器窗口"""
        try:
            viewer = ImageViewerDialog(image_path, self)
            viewer.exec()
        except Exception as e:
            QMessageBox.warning(self, '错误', f'打开图片查看器失败：\n{str(e)}')

    def display_downloaded_images(self, image1_path, image2_path):
        """显示下载的两张图片（水平并排）"""
        # 清空之前的图片
        while self.image_layout.count():
            item = self.image_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 创建水平布局容器
        h_layout = QHBoxLayout()
        h_layout.setSpacing(10)

        # 显示第一张图片
        try:
            pixmap1 = QPixmap(image1_path)
            if not pixmap1.isNull():
                # 缩放图片（250x250）
                scaled_pixmap1 = pixmap1.scaled(250, 250, Qt.KeepAspectRatio, Qt.SmoothTransformation)

                img_label1 = QLabel()
                img_label1.setPixmap(scaled_pixmap1)
                img_label1.setAlignment(Qt.AlignCenter)
                img_label1.setStyleSheet('border: 1px solid #ddd; padding: 5px; background: white;')

                # 创建垂直容器（图片+文件名）
                v_container1 = QWidget()
                v_layout1 = QVBoxLayout(v_container1)
                v_layout1.setContentsMargins(0, 0, 0, 0)
                v_layout1.addWidget(img_label1)

                name_label1 = QLabel(os.path.basename(image1_path))
                name_label1.setAlignment(Qt.AlignCenter)
                name_label1.setStyleSheet('color: #666666; font-size: 9pt;')
                v_layout1.addWidget(name_label1)

                h_layout.addWidget(v_container1)
        except Exception as e:
            print(f'加载图片1失败: {image1_path}, {e}')

        # 显示第二张图片
        try:
            pixmap2 = QPixmap(image2_path)
            if not pixmap2.isNull():
                # 缩放图片（250x250）
                scaled_pixmap2 = pixmap2.scaled(250, 250, Qt.KeepAspectRatio, Qt.SmoothTransformation)

                img_label2 = QLabel()
                img_label2.setPixmap(scaled_pixmap2)
                img_label2.setAlignment(Qt.AlignCenter)
                img_label2.setStyleSheet('border: 1px solid #ddd; padding: 5px; background: white;')

                # 创建垂直容器（图片+文件名）
                v_container2 = QWidget()
                v_layout2 = QVBoxLayout(v_container2)
                v_layout2.setContentsMargins(0, 0, 0, 0)
                v_layout2.addWidget(img_label2)

                name_label2 = QLabel(os.path.basename(image2_path))
                name_label2.setAlignment(Qt.AlignCenter)
                name_label2.setStyleSheet('color: #666666; font-size: 9pt;')
                v_layout2.addWidget(name_label2)

                h_layout.addWidget(v_container2)
        except Exception as e:
            print(f'加载图片2失败: {image2_path}, {e}')

        # 将水平布局添加到主布局
        h_container = QWidget()
        h_container.setLayout(h_layout)
        self.image_layout.addWidget(h_container)

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

    def save_log_only(self):
        """仅保存日志"""
        if not full_log_cache:
            QMessageBox.information(self, '提示', '当前还没有产生任何日志')
            return

        # 简单对话框
        remark, ok = QInputDialog.getText(self, '保存日志', '备注（可选）:')

        if not ok:
            return

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if remark:
            name = f'{remark}_{timestamp}'
        else:
            name = timestamp

        dst_folder = os.path.join(self.output if self.output else './result', name)

        if os.path.exists(dst_folder):
            dst_folder += '_' + datetime.now().strftime('%H%M%S')

        os.makedirs(dst_folder, exist_ok=True)

        logs_to_save = extract_logs('all')

        log_file = os.path.join(dst_folder, 'log.txt')
        with open(log_file, 'w', encoding='utf-8') as f:
            for line in logs_to_save:
                f.write(line + "\n")

        QMessageBox.information(self, '保存成功', f'已保存 {len(logs_to_save)} 行日志至:\n{dst_folder}')

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
            self.btn_theme.setText('☀️ 浅色模式')
        else:
            self.apply_light_theme()
            self.btn_theme.setText('🌙 深色模式')

        # 重新渲染所有日志以应用新主题的颜色
        self.rerender_all_logs()

    def rerender_all_logs(self):
        """重新渲染所有日志（切换主题时调用）"""
        # 保存当前日志内容
        logs_to_rerender = []
        for log_line in full_log_cache:
            logs_to_rerender.append(log_line)

        # 清空日志显示
        self.log_text.clear()

        # 重新渲染每一行
        for log_line in logs_to_rerender:
            self.append_log(log_line)

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
                background-color: #2b2b2b;
                border: 1px solid #555555;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #e0e0e0;
            }
            QSpinBox::up-button, QSpinBox::down-button {
                background-color: #404040;
                border: 1px solid #555555;
            }
        ''')

        # 日志文本特殊处理
        self.log_text.setStyleSheet('''
            QTextEdit {
                background-color: #1e1e1e;
                color: #e0e0e0;
                border: 1px solid #555555;
            }
        ''')

        # 搜索栏样式
        self.search_bar.setStyleSheet('QWidget { background-color: #353535; border: 1px solid #555555; }')

        # 状态标签保持原有颜色逻辑，只调整默认色
        # 其他动态颜色（连接状态等）保持不变

    def apply_light_theme(self):
        """应用浅色主题"""
        # 清除所有自定义样式，恢复默认
        self.setStyleSheet('')
        self.log_text.setStyleSheet('')
        self.search_bar.setStyleSheet('QWidget { background-color: #f0f0f0; border: 1px solid #ccc; }')

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
        self.log_text.setExtraSelections(extra_selections)

        print(f'[调试-搜索] find_all_matches 完成，共找到 {match_count} 个匹配项')

    def clear_search_highlights(self):
        """清除搜索高亮（使用额外格式，不影响原有颜色）"""
        # 使用 ExtraSelections 来清除高亮，这样不会影响原有的文字颜色
        self.log_text.setExtraSelections([])

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
        self.log_text.setExtraSelections(extra_selections)

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

        # 检查串口是否被日志串口占用
        if port == self.port:
            QMessageBox.warning(self, '提示', '该串口已被日志串口使用，请选择其他串口')
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
            self.module_response_timer = QTimer()
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

            self.append_module_log('[模组] 已断开连接')

        except Exception as e:
            print(f'[模组] 断开连接失败: {e}')

    def module_receive_worker(self):
        """模组串口接收线程（优化版）"""
        while self.module_connected and self.module_serial:
            try:
                # 等待至少有 6 字节数据（同步头2 + 消息类型1 + 长度2 + 校验和1）
                if self.module_serial.in_waiting < 6:
                    time.sleep(0.001)  # 减少到 1ms
                    continue

                # 一次性读取头部（同步头 + 消息类型 + 数据长度）
                header = self.module_serial.read(5)
                if len(header) < 5:
                    continue

                sync = header[0:2]
                if sync != b'\xEF\xAA':
                    continue

                msg_type = header[2:3]
                data_size_bytes = header[3:5]
                data_size = int.from_bytes(data_size_bytes, byteorder='big')

                # 等待数据和校验和到达（优化：避免读取不完整）
                wait_count = 0
                while self.module_serial.in_waiting < data_size + 1 and wait_count < 100:
                    time.sleep(0.001)
                    wait_count += 1

                # 一次性读取数据和校验和
                tail = self.module_serial.read(data_size + 1)
                if len(tail) < data_size + 1:
                    continue

                data = tail[0:data_size]
                checksum = tail[data_size:data_size + 1]

                # 快速校验和计算（优化：减少循环开销）
                calc_checksum = msg_type[0] ^ data_size_bytes[0] ^ data_size_bytes[1]
                for b in data:
                    calc_checksum ^= b

                if calc_checksum != checksum[0]:
                    self.module_response_queue.put(('error', b'Checksum error'))
                    continue

                # 自定义命令对话框（只在需要时构造数据包）
                if self.custom_command_dialog and self.custom_command_dialog.isVisible():
                    raw_packet = sync + msg_type + data_size_bytes + data + checksum
                    self.custom_command_data_signal.emit(raw_packet)

                # 根据消息类型处理（优化：减少打印）
                msg_type_val = msg_type[0]
                if msg_type_val == 0x00:  # Reply消息
                    if data_size >= 1:
                        msg_id = data[0]
                        result = data[1] if data_size >= 2 else 0xFF
                        payload = data[2:] if data_size > 2 else b''
                        self.module_response_queue.put(('reply', msg_id, result, payload))

                elif msg_type_val == 0x01:  # Note消息
                    self.module_response_queue.put(('note', data))

                elif msg_type_val == 0x02:  # 图片数据消息（优化：直接放入队列，减少打印）
                    self.module_response_queue.put(('image_data', data_size, data))

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

                # 调试打印
                # print(f'[调试-响应] 收到响应: {response}')

                if response[0] == 'error':
                    error_msg = response[1].decode('utf-8', errors='ignore')
                    self.append_module_log(f'[错误] {error_msg}', error=True)
                elif response[0] == 'reply':
                    # Reply消息: ('reply', msg_id, result, payload)
                    _, msg_id, result, payload = response
                    print(f'[调试-Reply] msg_id=0x{msg_id:02X}, result=0x{result:02X}, payload长度={len(payload)}')
                    self.module_response_signal.emit(f'0x{msg_id:02X}', ('reply', result, payload))
                elif response[0] == 'note':
                    # Note消息: ('note', data)
                    _, data = response
                    print(f'[调试-Note] data长度={len(data)}, 前4字节={data[:4].hex().upper() if len(data) >= 4 else data.hex().upper()}')
                    self.module_response_signal.emit('note', ('note', 0, data))
                elif response[0] == 'image_data':
                    # 图片数据消息: ('image_data', data_size, img_data)
                    _, data_size, img_data = response
                    print(f'[调试-图片数据] 接收到 {data_size} 字节')
                    self.handle_image_data(data_size, img_data)
        except queue.Empty:
            pass

    def handle_module_response(self, msg_id, data):
        """处理模组响应"""
        msg_type, result, payload = data

        # 调试打印
        print(f'[调试-处理响应] msg_id={msg_id}, msg_type={msg_type}, result={result if msg_type == "reply" else "N/A"}')

        # 处理Note消息
        if msg_id == 'note':
            self.handle_note_message(payload)
            return

        # 处理Reply消息
        if msg_type != 'reply':
            return

        # 计算时长
        elapsed_time = self.get_command_elapsed_time(msg_id)

        if msg_id == '0x30':  # 获取版本号
            if result == 0x00:
                # 成功，解析版本号
                
                version = payload[:-1].decode('utf-8', errors='ignore').rstrip('\x00')
                self.append_module_log(f'[版本号] {version} {elapsed_time}', success=True)
            
            else:
                self.append_module_log(f'[错误] 获取版本号失败，结果码: 0x{result:02X} {elapsed_time}', error=True)

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
            else:
                error_messages = {
                    0x01: '模组拒绝此命令',
                    0x04: 'Camera open fail',
                    0x08: '无人脸录入',
                    0x09: '超出最大注册用户数量',
                    0x0C: '活体检测失败',
                    0x0D: '超时',
                    0x10: '验证失败',
                }
                error_msg = error_messages.get(result, f'未知错误 (0x{result:02X})')
                self.append_module_log(f'注册失败: {error_msg} {elapsed_time}', error=True)

            # 检查是否需要继续重复执行
            self.check_repeat_next()

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

            # 检查是否需要继续重复执行
            if result != 0x23:
                self.check_repeat_next()

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
                    self.append_module_log(f'设置波特率为 {baudrate} 成功！{elapsed_time}', success=True)
                    print(f'[调试-0x51] 准备切换波特率到 {baudrate}')

                    # 如果设置了高速波特率，需要更新串口波特率并继续下载流程
                    if baudrate == 1500000 and self.module_serial:
                        print(f'[调试-0x51] 开始切换串口波特率...')
                        try:
                            self.module_serial.baudrate = baudrate
                            print(f'[调试-0x51] 串口波特率切换成功')
                            self.append_module_log(f'串口波特率已切换到 {baudrate}')
                            # 步骤2: 根据下载类型发送获取图片大小指令
                            if self.download_type == 'raw':
                                self.append_module_log('[步骤2] 发送获取RAW图大小指令')
                                print(f'[调试-0x51] 准备发送0x15指令')
                                self.send_module_command(0x15)
                                print(f'[调试-0x51] 已发送0x15指令')
                            else:  # jpeg
                                self.append_module_log('[步骤2] 发送获取JPEG大小指令')
                                print(f'[调试-0x51] 准备发送0x14指令')
                                self.send_module_command(0x14)
                                print(f'[调试-0x51] 已发送0x14指令')
                        except Exception as e:
                            print(f'[调试-0x51] 切换波特率异常: {e}')
                            self.append_module_log(f'[错误] 切换波特率失败: {e}', error=True)
                    elif baudrate == 115200 and self.module_serial:
                        # 恢复标准波特率
                        print(f'[调试-0x51] 恢复标准波特率')
                        self.module_serial.baudrate = baudrate
                        self.append_module_log(f'串口波特率已恢复到 {baudrate}')
                        self.append_module_log('[完成] 图片下载流程完成！', success=True)
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

            # 检查是否需要继续重复执行
            self.check_repeat_next()

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
            elif result == 0x23:
                self.append_module_log(f'palm switch {elapsed_time}', success=True)
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

            # 检查是否需要继续重复执行
            if result != 0x23:
                self.check_repeat_next()

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

        elif msg_id == '0x21':  # 人脸删除所有用户
            if result == 0x00:
                self.append_module_log(f'[人脸模式] 删除所有用户成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[人脸模式] 删除所有用户失败，结果码: 0x{result:02X} {elapsed_time}', error=True)

        elif msg_id == '0x65':  # 手掌删除指定用户ID
            if result == 0x00:
                self.append_module_log(f'[手掌模式] 删除用户成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[手掌模式] 删除用户失败，结果码: 0x{result:02X} {elapsed_time}', error=True)

        elif msg_id == '0x66':  # 手掌删除所有用户
            if result == 0x00:
                self.append_module_log(f'[手掌模式] 删除所有用户成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[手掌模式] 删除所有用户失败，结果码: 0x{result:02X} {elapsed_time}', error=True)

        elif msg_id == '0x55':  # 重启模组
            if result == 0x00:
                self.append_module_log(f'[重启模组] 重启成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[重启模组] 重启失败，结果码: 0x{result:02X} {elapsed_time}', error=True)

        elif msg_id == '0x10':  # 待机
            if result == 0x00:
                self.append_module_log(f'[待机] 待机成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[待机] 待机失败，结果码: 0x{result:02X} {elapsed_time}', error=True)

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

        elif msg_id == '0xF0':  # Debug模式
            if result == 0x00:
                self.append_module_log(f'[Debug模式] 操作成功 {elapsed_time}', success=True)
            else:
                self.append_module_log(f'[Debug模式] 操作失败，结果码: 0x{result:02X} {elapsed_time}', error=True)

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

            # 判断是人脸还是手掌
            if nid == 0x01:  # 人脸Note消息
                # 第1字节是固定字段0x01，第2字节是第一个状态信息
                status = data[1]

                status_messages = {
                    0: '人脸正常',
                    1: '未检测到人脸',
                    2: '人脸太靠上，请向下移动',
                    3: '人脸太靠下，请向上移动',
                    4: '人脸太靠左，请向右移动',
                    5: '人脸太靠右，请向左移动',
                    6: '人脸太远，请靠近',
                    7: '人脸太近，请远离',
                    8: '眉毛遮挡/检测到多人',
                    9: '眼睛遮挡',
                    10: '脸部遮挡',
                    11: '录入人脸方向错误',
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
                self.append_module_log(f'[DSM手掌状态] {status_msg}')

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
            QMessageBox.warning(self, '提示', '模组串口未连接')
            return False

        try:
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

            # 发送
            self.module_serial.write(message)
            self.module_serial.flush()

            # 记录发送时间
            self.module_command_start_time[msg_id] = time.time()

            # 记录当前命令类型（用于Note消息识别）
            self.current_command_type = msg_id

            # 记录日志
            hex_str = ' '.join(f'{b:02X}' for b in message)
            self.append_module_log(f'[发送] {hex_str}')

            return True

        except Exception as e:
            QMessageBox.critical(self, '发送失败', f'发送指令失败:\n{e}')
            return False

    def get_module_version(self):
        """获取模组版本号"""
        # 发送0x30指令获取版本号
        self.send_module_command(0x30)
        self.append_module_log('[获取版本号] 已发送指令，等待响应...')

    def get_all_user_ids(self):
        """获取所有用户ID"""
        # 根据模式选择发送不同的指令
        if self.palm_mode_radio.isChecked():
            # 手掌模式：根据项目模式选择命令
            if self.kds_mode_radio.isChecked():
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
        if self.kds_mode_radio.isChecked():
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
        if self.kds_mode_radio.isChecked():
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
        if self.palm_mode_radio.isChecked():
            # 手掌模式：根据项目模式选择命令
            if self.kds_mode_radio.isChecked():
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
        if self.palm_mode_radio.isChecked():
            # 手掌模式：根据项目模式选择命令
            if self.kds_mode_radio.isChecked():
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
        # 发送待机指令：0x10
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

    def download_image(self):
        """下载JPEG图片（完整流程）"""
        if self.is_downloading:
            self.append_module_log('[警告] 正在下载中，请勿重复操作', error=True)
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

        self.append_module_log('[下载JPEG] 开始下载流程...')

        # 步骤1: 设置高速波特率 1500000
        self.append_module_log('[步骤1] 设置波特率为 1500000')
        self.send_module_command(0x51, b'\x04')  # 0x04 = 1500000

    def download_raw_image(self):
        """下载RAW图片（完整流程）"""
        if self.is_downloading:
            self.append_module_log('[警告] 正在下载中，请勿重复操作', error=True)
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

        self.append_module_log('[下载RAW] 开始下载流程...')

        # 步骤1: 设置高速波特率 1500000
        self.append_module_log('[步骤1] 设置波特率为 1500000')
        self.send_module_command(0x51, b'\x04')  # 0x04 = 1500000

    def start_image_download(self):
        """开始图片下载传输"""
        # 初始化下载状态
        self.download_buffer = bytearray()
        self.download_offset = 0
        self.download_total_size = self.image1_size + self.image2_size
        self.is_downloading = True

        self.append_module_log(f'[步骤3] 开始下载图片数据，总大小 {self.download_total_size} 字节')

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
        self.last_upload_time = datetime.now()  # 记录发送时间

        self.send_module_command(0x18, data)

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
            self.append_module_log(f'[超时重传] 未收到响应，重新发送请求，偏移量={offset}, 大小={size}', error=True)
            self.send_image_upload_request(offset, size)

    def retry_last_upload(self):
        """重传最后一次的上传指令"""
        if self.last_upload_command and self.is_downloading:
            offset, size = self.last_upload_command
            self.append_module_log(f'[重传] 重新发送上传请求，偏移量={offset}, 大小={size}')
            self.send_image_upload_request(offset, size)
        else:
            print('[调试-重传] 没有可重传的指令或未在下载中')

    def handle_image_data(self, data_size, img_data):
        """处理接收到的图片数据"""
        if not self.is_downloading:
            return

        # 停止重传定时器（收到数据说明传输成功）
        if self.retry_timer:
            self.retry_timer.stop()

        # 将接收到的数据追加到缓冲区
        self.download_buffer.extend(img_data)
        self.download_offset += data_size

        # 显示进度
        progress = (self.download_offset / self.download_total_size) * 100
        self.append_module_log(f'[下载进度] {self.download_offset}/{self.download_total_size} ({progress:.1f}%)')

        # 检查是否下载完成
        if self.download_offset >= self.download_total_size:
            self.finish_image_download()
        else:
            # 继续下载下一个数据包
            remaining = self.download_total_size - self.download_offset
            next_size = min(4000, remaining)
            self.send_image_upload_request(self.download_offset, next_size)

    def finish_image_download(self):
        """完成图片下载，分离并保存两张图片，并显示到预览区"""
        self.append_module_log('[下载完成] 开始处理图片数据...')

        try:
            # 分离两张图片
            image1_data = bytes(self.download_buffer[:self.image1_size])
            image2_data = bytes(self.download_buffer[self.image1_size:self.image1_size + self.image2_size])

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

            # 步骤4: 恢复标准波特率 115200
            self.append_module_log('[步骤4] 恢复波特率为 115200')

            # 重置下载状态（在发送恢复波特率指令之前）
            self.is_downloading = False
            self.download_buffer = bytearray()
            # 注意：不要重置download_offset和download_total_size，用于判断是恢复波特率

            self.send_module_command(0x51, b'\x01')  # 0x01 = 115200

        except Exception as e:
            self.append_module_log(f'[错误] 处理图片失败: {e}', error=True)
            self.is_downloading = False

    def append_module_log(self, text, success=False, error=False):
        """添加模组日志"""
        # 保存到缓存
        module_log_cache.append((text, success, error))

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
        format.setBackground(QColor(bg_color))  # 设置背景色
        cursor.setCharFormat(format)
        cursor.insertText(log + '\n')

        self.module_response_text.moveCursor(QTextCursor.End)

    def closeEvent(self, event):
        """关闭事件"""
        # 断开模组串口
        if self.module_connected:
            self.disconnect_module()

        # 停止文件监控
        if self.observer:
            self.observer.stop()
            self.observer.join()

        event.accept()


# ==============================
# 主函数
# ==============================

def main():
    app = QApplication(sys.argv)

    # 设置应用样式
    app.setStyle('Fusion')

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
