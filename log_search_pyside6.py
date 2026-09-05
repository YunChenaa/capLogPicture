"""
串口日志查询模块 - PySide6版本
提供日志搜索功能，支持不区分大小写和完全匹配选项
"""
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QCheckBox, QMessageBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor, QColor, QTextCharFormat


class LogSearchWindow(QDialog):
    """日志查询窗口"""

    def __init__(self, parent, log_text_widget):
        """
        Args:
            parent: 父窗口
            log_text_widget: 主窗口的 QTextEdit 控件（用于搜索和高亮）
        """
        super().__init__(parent)
        self.log_text = log_text_widget
        self.current_match_index = 0
        self.match_positions = []

        self.setWindowTitle('日志查询')
        self.setMinimumSize(450, 180)
        self.setMaximumSize(450, 180)

        self.setup_ui()

    def setup_ui(self):
        """构建查询界面"""
        layout = QVBoxLayout(self)

        # 查询输入框
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel('查询内容:'))

        self.search_entry = QLineEdit()
        self.search_entry.returnPressed.connect(self.search)
        search_layout.addWidget(self.search_entry)

        layout.addLayout(search_layout)

        # 选项区域
        options_layout = QHBoxLayout()

        self.case_sensitive_check = QCheckBox('区分大小写')
        self.case_sensitive_check.toggled.connect(self.clear_results)
        options_layout.addWidget(self.case_sensitive_check)

        self.exact_match_check = QCheckBox('完全匹配')
        self.exact_match_check.toggled.connect(self.clear_results)
        options_layout.addWidget(self.exact_match_check)

        options_layout.addStretch()

        layout.addLayout(options_layout)

        # 结果显示
        self.result_label = QLabel('输入关键词后点击"查询"')
        self.result_label.setStyleSheet('color: #666666;')
        layout.addWidget(self.result_label)

        # 按钮区域
        button_layout = QHBoxLayout()

        btn_search = QPushButton('🔍 查询')
        btn_search.clicked.connect(self.search)
        button_layout.addWidget(btn_search)

        self.prev_button = QPushButton('⬆ 上一个')
        self.prev_button.clicked.connect(self.find_previous)
        self.prev_button.setEnabled(False)
        button_layout.addWidget(self.prev_button)

        self.next_button = QPushButton('⬇ 下一个')
        self.next_button.clicked.connect(self.find_next)
        self.next_button.setEnabled(False)
        button_layout.addWidget(self.next_button)

        button_layout.addStretch()

        layout.addLayout(button_layout)

    def clear_results(self):
        """清除之前的查询结果"""
        # 清除高亮
        cursor = self.log_text.textCursor()
        cursor.select(QTextCursor.Document)
        format = QTextCharFormat()
        cursor.mergeCharFormat(format)

        self.match_positions = []
        self.current_match_index = 0
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)

    def search(self):
        """执行查询"""
        keyword = self.search_entry.text()

        if not keyword:
            QMessageBox.warning(self, '提示', '请输入查询内容')
            return

        # 清除之前的结果
        self.clear_results()

        case_sensitive = self.case_sensitive_check.isChecked()
        exact_match = self.exact_match_check.isChecked()

        # 获取所有文本
        content = self.log_text.toPlainText()

        if not content:
            self.result_label.setText('日志为空，无内容可查询')
            self.result_label.setStyleSheet('color: #c0392b;')
            return

        # 执行搜索
        self.match_positions = self.find_all_matches(
            content, keyword, case_sensitive, exact_match
        )

        if not self.match_positions:
            self.result_label.setText(f'未找到 "{keyword}"')
            self.result_label.setStyleSheet('color: #c0392b;')
            return

        # 显示结果并高亮
        count = len(self.match_positions)
        self.result_label.setText(f'找到 {count} 处匹配')
        self.result_label.setStyleSheet('color: #2e8b57;')

        # 高亮所有匹配项
        self.highlight_matches(keyword)

        # 跳转到第一个匹配项
        self.current_match_index = 0
        self.jump_to_current_match()

        # 启用导航按钮
        if count > 1:
            self.next_button.setEnabled(True)
            self.prev_button.setEnabled(True)

    def find_all_matches(self, content, keyword, case_sensitive, exact_match):
        """
        查找所有匹配项

        Returns:
            list: [start_pos, ...]
        """
        matches = []

        search_content = content if case_sensitive else content.lower()
        search_keyword = keyword if case_sensitive else keyword.lower()

        start_pos = 0

        while True:
            pos = search_content.find(search_keyword, start_pos)
            if pos == -1:
                break

            # 如果是完全匹配模式，检查边界
            if exact_match:
                before_ok = (pos == 0 or not search_content[pos - 1].isalnum())
                after_ok = (
                    pos + len(search_keyword) >= len(search_content) or
                    not search_content[pos + len(search_keyword)].isalnum()
                )

                if not (before_ok and after_ok):
                    start_pos = pos + 1
                    continue

            matches.append(pos)
            start_pos = pos + 1

        return matches

    def highlight_matches(self, keyword):
        """高亮所有匹配项"""
        cursor = self.log_text.textCursor()

        # 创建高亮格式
        highlight_format = QTextCharFormat()
        highlight_format.setBackground(QColor('#ffff00'))
        highlight_format.setForeground(QColor('#000000'))

        # 移动到文档开始
        cursor.movePosition(QTextCursor.Start)

        # 查找并高亮
        flags = QTextCursor.FindFlags()
        if self.case_sensitive_check.isChecked():
            flags |= QTextCursor.FindCaseSensitively

        while True:
            cursor = self.log_text.document().find(keyword, cursor, flags)
            if cursor.isNull():
                break

            # 如果是完全匹配，需要额外检查
            if self.exact_match_check.isChecked():
                pos = cursor.position() - len(keyword)
                content = self.log_text.toPlainText()

                before_ok = (pos == 0 or not content[pos - 1].isalnum())
                after_ok = (
                    cursor.position() >= len(content) or
                    not content[cursor.position()].isalnum()
                )

                if not (before_ok and after_ok):
                    continue

            cursor.mergeCharFormat(highlight_format)

    def find_next(self):
        """跳转到下一个匹配项"""
        if not self.match_positions:
            return

        self.current_match_index = (self.current_match_index + 1) % len(self.match_positions)
        self.jump_to_current_match()

    def find_previous(self):
        """跳转到上一个匹配项"""
        if not self.match_positions:
            return

        self.current_match_index = (self.current_match_index - 1) % len(self.match_positions)
        self.jump_to_current_match()

    def jump_to_current_match(self):
        """跳转到当前匹配项并更新标签"""
        if not self.match_positions:
            return

        pos = self.match_positions[self.current_match_index]

        # 移动光标到该位置
        cursor = self.log_text.textCursor()
        cursor.setPosition(pos)
        self.log_text.setTextCursor(cursor)
        self.log_text.ensureCursorVisible()

        # 更新结果标签
        total = len(self.match_positions)
        current = self.current_match_index + 1
        keyword = self.search_entry.text()
        self.result_label.setText(f'找到 {total} 处匹配 - 当前第 {current} 处')
        self.result_label.setStyleSheet('color: #2e8b57;')

    def showEvent(self, event):
        """显示事件 - 窗口显示时聚焦输入框"""
        super().showEvent(event)
        self.search_entry.setFocus()

    def closeEvent(self, event):
        """关闭窗口时清除高亮"""
        self.clear_results()
        super().closeEvent(event)
