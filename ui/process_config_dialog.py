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
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)


class ProcessConfigDialog(QDialog):
    """
    Evaporator process config dialog.

    Provides step ramp, policy, DAC tuning, and rate/shortage judge settings.
    get_config() returns a normalized config dictionary.
    """

    MAX_STEPS = 10
    MIN_STEPS = 1

    def __init__(self, initial_config: dict[str, Any] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Process Config")
        self.setModal(True)
        self.resize(900, 700)
        self.setMinimumSize(860, 620)

        self._initial_config = self._normalize_config(initial_config or self._default_config())

        self._build_ui()
        self._load_config(self._initial_config)

    # =========================================================
    # defaults / normalize
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

            # DAC / ramp
            "ramp_seg1_max_dac": 700,
            "ramp_interval_seg1_s": 10.0,
            "ramp_seg2_max_dac": 2000,
            "ramp_interval_seg2_s": 30.0,
            "ramp_interval_after_seg2_s": 30.0,

            # pre-rate
            "pre_rate": 0.4,

            # DAC adjust
            "dac_adjust_interval_s": 10.0,
            "fine_step_dac": 10,

            # shortage
            "material_shortage_dac": 2000,
            "material_shortage_rate_max": 0.0,
            "material_shortage_time_s": 10.0,

            # dep.rate judge
            "rate_filter_window": 5,
            "rate_stable_sec": 3.0,
            "rate_drop_ratio": 0.50,
            "rate_drop_count": 3,
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

            "ramp_seg1_max_dac": _as_int("ramp_seg1_max_dac", 700, 1),
            "ramp_interval_seg1_s": _as_float("ramp_interval_seg1_s", 10.0, 0.1),
            "ramp_seg2_max_dac": _as_int("ramp_seg2_max_dac", 2000, 1),
            "ramp_interval_seg2_s": _as_float("ramp_interval_seg2_s", 30.0, 0.1),
            "ramp_interval_after_seg2_s": _as_float("ramp_interval_after_seg2_s", 30.0, 0.1),

            "pre_rate": _as_float("pre_rate", 0.4, 0.0),

            "dac_adjust_interval_s": _as_float("dac_adjust_interval_s", 10.0, 0.1),
            "fine_step_dac": _as_int("fine_step_dac", 10, 1),

            "material_shortage_dac": _as_int("material_shortage_dac", 2000, 0),
            "material_shortage_rate_max": _as_float("material_shortage_rate_max", 0.0, 0.0),
            "material_shortage_time_s": _as_float("material_shortage_time_s", 10.0, 0.0),

            "rate_filter_window": _as_int("rate_filter_window", 5, 1, 21),
            "rate_stable_sec": _as_float("rate_stable_sec", 3.0, 0.0),
            "rate_drop_ratio": _as_float("rate_drop_ratio", 0.50, 0.01, 1.0),
            "rate_drop_count": _as_int("rate_drop_count", 3, 1, 20),
        }

    # =========================================================
    # UI build
    # =========================================================
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        tabs = QTabWidget(self)
        root.addWidget(tabs, 1)

        def _make_tab_with_scroll() -> tuple[QWidget, QVBoxLayout]:
            tab = QWidget(self)
            tab_layout = QVBoxLayout(tab)
            tab_layout.setContentsMargins(0, 0, 0, 0)
            tab_layout.setSpacing(0)

            scroll = QScrollArea(tab)
            scroll.setWidgetResizable(True)

            body = QWidget(scroll)
            body_layout = QVBoxLayout(body)
            body_layout.setContentsMargins(6, 6, 6, 6)
            body_layout.setSpacing(8)
            scroll.setWidget(body)

            tab_layout.addWidget(scroll)
            return tab, body_layout

        step_tab, step_root = _make_tab_with_scroll()
        adv_tab, adv_root = _make_tab_with_scroll()
        tabs.addTab(step_tab, "Step / Policy")
        tabs.addTab(adv_tab, "Advanced")

        desc = QLabel(
            "Step 기반 ramp를 먼저 진행하고, dep.rate 도달 시 main 공정으로 진입합니다.\n"
            "Step/정책은 첫 탭에서, 세부 DAC tuning과 rate 판단은 Advanced 탭에서 설정하세요."
        )
        desc.setWordWrap(True)
        step_root.addWidget(desc)

        top_box = QGroupBox("Step Settings")
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

        self.stepTable.verticalHeader().setDefaultSectionSize(34)
        visible_rows = 4
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
        step_root.addWidget(top_box)

        common_box = QGroupBox("Main Entry Policy")
        common_layout = QFormLayout(common_box)
        self.reachMainOnRateCheck = QCheckBox("dep.rate 도달 시 즉시 main 공정 진입")
        common_layout.addRow(self.reachMainOnRateCheck)

        self.afterLastStepPolicyCombo = QComboBox()
        self.afterLastStepPolicyCombo.addItem("Auto Extra Ramp", "extra_ramp")
        self.afterLastStepPolicyCombo.addItem("Stop Process", "stop")
        self.afterLastStepPolicyCombo.currentIndexChanged.connect(self._update_extra_ramp_enabled)
        common_layout.addRow("After Last Step", self.afterLastStepPolicyCombo)
        step_root.addWidget(common_box)

        extra_box = QGroupBox("Extra Ramp Policy")
        extra_layout = QGridLayout(extra_box)
        self.extraRampEnabledCheck = QCheckBox("Enable Extra Ramp")
        self.extraRampEnabledCheck.toggled.connect(self._update_extra_ramp_enabled)
        extra_layout.addWidget(self.extraRampEnabledCheck, 0, 0, 1, 2)

        extra_layout.addWidget(QLabel("Max ADC"), 1, 0)
        self.extraMaxAdcSpin = self._make_adc_spin(maximum=9999.0, decimals=1)
        extra_layout.addWidget(self.extraMaxAdcSpin, 1, 1)

        extra_layout.addWidget(QLabel("Interval (s)"), 2, 0)
        self.extraIntervalSpin = self._make_delay_spin(maximum=9999.0, decimals=1)
        self.extraIntervalSpin.setMinimum(0.1)
        extra_layout.addWidget(self.extraIntervalSpin, 2, 1)
        step_root.addWidget(extra_box)
        step_root.addStretch(1)

        dac_box = QGroupBox("DAC Ramp Tuning")
        dac_form = QFormLayout(dac_box)
        self.extraStepMaxSpin = self._make_adc_spin(maximum=100.0, decimals=1)
        self.extraStepMaxSpin.setMinimum(1.0)

        self.rampSeg1MaxDacSpin = self._make_int_spin(minimum=1, maximum=9999, step=10)
        self.rampSeg1IntervalSpin = self._make_delay_spin(maximum=9999.0, decimals=1)
        self.rampSeg1IntervalSpin.setMinimum(0.1)
        self.rampSeg2MaxDacSpin = self._make_int_spin(minimum=1, maximum=9999, step=10)
        self.rampSeg2IntervalSpin = self._make_delay_spin(maximum=9999.0, decimals=1)
        self.rampSeg2IntervalSpin.setMinimum(0.1)
        self.rampAfterSeg2IntervalSpin = self._make_delay_spin(maximum=9999.0, decimals=1)
        self.rampAfterSeg2IntervalSpin.setMinimum(0.1)
        self.dacAdjustIntervalSpin = self._make_delay_spin(maximum=99999.0, decimals=1)
        self.fineStepDacSpin = self._make_int_spin(minimum=1, maximum=1000, step=1)

        dac_form.addRow("Dynamic Step Max", self.extraStepMaxSpin)
        dac_form.addRow("Seg1 Max DAC", self.rampSeg1MaxDacSpin)
        dac_form.addRow("Seg1 Interval (s)", self.rampSeg1IntervalSpin)
        dac_form.addRow("Seg2 Max DAC", self.rampSeg2MaxDacSpin)
        dac_form.addRow("Seg2 Interval (s)", self.rampSeg2IntervalSpin)
        dac_form.addRow("After Seg2 Interval (s)", self.rampAfterSeg2IntervalSpin)
        dac_form.addRow("DAC Adjust Interval (s)", self.dacAdjustIntervalSpin)
        dac_form.addRow("Fine Step DAC", self.fineStepDacSpin)
        adv_root.addWidget(dac_box)

        pre_box = QGroupBox("Pre Rate")
        pre_form = QFormLayout(pre_box)
        self.preRateSpin = self._make_adc_spin(maximum=999.0, decimals=2)
        pre_form.addRow("Pre Rate (A/s)", self.preRateSpin)
        adv_root.addWidget(pre_box)

        shortage_box = QGroupBox("Material Shortage Guard")
        shortage_form = QFormLayout(shortage_box)
        self.shortageDacSpin = self._make_int_spin(minimum=0, maximum=9999, step=10)
        self.shortageRateMaxSpin = self._make_adc_spin(maximum=999.0, decimals=2)
        self.shortageTimeSpin = self._make_delay_spin(maximum=99999.0, decimals=1)
        shortage_form.addRow("Shortage DAC", self.shortageDacSpin)
        shortage_form.addRow("Shortage Rate Max (횇/s)", self.shortageRateMaxSpin)
        shortage_form.addRow("Shortage Time (s)", self.shortageTimeSpin)
        adv_root.addWidget(shortage_box)

        rate_box = QGroupBox("Rate Filter / Judge")
        rate_form = QFormLayout(rate_box)
        self.rateFilterWindowSpin = self._make_int_spin(minimum=1, maximum=21, step=2)
        self.rateStableSecSpin = self._make_delay_spin(maximum=60.0, decimals=1)
        self.rateDropRatioSpin = self._make_adc_spin(maximum=1.0, decimals=2)
        self.rateDropRatioSpin.setRange(0.01, 1.0)
        self.rateDropCountSpin = self._make_int_spin(minimum=1, maximum=20, step=1)
        rate_form.addRow("Rate Filter Window", self.rateFilterWindowSpin)
        rate_form.addRow("Rate Stable Sec", self.rateStableSecSpin)
        rate_form.addRow("Rate Drop Ratio", self.rateDropRatioSpin)
        rate_form.addRow("Rate Drop Count", self.rateDropCountSpin)
        adv_root.addWidget(rate_box)
        adv_root.addStretch(1)

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
    
    def _make_int_spin(self, *, minimum: int = 0, maximum: int = 999999, step: int = 1) -> QSpinBox:
        w = QSpinBox(self)
        w.setRange(minimum, maximum)
        w.setSingleStep(step)
        w.setAlignment(Qt.AlignmentFlag.AlignRight)
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
    # load / apply
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

        self.rampSeg1MaxDacSpin.setValue(int(cfg.get("ramp_seg1_max_dac", 700)))
        self.rampSeg1IntervalSpin.setValue(float(cfg.get("ramp_interval_seg1_s", 10.0)))
        self.rampSeg2MaxDacSpin.setValue(int(cfg.get("ramp_seg2_max_dac", 2000)))
        self.rampSeg2IntervalSpin.setValue(float(cfg.get("ramp_interval_seg2_s", 30.0)))
        self.rampAfterSeg2IntervalSpin.setValue(float(cfg.get("ramp_interval_after_seg2_s", 30.0)))

        self.preRateSpin.setValue(float(cfg.get("pre_rate", 0.4)))

        self.dacAdjustIntervalSpin.setValue(float(cfg.get("dac_adjust_interval_s", 10.0)))
        self.fineStepDacSpin.setValue(int(cfg.get("fine_step_dac", 10)))

        self.shortageDacSpin.setValue(int(cfg.get("material_shortage_dac", 2000)))
        self.shortageRateMaxSpin.setValue(float(cfg.get("material_shortage_rate_max", 0.0)))
        self.shortageTimeSpin.setValue(float(cfg.get("material_shortage_time_s", 10.0)))

        self.rateFilterWindowSpin.setValue(int(cfg.get("rate_filter_window", 5)))
        self.rateStableSecSpin.setValue(float(cfg.get("rate_stable_sec", 3.0)))
        self.rateDropRatioSpin.setValue(float(cfg.get("rate_drop_ratio", 0.5)))
        self.rateDropCountSpin.setValue(int(cfg.get("rate_drop_count", 3)))

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
    # convenience
    # =========================================================
    def _on_auto_fill_clicked(self) -> None:
        """Auto-fill step rows based on Step 1 target/delay."""
        count = int(self.stepCountSpin.value())
        if count <= 1:
            return

        first_adc = self._cell_adc_spin(0).value()
        first_delay = self._cell_delay_spin(0).value()

        if first_adc <= 0:
            QMessageBox.information(self, "Auto Fill", "Enter Step 1 Target ADC first.")
            return

        # Keep incremental fill conservative.
        increment = min(50.0, max(10.0, first_adc * 0.2))

        current = first_adc
        for row in range(1, count):
            current += increment
            self._cell_adc_spin(row).setValue(current)
            self._cell_delay_spin(row).setValue(first_delay)

    # =========================================================
    # output
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

        policy = str(self.afterLastStepPolicyCombo.currentData() or "extra_ramp")
        extra_enabled = bool(self.extraRampEnabledCheck.isChecked()) if policy == "extra_ramp" else False

        cfg = {
            "step_count": count,
            "ramp_steps": steps,
            "reach_main_on_rate": bool(self.reachMainOnRateCheck.isChecked()),
            "after_last_step_policy": policy,
            "extra_ramp": {
                "enabled": extra_enabled,
                "max_adc": float(self.extraMaxAdcSpin.value()),
                "step_max": float(self.extraStepMaxSpin.value()),
                "interval_s": float(self.extraIntervalSpin.value()),
            },

            "ramp_seg1_max_dac": int(self.rampSeg1MaxDacSpin.value()),
            "ramp_interval_seg1_s": float(self.rampSeg1IntervalSpin.value()),
            "ramp_seg2_max_dac": int(self.rampSeg2MaxDacSpin.value()),
            "ramp_interval_seg2_s": float(self.rampSeg2IntervalSpin.value()),
            "ramp_interval_after_seg2_s": float(self.rampAfterSeg2IntervalSpin.value()),

            "pre_rate": float(self.preRateSpin.value()),

            "dac_adjust_interval_s": float(self.dacAdjustIntervalSpin.value()),
            "fine_step_dac": int(self.fineStepDacSpin.value()),

            "material_shortage_dac": int(self.shortageDacSpin.value()),
            "material_shortage_rate_max": float(self.shortageRateMaxSpin.value()),
            "material_shortage_time_s": float(self.shortageTimeSpin.value()),

            "rate_filter_window": int(self.rateFilterWindowSpin.value()),
            "rate_stable_sec": float(self.rateStableSecSpin.value()),
            "rate_drop_ratio": float(self.rateDropRatioSpin.value()),
            "rate_drop_count": int(self.rateDropCountSpin.value()),
        }
        return self._normalize_config(cfg)

    # =========================================================
    # OK validation
    # =========================================================
    def accept(self) -> None:
        cfg = self.get_config()
        steps = cfg.get("ramp_steps") or []

        if not steps:
            QMessageBox.warning(self, "Process Config", "At least one step is required.")
            return

        if cfg["ramp_seg2_max_dac"] < cfg["ramp_seg1_max_dac"]:
            QMessageBox.warning(self, "Process Config", "Seg2 Max DAC must be >= Seg1 Max DAC.")
            return
        if cfg["rate_filter_window"] < 1:
            QMessageBox.warning(self, "Process Config", "Rate Filter Window must be >= 1.")
            return

        if not (0.0 < cfg["rate_drop_ratio"] <= 1.0):
            QMessageBox.warning(self, "Process Config", "Rate Drop Ratio must be in (0, 1].")
            return

        # step target_adc ascending check
        last_adc = -1.0
        for idx, step in enumerate(steps, start=1):
            adc = float(step.get("target_adc", 0.0) or 0.0)
            delay_s = float(step.get("delay_s", 0.0) or 0.0)

            if adc <= 0:
                QMessageBox.warning(self, "Process Config", f"Step {idx} Target ADC must be > 0.")
                return

            if delay_s < 0:
                QMessageBox.warning(self, "Process Config", f"Step {idx} Delay must be >= 0.")
                return

            if last_adc >= 0 and adc < last_adc:
                QMessageBox.warning(
                    self,
                    "Process Config",
                    f"Step {idx} Target ADC is lower than the previous step.\n"
                    "Use non-decreasing Target ADC values.",
                )
                return

            last_adc = adc

        if cfg["after_last_step_policy"] == "extra_ramp":
            extra = cfg.get("extra_ramp") or {}
            if not bool(extra.get("enabled", False)):
                QMessageBox.warning(
                    self,
                    "Process Config",
                    "After Last Step is extra_ramp, but Extra Ramp is disabled.",
                )
                return

            max_adc = float(extra.get("max_adc", 0.0) or 0.0)
            step_max = float(extra.get("step_max", 0.0) or 0.0)
            interval_s = float(extra.get("interval_s", 0.0) or 0.0)

            if max_adc <= 0:
                QMessageBox.warning(self, "Process Config", "Extra Ramp Max ADC must be > 0.")
                return
            if not (1.0 <= step_max <= 100.0):
                QMessageBox.warning(self, "Process Config", "Extra Ramp Dynamic Step Max must be 1~100.")
                return
            if interval_s <= 0:
                QMessageBox.warning(self, "Process Config", "Extra Ramp Interval must be > 0.")
                return

            last_step_adc = float(steps[-1].get("target_adc", 0.0) or 0.0)
            if max_adc < last_step_adc:
                QMessageBox.warning(
                    self,
                    "Process Config",
                    "Extra Ramp Max ADC must be >= last step Target ADC.",
                )
                return

        super().accept()
