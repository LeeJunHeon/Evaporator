from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


PROCESS_CONFIG_TOOLTIPS = {
    "adc_max": "공정 중 허용되는 최대 ADC 값.\nramp 및 Hold 중 ADC가 이 값 이상이면 DAC 증가를 중단합니다.\n기본값: 200",
    "dac_max": "공정 중 허용되는 최대 DAC 값.\n이 값을 초과하면 공정이 중단됩니다.",
    "rate_tol_ratio": "dep.rate 안정 판정 허용 오차 비율.\n예: 0.05 → 목표 rate의 ±5% 이내면 안정으로 판정.",
    "rate_stable_sec": "dep.rate가 허용 오차 내에서 연속으로 유지돼야 하는 시간(초).\n이 시간 동안 안정 상태가 유지되면 Hold 구간으로 진입합니다.",
    "hold_control_interval_s": "Hold 구간에서 DAC를 업데이트하는 주기(초).\n짧을수록 더 자주 조정하지만 노이즈에 민감해집니다.",
    "fine_step_dac": "Hold 구간에서 DAC를 조정할 때의 기본 단위.\nSTEP 모드에서는 이 값으로 증감합니다.",
    "hold_control_mode": "Hold 구간 제어 방식.\nPI: 비례+적분 제어 (권장)\nPID: 비례+적분+미분 제어\nSTEP: 단순 단계 제어 (안전 fallback)",
    "hold_pi_kp": "PI/PID 제어의 비례 게인 (Kp).\n값이 클수록 오차에 빠르게 반응하지만 불안정해질 수 있습니다.",
    "hold_pi_ki": "PI/PID 제어의 적분 게인 (Ki).\n지속적인 오차를 보정합니다. 너무 크면 진동이 생길 수 있습니다.",
    "hold_pi_kd": "PID 제어의 미분 게인 (Kd).\nrate 변화 속도에 반응해 오버슈트를 줄입니다.\n기본값 0.0 (PI와 동일). 노이즈 민감하므로 작은 값부터 시작하세요.",
    "hold_integral_limit": "적분항의 최대 절댓값 (Anti-windup).\n적분이 과도하게 쌓이는 것을 방지합니다.",
    "rate_filter_alpha": "dep.rate EMA 필터 강도 (0~1).\n값이 작을수록 더 강하게 스무딩됩니다. 기본값 0.35",
    "rate_jump_guard_ratio": "rate 순간 급변 억제 비율.\n이전 값 대비 이 비율 이상 변하면 변화량을 제한합니다.",
    "rate_jump_guard_abs": "rate 순간 급변 억제 절댓값 (Å/s).\n한 번에 이 값 이상 변하면 변화량을 제한합니다.",
    "hold_max_dac_delta": "Hold 구간에서 1회 DAC 변화 최대값.\n급격한 DAC 변동을 방지합니다.",
    "rate_abort_ratio": "물질 부족 판정 기준 비율.\n예: 0.3 → dep.rate가 목표의 30% 이하로 떨어지면 카운트 시작.",
    "rate_abort_sec": "물질 부족 판정 지속 시간(초).\n위 비율 이하 상태가 이 시간 이상 지속되면 공정을 종료합니다.",
    "sensor_none_abort_s": "STM 센서 신호 없음 허용 시간(초).\n이 시간 동안 dep.rate 값이 없으면 공정을 종료합니다.",
    "adc_none_abort_s": "ADC 신호 없음 허용 시간(초).\n이 시간 동안 ADC 값이 없으면 공정을 종료합니다.",
    "spike_abort_ratio": "STM 스파이크 감지 기준 배율.\n예: 3.0 → dep.rate가 목표의 3배 이상이면 센서 오류로 간주.",
    "spike_grace_s": "스파이크 감지 후 물질 부족 판정 유예 시간(초).\n이 시간 동안은 rate가 낮아도 공정을 종료하지 않습니다.",
    "ramp_spike_pct": (
        "Ramp-up 중 스파이크 필터 기준 (%).\n"
        "dep.rate가 목표값의 (100 + 이 값)% 이상이면 센서 스파이크로 간주하여\n"
        "stable 카운트를 올리지 않습니다.\n"
        "예: 100 → 목표의 2배 이상이면 스파이크.\n"
        "예: 50 → 목표의 1.5배 이상이면 스파이크."
    ),
    "ramp_spike_abort_sec": (
        "Ramp-up 중 스파이크 지속 허용 시간 (초).\n"
        "dep.rate이 스파이크 판정 기준 이상으로 이 시간을 초과하면 공정을 종료합니다.\n"
        "예: 10 → 10초 이상 지속되면 공정 종료."
    ),
    "pre_hold_entry_ratio": (
        "Pre-Hold 진입 배율 (target 대비).\n"
        "Ramp 중 dep.rate이 목표 rate의 이 배수 이상으로 진입 지속 시간만큼 지속되면\n"
        "shutter를 열지 않고 PID로 rate를 안정화하는 Pre-Hold 모드에 진입합니다.\n"
        "예: 2.0 → 목표의 2배 이상이면 Pre-Hold 진입."
    ),
    "pre_hold_entry_sec": (
        "Pre-Hold 진입 지속 시간 (초).\n"
        "dep.rate이 진입 배율 이상인 상태가 이 시간 이상 지속되면 Pre-Hold 모드로 전환합니다.\n"
        "예: 5.0 → 5초 이상 지속되면 진입."
    ),
    "pre_hold_ready_ratio": (
        "Pre-Hold 안정화 허용 범위 (±비율).\n"
        "Pre-Hold 중 dep.rate이 목표 rate의 ±이 비율 이내로 안정화되면\n"
        "STM ZERO 후 Main Shutter를 열고 정상 Hold 모드로 진입합니다.\n"
        "예: 0.3 → 목표의 ±30% 이내면 안정화 판정."
    ),
    "pre_hold_timeout_sec": (
        "Pre-Hold 최대 대기 시간 (초).\n"
        "이 시간 내에 rate가 안정화되지 않으면 공정을 종료합니다.\n"
        "0으로 설정 시 Pre-Hold 기능 비활성화."
    ),
}


