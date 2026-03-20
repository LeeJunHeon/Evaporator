from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
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
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)

# 현재 실행 중 step 하이라이트 색
_HIGHLIGHT_COLOR = QColor(255, 230, 100)   # 노란색 계열
_NORMAL_BG       = QColor(255, 255, 255)
_DISABLED_BG     = QColor(230, 230, 230)

# step 테이블 컬럼 인덱스
_COL_ENABLED    = 0
_COL_TARGET_ADC = 1
_COL_DAC_STEP   = 2
_COL_INTERVAL   = 3
_COL_WAIT       = 4
_COL_MIN_RATE   = 5
_COL_ACTION     = 6
_COL_BOOST_DAC  = 7
_COL_BOOST_MAX  = 8

_COL_HEADERS = [
    "활성화",
    "Target ADC",
    "DAC step",
    "Interval(s)",
    "Wait(s)",
    "Min rate(Å/s)",
    "Low rate action",
    "Boost DAC step",
    "Boost Max",
]

_ACTIONS = [
    ("next_step", "다음 step"),
    ("boost_dac",  "Boost DAC"),
    ("stop",       "즉시 정지"),
]


class ProcessConfigDialog(QDialog):
    """
    Evaporator process config dialog — EvapStep 기반.

    Step 탭:
    - 5행 고정 테이블 (행 = EvapStep 1~5)
    - 컬럼: 활성화 | Target ADC | DAC step | Interval(s) | Wait(s) | Min rate | Low rate action
            | Boost DAC step | Boost Max
    - Low rate action = "boost_dac" 선택 시 Boost 컬럼 활성화, 나머지는 비활성화
    - highlight_step(idx) : 현재 실행 중인 step 행 하이라이트
    - 저장 버튼: _on_save_steps_clicked → _process_cfg에 반영

    Advanced 탭: DAC tuning, pre-rate, shortage guard, rate filter 설정
    """

    MAX_STEPS = 5
    MIN_STEPS = 1

    def __init__(self, initial_config: dict[str, Any] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Process Config")
        self.setModal(True)
        self.resize(1100, 640)
        self.setMinimumSize(900, 560)

        self._initial_config = self._normalize_config(initial_config or self._default_config())
        self._active_step: Optional[int] = None   # 현재 실행 중 step (0-based), None=없음

        self._build_ui()
        self._load_config(self._initial_config)

    # =========================================================
    # defaults / normalize
    # =========================================================
    @staticmethod
    def _default_step() -> dict[str, Any]:
        """EvapStep 하나의 기본값."""
        return {
            "target_adc":     100.0,
            "dac_step":       10,
            "dac_interval_sec": 30.0,
            "rate_wait_sec":  0.0,
            "min_dep_rate":   0.1,
            "rate_low_action": "next_step",
            "boost_dac_step": 0,
            "boost_max_count": 0,
            # 레거시 호환
            "delay_s":        0.0,
        }

    def _default_config(self) -> dict[str, Any]:
        return {
            "step_count": 1,
            "ramp_steps": [self._default_step()],
            "reach_main_on_rate": True,
            "after_last_step_policy": "extra_ramp",
            "extra_ramp": {
                "enabled": True,
                "max_adc": 300.0,
                "step_max": 50.0,
                "interval_s": 5.0,
            },
            "ramp_seg1_max_dac": 700,
            "ramp_interval_seg1_s": 10.0,
            "ramp_seg2_max_dac": 2000,
            "ramp_interval_seg2_s": 30.0,
            "ramp_interval_after_seg2_s": 30.0,
            "pre_rate": 0.4,
            "dac_adjust_interval_s": 10.0,
            "fine_step_dac": 10,
            "material_shortage_dac": 2000,
            "material_shortage_rate_max": 0.0,
            "material_shortage_time_s": 10.0,
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
        steps: list[dict[str, Any]] = []

        for item in list(raw_steps)[: self.MAX_STEPS]:
            item = dict(item or {})
            try:
                target_adc = max(0.0, float(item.get("target_adc", 0.0)))
            except Exception:
                target_adc = 0.0
            # 레거시: delay_s → rate_wait_sec
            try:
                legacy_delay = max(0.0, float(item.get("delay_s", 0.0)))
            except Exception:
                legacy_delay = 0.0
            try:
                rate_wait_sec = max(0.0, float(item.get("rate_wait_sec", legacy_delay)))
            except Exception:
                rate_wait_sec = legacy_delay

            try:
                dac_step = max(1, int(item.get("dac_step", 10)))
            except Exception:
                dac_step = 10
            try:
                dac_interval = max(0.1, float(item.get("dac_interval_sec", 30.0)))
            except Exception:
                dac_interval = 30.0
            try:
                min_dep_rate = max(0.0, float(item.get("min_dep_rate", 0.1)))
            except Exception:
                min_dep_rate = 0.1

            action = str(item.get("rate_low_action", "next_step") or "next_step").strip().lower()
            if action not in {"next_step", "boost_dac", "stop"}:
                action = "next_step"

            try:
                boost_dac_step = max(0, int(item.get("boost_dac_step", 0)))
            except Exception:
                boost_dac_step = 0
            try:
                boost_max_count = max(0, int(item.get("boost_max_count", 0)))
            except Exception:
                boost_max_count = 0

            steps.append({
                "target_adc":      target_adc,
                "dac_step":        dac_step,
                "dac_interval_sec": dac_interval,
                "rate_wait_sec":   rate_wait_sec,
                "delay_s":         rate_wait_sec,   # 레거시 호환
                "min_dep_rate":    min_dep_rate,
                "rate_low_action": action,
                "boost_dac_step":  boost_dac_step,
                "boost_max_count": boost_max_count,
            })

        if not steps:
            steps = [self._default_step()]

        step_count = len(steps)
        step_count = max(self.MIN_STEPS, min(self.MAX_STEPS, step_count))
        steps = steps[:step_count]

        extra_src = dict(src.get("extra_ramp") or default["extra_ramp"])
        try:
            extra_enabled = bool(extra_src.get("enabled", True))
        except Exception:
            extra_enabled = True
        try:
            extra_max_adc = max(0.0, float(extra_src.get("max_adc", 300.0) or 0.0))
        except Exception:
            extra_max_adc = 300.0
        try:
            extra_step_max = float(extra_src.get("step_max", 50.0) or 50.0)
            extra_step_max = min(100.0, max(1.0, extra_step_max))
        except Exception:
            extra_step_max = 50.0
        try:
            extra_interval_s = max(0.1, float(extra_src.get("interval_s", 5.0) or 5.0))
        except Exception:
            extra_interval_s = 5.0

        policy = str(src.get("after_last_step_policy", "extra_ramp") or "extra_ramp").strip().lower()
        if policy not in {"extra_ramp", "stop"}:
            policy = "extra_ramp"

        return {
            "step_count": len(steps),
            "ramp_steps": steps,
            "reach_main_on_rate": bool(src.get("reach_main_on_rate", True)),
            "after_last_step_policy": policy,
            "extra_ramp": {
                "enabled":    extra_enabled,
                "max_adc":    extra_max_adc,
                "step_max":   extra_step_max,
                "interval_s": extra_interval_s,
            },
            "ramp_seg1_max_dac":            _as_int("ramp_seg1_max_dac", 700, 1),
            "ramp_interval_seg1_s":         _as_float("ramp_interval_seg1_s", 10.0, 0.1),
            "ramp_seg2_max_dac":            _as_int("ramp_seg2_max_dac", 2000, 1),
            "ramp_interval_seg2_s":         _as_float("ramp_interval_seg2_s", 30.0, 0.1),
            "ramp_interval_after_seg2_s":   _as_float("ramp_interval_after_seg2_s", 30.0, 0.1),
            "pre_rate":                     _as_float("pre_rate", 0.4, 0.0),
            "dac_adjust_interval_s":        _as_float("dac_adjust_interval_s", 10.0, 0.1),
            "fine_step_dac":                _as_int("fine_step_dac", 10, 1),
            "material_shortage_dac":        _as_int("material_shortage_dac", 2000, 0),
            "material_shortage_rate_max":   _as_float("material_shortage_rate_max", 0.0, 0.0),
            "material_shortage_time_s":     _as_float("material_shortage_time_s", 10.0, 0.0),
            "rate_filter_window":           _as_int("rate_filter_window", 5, 1, 21),
            "rate_stable_sec":              _as_float("rate_stable_sec", 3.0, 0.0),
            "rate_drop_ratio":              _as_float("rate_drop_ratio", 0.50, 0.01, 1.0),
            "rate_drop_count":              _as_int("rate_drop_count", 3, 1, 20),
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

        def _make_scrolled_tab() -> tuple[QWidget, QVBoxLayout]:
            tab = QWidget(self)
            tab_layout = QVBoxLayout(tab)
            tab_layout.setContentsMargins(0, 0, 0, 0)
            scroll = QScrollArea(tab)
            scroll.setWidgetResizable(True)
            body = QWidget(scroll)
            body_layout = QVBoxLayout(body)
            body_layout.setContentsMargins(6, 6, 6, 6)
            body_layout.setSpacing(8)
            scroll.setWidget(body)
            tab_layout.addWidget(scroll)
            return tab, body_layout

        step_tab, step_root = _make_scrolled_tab()
        adv_tab,  adv_root  = _make_scrolled_tab()
        tabs.addTab(step_tab, "Step / Policy")
        tabs.addTab(adv_tab,  "Advanced")

        # ----- Step 탭 -----
        # EvapStep 테이블
        step_box = QGroupBox("Step Settings (최대 5 step)")
        step_box_layout = QVBoxLayout(step_box)

        self.stepTable = QTableWidget(self.MAX_STEPS, len(_COL_HEADERS), self)
        self.stepTable.setHorizontalHeaderLabels(_COL_HEADERS)

        _COL_TOOLTIPS = [
            "이 step 활성화 여부. 체크 해제 시 해당 step은 공정에서 제외됩니다.",
            "목표 ADC 피드백 값 (EMA 필터 적용 기준).\n이 값에 도달할 때까지 DAC을 올립니다.",
            "한 번에 올리는 DAC 증분.\n예) 20이면 Interval마다 DAC을 20씩 증가시킵니다.",
            "DAC을 올리는 주기 (초).\n예) 30이면 30초마다 DAC step씩 증가합니다.",
            "Target ADC 도달 후 dep.rate을 관찰하는 대기 시간 (초).\n이 시간 동안 dep.rate을 측정해 Min rate과 비교합니다.",
            "정상 판정 최소 dep.rate (Å/s).\n이 값 이상이면 다음 step으로 진행합니다.",
            "Wait 후에도 dep.rate이 Min rate 미만일 때의 동작.\n\n"
            "다음 step: 그냥 다음으로 진행 (초기 가열 구간처럼 아직 rate이 나오지 않는 게 정상인 구간에 적합)\n"
            "Boost DAC: DAC을 소폭 추가로 올리며 재시도 (Boost 설정 값 사용)\n"
            "즉시 정지: 물질 소진으로 판정하고 안전 정지",
            "Low rate action = Boost DAC 선택 시 추가로 올리는 DAC 증분.\nBoost DAC이 아닌 경우 비활성화됩니다.",
            "Boost 최대 시도 횟수.\n모두 소진한 후에도 dep.rate이 Min rate 미만이면 소진 판정 후 안전 정지합니다.",
        ]
        for col, tip in enumerate(_COL_TOOLTIPS):
            item = self.stepTable.horizontalHeaderItem(col)
            if item is None:
                item = QTableWidgetItem(_COL_HEADERS[col])
                self.stepTable.setHorizontalHeaderItem(col, item)
            item.setToolTip(tip)

        self.stepTable.verticalHeader().setVisible(True)
        self.stepTable.setAlternatingRowColors(False)
        self.stepTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stepTable.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.stepTable.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        hh = self.stepTable.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(_COL_ACTION, QHeaderView.ResizeMode.Fixed)
        hh.resizeSection(_COL_ACTION, 110)
        hh.setStretchLastSection(False)
        self.stepTable.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stepTable.verticalHeader().setDefaultSectionSize(36)

        for row in range(self.MAX_STEPS):
            self.stepTable.setVerticalHeaderItem(
                row, QTableWidgetItem(f"Step {row + 1}")
            )
            self._build_step_row(row)

        table_h = (
            self.stepTable.horizontalHeader().sizeHint().height()
            + self.stepTable.verticalHeader().defaultSectionSize() * self.MAX_STEPS
            + self.stepTable.frameWidth() * 2
            + 2
        )
        self.stepTable.setFixedHeight(table_h)

        step_box_layout.addWidget(self.stepTable)
        step_root.addWidget(step_box)

        # Policy
        common_box = QGroupBox("Main Entry Policy")
        common_form = QFormLayout(common_box)
        self.reachMainOnRateCheck = QCheckBox("dep.rate 도달 시 즉시 main 공정 진입")
        common_form.addRow(self.reachMainOnRateCheck)
        self.afterLastStepPolicyCombo = QComboBox()
        self.afterLastStepPolicyCombo.addItem("Auto Extra Ramp", "extra_ramp")
        self.afterLastStepPolicyCombo.addItem("Stop Process", "stop")
        self.afterLastStepPolicyCombo.currentIndexChanged.connect(self._update_extra_ramp_enabled)
        common_form.addRow("After Last Step", self.afterLastStepPolicyCombo)
        step_root.addWidget(common_box)

        extra_box = QGroupBox("Extra Ramp Policy")
        extra_layout = QGridLayout(extra_box)
        self.extraRampEnabledCheck = QCheckBox("Enable Extra Ramp")
        self.extraRampEnabledCheck.toggled.connect(self._update_extra_ramp_enabled)
        extra_layout.addWidget(self.extraRampEnabledCheck, 0, 0, 1, 2)
        extra_layout.addWidget(QLabel("Max ADC"), 1, 0)
        self.extraMaxAdcSpin = self._make_double_spin(0.0, 9999.0, step=10.0)
        extra_layout.addWidget(self.extraMaxAdcSpin, 1, 1)
        extra_layout.addWidget(QLabel("Interval (s)"), 2, 0)
        self.extraIntervalSpin = self._make_double_spin(0.1, 9999.0)
        extra_layout.addWidget(self.extraIntervalSpin, 2, 1)
        step_root.addWidget(extra_box)
        step_root.addStretch(1)

        # ----- Advanced 탭 -----
        dac_box = QGroupBox("DAC Ramp Tuning")
        dac_form = QFormLayout(dac_box)
        self.extraStepMaxSpin       = self._make_double_spin(1.0, 100.0)
        self.rampSeg1MaxDacSpin     = self._make_int_spin(1, 9999, step=10)
        self.rampSeg1IntervalSpin   = self._make_double_spin(0.1, 9999.0)
        self.rampSeg2MaxDacSpin     = self._make_int_spin(1, 9999, step=10)
        self.rampSeg2IntervalSpin   = self._make_double_spin(0.1, 9999.0)
        self.rampAfterSeg2IntervalSpin = self._make_double_spin(0.1, 9999.0)
        self.dacAdjustIntervalSpin  = self._make_double_spin(0.1, 99999.0)
        self.fineStepDacSpin        = self._make_int_spin(1, 1000)
        dac_form.addRow("Dynamic Step Max",       self.extraStepMaxSpin)
        dac_form.addRow("Seg1 Max DAC",           self.rampSeg1MaxDacSpin)
        dac_form.addRow("Seg1 Interval (s)",      self.rampSeg1IntervalSpin)
        dac_form.addRow("Seg2 Max DAC",           self.rampSeg2MaxDacSpin)
        dac_form.addRow("Seg2 Interval (s)",      self.rampSeg2IntervalSpin)
        dac_form.addRow("After Seg2 Interval (s)",self.rampAfterSeg2IntervalSpin)
        dac_form.addRow("DAC Adjust Interval (s)",self.dacAdjustIntervalSpin)
        dac_form.addRow("Fine Step DAC",          self.fineStepDacSpin)
        adv_root.addWidget(dac_box)

        pre_box = QGroupBox("Pre Rate")
        pre_form = QFormLayout(pre_box)
        self.preRateSpin = self._make_double_spin(0.0, 999.0, decimals=2)
        pre_form.addRow("Pre Rate (Å/s)", self.preRateSpin)
        adv_root.addWidget(pre_box)

        shortage_box = QGroupBox("Material Shortage Guard")
        shortage_form = QFormLayout(shortage_box)
        self.shortageDacSpin      = self._make_int_spin(0, 9999, step=10)
        self.shortageRateMaxSpin  = self._make_double_spin(0.0, 999.0, decimals=2)
        self.shortageTimeSpin     = self._make_double_spin(0.0, 99999.0)
        shortage_form.addRow("Shortage DAC",          self.shortageDacSpin)
        shortage_form.addRow("Shortage Rate Max (Å/s)",self.shortageRateMaxSpin)
        shortage_form.addRow("Shortage Time (s)",     self.shortageTimeSpin)
        adv_root.addWidget(shortage_box)

        rate_box = QGroupBox("Rate Filter / Judge")
        rate_form = QFormLayout(rate_box)
        self.rateFilterWindowSpin = self._make_int_spin(1, 21, step=2)
        self.rateStableSecSpin    = self._make_double_spin(0.0, 60.0)
        self.rateDropRatioSpin    = self._make_double_spin(0.01, 1.0, decimals=2)
        self.rateDropCountSpin    = self._make_int_spin(1, 20)
        rate_form.addRow("Rate Filter Window", self.rateFilterWindowSpin)
        rate_form.addRow("Rate Stable Sec",    self.rateStableSecSpin)
        rate_form.addRow("Rate Drop Ratio",    self.rateDropRatioSpin)
        rate_form.addRow("Rate Drop Count",    self.rateDropCountSpin)
        adv_root.addWidget(rate_box)
        adv_root.addStretch(1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal, self,
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    # =========================================================
    # step 행 위젯 생성
    # =========================================================
    def _build_step_row(self, row: int) -> None:
        """한 행 전체 위젯 생성 + combo changeEvent 연결."""
        # 활성화 체크박스 — 컨테이너 없이 직접 배치 (컨테이너가 클릭 이벤트를 가로채는 버그 방지)
        chk = QCheckBox(self)
        chk.setStyleSheet("QCheckBox { margin-left: 18px; }")
        chk.stateChanged.connect(lambda _s, r=row: self._on_enabled_changed(r))
        self.stepTable.setCellWidget(row, _COL_ENABLED, chk)

        self.stepTable.setCellWidget(row, _COL_TARGET_ADC, self._make_double_spin(0.0, 9999.0, step=10.0))
        self.stepTable.setCellWidget(row, _COL_DAC_STEP,   self._make_int_spin(1, 9999, step=5))
        self.stepTable.setCellWidget(row, _COL_INTERVAL,   self._make_double_spin(0.1, 9999.0))
        self.stepTable.setCellWidget(row, _COL_WAIT,       self._make_double_spin(0.0, 9999.0))
        self.stepTable.setCellWidget(row, _COL_MIN_RATE,   self._make_double_spin(0.0, 999.0, decimals=2))

        combo = QComboBox(self)
        for key, label in _ACTIONS:
            combo.addItem(label, key)
        combo.currentIndexChanged.connect(lambda _idx, r=row: self._on_action_changed(r))
        self.stepTable.setCellWidget(row, _COL_ACTION, combo)

        self.stepTable.setCellWidget(row, _COL_BOOST_DAC, self._make_int_spin(0, 9999, step=5))
        self.stepTable.setCellWidget(row, _COL_BOOST_MAX, self._make_int_spin(0, 100))

        # 초기 상태 동기화
        self._on_action_changed(row)

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
        w = QDoubleSpinBox()
        w.setDecimals(decimals)
        w.setRange(min_val, max_val)
        w.setSingleStep(step)
        w.setAlignment(Qt.AlignmentFlag.AlignRight)
        w.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        return w

    @staticmethod
    def _make_int_spin(min_val: int = 0, max_val: int = 9999, *, step: int = 1) -> QSpinBox:
        w = QSpinBox()
        w.setRange(min_val, max_val)
        w.setSingleStep(step)
        w.setAlignment(Qt.AlignmentFlag.AlignRight)
        return w

    # =========================================================
    # step 행 접근자
    # =========================================================
    def _row_enabled_chk(self, row: int) -> QCheckBox:
        w = self.stepTable.cellWidget(row, _COL_ENABLED)
        assert isinstance(w, QCheckBox), f"row={row} _COL_ENABLED is not QCheckBox"
        return w

    def _row_spin(self, row: int, col: int) -> QDoubleSpinBox:
        w = self.stepTable.cellWidget(row, col)
        assert isinstance(w, QDoubleSpinBox), f"row={row} col={col} is not QDoubleSpinBox"
        return w

    def _row_int_spin(self, row: int, col: int) -> QSpinBox:
        w = self.stepTable.cellWidget(row, col)
        assert isinstance(w, QSpinBox), f"row={row} col={col} is not QSpinBox"
        return w

    def _row_combo(self, row: int) -> QComboBox:
        w = self.stepTable.cellWidget(row, _COL_ACTION)
        assert isinstance(w, QComboBox)
        return w

    # =========================================================
    # 이벤트 핸들러
    # =========================================================
    def _on_enabled_changed(self, row: int) -> None:
        """활성화 체크박스 변경 시 해당 행 위젯 전체 활성/비활성 처리."""
        enabled = self._row_enabled_chk(row).isChecked()
        for col in (_COL_TARGET_ADC, _COL_DAC_STEP, _COL_INTERVAL,
                    _COL_WAIT, _COL_MIN_RATE, _COL_ACTION,
                    _COL_BOOST_DAC, _COL_BOOST_MAX):
            w = self.stepTable.cellWidget(row, col)
            if w is not None:
                w.setEnabled(enabled)
        if enabled:
            # Boost 컬럼은 action 선택에 따라 다시 판정
            self._on_action_changed(row)
        self._refresh_row_bg(row)

    def _on_action_changed(self, row: int) -> None:
        """Low rate action 변경 시 Boost 컬럼 활성/비활성 + 배경색 동기화."""
        combo = self._row_combo(row)
        action = str(combo.currentData() or "next_step")
        boost_enabled = (action == "boost_dac")
        self.stepTable.cellWidget(row, _COL_BOOST_DAC).setEnabled(boost_enabled)
        self.stepTable.cellWidget(row, _COL_BOOST_MAX).setEnabled(boost_enabled)
        self._refresh_row_bg(row)

    def _update_extra_ramp_enabled(self) -> None:
        policy = self.afterLastStepPolicyCombo.currentData()
        extra_enabled = bool(self.extraRampEnabledCheck.isChecked()) and (policy == "extra_ramp")
        self.extraMaxAdcSpin.setEnabled(extra_enabled)
        self.extraIntervalSpin.setEnabled(extra_enabled)
        self.extraRampEnabledCheck.setEnabled(policy == "extra_ramp")

    # =========================================================
    # load / apply
    # =========================================================
    def _load_config(self, cfg: dict[str, Any]) -> None:
        cfg = self._normalize_config(cfg)

        steps = cfg.get("ramp_steps") or []

        for row in range(self.MAX_STEPS):
            if row < len(steps):
                s = steps[row]
                self._row_enabled_chk(row).setChecked(True)
                self._row_spin(row, _COL_TARGET_ADC).setValue(float(s.get("target_adc", 100.0)))
                self._row_int_spin(row, _COL_DAC_STEP).setValue(int(s.get("dac_step", 10)))
                self._row_spin(row, _COL_INTERVAL).setValue(float(s.get("dac_interval_sec", 30.0)))
                self._row_spin(row, _COL_WAIT).setValue(float(s.get("rate_wait_sec", s.get("delay_s", 0.0))))
                self._row_spin(row, _COL_MIN_RATE).setValue(float(s.get("min_dep_rate", 0.1)))
                action = str(s.get("rate_low_action", "next_step"))
                combo = self._row_combo(row)
                idx = combo.findData(action)
                combo.setCurrentIndex(max(0, idx))
                self._row_int_spin(row, _COL_BOOST_DAC).setValue(int(s.get("boost_dac_step", 0)))
                self._row_int_spin(row, _COL_BOOST_MAX).setValue(int(s.get("boost_max_count", 0)))
            else:
                self._row_enabled_chk(row).setChecked(False)
                self._row_spin(row, _COL_TARGET_ADC).setValue(0.0)
                self._row_int_spin(row, _COL_DAC_STEP).setValue(10)
                self._row_spin(row, _COL_INTERVAL).setValue(30.0)
                self._row_spin(row, _COL_WAIT).setValue(0.0)
                self._row_spin(row, _COL_MIN_RATE).setValue(0.1)
                self._row_combo(row).setCurrentIndex(0)
                self._row_int_spin(row, _COL_BOOST_DAC).setValue(0)
                self._row_int_spin(row, _COL_BOOST_MAX).setValue(0)

            self._on_action_changed(row)
            self._refresh_row_bg(row)

        self.reachMainOnRateCheck.setChecked(bool(cfg.get("reach_main_on_rate", True)))

        policy = str(cfg.get("after_last_step_policy", "extra_ramp"))
        idx = self.afterLastStepPolicyCombo.findData(policy)
        self.afterLastStepPolicyCombo.setCurrentIndex(max(0, idx))

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

        self._update_extra_ramp_enabled()

    # =========================================================
    # 하이라이트 API
    # =========================================================
    def highlight_step(self, step_idx: Optional[int]) -> None:
        """
        현재 실행 중인 step 행을 하이라이트 처리.
        step_idx: 0-based 인덱스, None이면 모두 해제.
        외부(공정 컨트롤러)에서 step 변경 시 호출.
        """
        self._active_step = step_idx
        for row in range(self.MAX_STEPS):
            self._refresh_row_bg(row)

    def _refresh_row_bg(self, row: int) -> None:
        """행 배경색 동기화 — 우선순위:
        1. is_active  → 전체 노란색(#FFE664)
        2. not enabled → 전체 회색(#E6E6E6)
        3. Boost 컬럼이고 action != boost_dac → 회색(#E6E6E6)
        4. 나머지 → 기본("")
        """
        is_active  = (self._active_step == row)
        is_enabled = self._row_enabled_chk(row).isChecked()
        action     = str(self._row_combo(row).currentData() or "next_step")
        boost_grey = (action != "boost_dac")

        for col in range(len(_COL_HEADERS)):
            w = self.stepTable.cellWidget(row, col)
            if w is None:
                continue

            if is_active:
                color = "#FFE664"
            elif not is_enabled:
                color = "#E6E6E6"
            elif col in (_COL_BOOST_DAC, _COL_BOOST_MAX) and boost_grey:
                color = "#E6E6E6"
            else:
                color = ""

            if col == _COL_ENABLED:
                # 체크박스는 margin-left 스타일을 함께 유지
                if color:
                    w.setStyleSheet(f"QCheckBox {{ margin-left: 18px; background-color: {color}; }}")
                else:
                    w.setStyleSheet("QCheckBox { margin-left: 18px; }")
            else:
                w.setStyleSheet(f"background-color: {color};" if color else "")

    # =========================================================
    # step 수집
    # =========================================================
    def _collect_steps(self) -> Optional[list[dict[str, Any]]]:
        """테이블에서 활성화된 step만 수집. 오류 시 None 반환."""
        steps: list[dict[str, Any]] = []
        last_adc = -1.0

        for row in range(self.MAX_STEPS):
            if not self._row_enabled_chk(row).isChecked():
                continue

            target_adc = float(self._row_spin(row, _COL_TARGET_ADC).value())
            if target_adc <= 0:
                QMessageBox.warning(self, "입력 오류", f"Step {row+1}: Target ADC는 0보다 커야 합니다.")
                return None
            if last_adc >= 0 and target_adc < last_adc:
                QMessageBox.warning(
                    self, "입력 오류",
                    f"Step {row+1}: Target ADC({target_adc})가 이전 step({last_adc})보다 작습니다.\n"
                    "오름차순으로 입력하세요."
                )
                return None

            action = str(self._row_combo(row).currentData() or "next_step")
            steps.append({
                "target_adc":       target_adc,
                "dac_step":         int(self._row_int_spin(row, _COL_DAC_STEP).value()),
                "dac_interval_sec": float(self._row_spin(row, _COL_INTERVAL).value()),
                "rate_wait_sec":    float(self._row_spin(row, _COL_WAIT).value()),
                "delay_s":          float(self._row_spin(row, _COL_WAIT).value()),  # 레거시 호환
                "min_dep_rate":     float(self._row_spin(row, _COL_MIN_RATE).value()),
                "rate_low_action":  action,
                "boost_dac_step":   int(self._row_int_spin(row, _COL_BOOST_DAC).value()),
                "boost_max_count":  int(self._row_int_spin(row, _COL_BOOST_MAX).value()),
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

        policy = str(self.afterLastStepPolicyCombo.currentData() or "extra_ramp")
        extra_enabled = bool(self.extraRampEnabledCheck.isChecked()) if policy == "extra_ramp" else False

        cfg: dict[str, Any] = {
            "step_count":  len(steps),
            "ramp_steps":  steps,
            "reach_main_on_rate": bool(self.reachMainOnRateCheck.isChecked()),
            "after_last_step_policy": policy,
            "extra_ramp": {
                "enabled":    extra_enabled,
                "max_adc":    float(self.extraMaxAdcSpin.value()),
                "step_max":   float(self.extraStepMaxSpin.value()),
                "interval_s": float(self.extraIntervalSpin.value()),
            },
            "ramp_seg1_max_dac":            int(self.rampSeg1MaxDacSpin.value()),
            "ramp_interval_seg1_s":         float(self.rampSeg1IntervalSpin.value()),
            "ramp_seg2_max_dac":            int(self.rampSeg2MaxDacSpin.value()),
            "ramp_interval_seg2_s":         float(self.rampSeg2IntervalSpin.value()),
            "ramp_interval_after_seg2_s":   float(self.rampAfterSeg2IntervalSpin.value()),
            "pre_rate":                     float(self.preRateSpin.value()),
            "dac_adjust_interval_s":        float(self.dacAdjustIntervalSpin.value()),
            "fine_step_dac":                int(self.fineStepDacSpin.value()),
            "material_shortage_dac":        int(self.shortageDacSpin.value()),
            "material_shortage_rate_max":   float(self.shortageRateMaxSpin.value()),
            "material_shortage_time_s":     float(self.shortageTimeSpin.value()),
            "rate_filter_window":           int(self.rateFilterWindowSpin.value()),
            "rate_stable_sec":              float(self.rateStableSecSpin.value()),
            "rate_drop_ratio":              float(self.rateDropRatioSpin.value()),
            "rate_drop_count":              int(self.rateDropCountSpin.value()),
        }
        return self._normalize_config(cfg)

    # =========================================================
    # validation + accept
    # =========================================================
    def accept(self) -> None:
        cfg  = self.get_config()
        steps = cfg.get("ramp_steps") or []

        if not steps:
            QMessageBox.warning(self, "Process Config", "활성화된 step이 최소 1개 이상이어야 합니다.")
            return

        if cfg["ramp_seg2_max_dac"] < cfg["ramp_seg1_max_dac"]:
            QMessageBox.warning(self, "Process Config", "Seg2 Max DAC는 Seg1 Max DAC 이상이어야 합니다.")
            return

        last_adc = -1.0
        for idx, s in enumerate(steps, start=1):
            adc = float(s.get("target_adc", 0.0))
            if adc <= 0:
                QMessageBox.warning(self, "Process Config", f"Step {idx} Target ADC는 0보다 커야 합니다.")
                return
            if last_adc >= 0 and adc < last_adc:
                QMessageBox.warning(self, "Process Config",
                    f"Step {idx} Target ADC가 이전 step보다 작습니다. 오름차순으로 입력하세요.")
                return
            last_adc = adc

        if cfg["after_last_step_policy"] == "extra_ramp":
            extra = cfg.get("extra_ramp") or {}
            if not extra.get("enabled", False):
                QMessageBox.warning(self, "Process Config",
                    "After Last Step = extra_ramp 이지만 Extra Ramp가 비활성화되어 있습니다.")
                return
            if float(extra.get("max_adc", 0.0)) <= 0:
                QMessageBox.warning(self, "Process Config", "Extra Ramp Max ADC는 0보다 커야 합니다.")
                return

        super().accept()
