from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)


class ProcessConfigDialog(QDialog):
    """
    Evaporator Process 전용 Config Dialog

    기능:
    - step 개수: 1 ~ 10
    - 각 step:
        * target_adc
        * delay_s
    - 공통 옵션:
        * dep.rate 도달 시 즉시 메인 공정 진입
        * 마지막 step 후 정책(extra_ramp / stop)
        * extra ramp 설정(enabled / max_adc / step_max / interval_s)

    process_window.py 와 맞춰서
    get_config() -> dict 형태를 반환한다.
    """

    MAX_STEPS = 10
    MIN_STEPS = 1

    def __init__(self, initial_config: dict[str, Any] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Process Config")
        self.setModal(True)
        self.resize(900, 760)
        self.setMinimumSize(900, 760)

        self._initial_config = self._normalize_config(initial_config or self._default_config())

        self._build_ui()
        self._load_config(self._initial_config)

    # =========================================================
    # 기본/정규화
    # =========================================================
    def _default_config(self) -> dict[str, Any]:
        return {
            "step_count": 1,
            "ramp_steps": [
                {
                    "target_adc": 100.0,
                    "delay_s": 0.0,
                }
            ],
            "reach_main_on_rate": True,
            "after_last_step_policy": "extra_ramp",
            "extra_ramp": {
                "enabled": True,
                "max_adc": 300.0,
                "step_max": 50.0,
                "interval_s": 5.0,
            },
        }

    def _normalize_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        src = dict(cfg or {})
        default = self._default_config()

        raw_steps = src.get("ramp_steps") or src.get("steps") or default["ramp_steps"]
        steps: list[dict[str, float]] = []

        for item in list(raw_steps)[: self.MAX_STEPS]:
            try:
                target_adc = max(0.0, float((item or {}).get("target_adc", 0.0)))
            except Exception:
                target_adc = 0.0

            try:
                delay_s = max(0.0, float((item or {}).get("delay_s", 0.0)))
            except Exception:
                delay_s = 0.0

            steps.append(
                {
                    "target_adc": target_adc,
                    "delay_s": delay_s,
                }
            )

        if not steps:
            steps = list(default["ramp_steps"])

        step_count = src.get("step_count", len(steps))
        try:
            step_count = int(step_count)
        except Exception:
            step_count = len(steps)

        step_count = max(self.MIN_STEPS, min(self.MAX_STEPS, step_count))
        steps = steps[:step_count]

        extra_src = dict(src.get("extra_ramp") or default["extra_ramp"])
        try:
            enabled = bool(extra_src.get("enabled", True))
        except Exception:
            enabled = True

        try:
            max_adc = max(0.0, float(extra_src.get("max_adc", 300.0) or 0.0))
        except Exception:
            max_adc = 300.0

        try:
            step_max = float(extra_src.get("step_max", 50.0) or 50.0)
        except Exception:
            step_max = 50.0
        step_max = min(100.0, max(1.0, step_max))

        try:
            interval_s = float(extra_src.get("interval_s", 5.0) or 5.0)
        except Exception:
            interval_s = 5.0
        interval_s = max(0.1, interval_s)

        policy = str(src.get("after_last_step_policy", "extra_ramp") or "extra_ramp").strip().lower()
        if policy not in {"extra_ramp", "stop"}:
            policy = "extra_ramp"

        return {
            "step_count": len(steps),
            "ramp_steps": steps,
            "reach_main_on_rate": bool(src.get("reach_main_on_rate", True)),
            "after_last_step_policy": policy,
            "extra_ramp": {
                "enabled": enabled,
                "max_adc": max_adc,
                "step_max": step_max,
                "interval_s": interval_s,
            },
        }

    # =========================================================
    # UI 생성
    # =========================================================
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # -------------------------
        # 상단 설명
        # -------------------------
        desc = QLabel(
            "Ramp Step은 최대 10개까지 설정할 수 있습니다.\n"
            "각 step은 목표 ADC와 도달 후 대기시간(delay)을 의미합니다.\n"
            "공정 중 dep.rate 도달 시 메인 공정 진입 여부와 마지막 step 이후 정책도 설정합니다."
        )
        desc.setWordWrap(True)
        root.addWidget(desc)

        # -------------------------
        # Step Count
        # -------------------------
        top_box = QGroupBox("Step Settings")
        top_box.setMinimumHeight(320)
        top_layout = QVBoxLayout(top_box)

        step_count_row = QHBoxLayout()
        step_count_label = QLabel("Step Count")
        self.stepCountSpin = QSpinBox()
        self.stepCountSpin.setRange(self.MIN_STEPS, self.MAX_STEPS)
        self.stepCountSpin.valueChanged.connect(self._on_step_count_changed)

        self.btnFillIncrement = QPushButton("Auto Fill")
        self.btnFillIncrement.clicked.connect(self._on_auto_fill_clicked)

        step_count_row.addWidget(step_count_label)
        step_count_row.addWidget(self.stepCountSpin)
        step_count_row.addStretch(1)
        step_count_row.addWidget(self.btnFillIncrement)

        top_layout.addLayout(step_count_row)

        # -------------------------
        # Step Table
        # -------------------------
        self.stepTable = QTableWidget(self.MAX_STEPS, 2, self)
        self.stepTable.setHorizontalHeaderLabels(["Target ADC", "Delay (s)"])
        self.stepTable.verticalHeader().setVisible(True)
        self.stepTable.setAlternatingRowColors(True)
        self.stepTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stepTable.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.stepTable.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        hh = self.stepTable.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        # Step 5까지는 기본으로 보이도록 최소 높이 확보
        self.stepTable.verticalHeader().setDefaultSectionSize(34)
        visible_rows = 5
        table_min_h = (
            self.stepTable.horizontalHeader().sizeHint().height()
            + self.stepTable.verticalHeader().defaultSectionSize() * visible_rows
            + self.stepTable.frameWidth() * 2
            + 8
        )
        self.stepTable.setMinimumHeight(table_min_h)

        for row in range(self.MAX_STEPS):
            self.stepTable.setVerticalHeaderItem(row, self._make_row_header_item(row))
            self.stepTable.setCellWidget(row, 0, self._make_adc_spin())
            self.stepTable.setCellWidget(row, 1, self._make_delay_spin())

        top_layout.addWidget(self.stepTable)
        root.addWidget(top_box, 1)

        # -------------------------
        # 공통 옵션
        # -------------------------
        common_box = QGroupBox("Common Policy")
        common_layout = QFormLayout(common_box)

        self.reachMainOnRateCheck = QCheckBox("dep.rate 도달 시 즉시 메인 공정 진입")
        common_layout.addRow(self.reachMainOnRateCheck)

        self.afterLastStepPolicyCombo = QComboBox()
        self.afterLastStepPolicyCombo.addItem("Auto Extra Ramp", "extra_ramp")
        self.afterLastStepPolicyCombo.addItem("Stop Process", "stop")
        self.afterLastStepPolicyCombo.currentIndexChanged.connect(self._update_extra_ramp_enabled)
        common_layout.addRow("After Last Step", self.afterLastStepPolicyCombo)

        root.addWidget(common_box)

        # -------------------------
        # Extra Ramp
        # -------------------------
        extra_box = QGroupBox("Extra Ramp")
        extra_layout = QGridLayout(extra_box)

        self.extraRampEnabledCheck = QCheckBox("Enable Extra Ramp")
        self.extraRampEnabledCheck.toggled.connect(self._update_extra_ramp_enabled)
        extra_layout.addWidget(self.extraRampEnabledCheck, 0, 0, 1, 2)

        extra_layout.addWidget(QLabel("Max ADC"), 1, 0)
        self.extraMaxAdcSpin = self._make_adc_spin(maximum=9999.0, decimals=1)
        extra_layout.addWidget(self.extraMaxAdcSpin, 1, 1)

        extra_layout.addWidget(QLabel("Dynamic Step Max"), 2, 0)
        self.extraStepMaxSpin = self._make_adc_spin(maximum=100.0, decimals=1)
        self.extraStepMaxSpin.setMinimum(1.0)
        extra_layout.addWidget(self.extraStepMaxSpin, 2, 1)

        extra_layout.addWidget(QLabel("Interval (s)"), 3, 0)
        self.extraIntervalSpin = self._make_delay_spin(maximum=9999.0, decimals=1)
        self.extraIntervalSpin.setMinimum(0.1)
        extra_layout.addWidget(self.extraIntervalSpin, 3, 1)

        root.addWidget(extra_box)

        # -------------------------
        # 버튼
        # -------------------------
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _make_row_header_item(self, row: int):
        from PySide6.QtWidgets import QTableWidgetItem

        item = QTableWidgetItem(f"Step {row + 1}")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        return item

    def _make_adc_spin(self, *, maximum: float = 9999.0, decimals: int = 1) -> QDoubleSpinBox:
        w = QDoubleSpinBox(self)
        w.setDecimals(decimals)
        w.setRange(0.0, maximum)
        w.setSingleStep(10.0)
        w.setAlignment(Qt.AlignmentFlag.AlignRight)
        w.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        return w

    def _make_delay_spin(self, *, maximum: float = 9999.0, decimals: int = 1) -> QDoubleSpinBox:
        w = QDoubleSpinBox(self)
        w.setDecimals(decimals)
        w.setRange(0.0, maximum)
        w.setSingleStep(1.0)
        w.setAlignment(Qt.AlignmentFlag.AlignRight)
        w.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        return w

    # =========================================================
    # 로드 / 반영
    # =========================================================
    def _load_config(self, cfg: dict[str, Any]) -> None:
        cfg = self._normalize_config(cfg)

        steps = cfg.get("ramp_steps") or []
        self.stepCountSpin.setValue(len(steps))

        for row in range(self.MAX_STEPS):
            adc_spin = self._cell_adc_spin(row)
            delay_spin = self._cell_delay_spin(row)

            if row < len(steps):
                adc_spin.setValue(float((steps[row] or {}).get("target_adc", 0.0)))
                delay_spin.setValue(float((steps[row] or {}).get("delay_s", 0.0)))
            else:
                adc_spin.setValue(0.0)
                delay_spin.setValue(0.0)

        self.reachMainOnRateCheck.setChecked(bool(cfg.get("reach_main_on_rate", True)))

        policy = str(cfg.get("after_last_step_policy", "extra_ramp"))
        idx = self.afterLastStepPolicyCombo.findData(policy)
        if idx < 0:
            idx = 0
        self.afterLastStepPolicyCombo.setCurrentIndex(idx)

        extra = dict(cfg.get("extra_ramp") or {})
        self.extraRampEnabledCheck.setChecked(bool(extra.get("enabled", True)))
        self.extraMaxAdcSpin.setValue(float(extra.get("max_adc", 300.0) or 300.0))
        self.extraStepMaxSpin.setValue(float(extra.get("step_max", 50.0) or 50.0))
        self.extraIntervalSpin.setValue(float(extra.get("interval_s", 5.0) or 5.0))

        self._sync_step_enabled_rows()
        self._update_extra_ramp_enabled()

    def _on_step_count_changed(self, _value: int) -> None:
        self._sync_step_enabled_rows()

    def _sync_step_enabled_rows(self) -> None:
        count = int(self.stepCountSpin.value())
        for row in range(self.MAX_STEPS):
            enabled = row < count
            self._cell_adc_spin(row).setEnabled(enabled)
            self._cell_delay_spin(row).setEnabled(enabled)

    def _update_extra_ramp_enabled(self) -> None:
        policy = self.afterLastStepPolicyCombo.currentData()
        extra_enabled = bool(self.extraRampEnabledCheck.isChecked()) and (policy == "extra_ramp")

        self.extraMaxAdcSpin.setEnabled(extra_enabled)
        self.extraStepMaxSpin.setEnabled(extra_enabled)
        self.extraIntervalSpin.setEnabled(extra_enabled)
        self.extraRampEnabledCheck.setEnabled(policy == "extra_ramp")

    def _cell_adc_spin(self, row: int) -> QDoubleSpinBox:
        w = self.stepTable.cellWidget(row, 0)
        assert isinstance(w, QDoubleSpinBox)
        return w

    def _cell_delay_spin(self, row: int) -> QDoubleSpinBox:
        w = self.stepTable.cellWidget(row, 1)
        assert isinstance(w, QDoubleSpinBox)
        return w

    # =========================================================
    # 편의 기능
    # =========================================================
    def _on_auto_fill_clicked(self) -> None:
        """
        현재 Step 1 값을 기준으로
        뒤 step들을 완만하게 증가하도록 자동 채운다.
        """
        count = int(self.stepCountSpin.value())
        if count <= 1:
            return

        first_adc = self._cell_adc_spin(0).value()
        first_delay = self._cell_delay_spin(0).value()

        if first_adc <= 0:
            QMessageBox.information(self, "Auto Fill", "Step 1의 Target ADC를 먼저 입력하세요.")
            return

        # 기본 증가폭은 50, 단 100 초과 금지 컨셉과 맞게 유지
        increment = min(50.0, max(10.0, first_adc * 0.2))

        current = first_adc
        for row in range(1, count):
            current += increment
            self._cell_adc_spin(row).setValue(current)
            self._cell_delay_spin(row).setValue(first_delay)

    # =========================================================
    # 반환
    # =========================================================
    def get_config(self) -> dict[str, Any]:
        count = int(self.stepCountSpin.value())
        steps: list[dict[str, float]] = []

        for row in range(count):
            steps.append(
                {
                    "target_adc": float(self._cell_adc_spin(row).value()),
                    "delay_s": float(self._cell_delay_spin(row).value()),
                }
            )

        cfg = {
            "step_count": count,
            "ramp_steps": steps,
            "reach_main_on_rate": bool(self.reachMainOnRateCheck.isChecked()),
            "after_last_step_policy": str(self.afterLastStepPolicyCombo.currentData() or "extra_ramp"),
            "extra_ramp": {
                "enabled": bool(self.extraRampEnabledCheck.isChecked()),
                "max_adc": float(self.extraMaxAdcSpin.value()),
                "step_max": float(self.extraStepMaxSpin.value()),
                "interval_s": float(self.extraIntervalSpin.value()),
            },
        }
        return self._normalize_config(cfg)

    # =========================================================
    # OK validation
    # =========================================================
    def accept(self) -> None:
        cfg = self.get_config()
        steps = cfg.get("ramp_steps") or []

        if not steps:
            QMessageBox.warning(self, "Process Config", "최소 1개의 step이 필요합니다.")
            return

        # step target_adc 오름차순 체크
        last_adc = -1.0
        for idx, step in enumerate(steps, start=1):
            adc = float(step.get("target_adc", 0.0) or 0.0)
            delay_s = float(step.get("delay_s", 0.0) or 0.0)

            if adc <= 0:
                QMessageBox.warning(self, "Process Config", f"Step {idx}의 Target ADC는 0보다 커야 합니다.")
                return

            if delay_s < 0:
                QMessageBox.warning(self, "Process Config", f"Step {idx}의 Delay는 0 이상이어야 합니다.")
                return

            if last_adc >= 0 and adc < last_adc:
                QMessageBox.warning(
                    self,
                    "Process Config",
                    f"Step {idx}의 Target ADC가 이전 step보다 작습니다.\n"
                    "step 순서는 일반적으로 오름차순으로 입력하는 것이 안전합니다.",
                )
                return

            last_adc = adc

        if cfg["after_last_step_policy"] == "extra_ramp":
            extra = cfg.get("extra_ramp") or {}
            if not bool(extra.get("enabled", False)):
                QMessageBox.warning(
                    self,
                    "Process Config",
                    "After Last Step 정책이 Extra Ramp인데, Extra Ramp Enable이 꺼져 있습니다.",
                )
                return

            max_adc = float(extra.get("max_adc", 0.0) or 0.0)
            step_max = float(extra.get("step_max", 0.0) or 0.0)
            interval_s = float(extra.get("interval_s", 0.0) or 0.0)

            if max_adc <= 0:
                QMessageBox.warning(self, "Process Config", "Extra Ramp의 Max ADC는 0보다 커야 합니다.")
                return
            if not (1.0 <= step_max <= 100.0):
                QMessageBox.warning(self, "Process Config", "Extra Ramp의 Dynamic Step Max는 1 ~ 100 범위여야 합니다.")
                return
            if interval_s <= 0:
                QMessageBox.warning(self, "Process Config", "Extra Ramp의 Interval은 0보다 커야 합니다.")
                return

            last_step_adc = float(steps[-1].get("target_adc", 0.0) or 0.0)
            if max_adc < last_step_adc:
                QMessageBox.warning(
                    self,
                    "Process Config",
                    "Extra Ramp의 Max ADC는 마지막 Step의 Target ADC보다 작을 수 없습니다.",
                )
                return

        super().accept()