#!/usr/bin/env python3
"""随机生成擦除特定范围的 OTA bin 文件。

按权重分配不同类型文件的生成数量，将擦除后的文件放在各自的文件夹中。
"""

import random
import sys
from pathlib import Path

# 导入 full_bin_ff 模块的功能
sys.path.insert(0, str(Path(__file__).parent))
from full_bin_ff import fill_ff


# 配置信息：文件后缀、擦除范围（字节）、权重
CONFIGS = [
    {
        "suffix": "p1",
        "min_erase": 20 * 1024,
        "max_erase": 452608,
        "step": 20 * 1024,
        "weight": 4,  # 最少
        "output_dir": "p1_erased"
    },
    {
        "suffix": "p10",
        "min_erase": 20 * 1024,
        "max_erase": 6977805,
        "step": 20 * 1024,
        "weight": 56,  # 最多（340/611 ≈ 56%）
        "output_dir": "p10_erased"
    },
    {
        "suffix": "p9",
        "min_erase": 20 * 1024,
        "max_erase": 917504,
        "step": 20 * 1024,
        "weight": 7,
        "output_dir": "p9_erased"
    },
    {
        "suffix": "p2",
        "min_erase": 20 * 1024,
        "max_erase": 812400,
        "step": 20 * 1024,
        "weight": 7,
        "output_dir": "p2_erased"
    },
    {
        "suffix": "p33",
        "min_erase": 20 * 1024,
        "max_erase": 3357206,
        "step": 20 * 1024,
        "weight": 27,
        "output_dir": "p33_erased"
    },
]


def generate_erase_positions(config, count):
    """为指定配置生成随机擦除位置列表。"""
    min_val = config["min_erase"]
    max_val = config["max_erase"]
    step = config["step"]

    # 计算可能的擦除大小（按 step 对齐）
    possible_values = list(range(min_val, max_val + 1, step))

    # 随机选择不重复的擦除大小
    if count > len(possible_values):
        # 如果需要的数量超过可能值，允许重复
        return random.choices(possible_values, k=count)
    else:
        return random.sample(possible_values, count)


def main():
    total_files = 200
    ota_packet_dir = Path(__file__).parent / "OtaPacket"
    output_base = Path(__file__).parent / "OtaPacket_erased"

    if not ota_packet_dir.exists():
        print(f"错误: OtaPacket 目录不存在: {ota_packet_dir}")
        return 1

    # 按权重分配文件数量
    total_weight = sum(c["weight"] for c in CONFIGS)
    file_counts = []
    allocated = 0

    for i, config in enumerate(CONFIGS):
        if i == len(CONFIGS) - 1:
            # 最后一个配置分配剩余数量
            count = total_files - allocated
        else:
            count = round(total_files * config["weight"] / total_weight)
            allocated += count
        file_counts.append(count)
        print(f"{config['suffix']} 文件将生成 {count} 个")

    print(f"\n总计: {sum(file_counts)} 个文件\n")

    # 为每种类型生成文件
    generated_count = 0

    for config, count in zip(CONFIGS, file_counts):
        suffix = config["suffix"]
        output_dir = output_base / config["output_dir"]
        output_dir.mkdir(parents=True, exist_ok=True)

        # 查找源文件
        source_files = list(ota_packet_dir.glob(f"*_{suffix}.bin"))
        if not source_files:
            print(f"警告: 未找到 {suffix} 类型的源文件，跳过")
            continue

        source_file = source_files[0]
        source_size = source_file.stat().st_size

        # 调整最大擦除大小不超过文件大小
        actual_max = min(config["max_erase"], source_size)
        adjusted_config = {**config, "max_erase": actual_max}

        print(f"处理 {source_file.name} (大小: {source_size:,} 字节)")

        # 生成随机擦除位置
        erase_sizes = generate_erase_positions(adjusted_config, count)

        # 生成文件
        for erase_size in erase_sizes:
            # 计算擦除起始位置（从文件末尾往前）
            offset = source_size - erase_size

            # 生成输出文件名：原文件名_erase_起始位置_擦除长度.bin
            base_name = source_file.stem  # 去掉 .bin 后缀
            output_name = f"{base_name}_erase_{offset}_{erase_size}.bin"
            output_file = output_dir / output_name

            try:
                fill_ff(str(source_file), str(output_file), offset, erase_size)
                generated_count += 1
                print(f"  [{generated_count}/{total_files}] 生成: {output_name}")
            except Exception as exc:
                print(f"  错误: 生成 {output_name} 失败: {exc}")

        print()

    print(f"完成! 共生成 {generated_count} 个文件")
    print(f"输出目录: {output_base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
