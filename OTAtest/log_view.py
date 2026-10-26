"""可暂停自动跟随的日志显示，不影响后台接收和归档。"""

from PySide6.QtWidgets import QPlainTextEdit


class LogView(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.follow_latest = True
        self._appending = False
        self.verticalScrollBar().valueChanged.connect(self._scroll_changed)

    def _scroll_changed(self, value):
        if not self._appending:
            self.follow_latest = value >= self.verticalScrollBar().maximum()

    def appendPlainText(self, text):
        bar = self.verticalScrollBar()
        follow = self.follow_latest
        self._appending = True
        try:
            # Qt 自身会保留非底部视口；不要移动文本光标，以免破坏选择和历史位置。
            super().appendPlainText(text)
            if follow:
                bar.setValue(bar.maximum())
        finally:
            self._appending = False
            self.follow_latest = follow

    def jump_to_latest(self):
        self.follow_latest = True
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def clear(self):
        super().clear()
        self.follow_latest = True
