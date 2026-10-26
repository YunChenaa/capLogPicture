"""OTA 掉电梯度压测，复用标准升级、串口重连及 BAT 还原。"""

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QCheckBox, QLabel, QSpinBox
from ota_stress import OtaStressConfig, OtaStressDialog


@dataclass(frozen=True)
class PowerStressConfig(OtaStressConfig):
    transfer_cut: bool = False
    percent_step: int = 10
    delayed_cut: bool = False
    delay_step_ms: int = 100

    @property
    def power_cut_enabled(self):
        return self.use_test and (self.transfer_cut or self.delayed_cut)

    def validate(self, relay_port):
        if not self.use_test and (self.transfer_cut or self.delayed_cut):
            raise ValueError("掉电只能用于步骤 1：请勾选测试包升级或取消掉电选项")
        if self.transfer_cut and not 1 <= self.percent_step <= 99:
            raise ValueError("进度梯度须为 1～99%")
        if self.delayed_cut and self.delay_step_ms < 1:
            raise ValueError("延时梯度至少 1ms")
        return super().validate(relay_port)

    def percentages(self):
        return range(self.percent_step, 100, self.percent_step) if self.transfer_cut else ()


class PowerStressDialog(OtaStressDialog):
    config_type = PowerStressConfig
    settings_prefix = "ota/"
    title = "OTA 压测（步骤组合 / 可选掉电）"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.flow_note.setText("步骤按 1 → 2 → 3 执行，只执行勾选项；至少选择一步。\n"
                               "不选掉电即正常 OTA；掉电仅注入测试包，重启观察后执行其余选定步骤。\n"
                               "不选自动烧录时不会还原基线，下一点使用上一点留下的版本。")
        for checkbox in (self.use_test, self.use_standard, self.transfer_cut, self.delayed_cut):
            checkbox.toggled.connect(self._update_options)
        self._update_options()

    def _update_options(self):
        if not self.use_test.isChecked():
            self.transfer_cut.setChecked(False)
            self.delayed_cut.setChecked(False)
        self.transfer_cut.setEnabled(self.use_test.isChecked())
        self.delayed_cut.setEnabled(self.use_test.isChecked())
        self.percent_step.setEnabled(self.use_test.isChecked() and self.transfer_cut.isChecked())
        self.delay_step_ms.setEnabled(self.use_test.isChecked() and self.delayed_cut.isChecked())
        needs_ota = self.use_test.isChecked() or self.use_standard.isChecked()
        for widget in (self.ota_port, self.log_port, self.ota_baudrate, self.log_baudrate):
            widget.setEnabled(needs_ota)

    def _extra_fields(self, form):
        self.transfer_cut = QCheckBox("传输进度掉电（不含 0% 和 100%）")
        self.percent_step = QSpinBox()
        self.percent_step.setRange(1, 99)
        self.percent_step.setSuffix(" %")
        self.delayed_cut = QCheckBox("100% 后延时掉电（n、2n、3n…，成功先到则结束梯度）")
        self.delay_step_ms = QSpinBox()
        self.delay_step_ms.setRange(1, 86400000)
        self.delay_step_ms.setSuffix(" ms")
        form.addRow(self.transfer_cut)
        form.addRow("传输进度梯度", self.percent_step)
        form.addRow(self.delayed_cut)
        form.addRow("100% 后延时梯度", self.delay_step_ms)
        note = QLabel("每轮使用一个测试包扫完所选梯度；两种均选时先测进度，再测延时。\n"
                      "每点重启观察后，仅执行已勾选的标准升级 / 自动烧录。\n"
                      "进度按已确认包数计算；机械继电器不能保证毫秒级断电精度。\n"
                      "延时最大不超过完成超时；一直没有成功标志则报错停止。")
        note.setWordWrap(True)
        form.addRow(note)


class PointFinished(Exception):
    pass


