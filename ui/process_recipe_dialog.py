from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFrame,
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


class ProcessRecipeDialog(QDialog):
    def __init__(
        self,
        *,
        initial_config: Optional[dict[str, Any]] = None,
        recommend_callback: Optional[Callable[[dict[str, Any], QWidget], Optional[dict[str, Any]]]] = None,
        history_callback: Optional[Callable[[QWidget], Optional[dict[str, Any]]]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Process Recipe")
        self.setModal(True)
        self.resize(860, 620)
        self.setMinimumSize(760, 520)

        self._recommend_callback = recommend_callback
        self._history_callback = history_callback
        self._last_recommendation: Optional[dict[str, Any]] = None
        self._config_state = self._normalize_config(initial_config or self._default_config())
        self._step_rows: list[dict[str, Any]] = []

        self._build_ui()
        self._load_config(self._config_state)

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
        for item in list(raw_steps)[:MAX_STEPS]:
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
        if hold_control_mode not in {"PI", "PID", "STEP"}:
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

        self.infoLabel = QLabel("Ramp Step 설정   |   이전 성공 공정을 불러오려면 아래 버튼을 사용하세요")
        self.infoLabel.setWordWrap(True)
        root.addWidget(self.infoLabel)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        root.addWidget(scroll, 1)

        body = QWidget(scroll)
        body_root = QVBoxLayout(body)
        body_root.setContentsMargins(4, 4, 4, 4)
        body_root.setSpacing(10)
        scroll.setWidget(body)

        top_bar = QHBoxLayout()
        self.stepCountLabel = QLabel("")
        self.addStepBtn = QPushButton("+ Step", self)
        self.removeStepBtn = QPushButton("- Step", self)
        self.addStepBtn.clicked.connect(self._add_step_row_ui)
        self.removeStepBtn.clicked.connect(self._remove_last_step_row_ui)
        top_bar.addWidget(QLabel("Ramp steps"))
        top_bar.addStretch(1)
        top_bar.addWidget(self.stepCountLabel)
        top_bar.addWidget(self.removeStepBtn)
        top_bar.addWidget(self.addStepBtn)
        body_root.addLayout(top_bar)

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 0, 10, 0)
        header_layout.setSpacing(8)
        for text, width in (
            ("Step", _STEP_LABEL_W),
            ("Use", _STEP_CHECK_W),
            ("Target ADC", _STEP_TARGET_W),
            ("DAC Step", _STEP_DAC_W),
            ("Interval (s)", _STEP_INTERVAL_W),
            ("Hold (s)", _STEP_HOLD_W),
        ):
            label = QLabel(text)
            label.setFixedWidth(width)
            header_layout.addWidget(label)
        header_layout.addStretch(1)
        body_root.addWidget(header)

        self.stepRowsContainer = QWidget(self)
        self.stepRowsLayout = QVBoxLayout(self.stepRowsContainer)
        self.stepRowsLayout.setContentsMargins(0, 0, 0, 0)
        self.stepRowsLayout.setSpacing(6)
        body_root.addWidget(self.stepRowsContainer)
        body_root.addStretch(1)

        command_row = QHBoxLayout()
        self.historyButton = QPushButton("로그 스캔", self)
        self.historyButton.setToolTip(
            "NAS에 저장된 공정 로그를 읽어 추천 데이터베이스를 갱신합니다.\n"
            "처음 사용하거나 새 로그가 쌓였을 때 실행하세요."
        )
        self.historyButton.clicked.connect(self._on_history_clicked)
        self.hintLabel = QLabel("처음 사용 시:  로그 스캔 → 이전 설정 불러오기 순으로 진행하세요", self)
        self.hintLabel.setStyleSheet("font-size: 10px; color: #888888;")
        self.recommendButton = QPushButton("이전 설정 불러오기", self)
        self.recommendButton.setToolTip(
            "같은 소재의 이전 성공 공정 설정을 불러옵니다.\n"
            "로그 스캔을 먼저 실행해야 데이터가 나타납니다."
        )
        self.recommendButton.clicked.connect(self._on_recommend_clicked)
        command_row.addWidget(self.historyButton)
        command_row.addWidget(self.hintLabel)
        command_row.addStretch(1)
        command_row.addWidget(self.recommendButton)
        root.addLayout(command_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _make_double_spin(
        min_val: float,
        max_val: float,
        *,
        step: float,
        decimals: int = 1,
    ) -> QDoubleSpinBox:
        widget = _NoWheelDoubleSpinBox()
        widget.setDecimals(decimals)
        widget.setRange(min_val, max_val)
        widget.setSingleStep(step)
        widget.setAlignment(Qt.AlignmentFlag.AlignRight)
        widget.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        widget.setKeyboardTracking(False)
        return widget

    @staticmethod
    def _make_int_spin(min_val: int, max_val: int, *, step: int = 1) -> QSpinBox:
        widget = _NoWheelSpinBox()
        widget.setRange(min_val, max_val)
        widget.setSingleStep(step)
        widget.setAlignment(Qt.AlignmentFlag.AlignRight)
        widget.setKeyboardTracking(False)
        return widget

    def _add_step_row_ui(self, step_data: Optional[dict[str, Any]] = None) -> None:
        if len(self._step_rows) >= MAX_STEPS:
            return

        data = dict(step_data or self._default_step())
        frame = QFrame(self.stepRowsContainer)
        frame.setFrameShape(QFrame.Shape.NoFrame)
        frame.setStyleSheet("QFrame { background: #ffffff; border: 1px solid #d9d9d9; border-radius: 8px; }")

        row_layout = QHBoxLayout(frame)
        row_layout.setContentsMargins(10, 8, 10, 8)
        row_layout.setSpacing(8)

        lbl_step = QLabel("", frame)
        lbl_step.setFixedWidth(_STEP_LABEL_W)

        chk_wrap = QWidget(frame)
        chk_wrap.setFixedWidth(_STEP_CHECK_W)
        chk_layout = QHBoxLayout(chk_wrap)
        chk_layout.setContentsMargins(0, 0, 0, 0)
        chk_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        enabled = QCheckBox(frame)
        chk_layout.addWidget(enabled)

        target_adc = self._make_double_spin(0.0, 9999.0, step=10.0, decimals=1)
        target_adc.setFixedWidth(_STEP_TARGET_W)
        dac_step = self._make_int_spin(1, 9999, step=5)
        dac_step.setFixedWidth(_STEP_DAC_W)
        interval = self._make_double_spin(0.1, 9999.0, step=1.0, decimals=1)
        interval.setFixedWidth(_STEP_INTERVAL_W)
        hold = self._make_double_spin(0.0, 9999.0, step=1.0, decimals=1)
        hold.setFixedWidth(_STEP_HOLD_W)

        target_adc.setValue(float(data.get("target_adc", 100.0) or 100.0))
        dac_step.setValue(int(data.get("dac_step", 10) or 10))
        interval.setValue(float(data.get("dac_interval_sec", 30.0) or 30.0))
        hold.setValue(float(data.get("hold_sec", 0.0) or 0.0))

        row_layout.addWidget(lbl_step)
        row_layout.addWidget(chk_wrap)
        row_layout.addWidget(target_adc)
        row_layout.addWidget(dac_step)
        row_layout.addWidget(interval)
        row_layout.addWidget(hold)
        row_layout.addStretch(1)

        row_info = {
            "frame": frame,
            "label": lbl_step,
            "enabled": enabled,
            "target_adc": target_adc,
            "dac_step": dac_step,
            "interval": interval,
            "hold": hold,
        }
        enabled.stateChanged.connect(lambda _state, info=row_info: self._on_step_enabled_changed(info))
        enabled.setChecked(True)

        self.stepRowsLayout.addWidget(frame)
        self._step_rows.append(row_info)
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
        for row_info in self._step_rows:
            frame = row_info["frame"]
            enabled = row_info["enabled"].isChecked()
            if enabled:
                style = "QFrame { background: #ffffff; border: 1px solid #d9d9d9; border-radius: 8px; }"
            else:
                style = "QFrame { background: #f1f1f1; border: 1px solid #d8d8d8; border-radius: 8px; }"
            frame.setStyleSheet(style)

    def _load_config(self, cfg: dict[str, Any]) -> None:
        self._config_state = self._normalize_config(cfg)
        self._clear_step_rows()
        for step in self._config_state.get("ramp_steps") or [self._default_step()]:
            self._add_step_row_ui(step)
        if not self._step_rows:
            self._add_step_row_ui(self._default_step())
        self._sync_step_buttons()
        self._refresh_all_step_row_styles()

    def _collect_steps(self) -> Optional[list[dict[str, Any]]]:
        steps: list[dict[str, Any]] = []
        last_adc = -1.0
        for idx, row_info in enumerate(self._step_rows, start=1):
            if not row_info["enabled"].isChecked():
                continue
            target_adc = float(row_info["target_adc"].value())
            dac_step = int(row_info["dac_step"].value())
            interval = float(row_info["interval"].value())
            hold = float(row_info["hold"].value())
            if target_adc <= 0:
                QMessageBox.warning(self, "Recipe", f"Step {idx} target ADC must be greater than 0.")
                return None
            if last_adc >= 0 and target_adc < last_adc:
                QMessageBox.warning(self, "Recipe", f"Step {idx} target ADC must be greater than or equal to the previous step.")
                return None
            steps.append(
                {
                    "target_adc": target_adc,
                    "dac_step": dac_step,
                    "dac_interval_sec": interval,
                    "hold_sec": hold,
                }
            )
            last_adc = target_adc
        if not steps:
            QMessageBox.warning(self, "Recipe", "At least one enabled step is required.")
            return None
        return steps

    def get_config(self) -> dict[str, Any]:
        steps = self._collect_steps()
        if steps is None:
            steps = list(self._config_state.get("ramp_steps") or [self._default_step()])
        merged = dict(self._config_state)
        merged["step_count"] = len(steps)
        merged["ramp_steps"] = steps
        return self._normalize_config(merged)

    def last_recommendation(self) -> Optional[dict[str, Any]]:
        if not self._last_recommendation:
            return None
        return dict(self._last_recommendation)

    def _on_recommend_clicked(self) -> None:
        if not callable(self._recommend_callback):
            QMessageBox.information(self, "Recommendation", "Recommendation is not available.")
            return
        payload = self._recommend_callback(self.get_config(), self)
        if not payload:
            return
        cfg = payload.get("process_config")
        if isinstance(cfg, dict):
            self._load_config(cfg)
            confidence = float((payload.get("recommendation") or {}).get("confidence", 0.0) or 0.0)
            self.infoLabel.setText(f"✓ 이전 공정 설정이 적용되었습니다  (신뢰도 {confidence*100:.0f}%)")
            self.infoLabel.setStyleSheet("color: #1a7a1a; font-weight: bold;")
            self.hintLabel.setVisible(False)
        self._last_recommendation = dict(payload.get("recommendation") or {})

    def _on_history_clicked(self) -> None:
        if not callable(self._history_callback):
            QMessageBox.information(self, "History Rebuild", "History rebuild is not available.")
            return
        self._history_callback(self)

    def accept(self) -> None:
        cfg = self.get_config()
        steps = list(cfg.get("ramp_steps") or [])
        if not steps:
            QMessageBox.warning(self, "Recipe", "At least one step is required.")
            return
        self._config_state = cfg
        super().accept()
