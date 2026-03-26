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
    "dac_max": "Maximum DAC value allowed during the process.",
    "rate_tol_ratio": "Allowed target-rate tolerance ratio used for stable-rate 판단.",
    "rate_stable_sec": "Time that dep.rate must stay within tolerance before stable is reached.",
    "hold_control_interval_s": "DAC update interval used during hold control.",
    "fine_step_dac": "Fallback/manual hold adjustment step and default DAC delta limit basis.",
    "hold_control_mode": "Hold control mode. PI is the default V3 path, STEP remains as a safe fallback.",
    "hold_pi_kp": "Proportional gain for hold PI control.",
    "hold_pi_ki": "Integral gain for hold PI control.",
    "hold_integral_limit": "Absolute clamp applied to the hold PI integral term.",
    "rate_filter_alpha": "EMA alpha used for filtered dep.rate in hold control.",
    "rate_jump_guard_ratio": "Relative jump guard for filtered rate input.",
    "rate_jump_guard_abs": "Absolute jump guard for filtered rate input.",
    "hold_max_dac_delta": "Maximum DAC change allowed per hold-control update.",
    "rate_abort_ratio": "Abort when raw dep.rate stays below this ratio of target rate.",
    "rate_abort_sec": "Seconds that low-rate abort condition must persist.",
    "sensor_none_abort_s": "Allowed duration for missing STM rate/thickness before abort.",
    "adc_none_abort_s": "Allowed duration for missing ADC feedback before abort.",
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
            "rate_tol_ratio": 0.05,
            "rate_stable_sec": 3.0,
            "hold_control_interval_s": 1.0,
            "fine_step_dac": 10,
            "hold_control_mode": "PI",
            "hold_pi_kp": 50.0,
            "hold_pi_ki": 8.0,
            "hold_integral_limit": 2.5,
            "rate_filter_alpha": 0.35,
            "rate_jump_guard_ratio": 0.50,
            "rate_jump_guard_abs": 0.15,
            "hold_max_dac_delta": 10,
            "rate_abort_ratio": 0.30,
            "rate_abort_sec": 5.0,
            "sensor_none_abort_s": 5.0,
            "adc_none_abort_s": 5.0,
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
        hold_pi_ki = _as_float("hold_pi_ki", max(0.0, hold_max_dac_delta * 0.8), 0.0)
        hold_control_mode = str(src.get("hold_control_mode", default["hold_control_mode"]) or "").strip().upper() or "PI"
        if hold_control_mode not in {"PI", "STEP"}:
            hold_control_mode = "PI"

        return {
            "step_count": len(steps),
            "ramp_steps": steps,
            "dac_max": _as_int("dac_max", 4000, 1),
            "rate_tol_ratio": _as_float("rate_tol_ratio", 0.05, 0.001, 1.0),
            "rate_stable_sec": _as_float("rate_stable_sec", 3.0, 0.0),
            "hold_control_interval_s": _as_float("hold_control_interval_s", 1.0, 0.1),
            "fine_step_dac": fine_step_dac,
            "hold_control_mode": hold_control_mode,
            "hold_pi_kp": _as_float("hold_pi_kp", max(1.0, hold_max_dac_delta * 5.0), 0.0),
            "hold_pi_ki": hold_pi_ki,
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
        self.holdControlModeCombo.addItems(["PI", "STEP"])
        self.rateTolRatioSpin = self._make_double_spin(0.001, 1.0, step=0.01, decimals=3)
        self.rateStableSecSpin = self._make_double_spin(0.0, 60.0, step=0.5, decimals=1)
        self.holdControlIntervalSpin = self._make_double_spin(0.1, 60.0, step=0.5, decimals=1)
        self.fineStepDacSpin = self._make_int_spin(1, 1000, step=1)
        self.holdMaxDacDeltaSpin = self._make_int_spin(1, 1000, step=1)
        self.holdPiKpSpin = self._make_double_spin(0.0, 9999.0, step=1.0, decimals=3)
        self.holdPiKiSpin = self._make_double_spin(0.0, 9999.0, step=0.1, decimals=3)
        self.holdIntegralLimitSpin = self._make_double_spin(0.1, 9999.0, step=0.1, decimals=3)

        self._add_form_row(hold_form, "Control Mode", self.holdControlModeCombo, "hold_control_mode")
        self._add_form_row(hold_form, "Rate Tolerance Ratio", self.rateTolRatioSpin, "rate_tol_ratio")
        self._add_form_row(hold_form, "Rate Stable Sec", self.rateStableSecSpin, "rate_stable_sec")
        self._add_form_row(hold_form, "Control Interval (s)", self.holdControlIntervalSpin, "hold_control_interval_s")
        self._add_form_row(hold_form, "Fine Step DAC", self.fineStepDacSpin, "fine_step_dac")
        self._add_form_row(hold_form, "Max DAC Delta / Update", self.holdMaxDacDeltaSpin, "hold_max_dac_delta")
        self._add_form_row(hold_form, "PI Kp", self.holdPiKpSpin, "hold_pi_kp")
        self._add_form_row(hold_form, "PI Ki", self.holdPiKiSpin, "hold_pi_ki")
        self._add_form_row(hold_form, "Integral Limit", self.holdIntegralLimitSpin, "hold_integral_limit")
        body_root.addWidget(hold_box)

        filter_box = QGroupBox("Filter / Guard")
        filter_form = QFormLayout(filter_box)
        self.rateFilterAlphaSpin = self._make_double_spin(0.01, 1.0, step=0.01, decimals=3)
        self.rateJumpGuardRatioSpin = self._make_double_spin(0.0, 10.0, step=0.05, decimals=3)
        self.rateJumpGuardAbsSpin = self._make_double_spin(0.0, 10.0, step=0.01, decimals=3)
        self._add_form_row(filter_form, "Rate Filter Alpha", self.rateFilterAlphaSpin, "rate_filter_alpha")
        self._add_form_row(filter_form, "Rate Jump Guard Ratio", self.rateJumpGuardRatioSpin, "rate_jump_guard_ratio")
        self._add_form_row(filter_form, "Rate Jump Guard Abs", self.rateJumpGuardAbsSpin, "rate_jump_guard_abs")
        body_root.addWidget(filter_box)

        safety_box = QGroupBox("Safety / Abort")
        safety_form = QFormLayout(safety_box)
        self.dacMaxSpin = self._make_int_spin(1, 9999, step=10)
        self.rateAbortRatioSpin = self._make_double_spin(0.001, 1.0, step=0.01, decimals=3)
        self.rateAbortSecSpin = self._make_double_spin(0.0, 99999.0, step=1.0, decimals=1)
        self.sensorNoneAbortSpin = self._make_double_spin(0.0, 99999.0, step=1.0, decimals=1)
        self.adcNoneAbortSpin = self._make_double_spin(0.0, 99999.0, step=1.0, decimals=1)
        self._add_form_row(safety_form, "Max DAC", self.dacMaxSpin, "dac_max")
        self._add_form_row(safety_form, "Rate Abort Ratio", self.rateAbortRatioSpin, "rate_abort_ratio")
        self._add_form_row(safety_form, "Rate Abort Sec", self.rateAbortSecSpin, "rate_abort_sec")
        self._add_form_row(safety_form, "Sensor None Abort (s)", self.sensorNoneAbortSpin, "sensor_none_abort_s")
        self._add_form_row(safety_form, "ADC None Abort (s)", self.adcNoneAbortSpin, "adc_none_abort_s")
        body_root.addWidget(safety_box)
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
        tooltip = PROCESS_CONFIG_TOOLTIPS[tooltip_key]
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
        self.holdControlModeCombo.setCurrentText(str(cfg.get("hold_control_mode", "PI") or "PI"))
        self.rateTolRatioSpin.setValue(float(cfg.get("rate_tol_ratio", 0.05)))
        self.rateStableSecSpin.setValue(float(cfg.get("rate_stable_sec", 3.0)))
        self.holdControlIntervalSpin.setValue(float(cfg.get("hold_control_interval_s", 1.0)))
        self.fineStepDacSpin.setValue(int(cfg.get("fine_step_dac", 10)))
        self.holdMaxDacDeltaSpin.setValue(int(cfg.get("hold_max_dac_delta", 10)))
        self.holdPiKpSpin.setValue(float(cfg.get("hold_pi_kp", 50.0)))
        self.holdPiKiSpin.setValue(float(cfg.get("hold_pi_ki", 8.0)))
        self.holdIntegralLimitSpin.setValue(float(cfg.get("hold_integral_limit", 2.5)))
        self.rateFilterAlphaSpin.setValue(float(cfg.get("rate_filter_alpha", 0.35)))
        self.rateJumpGuardRatioSpin.setValue(float(cfg.get("rate_jump_guard_ratio", 0.50)))
        self.rateJumpGuardAbsSpin.setValue(float(cfg.get("rate_jump_guard_abs", 0.15)))
        self.dacMaxSpin.setValue(int(cfg.get("dac_max", 4000)))
        self.rateAbortRatioSpin.setValue(float(cfg.get("rate_abort_ratio", 0.30)))
        self.rateAbortSecSpin.setValue(float(cfg.get("rate_abort_sec", 5.0)))
        self.sensorNoneAbortSpin.setValue(float(cfg.get("sensor_none_abort_s", 5.0)))
        self.adcNoneAbortSpin.setValue(float(cfg.get("adc_none_abort_s", 5.0)))

    def get_config(self) -> dict[str, Any]:
        cfg = dict(self._initial_config)
        cfg.update(
            {
                "dac_max": int(self.dacMaxSpin.value()),
                "rate_tol_ratio": float(self.rateTolRatioSpin.value()),
                "rate_stable_sec": float(self.rateStableSecSpin.value()),
                "hold_control_interval_s": float(self.holdControlIntervalSpin.value()),
                "fine_step_dac": int(self.fineStepDacSpin.value()),
                "hold_control_mode": str(self.holdControlModeCombo.currentText() or "PI").strip().upper(),
                "hold_pi_kp": float(self.holdPiKpSpin.value()),
                "hold_pi_ki": float(self.holdPiKiSpin.value()),
                "hold_integral_limit": float(self.holdIntegralLimitSpin.value()),
                "rate_filter_alpha": float(self.rateFilterAlphaSpin.value()),
                "rate_jump_guard_ratio": float(self.rateJumpGuardRatioSpin.value()),
                "rate_jump_guard_abs": float(self.rateJumpGuardAbsSpin.value()),
                "hold_max_dac_delta": int(self.holdMaxDacDeltaSpin.value()),
                "rate_abort_ratio": float(self.rateAbortRatioSpin.value()),
                "rate_abort_sec": float(self.rateAbortSecSpin.value()),
                "sensor_none_abort_s": float(self.sensorNoneAbortSpin.value()),
                "adc_none_abort_s": float(self.adcNoneAbortSpin.value()),
            }
        )
        return self._normalize_config(cfg)

    def accept(self) -> None:
        cfg = self.get_config()
        if str(cfg.get("hold_control_mode", "")).upper() not in {"PI", "STEP"}:
            QMessageBox.warning(self, "Process Config", "Hold Control Mode must be PI or STEP.")
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