class PowerStressMixin:
    def setup_stress(self, config, files):
        super().setup_stress(config, files)
        self.completed_sweeps = 0

    def enqueue_manual(self, channel, enabled):
        # 人工操作不能混入定时掉电实验。
        if self.ota_config.power_cut_enabled:
            return False
        return super().enqueue_manual(channel, enabled)

    def _statistics(self):
        stats = super()._statistics()
        stats.update(completed=self.completed_sweeps, mode="OTA 压测",
                     use_test=self.ota_config.use_test, points=len(self.round_records))
        return stats

    def _summary_text(self):
        stats = self._statistics()
        results = [r['test_ota'] for r in self.round_records if isinstance(r.get('test_ota'), dict)]
        cuts = sum(r.get("cut", False) for r in results)
        commands = sum(r.get("cut_command_sent", False) for r in results)
        skipped = "，已跳过：" + "、".join(name for name, enabled in (
            ("测试包", self.ota_config.use_test), ("标准包", self.ota_config.use_standard),
            ("自动烧录", self.ota_config.use_restore)) if not enabled)
        return (f"OTA 压测：完成 {self.completed_sweeps}/{self.ota_config.rounds} 轮，"
                f"记录 {len(self.round_records)} 个测试点，已发掉电指令 {commands} 次、确认 OFF {cuts} 次，"
                f"测试包 Recovery {stats['test_recovery']} 次，标准包 Recovery {stats['standard_recovery']} 次"
                + (skipped if not all((self.ota_config.use_test, self.ota_config.use_standard, self.ota_config.use_restore)) else ""))

    def _round_result_line(self, record):
        if record.get("cut_mode") == "normal":
            return super()._round_result_line(record)
        result = record.get("test_ota", {})
        detail = (f" | 方式：{record['cut_mode']} | 目标：{record['target']}"
                  f" | 掉电指令已发：{result.get('cut_command_sent', '未完成')}"
                  f" | OFF 已确认：{result.get('cut_confirmed', '未完成')}"
                  f" | 发送指令延时(ms)：{result.get('actual_delay_ms', '不适用')}"
                  f" | 实际进度(%)：{result.get('actual_percent', '未完成')}")
        return super()._round_result_line(record) + detail

    def _power_test(self, path, mode, target, result):
        cfg = self.ota_config
        self.monitor.disarm()
        firmware = Path(path).read_bytes()
        if not firmware:
            raise RuntimeError("测试包为空")
        self.relay.set_channel(self.config.boot_channel, False)
        self.emit_log(f"掉电测试：{path} | {mode}={target}")
        self.phase.emit(f"掉电测试：{mode}={target}")
        with self._connect_ota("掉电测试包") as connection:
            self.monitor.arm_success()

            def on_packet(count, total, acknowledged_at):
                self._check_log_port()
                if mode == "percent":
                    if count * 100 < target * total:
                        return
                    if count == total:
                        raise RuntimeError("分包数量不足，目标梯度落在 100%；请减小分包大小")
                    origin = acknowledged_at
                else:
                    if count != total:
                        return
                    origin = acknowledged_at
                    deadline = origin + target / 1000
                    while not self.monitor.success.is_set():
                        self._check_log_port()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self.monitor.success.wait(min(0.001, remaining))
                self._check_log_port()
                result.update(actual_percent=count * 100 / total, cut=False, cut_command_sent=False,
                              cut_confirmed=False, actual_delay_ms=None)
                def on_sent(sent_at):
                    result.update(cut_command_sent=True, actual_delay_ms=(sent_at - origin) * 1000)
                sent_at = self.relay.cut_power(self.config.power_channel, self.monitor.success, on_sent=on_sent)
                result.update(cut=sent_at is not None, cut_confirmed=sent_at is not None,
                              success_before_cut=sent_at is None)
                self.emit_log("掉电测试点：" + json.dumps(result, ensure_ascii=False))
                raise PointFinished()

            # 提前检查梯度能否落在最后一包之前，避免误测首尾。
            if mode == "percent":
                total = math.ceil(len(firmware) / cfg.packet_size)
                if math.ceil(target * total / 100) >= total:
                    raise RuntimeError("分包数量不足，目标梯度落在 100%；请减小分包大小")
            try:
                self._send_ota(connection, firmware, on_packet=on_packet)
            except PointFinished:
                pass
            else:
                raise RuntimeError("未触发预期掉电测试点")
        if not result["cut"]:
            self.emit_log(f"升级结束标识 {cfg.ota_success_keyword!r} 先于掉电指令到达，本次未注入掉电")
            self._stress_wait(cfg.success_delay, service_manual=False)
            self.relay.set_channel(self.config.power_channel, False)
        self._stress_wait(cfg.power_off_delay, service_manual=False)
        self.monitor.arm_recovery()
        self.relay.set_channel(self.config.power_channel, True)
        self._stress_wait(cfg.recovery_window, service_manual=False)
        result["recovery"] = self.monitor.recovery.is_set()
        self.monitor.disarm()
        self.emit_log(f"掉电测试重启观察：Recovery={result['recovery']}（未检出不代表启动正常）")
        return result

    def _run_point(self, round_number, path, mode, target):
        cfg = self.ota_config
        result = {"path": path}
        record = {"round": round_number, "test_package": path or "已跳过",
                  "standard_package": cfg.standard if cfg.use_standard else None, "cut_mode": mode, "target": target,
                  "status": "running", "restored": False if cfg.use_restore else "skipped", "stage": "开始",
                  "started_at": datetime.now().isoformat(timespec="seconds"),
                  "test_ota": result if cfg.use_test else "skipped",
                  "standard_ota": None if cfg.use_standard else "skipped"}
        self.active_record = record
        self._publish_statistics()
        try:
            self._check_log_port()
            if cfg.use_test:
                record["stage"] = "测试包升级" if mode == "normal" else "测试包掉电"
                if mode == "normal":
                    result.update(self._ota_stage(path, "测试包升级"))
                else:
                    self._power_test(path, mode, target, result)
                self._publish_statistics()
            if cfg.use_standard:
                record["stage"] = "标准包完整升级"
                record["standard_ota"] = self._ota_stage(cfg.standard, "标准包完整升级")
                self._publish_statistics()
            if cfg.use_restore:
                record["stage"] = "模组自动烧录"
                self._restore_version()
                record["restored"] = True
            record.update(status="completed", stage="完成")
        except Exception as exc:
            record.update(status="stopped" if self.stop_event.is_set() else "failed", error=str(exc) or "用户停止")
            raise
        finally:
            record["finished_at"] = datetime.now().isoformat(timespec="seconds")
            self.round_records.append(record)
            self.active_record = None
            self._publish_statistics()
            self.emit_log(self._round_result_line(record))
            if self.report_stream is not None:
                self.report_stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                self.report_stream.flush()
        return result

    def _run_rounds(self):
        cfg = self.ota_config
        for index in range(cfg.rounds):
            self._check_log_port()
            path = self.ota_files[index % len(self.ota_files)] if cfg.use_test else None
            self.emit_log(f"========== OTA 压测第 {index + 1}/{cfg.rounds} 轮 ==========")
            if not cfg.power_cut_enabled:
                self._run_point(index + 1, path, "normal", None)
            for percent in cfg.percentages():
                self._check_log_port()
                self._run_point(index + 1, path, "percent", percent)
            if cfg.delayed_cut:
                delay = cfg.delay_step_ms
                while True:
                    self._check_log_port()
                    # 最后一个测试点封顶到完成超时，不能无界扫描。
                    target = min(delay, cfg.completion_timeout * 1000)
                    result = self._run_point(index + 1, path, "delay_ms", target)
                    if result["success_before_cut"]:
                        self.emit_log("升级成功先到，结束本轮延时掉电梯度")
                        break
                    if target >= cfg.completion_timeout * 1000:
                        raise RuntimeError("延时梯度已到完成超时上限，仍未在掉电前检测到成功标志")
                    delay += cfg.delay_step_ms
            self.completed_sweeps += 1
            self._publish_statistics()
