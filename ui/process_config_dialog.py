from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


MAX_STEPS = 10
MIN_STEPS = 1
_STEP_LABEL_W = 55
_STEP_CHECK_W = 55
_STEP_TARGET_W = 120
_STEP_DAC_W = 100
_STEP_INTERVAL_W = 100
_STEP_HOLD_W = 100


class _NoWheelDoubleSpinBox(QDoubleSpinBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class _NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event: QWheelEvent) -> None:
        event.ignore()


class ProcessConfigDialog(QDialog):
    """
    Evaporator process config dialog.

    구성:
    - 단일 페이지 UI
    - Step Settings:
    step 추가/삭제, Target ADC, DAC step, Interval, Hold 입력
    - Hold Rate:
    dep.rate 유지용 공통 파라미터 입력
    - Safety:
    abort / 최대 DAC / 센서 timeout 파라미터 입력

    역할:
    - 1단계 상승용 step 입력
    - 2단계 유지용 제어/안전 파라미터 입력
    - 3단계 하강은 별도 입력 없이 safety 시퀀스 사용
    """

    def __init__(self, initial_config: dict[str, Any] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Process Config")
        self.setModal(True)
        self.resize(1100, 640)
        self.setMinimumSize(900, 560)

        self._initial_config = self._normalize_config(initial_config or self._default_config())
        self._active_step: Optional[int] = None
        self._step_rows: list[dict[str, Any]] = []

        self._build_ui()
        self._load_config(self._initial_config)

    # =========================================================
    # defaults / normalize
    # =========================================================
    @staticmethod
    def _default_step() -> dict[str, Any]:
        """EvapStep 하나의 기본값."""
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
            "rate_abort_ratio": 0.30,
            "rate_abort_sec": 5.0,
            "sensor_none_abort_s": 5.0,
            "adc_none_abort_s": 5.0,
        }

    def _normalize_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        def _as_int(name: str, default_val: int, min_v: int | None = None, max_v: int | None = None) -> int:
            try:
                v = int(src.get(name, default_val))
            except Exception:
                v = int(default_val)
            if min_v is not None:
                v = max(min_v, v)
            if max_v is not None:
                v = min(max_v, v)
            return v

        def _as_float(name: str, default_val: float, min_v: float | None = None, max_v: float | None = None) -> float:
            try:
                v = float(src.get(name, default_val))
            except Exception:
                v = float(default_val)
            if min_v is not None:
                v = max(min_v, v)
            if max_v is not None:
                v = min(max_v, v)
            return v

        src = dict(cfg or {})
        default = self._default_config()

        raw_steps = src.get("ramp_steps") or src.get("steps") or default["ramp_steps"]
        steps: list[dict[str, Any]] = []

        for item in list(raw_steps)[: MAX_STEPS]:
            item = dict(item or {})
            try:
                target_adc = max(0.0, float(item.get("target_adc", 0.0)))
            except Exception:
                target_adc = 0.0
            try:
                dac_step = max(1, int(item.get("dac_step", 10)))
            except Exception:
                dac_step = 10
            try:
                dac_interval = max(0.1, float(item.get("dac_interval_sec", 30.0)))
            except Exception:
                dac_interval = 30.0

            try:
                legacy_hold = float(item.get("hold_sec", item.get("rate_wait_sec", item.get("delay_s", 0.0))))
            except Exception:
                legacy_hold = 0.0

            steps.append({
                "target_adc": target_adc,
                "dac_step": dac_step,
                "dac_interval_sec": dac_interval,
                "hold_sec": max(0.0, legacy_hold),
            })

        if not steps:
            steps = [self._default_step()]

        step_count = len(steps)
        step_count = max(MIN_STEPS, min(MAX_STEPS, step_count))
        steps = steps[:step_count]

        return {
            "step_count": len(steps),
            "ramp_steps": steps,
            "dac_max": _as_int("dac_max", 4000, 1),
            "rate_tol_ratio": _as_float("rate_tol_ratio", 0.05, 0.001, 1.0),
            "rate_stable_sec": _as_float("rate_stable_sec", 3.0, 0.0),
            "hold_control_interval_s": _as_float("hold_control_interval_s", 1.0, 0.1),
            "fine_step_dac": _as_int("fine_step_dac", 10, 1),
            "rate_abort_ratio": _as_float("rate_abort_ratio", 0.30, 0.001, 1.0),
            "rate_abort_sec": _as_float("rate_abort_sec", 5.0, 0.0),
            "sensor_none_abort_s": _as_float("sensor_none_abort_s", 5.0, 0.0),
            "adc_none_abort_s": _as_float("adc_none_abort_s", 5.0, 0.0),
        }

    # =========================================================
    # UI build
    # =========================================================
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        root.addWidget(scroll, 1)

        body = QWidget(scroll)
        body_root = QVBoxLayout(body)
        body_root.setContentsMargins(6, 6, 6, 6)
        body_root.setSpacing(10)
        scroll.setWidget(body)

        # -----------------------------
        # Step Settings
        # -----------------------------
        step_box = QGroupBox("Step Settings")
        step_box_layout = QVBoxLayout(step_box)
        step_box_layout.setSpacing(8)

        top_bar = QHBoxLayout()
        self.stepCountLabel = QLabel("")
        self.addStepBtn = QPushButton("+ Step", self)
        self.removeStepBtn = QPushButton("- Step", self)
        self.addStepBtn.clicked.connect(self._add_step_row_ui)
        self.removeStepBtn.clicked.connect(self._remove_last_step_row_ui)

        top_bar.addWidget(QLabel("공정 상승 step을 추가/삭제할 수 있습니다."))
        top_bar.addStretch(1)
        top_bar.addWidget(self.stepCountLabel)
        top_bar.addWidget(self.removeStepBtn)
        top_bar.addWidget(self.addStepBtn)

        step_box_layout.addLayout(top_bar)

        # 헤더 행
        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 0, 10, 0)
        header_layout.setSpacing(8)

        lbl_step = QLabel("Step")
        lbl_step.setFixedWidth(_STEP_LABEL_W)
        lbl_enabled = QLabel("활성화")
        lbl_enabled.setFixedWidth(_STEP_LABEL_W)
        lbl_target = QLabel("Target ADC")
        lbl_target.setFixedWidth(_STEP_TARGET_W)
        lbl_dac = QLabel("DAC step")
        lbl_dac.setFixedWidth(_STEP_DAC_W)
        lbl_interval = QLabel("Interval(s)")
        lbl_interval.setFixedWidth(_STEP_INTERVAL_W)
        lbl_hold = QLabel("Hold(s)")
        lbl_hold.setFixedWidth(_STEP_HOLD_W)

        for w in (lbl_step, lbl_enabled, lbl_target, lbl_dac, lbl_interval, lbl_hold):
            header_layout.addWidget(w)
        header_layout.addStretch(1)

        step_box_layout.addWidget(header)

        # 동적 row 컨테이너
        self.stepRowsContainer = QWidget(self)
        self.stepRowsLayout = QVBoxLayout(self.stepRowsContainer)
        self.stepRowsLayout.setContentsMargins(0, 0, 0, 0)
        self.stepRowsLayout.setSpacing(6)
        step_box_layout.addWidget(self.stepRowsContainer)

        body_root.addWidget(step_box)

        # -----------------------------
        # Hold Rate
        # -----------------------------
        hold_box = QGroupBox("Hold Rate")
        hold_form = QFormLayout(hold_box)

        self.rateTolRatioSpin = self._make_double_spin(0.001, 1.0, step=0.01, decimals=3)
        self.rateStableSecSpin = self._make_double_spin(0.0, 60.0, step=0.5, decimals=1)
        self.holdControlIntervalSpin = self._make_double_spin(0.1, 60.0, step=0.5, decimals=1)
        self.fineStepDacSpin = self._make_int_spin(1, 1000, step=1)

        hold_form.addRow("Rate Tolerance Ratio", self.rateTolRatioSpin)
        hold_form.addRow("Rate Stable Sec", self.rateStableSecSpin)
        hold_form.addRow("Control Interval (s)", self.holdControlIntervalSpin)
        hold_form.addRow("Fine Step DAC", self.fineStepDacSpin)

        body_root.addWidget(hold_box)

        # -----------------------------
        # Safety
        # -----------------------------
        safety_box = QGroupBox("Safety")
        safety_form = QFormLayout(safety_box)

        self.dacMaxSpin = self._make_int_spin(1, 9999, step=10)
        self.rateAbortRatioSpin = self._make_double_spin(0.001, 1.0, step=0.01, decimals=3)
        self.rateAbortSecSpin = self._make_double_spin(0.0, 99999.0, step=1.0, decimals=1)
        self.sensorNoneAbortSpin = self._make_double_spin(0.0, 99999.0, step=1.0, decimals=1)
        self.adcNoneAbortSpin = self._make_double_spin(0.0, 99999.0, step=1.0, decimals=1)

        safety_form.addRow("Max DAC", self.dacMaxSpin)
        safety_form.addRow("Rate Abort Ratio", self.rateAbortRatioSpin)
        safety_form.addRow("Rate Abort Sec", self.rateAbortSecSpin)
        safety_form.addRow("Sensor None Abort (s)", self.sensorNoneAbortSpin)
        safety_form.addRow("ADC None Abort (s)", self.adcNoneAbortSpin)

        body_root.addWidget(safety_box)
        body_root.addStretch(1)

        # 하단 버튼
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _add_step_row_ui(self, step_data: dict[str, Any] | None = None) -> None:
        if len(self._step_rows) >= MAX_STEPS:
            return

        step_data = dict(step_data or self._default_step())

        frame = QFrame(self.stepRowsContainer)
        frame.setFrameShape(QFrame.Shape.NoFrame)
        frame.setStyleSheet(
            "QFrame { background: #ffffff; border: 1px solid #d9d9d9; border-radius: 8px; }"
        )

        row_layout = QHBoxLayout(frame)
        row_layout.setContentsMargins(10, 8, 10, 8)
        row_layout.setSpacing(8)

        lbl_step = QLabel("", frame)
        lbl_step.setFixedWidth(_STEP_LABEL_W)

        chk_wrap = QWidget(frame)
        chk_wrap.setFixedWidth(_STEP_CHECK_W)
        chk_wrap_layout = QHBoxLayout(chk_wrap)
        chk_wrap_layout.setContentsMargins(0, 0, 0, 0)
        chk_wrap_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        chk = QCheckBox(frame)
        chk_wrap_layout.addWidget(chk)

        target_adc = self._make_double_spin(0.0, 9999.0, step=10.0)
        target_adc.setFixedWidth(_STEP_TARGET_W)

        dac_step = self._make_int_spin(1, 9999, step=5)
        dac_step.setFixedWidth(_STEP_DAC_W)

        interval = self._make_double_spin(0.1, 9999.0, step=1.0, decimals=1)
        interval.setFixedWidth(_STEP_INTERVAL_W)

        hold = self._make_double_spin(0.0, 9999.0, step=1.0, decimals=1)
        hold.setFixedWidth(_STEP_HOLD_W)

        target_adc.setValue(float(step_data.get("target_adc", 100.0)))
        dac_step.setValue(int(step_data.get("dac_step", 10)))
        interval.setValue(float(step_data.get("dac_interval_sec", 30.0)))
        hold.setValue(float(step_data.get("hold_sec", step_data.get("rate_wait_sec", step_data.get("delay_s", 0.0)))))

        row_info = {
            "frame": frame,
            "label": lbl_step,
            "enabled": chk,
            "target_adc": target_adc,
            "dac_step": dac_step,
            "interval": interval,
            "hold": hold,
        }

        chk.stateChanged.connect(lambda _s, info=row_info: self._on_step_enabled_changed(info))

        row_layout.addWidget(lbl_step)
        row_layout.addWidget(chk_wrap)
        row_layout.addWidget(target_adc)
        row_layout.addWidget(dac_step)
        row_layout.addWidget(interval)
        row_layout.addWidget(hold)
        row_layout.addStretch(1)

        self.stepRowsLayout.addWidget(frame)
        self._step_rows.append(row_info)

        chk.setChecked(True)
        self._refresh_step_row_labels()
        self._sync_step_buttons()
        self._refresh_all_step_row_styles()


    def _remove_last_step_row_ui(self) -> None:
        if len(self._step_rows) <= MIN_STEPS:
            return

        row_info = self._step_rows.pop()
        frame = row_info["frame"]
        self.stepRowsLayout.removeWidget(frame)
        frame.deleteLater()

        if self._active_step is not None and self._active_step >= len(self._step_rows):
            self._active_step = None

        self._refresh_step_row_labels()
        self._sync_step_buttons()
        self._refresh_all_step_row_styles()


    def _clear_step_rows(self) -> None:
        while self._step_rows:
            row_info = self._step_rows.pop()
            frame = row_info["frame"]
            self.stepRowsLayout.removeWidget(frame)
            frame.deleteLater()


    def _refresh_step_row_labels(self) -> None:
        for idx, row_info in enumerate(self._step_rows, start=1):
            row_info["label"].setText(f"Step {idx}")
        self.stepCountLabel.setText(f"{len(self._step_rows)} / {MAX_STEPS}")


    def _sync_step_buttons(self) -> None:
        self.addStepBtn.setEnabled(len(self._step_rows) < MAX_STEPS)
        self.removeStepBtn.setEnabled(len(self._step_rows) > MIN_STEPS)


    def _on_step_enabled_changed(self, row_info: dict[str, Any]) -> None:
        enabled = row_info["enabled"].isChecked()
        for key in ("target_adc", "dac_step", "interval", "hold"):
            row_info[key].setEnabled(enabled)
        self._refresh_all_step_row_styles()


    def _refresh_all_step_row_styles(self) -> None:
        for idx, row_info in enumerate(self._step_rows):
            frame = row_info["frame"]
            enabled = row_info["enabled"].isChecked()
            active = (self._active_step == idx)

            if active:
                style = "QFrame { background: #FFF4B8; border: 1px solid #D8C36A; border-radius: 8px; }"
            elif not enabled:
                style = "QFrame { background: #F1F1F1; border: 1px solid #D8D8D8; border-radius: 8px; }"
            else:
                style = "QFrame { background: #FFFFFF; border: 1px solid #DCDCDC; border-radius: 8px; }"

            frame.setStyleSheet(style)

    # =========================================================
    # 위젯 팩토리
    # =========================================================
    @staticmethod
    def _make_double_spin(
        min_val: float = 0.0,
        max_val: float = 9999.0,
        *,
        step: float = 1.0,
        decimals: int = 1,
    ) -> QDoubleSpinBox:
        w = _NoWheelDoubleSpinBox()
        w.setDecimals(decimals)
        w.setRange(min_val, max_val)
        w.setSingleStep(step)
        w.setAlignment(Qt.AlignmentFlag.AlignRight)
        w.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        w.setKeyboardTracking(False)
        return w

    @staticmethod
    def _make_int_spin(min_val: int = 0, max_val: int = 9999, *, step: int = 1) -> QSpinBox:
        w = _NoWheelSpinBox()
        w.setRange(min_val, max_val)
        w.setSingleStep(step)
        w.setAlignment(Qt.AlignmentFlag.AlignRight)
        w.setKeyboardTracking(False)
        return w
    
    # =========================================================
    # load / apply
    # =========================================================
    def _load_config(self, cfg: dict[str, Any]) -> None:
        cfg = self._normalize_config(cfg)

        self._clear_step_rows()

        steps = cfg.get("ramp_steps") or [self._default_step()]
        for s in steps[: MAX_STEPS]:
            self._add_step_row_ui(s)

        if not self._step_rows:
            self._add_step_row_ui(self._default_step())

        self.dacMaxSpin.setValue(int(cfg.get("dac_max", 4000)))
        self.rateTolRatioSpin.setValue(float(cfg.get("rate_tol_ratio", 0.05)))
        self.rateStableSecSpin.setValue(float(cfg.get("rate_stable_sec", 3.0)))
        self.holdControlIntervalSpin.setValue(float(cfg.get("hold_control_interval_s", 1.0)))
        self.fineStepDacSpin.setValue(int(cfg.get("fine_step_dac", 10)))
        self.rateAbortRatioSpin.setValue(float(cfg.get("rate_abort_ratio", 0.30)))
        self.rateAbortSecSpin.setValue(float(cfg.get("rate_abort_sec", 5.0)))
        self.sensorNoneAbortSpin.setValue(float(cfg.get("sensor_none_abort_s", 5.0)))
        self.adcNoneAbortSpin.setValue(float(cfg.get("adc_none_abort_s", 5.0)))

        self._sync_step_buttons()
        self._refresh_all_step_row_styles()

    # =========================================================
    # 하이라이트 API
    # =========================================================
    def highlight_step(self, step_idx: Optional[int]) -> None:
        self._active_step = step_idx
        self._refresh_all_step_row_styles()

    # =========================================================
    # step 수집
    # =========================================================
    def _collect_steps(self) -> Optional[list[dict[str, Any]]]:
        steps: list[dict[str, Any]] = []
        last_adc = -1.0

        for idx, row_info in enumerate(self._step_rows, start=1):
            if not row_info["enabled"].isChecked():
                continue

            target_adc = float(row_info["target_adc"].value())
            if target_adc <= 0:
                QMessageBox.warning(self, "입력 오류", f"Step {idx}: Target ADC는 0보다 커야 합니다.")
                return None

            if last_adc >= 0 and target_adc < last_adc:
                QMessageBox.warning(
                    self,
                    "입력 오류",
                    f"Step {idx}: Target ADC({target_adc})가 이전 step({last_adc})보다 작습니다.\n오름차순으로 입력하세요."
                )
                return None

            steps.append({
                "target_adc": target_adc,
                "dac_step": int(row_info["dac_step"].value()),
                "dac_interval_sec": float(row_info["interval"].value()),
                "hold_sec": float(row_info["hold"].value()),
            })
            last_adc = target_adc

        if not steps:
            QMessageBox.warning(self, "입력 오류", "활성화된 step이 최소 1개 이상이어야 합니다.")
            return None

        return steps

    # =========================================================
    # output
    # =========================================================
    def get_config(self) -> dict[str, Any]:
        steps = self._collect_steps()
        if steps is None:
            steps = list(self._initial_config.get("ramp_steps") or [self._default_step()])

        cfg = {
            "step_count": len(steps),
            "ramp_steps": steps,
            "dac_max": int(self.dacMaxSpin.value()),
            "rate_tol_ratio": float(self.rateTolRatioSpin.value()),
            "rate_stable_sec": float(self.rateStableSecSpin.value()),
            "hold_control_interval_s": float(self.holdControlIntervalSpin.value()),
            "fine_step_dac": int(self.fineStepDacSpin.value()),
            "rate_abort_ratio": float(self.rateAbortRatioSpin.value()),
            "rate_abort_sec": float(self.rateAbortSecSpin.value()),
            "sensor_none_abort_s": float(self.sensorNoneAbortSpin.value()),
            "adc_none_abort_s": float(self.adcNoneAbortSpin.value()),
        }
        return self._normalize_config(cfg)

    # =========================================================
    # validation + accept
    # =========================================================
    def accept(self) -> None:
        cfg = self.get_config()
        steps = cfg.get("ramp_steps") or []

        if not steps:
            QMessageBox.warning(self, "Process Config", "활성화된 step이 최소 1개 이상이어야 합니다.")
            return

        last_adc = -1.0
        for idx, s in enumerate(steps, start=1):
            adc = float(s.get("target_adc", 0.0))
            if adc <= 0:
                QMessageBox.warning(self, "Process Config", f"Step {idx} Target ADC는 0보다 커야 합니다.")
                return
            if last_adc >= 0 and adc < last_adc:
                QMessageBox.warning(
                    self,
                    "Process Config",
                    f"Step {idx} Target ADC가 이전 step보다 작습니다. 오름차순으로 입력하세요."
                )
                return
            last_adc = adc

        if int(cfg.get("dac_max", 0)) <= 0:
            QMessageBox.warning(self, "Process Config", "Max DAC는 0보다 커야 합니다.")
            return

        if int(cfg.get("fine_step_dac", 0)) <= 0:
            QMessageBox.warning(self, "Process Config", "Fine Step DAC는 0보다 커야 합니다.")
            return

        if float(cfg.get("rate_tol_ratio", 0.0)) <= 0.0:
            QMessageBox.warning(self, "Process Config", "Rate Tolerance Ratio는 0보다 커야 합니다.")
            return

        if float(cfg.get("rate_abort_ratio", 0.0)) <= 0.0:
            QMessageBox.warning(self, "Process Config", "Rate Abort Ratio는 0보다 커야 합니다.")
            return

        super().accept()
