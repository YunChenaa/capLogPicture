"""界面日志的 UTF-8 分段归档，不依赖 Qt 或串口。"""

import tempfile
from datetime import datetime
from pathlib import Path


class RotatingLogArchive:
    def __init__(self, output_dir, max_lines=20000):
        if max_lines < 1:
            raise ValueError("每个日志文件至少保留一行")
        self.output_dir = Path(output_dir)
        self.max_lines = max_lines
        self.directory = None
        self.stream = None
        self.part = 0
        self.line_count = 0

    def _open_part(self):
        if self.directory is None:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            prefix = f"autoburn_{datetime.now():%Y%m%d_%H%M%S}_"
            self.directory = Path(tempfile.mkdtemp(prefix=prefix, dir=self.output_dir))
        self.part += 1
        self.stream = (self.directory / f"part_{self.part:05d}.log").open(
            "x", encoding="utf-8", errors="replace", newline="\n",
        )
        self.line_count = 0

    def append(self, text):
        # 一次信号可能包含多行，也可能超过一个文件的容量；空行同样保留。
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        offset = 0
        while offset < len(lines):
            if self.stream is None:
                self._open_part()
            count = min(self.max_lines - self.line_count, len(lines) - offset)
            self.stream.write("\n".join(lines[offset:offset + count]) + "\n")
            self.stream.flush()
            self.line_count += count
            offset += count
            if self.line_count == self.max_lines:
                self.close()

    def close(self):
        if self.stream is not None:
            stream = self.stream
            self.stream = None
            stream.close()
