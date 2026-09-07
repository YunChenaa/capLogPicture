"""
串口发送模块 - PySide6版本
支持文本、16进制、二进制格式发送数据
"""
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QRadioButton, QCheckBox, QTextEdit,
    QGroupBox, QButtonGroup, QMessageBox
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont


class SerialSendWindow(QDialog):
    """串口发送窗口"""

     def __init__(self, parent, serial_port, baudrate, send_queue):
        """
        Args:
            parent: 父窗口
            serial_port: 串口号（如 'COM3'）
            baudrate: 波特率
            send_queue: 发送数据队列（Queue对象）
        """
        super().__init__(parent)
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.send_queue = send_queue

        self.setWindowTitle('串口数据发送')
        self.resize(550, 400)

        self.setup_ui()

    def setup_ui(self):
        """构建发送界面"""
        layout = QVBoxLayout(self)

        # 串口信息显示
        info_label = QLabel(f'当前串口: {self.serial_port} @ {self.baudrate}bps')
        info_label.setStyleSheet('color: #666666; font-size: 9pt;')
        layout.addWidget(info_label)

        # 发送模式选择
        mode_group = QGroupBox('发送模式')
        mode_layout = QHBoxLayout(mode_group)

        self.mode_button_group = QButtonGroup()

        self.rb_text = QRadioButton('文本模式 (UTF-8)')
        self.rb_hex = QRadioButton('16进制 (HEX)')
        self.rb_binary = QRadioButton('二进制 (BIN)')

        self.rb_text.setChecked(True)

        self.mode_button_group.addButton(self.rb_text, 0)
        self.mode_button_group.addButton(self.rb_hex, 1)
        self.mode_button_group.addButton(self.rb_binary, 2)

        mode_layout.addWidget(self.rb_text)
        mode_layout.addWidget(self.rb_hex)
        mode_layout.addWidget(self.rb_binary)

        layout.addWidget(mode_group)

        # 发送选项
        self.add_newline_check = QCheckBox('自动添加换行符 (\\r\\n)')
        self.add_newline_check.setChecked(True)
        layout.addWidget(self.add_newline_check)

        # 输入区域
        input_group = QGroupBox('发送内容')
        input_layout = QVBoxLayout(input_group)

        self.send_text = QTextEdit()
        self.send_text.setFont(QFont('Consolas', 10))
        input_layout.addWidget(self.send_text)

        # 提示信息
        self.hint_label = QLabel('示例: Hello World')
        self.hint_label.setStyleSheet('color: #888888; font-size: 8pt;')
        input_layout.addWidget(self.hint_label)

        # 更新提示信息
        self.rb_text.toggled.connect(self.update_hint)
        self.rb_hex.toggled.connect(self.update_hint)
        self.rb_binary.toggled.connect(self.update_hint)

        layout.addWidget(input_group)

        # 按钮区域
        button_layout = QHBoxLayout()

        btn_send = QPushButton('📤 发送')
        btn_send.clicked.connect(self.send_data)
        button_layout.addWidget(btn_send)

        btn_clear = QPushButton('🗑️ 清空')
        btn_clear.clicked.connect(self.send_text.clear)
        button_layout.addWidget(btn_clear)

        button_layout.addStretch()

        # 状态显示
        self.status_label = QLabel('')
        self.status_label.setStyleSheet('color: #2e8b57; font-size: 9pt;')
        button_layout.addWidget(self.status_label)

        layout.addLayout(button_layout)

    def update_hint(self):
        """更新提示信息"""
        if self.rb_text.isChecked():
            self.hint_label.setText('示例: Hello World')
        elif self.rb_hex.isChecked():
            self.hint_label.setText('示例: 01 02 03 FF (空格分隔) 或 0102FF')
        elif self.rb_binary.isChecked():
            self.hint_label.setText('示例: 10101010 11110000 (空格分隔) 或 1010101011110000')

    def send_data(self):
        """发送数据到串口"""
        content = self.send_text.toPlainText().strip()

        if not content:
            QMessageBox.warning(self, '提示', '请输入要发送的内容')
            return

        try:
            # 根据模式转换数据
            if self.rb_text.isChecked():
                data = content.encode('utf-8')
                if self.add_newline_check.isChecked():
                    data += b'\r\n'
            elif self.rb_hex.isChecked():
                data = self.parse_hex(content)
            elif self.rb_binary.isChecked():
                data = self.parse_binary(content)
            else:
                raise ValueError('未知的发送模式')

            # 发送数据到队列
            self.send_queue.put(data)

            # 显示成功信息
            byte_count = len(data)
            self.status_label.setText(f'✓ 已发送 {byte_count} 字节')
            self.status_label.setStyleSheet('color: #2e8b57; font-size: 9pt;')

            # 3秒后清除状态
            QTimer.singleShot(3000, lambda: self.status_label.setText(''))

        except ValueError as e:
            QMessageBox.critical(self, '格式错误', str(e))
            self.status_label.setText('✗ 发送失败')
            self.status_label.setStyleSheet('color: #c0392b; font-size: 9pt;')
        except Exception as e:
            QMessageBox.critical(self, '错误', f'发送失败:\n{e}')
            self.status_label.setText('✗ 发送失败')
            self.status_label.setStyleSheet('color: #c0392b; font-size: 9pt;')

    def parse_hex(self, text):
        """
        解析16进制字符串
        支持格式: "01 02 03" 或 "010203"
        """
        hex_str = text.replace(' ', '').replace('\n', '')

        if not all(c in '0123456789abcdefABCDEF' for c in hex_str):
            raise ValueError('16进制格式错误：只能包含 0-9 和 A-F')

        if len(hex_str) % 2 != 0:
            raise ValueError('16进制格式错误：字符数必须是偶数（每2个字符代表1个字节）')

        try:
            return bytes.fromhex(hex_str)
        except ValueError as e:
            raise ValueError(f'16进制解析失败: {e}')

    def parse_binary(self, text):
        """
        解析二进制字符串
        支持格式: "10101010 11110000" 或 "1010101011110000"
        """
        bin_str = text.replace(' ', '').replace('\n', '')

        if not all(c in '01' for c in bin_str):
            raise ValueError('二进制格式错误：只能包含 0 和 1')

        if len(bin_str) % 8 != 0:
            raise ValueError('二进制格式错误：长度必须是8的倍数（每8位代表1个字节）')

        try:
            byte_list = []
            for i in range(0, len(bin_str), 8):
                byte_chunk = bin_str[i:i+8]
                byte_list.append(int(byte_chunk, 2))
            return bytes(byte_list)
        except ValueError as e:
            raise ValueError(f'二进制解析失败: {e}')
