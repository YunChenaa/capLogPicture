#!/usr/bin/env python3
"""将二进制文件指定区域填充为 0xFF，输出到新文件，不修改源文件。

用法：
    python full_bin_ff.py <源文件> <输出文件> <偏移> <长度>

偏移和长度支持 B、K、M、G 单位（1024 进制，大小写不敏感），
也支持 0x 开头的十六进制数。例如：0x1000、4096K、1M、2GB。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path


_SIZE_RE = re.compile(
    r"^([0-9]+|0[xX][0-9a-fA-F]+)([BbKkMmGg]?[Bb]?)$"
)
_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024,
          "m": 1024**2, "mb": 1024**2,
          "g": 1024**3, "gb": 1024**3}


def parse_size(value: str) -> int:
    """解析带单位的尺寸，返回字节数。"""
    compact = re.sub(r"\s+", "", value)
    match = _SIZE_RE.fullmatch(compact)
    if not match:
        raise ValueError(f"无法解析尺寸: {value}")

    number, unit = match.groups()
    base = int(number, 16) if number.lower().startswith("0x") else int(number, 10)
    return base * _UNITS[unit.lower()]


def _same_file(source: Path, destination: Path) -> bool:
    """检查输出路径是否与源文件相同，包括 Windows 下的硬链接情况。"""
    try:
        return os.path.samefile(source, destination)
    except (FileNotFoundError, OSError):
        return source.resolve() == destination.resolve()


def fill_ff(source_name: str, destination_name: str, offset: int, length: int) -> None:
    source = Path(source_name)
    destination = Path(destination_name)

    if not source.is_file():
        raise ValueError(f"源文件不存在: {source}")
    if length <= 0:
        raise ValueError("长度必须大于 0")
    if offset < 0:
        raise ValueError("偏移不能小于 0")
    if _same_file(source, destination):
        raise ValueError(f"输出文件不能与源文件相同: {source}")

    # 先完整复制，保证源文件不被修改，并保留文件的其他内容。
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    size = destination.stat().st_size

    if offset >= size:
        print(f"跳过: 偏移 {offset} 已超出文件大小 {size} 字节，"
              "已复制源文件但未做修改")
        return

    end = offset + length
    if end > size:
        print(f"提示: 请求区域 [{offset}, {end}) 超出文件末尾，"
              f"自动裁剪为 [{offset}, {size})", file=sys.stderr)
        length = size - offset

    # 分块写入，避免一次性为超大区域分配内存；r+b 不会改变文件长度。
    ff_block = b"\xff" * (1024 * 1024)
    remaining = length
    with destination.open("r+b") as output:
        output.seek(offset)
        while remaining:
            count = min(remaining, len(ff_block))
            output.write(ff_block[:count])
            remaining -= count

    print(f"完成: 已将 {source} 的 [{offset}, {offset + length}) 共 "
          f"{length} 字节填充为 0xFF，输出到 {destination}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将二进制文件指定区域填充为 0xFF，输出到新文件。"
    )
    parser.add_argument("source", help="原始二进制文件（不会被修改）")
    parser.add_argument("destination", help="输出文件（会被创建/覆盖）")
    parser.add_argument("offset", help="起始偏移，例如 0x1000、4096K")
    parser.add_argument("length", help="填充长度，例如 0x200、512K")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        offset = parse_size(args.offset)
        length = parse_size(args.length)
        fill_ff(args.source, args.destination, offset, length)
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