class _NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class _NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class ProcessConfigDialog(QDialog):
    def __init__(self, initial_config: Optional[dict[str, Any]] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Process Config")
        self.setModal(True)
        self.resize(620, 620)
        self.setMinimumSize(560, 520)

        self._initial_config = self._normalize_config(initial_config or self._default_config())

        self._build_ui()
        self._load_config(self._initial_config)

    @staticmethod
    def _default_step() -> dict[str, Any]:
        return {
            "target_adc": 100.0,
            "dac_step": 10,
            "dac_interval_sec": 30.0,
            "hold_sec": 0.0,
        }

    def _default_config(self) -> dict[str, Any]:
        return {
            "step_count": 1,
            "ramp_steps": [self._default_step()],
            "dac_max": 4000,
            "adc_max": 200,
            "rate_tol_ratio": 0.05,
            "rate_stable_sec": 3.0,
            "hold_control_interval_s": 1.0,
            "fine_step_dac": 10,
            "hold_control_mode": "PID",
            "hold_pi_kp": 50.0,
            "hold_pi_ki": 8.0,
            "hold_pi_kd": 0.0,
            "hold_integral_limit": 2.5,
            "rate_filter_alpha": 0.35,
            "rate_jump_guard_ratio": 0.50,
            "rate_jump_guard_abs": 0.15,
            "hold_max_dac_delta": 10,
            "rate_abort_ratio": 0.30,
            "rate_abort_sec": 5.0,
            "sensor_none_abort_s": 5.0,
            "adc_none_abort_s": 5.0,
            "spike_abort_ratio": 3.0,
            "spike_grace_s": 5.0,
            "pre_hold_entry_ratio": 2.0,
            "pre_hold_entry_sec": 5.0,
            "pre_hold_ready_ratio": 0.3,
            "pre_hold_timeout_sec": 180.0,
        }

    def _normalize_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        src = dict(cfg or {})
        default = self._default_config()

        def _as_int(name: str, default_val: int, min_v: int | None = None, max_v: int | None = None) -> int:
            try:
                value = int(src.get(name, default_val))
            except Exception:
                value = int(default_val)
            if min_v is not None:
                value = max(min_v, value)
            if max_v is not None:
                value = min(max_v, value)
            return value

        def _as_float(name: str, default_val: float, min_v: float | None = None, max_v: float | None = None) -> float:
            try:
                value = float(src.get(name, default_val))
            except Exception:
                value = float(default_val)
            if min_v is not None:
                value = max(min_v, value)
            if max_v is not None:
                value = min(max_v, value)
            return value

        raw_steps = src.get("ramp_steps") or default["ramp_steps"]
        steps: list[dict[str, Any]] = []
        for item in list(raw_steps)[:10]:
            row = dict(item or {})
            try:
                target_adc = max(0.0, float(row.get("target_adc", 0.0)))
            except Exception:
                target_adc = 0.0
            try:
                dac_step = max(1, int(row.get("dac_step", 10)))
            except Exception:
                dac_step = 10
            try:
                dac_interval_sec = max(0.1, float(row.get("dac_interval_sec", 30.0)))
            except Exception:
                dac_interval_sec = 30.0
            try:
                hold_sec = max(0.0, float(row.get("hold_sec", row.get("delay_s", 0.0))))
            except Exception:
                hold_sec = 0.0
            steps.append(
                {
                    "target_adc": target_adc,
                    "dac_step": dac_step,
                    "dac_interval_sec": dac_interval_sec,
                    "hold_sec": hold_sec,
                }
            )
        if not steps:
            steps = [self._default_step()]

        fine_step_dac = _as_int("fine_step_dac", 10, 1)
        hold_max_dac_delta = _as_int("hold_max_dac_delta", fine_step_dac, 1)
        # Ki 기본값: hold_max_dac_delta * 0.8 → 스텝 크기 대비 적분 속도 자동 스케일링
        hold_pi_ki = _as_float("hold_pi_ki", max(0.0, hold_max_dac_delta * 0.8), 0.0)
        hold_control_mode = str(src.get("hold_control_mode", default["hold_control_mode"]) or "").strip().upper() or "PID"
        if hold_control_mode not in {"PI", "PID", "STEP"}:
            hold_control_mode = "PID"

        return {
            "step_count": len(steps),
            "ramp_steps": steps,
            "dac_max": _as_int("dac_max", 4000, 1),
            "adc_max": _as_int("adc_max", 200, 1),
            "rate_tol_ratio": _as_float("rate_tol_ratio", 0.05, 0.001, 1.0),
            "rate_stable_sec": _as_float("rate_stable_sec", 3.0, 0.0),
            "hold_control_interval_s": _as_float("hold_control_interval_s", 1.0, 0.1),
            "fine_step_dac": fine_step_dac,
            "hold_control_mode": hold_control_mode,
            "hold_pi_kp": _as_float("hold_pi_kp", max(1.0, hold_max_dac_delta * 5.0), 0.0),
            "hold_pi_ki": hold_pi_ki,
            "hold_pi_kd": _as_float("hold_pi_kd", 0.0, 0.0),
            # integral_limit 기본값: 적분이 2스텝치 이상 쌓이지 않도록 Ki 크기 역산
            "hold_integral_limit": _as_float(
                "hold_integral_limit",
                max(1.0, (2.0 * hold_max_dac_delta) / max(hold_pi_ki, 1e-6)),
                0.1,
            ),
            "rate_filter_alpha": _as_float("rate_filter_alpha", 0.35, 0.01, 1.0),
            "rate_jump_guard_ratio": _as_float("rate_jump_guard_ratio", 0.50, 0.0),
            "rate_jump_guard_abs": _as_float("rate_jump_guard_abs", 0.15, 0.0),
            "hold_max_dac_delta": hold_max_dac_delta,
            "rate_abort_ratio": _as_float("rate_abort_ratio", 0.30, 0.001, 1.0),
            "rate_abort_sec": _as_float("rate_abort_sec", 5.0, 0.0),
            "sensor_none_abort_s": _as_float("sensor_none_abort_s", 5.0, 0.0),
            "adc_none_abort_s": _as_float("adc_none_abort_s", 5.0, 0.0),
            "spike_abort_ratio": _as_float("spike_abort_ratio", 3.0, 1.0),
            "spike_grace_s": _as_float("spike_grace_s", 5.0, 0.0),
            "pre_hold_entry_ratio": _as_float("pre_hold_entry_ratio", 2.0, 1.0),
            "pre_hold_entry_sec": _as_float("pre_hold_entry_sec", 5.0, 0.0),
            "pre_hold_ready_ratio": _as_float("pre_hold_ready_ratio", 0.3, 0.01, 1.0),
            "pre_hold_timeout_sec": _as_float("pre_hold_timeout_sec", 180.0, 0.0),
        }

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        info = QLabel("Config edits hold-control, filter, and safety parameters. Ramp-step recipe editing moved to Recipe.")
        info.setWordWrap(True)
        root.addWidget(info)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        root.addWidget(scroll, 1)

        body = QWidget(scroll)
        body_root = QVBoxLayout(body)
        body_root.setContentsMargins(4, 4, 4, 4)
        body_root.setSpacing(10)
        scroll.setWidget(body)

        hold_box = QGroupBox("Hold Control")
        hold_form = QFormLayout(hold_box)
        self.holdControlModeCombo = QComboBox(self)
        self.holdControlModeCombo.addItems(["PI", "PID", "STEP"])
        self.rateTolRatioSpin = self._make_double_spin(0.001, 1.0, step=0.01, decimals=3)
        self.rateStableSecSpin = self._make_double_spin(0.0, 60.0, step=0.5, decimals=1)
        self.holdControlIntervalSpin = self._make_double_spin(0.1, 60.0, step=0.5, decimals=1)
        self.fineStepDacSpin = self._make_int_spin(1, 1000, step=1)
        self.holdMaxDacDeltaSpin = self._make_int_spin(1, 1000, step=1)
        self.holdPiKpSpin = self._make_double_spin(0.0, 9999.0, step=1.0, decimals=3)
        self.holdPiKiSpin = self._make_double_spin(0.0, 9999.0, step=0.1, decimals=3)
        self.holdPiKdSpin = self._make_double_spin(0.0, 9999.0, step=0.1, decimals=3)
        self.holdIntegralLimitSpin = self._make_double_spin(0.1, 9999.0, step=0.1, decimals=3)

        self._add_form_row(hold_form, "제어 모드", self.holdControlModeCombo, "hold_control_mode")
        self._add_form_row(hold_form, "안정 판정 허용 오차", self.rateTolRatioSpin, "rate_tol_ratio")
        self._add_form_row(hold_form, "안정 유지 시간 (s)", self.rateStableSecSpin, "rate_stable_sec")
        self._add_form_row(hold_form, "DAC 업데이트 주기 (s)", self.holdControlIntervalSpin, "hold_control_interval_s")
        self._add_form_row(hold_form, "기본 DAC 조정 단위", self.fineStepDacSpin, "fine_step_dac")
        self._add_form_row(hold_form, "1회 최대 DAC 변화량", self.holdMaxDacDeltaSpin, "hold_max_dac_delta")
        self._add_form_row(hold_form, "비례 게인 (Kp)", self.holdPiKpSpin, "hold_pi_kp")
        self._add_form_row(hold_form, "적분 게인 (Ki)", self.holdPiKiSpin, "hold_pi_ki")
        self._add_form_row(hold_form, "미분 게인 (Kd)", self.holdPiKdSpin, "hold_pi_kd")
        self._add_form_row(hold_form, "적분 상한값", self.holdIntegralLimitSpin, "hold_integral_limit")
        body_root.addWidget(hold_box)

        filter_box = QGroupBox("Filter / Guard")
        filter_form = QFormLayout(filter_box)
        self.rateFilterAlphaSpin = self._make_double_spin(0.01, 1.0, step=0.01, decimals=3)
        self.rateJumpGuardRatioSpin = self._make_double_spin(0.0, 10.0, step=0.05, decimals=3)
        self.rateJumpGuardAbsSpin = self._make_double_spin(0.0, 10.0, step=0.01, decimals=3)
        self._add_form_row(filter_form, "Rate 필터 강도 (Alpha)", self.rateFilterAlphaSpin, "rate_filter_alpha")
        self._add_form_row(filter_form, "급변 억제 비율", self.rateJumpGuardRatioSpin, "rate_jump_guard_ratio")
        self._add_form_row(filter_form, "급변 억제 절댓값", self.rateJumpGuardAbsSpin, "rate_jump_guard_abs")
        body_root.addWidget(filter_box)

        safety_box = QGroupBox("Safety / Abort")
        safety_form = QFormLayout(safety_box)
        self.adcMaxSpin = self._make_int_spin(1, 9999, step=10)
        self.dacMaxSpin = self._make_int_spin(1, 9999, step=10)
        self.rateAbortRatioSpin = self._make_double_spin(0.001, 1.0, step=0.01, decimals=3)
        self.rateAbortSecSpin = self._make_double_spin(0.0, 99999.0, step=1.0, decimals=1)
        self.sensorNoneAbortSpin = self._make_double_spin(0.0, 99999.0, step=1.0, decimals=1)
        self.adcNoneAbortSpin = self._make_double_spin(0.0, 99999.0, step=1.0, decimals=1)
        self.spikeAbortRatioSpin = self._make_double_spin(1.0, 20.0, step=0.5, decimals=1)
        self.spikeGraceSSpin = self._make_double_spin(0.0, 30.0, step=0.5, decimals=1)
        self.rampSpikePctSpin = self._make_double_spin(10.0, 500.0, step=10.0, decimals=0)
        self.rampSpikeAbortSecSpin = self._make_double_spin(1.0, 120.0, step=1.0, decimals=0)
        self._add_form_row(safety_form, "최대 ADC", self.adcMaxSpin, "adc_max")
        self._add_form_row(safety_form, "최대 DAC", self.dacMaxSpin, "dac_max")
        self._add_form_row(safety_form, "물질 부족 판정 비율", self.rateAbortRatioSpin, "rate_abort_ratio")
        self._add_form_row(safety_form, "물질 부족 판정 시간 (s)", self.rateAbortSecSpin, "rate_abort_sec")
        self._add_form_row(safety_form, "센서 신호 없음 허용 시간 (s)", self.sensorNoneAbortSpin, "sensor_none_abort_s")
        self._add_form_row(safety_form, "ADC 신호 없음 허용 시간 (s)", self.adcNoneAbortSpin, "adc_none_abort_s")
        self._add_form_row(safety_form, "스파이크 감지 배율", self.spikeAbortRatioSpin, "spike_abort_ratio")
        self._add_form_row(safety_form, "스파이크 유예 시간 (s)", self.spikeGraceSSpin, "spike_grace_s")
        self._add_form_row(safety_form, "Ramp 스파이크 필터 (%)", self.rampSpikePctSpin, "ramp_spike_pct")
        self._add_form_row(safety_form, "Ramp 스파이크 허용 시간 (s)", self.rampSpikeAbortSecSpin, "ramp_spike_abort_sec")
        body_root.addWidget(safety_box)

        prehold_box = QGroupBox("Pre-Hold 대기")
        prehold_form = QFormLayout(prehold_box)
        self.preHoldEntryRatioSpin = self._make_double_spin(1.0, 20.0, step=0.1, decimals=1)
        self.preHoldEntrySecSpin = self._make_double_spin(0.0, 60.0, step=0.5, decimals=1)
        self.preHoldReadyRatioSpin = self._make_double_spin(0.01, 1.0, step=0.01, decimals=2)
        self.preHoldTimeoutSecSpin = self._make_double_spin(0.0, 600.0, step=10.0, decimals=0)
        self._add_form_row(prehold_form, "Pre-Hold 진입 배율 (target 대비)", self.preHoldEntryRatioSpin, "pre_hold_entry_ratio")
        self._add_form_row(prehold_form, "Pre-Hold 진입 지속 시간 (초)", self.preHoldEntrySecSpin, "pre_hold_entry_sec")
        self._add_form_row(prehold_form, "Pre-Hold 안정화 허용 범위 (±비율)", self.preHoldReadyRatioSpin, "pre_hold_ready_ratio")
        self._add_form_row(prehold_form, "Pre-Hold 최대 대기 시간 (초)", self.preHoldTimeoutSecSpin, "pre_hold_timeout_sec")
        body_root.addWidget(prehold_box)

        body_root.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _add_form_row(self, form: QFormLayout, label_text: str, widget: QWidget, tooltip_key: str) -> None:
        label = QLabel(label_text, self)
        tooltip = PROCESS_CONFIG_TOOLTIPS.get(tooltip_key, "")
        label.setToolTip(tooltip)
        widget.setToolTip(tooltip)
        form.addRow(label, widget)

    @staticmethod
    def _make_double_spin(min_val: float, max_val: float, *, step: float, decimals: int) -> QDoubleSpinBox:
        widget = _NoWheelDoubleSpinBox()
        widget.setDecimals(decimals)
        widget.setRange(min_val, max_val)
        widget.setSingleStep(step)
        widget.setAlignment(Qt.AlignmentFlag.AlignRight)
        widget.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        widget.setKeyboardTracking(False)
        return widget

    @staticmethod
    def _make_int_spin(min_val: int, max_val: int, *, step: int) -> QSpinBox:
        widget = _NoWheelSpinBox()
        widget.setRange(min_val, max_val)
        widget.setSingleStep(step)
        widget.setAlignment(Qt.AlignmentFlag.AlignRight)
        widget.setKeyboardTracking(False)
        return widget

    def _load_config(self, cfg: dict[str, Any]) -> None:
        cfg = self._normalize_config(cfg)
        self.holdControlModeCombo.setCurrentText(str(cfg.get("hold_control_mode", "PID") or "PID"))
        self.rateTolRatioSpin.setValue(float(cfg.get("rate_tol_ratio", 0.05)))
        self.rateStableSecSpin.setValue(float(cfg.get("rate_stable_sec", 3.0)))
        self.holdControlIntervalSpin.setValue(float(cfg.get("hold_control_interval_s", 1.0)))
        self.fineStepDacSpin.setValue(int(cfg.get("fine_step_dac", 10)))
        self.holdMaxDacDeltaSpin.setValue(int(cfg.get("hold_max_dac_delta", 10)))
        self.holdPiKpSpin.setValue(float(cfg.get("hold_pi_kp", 50.0)))
        self.holdPiKiSpin.setValue(float(cfg.get("hold_pi_ki", 8.0)))
        self.holdPiKdSpin.setValue(float(cfg.get("hold_pi_kd", 0.0)))
        self.holdIntegralLimitSpin.setValue(float(cfg.get("hold_integral_limit", 2.5)))
        self.rateFilterAlphaSpin.setValue(float(cfg.get("rate_filter_alpha", 0.35)))
        self.rateJumpGuardRatioSpin.setValue(float(cfg.get("rate_jump_guard_ratio", 0.50)))
        self.rateJumpGuardAbsSpin.setValue(float(cfg.get("rate_jump_guard_abs", 0.15)))
        self.adcMaxSpin.setValue(int(cfg.get("adc_max", 200)))
        self.dacMaxSpin.setValue(int(cfg.get("dac_max", 4000)))
        self.rateAbortRatioSpin.setValue(float(cfg.get("rate_abort_ratio", 0.30)))
        self.rateAbortSecSpin.setValue(float(cfg.get("rate_abort_sec", 5.0)))
        self.sensorNoneAbortSpin.setValue(float(cfg.get("sensor_none_abort_s", 5.0)))
        self.adcNoneAbortSpin.setValue(float(cfg.get("adc_none_abort_s", 5.0)))
        self.spikeAbortRatioSpin.setValue(float(cfg.get("spike_abort_ratio", 3.0)))
        self.spikeGraceSSpin.setValue(float(cfg.get("spike_grace_s", 5.0)))
        self.rampSpikePctSpin.setValue(float(cfg.get("ramp_spike_pct", 100.0)))
        self.rampSpikeAbortSecSpin.setValue(float(cfg.get("ramp_spike_abort_sec", 10.0)))
        self.preHoldEntryRatioSpin.setValue(float(cfg.get("pre_hold_entry_ratio", 2.0)))
        self.preHoldEntrySecSpin.setValue(float(cfg.get("pre_hold_entry_sec", 5.0)))
        self.preHoldReadyRatioSpin.setValue(float(cfg.get("pre_hold_ready_ratio", 0.3)))
        self.preHoldTimeoutSecSpin.setValue(float(cfg.get("pre_hold_timeout_sec", 180.0)))

    def get_config(self) -> dict[str, Any]:
        cfg = dict(self._initial_config)
        cfg.update(
            {
                "adc_max": int(self.adcMaxSpin.value()),
                "dac_max": int(self.dacMaxSpin.value()),
                "rate_tol_ratio": float(self.rateTolRatioSpin.value()),
                "rate_stable_sec": float(self.rateStableSecSpin.value()),
                "hold_control_interval_s": float(self.holdControlIntervalSpin.value()),
                "fine_step_dac": int(self.fineStepDacSpin.value()),
                "hold_control_mode": str(self.holdControlModeCombo.currentText() or "PID").strip().upper(),
                "hold_pi_kp": float(self.holdPiKpSpin.value()),
                "hold_pi_ki": float(self.holdPiKiSpin.value()),
                "hold_pi_kd": float(self.holdPiKdSpin.value()),
                "hold_integral_limit": float(self.holdIntegralLimitSpin.value()),
                "rate_filter_alpha": float(self.rateFilterAlphaSpin.value()),
                "rate_jump_guard_ratio": float(self.rateJumpGuardRatioSpin.value()),
                "rate_jump_guard_abs": float(self.rateJumpGuardAbsSpin.value()),
                "hold_max_dac_delta": int(self.holdMaxDacDeltaSpin.value()),
                "rate_abort_ratio": float(self.rateAbortRatioSpin.value()),
                "rate_abort_sec": float(self.rateAbortSecSpin.value()),
                "sensor_none_abort_s": float(self.sensorNoneAbortSpin.value()),
                "adc_none_abort_s": float(self.adcNoneAbortSpin.value()),
                "spike_abort_ratio": float(self.spikeAbortRatioSpin.value()),
                "spike_grace_s": float(self.spikeGraceSSpin.value()),
                "ramp_spike_pct": float(self.rampSpikePctSpin.value()),
                "ramp_spike_abort_sec": float(self.rampSpikeAbortSecSpin.value()),
                "pre_hold_entry_ratio": float(self.preHoldEntryRatioSpin.value()),
                "pre_hold_entry_sec": float(self.preHoldEntrySecSpin.value()),
                "pre_hold_ready_ratio": float(self.preHoldReadyRatioSpin.value()),
                "pre_hold_timeout_sec": float(self.preHoldTimeoutSecSpin.value()),
            }
        )
        return self._normalize_config(cfg)

    def accept(self) -> None:
        cfg = self.get_config()
        if str(cfg.get("hold_control_mode", "")).upper() not in {"PI", "PID", "STEP"}:
            QMessageBox.warning(self, "Process Config", "Hold Control Mode must be PI, PID or STEP.")
            return
        if int(cfg.get("dac_max", 0)) <= 0:
            QMessageBox.warning(self, "Process Config", "Max DAC must be greater than 0.")
            return
        if int(cfg.get("fine_step_dac", 0)) <= 0:
            QMessageBox.warning(self, "Process Config", "Fine Step DAC must be greater than 0.")
            return
        if int(cfg.get("hold_max_dac_delta", 0)) <= 0:
            QMessageBox.warning(self, "Process Config", "Max DAC Delta / Update must be greater than 0.")
            return
        if float(cfg.get("hold_pi_kp", -1.0)) < 0.0 or float(cfg.get("hold_pi_ki", -1.0)) < 0.0:
            QMessageBox.warning(self, "Process Config", "PI gains must be 0 or greater.")
            return
        if float(cfg.get("hold_integral_limit", 0.0)) <= 0.0:
            QMessageBox.warning(self, "Process Config", "Integral Limit must be greater than 0.")
            return
        if not (0.0 < float(cfg.get("rate_filter_alpha", 0.0)) <= 1.0):
            QMessageBox.warning(self, "Process Config", "Rate Filter Alpha must be between 0 and 1.")
            return
        if not (0.0 < float(cfg.get("rate_abort_ratio", 0.0)) <= 1.0):
            QMessageBox.warning(self, "Process Config", "Rate Abort Ratio must be between 0 and 1.")
            return
        if float(cfg.get("rate_abort_sec", -1.0)) < 0.0:
            QMessageBox.warning(self, "Process Config", "Rate Abort Sec must be 0 or greater.")
            return
        if float(cfg.get("sensor_none_abort_s", -1.0)) < 0.0 or float(cfg.get("adc_none_abort_s", -1.0)) < 0.0:
            QMessageBox.warning(self, "Process Config", "Sensor/ADC None Abort Sec must be 0 or greater.")
            return
        self._initial_config = cfg
        super().accept()
