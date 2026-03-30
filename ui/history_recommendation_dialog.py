# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class HistoryRecommendationDialog(QDialog):
    def __init__(
        self,
        *,
        recommendation: Optional[dict[str, Any]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("이전 공정 설정 불러오기")
        self.setModal(True)
        self.resize(820, 540)
        self.setMinimumSize(750, 480)

        self._recommendation = dict(recommendation or {})
        self._accepted_apply = False
        self._representative_runs = list(self._recommendation.get("representative_runs") or [])

        confidence = float(self._recommendation.get("confidence", 0.0) or 0.0)
        basis_run_count = int(self._recommendation.get("basis_run_count", 0) or 0)
        recommended_fine_step_dac = int(self._recommendation.get("recommended_fine_step_dac", 0) or 0)
        recommended_rate_stable_sec = float(self._recommendation.get("recommended_rate_stable_sec", 0.0) or 0.0)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ① 상단 배너
        banner = QFrame(self)
        banner.setStyleSheet(
            "QFrame { background: #f0f4ff; border-radius: 6px; }"
        )
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(8, 8, 8, 8)

        if basis_run_count >= 1:
            _mat = str((self._recommendation.get("representative_runs") or [{}])[0].get("material_name", "") or "").strip()
            banner_text = f"{_mat} 재료의 성공 공정 {basis_run_count}건 중 가장 최근 설정을 불러왔습니다"
            banner_color = "#1a7a1a"
        else:
            banner_text = "추천 데이터가 없습니다"
            banner_color = "#888888"

        banner_label = QLabel(banner_text, banner)
        banner_label.setStyleSheet(
            f"color: {banner_color}; font-weight: bold; background: transparent; border: none;"
        )
        banner_layout.addWidget(banner_label)
        root.addWidget(banner)

        # ② 중앙 QSplitter (수평)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # 왼쪽 패널
        left_widget = QWidget(splitter)
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 4, 0)
        left_layout.setSpacing(4)

        runs_title = QLabel("참고한 공정 목록", left_widget)
        runs_title.setStyleSheet("font-weight: bold;")
        left_layout.addWidget(runs_title)

        self.runsTable = QTableWidget(left_widget)
        self.runsTable.setColumnCount(4)
        self.runsTable.setHorizontalHeaderLabels(["Time", "Dep.Rate(Å/s)", "Thickness(Å)", "Stable DAC"])
        self.runsTable.verticalHeader().setVisible(False)
        self.runsTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.runsTable.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.runsTable.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.runsTable.setWordWrap(False)
        self.runsTable.setAlternatingRowColors(True)
        self.runsTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.runsTable.horizontalHeader().setStretchLastSection(True)
        self.runsTable.itemSelectionChanged.connect(self._on_run_selected)
        self.runsTable.setStyleSheet("""
            QTableWidget::item:selected {
                background-color: #ddeeff;
                color: #111111;
            }
            QTableWidget::item:hover {
                background-color: #eef4ff;
            }
        """)
        left_layout.addWidget(self.runsTable)

        splitter.addWidget(left_widget)

        # 오른쪽 패널
        right_widget = QWidget(splitter)
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(4, 0, 0, 0)
        right_layout.setSpacing(4)

        steps_title = QLabel("적용될 Ramp Steps", right_widget)
        steps_title.setStyleSheet("font-weight: bold;")
        right_layout.addWidget(steps_title)

        steps_sub = QLabel("아래 설정이 Recipe에 적용됩니다", right_widget)
        steps_sub.setStyleSheet("font-size: 10px; color: #666;")
        right_layout.addWidget(steps_sub)

        self.stepsTable = QTableWidget(right_widget)
        self.stepsTable.setColumnCount(5)
        self.stepsTable.setHorizontalHeaderLabels(["Step", "Target ADC", "DAC Step", "Interval(s)", "Hold(s)"])
        self.stepsTable.verticalHeader().setVisible(False)
        self.stepsTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stepsTable.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.stepsTable.setWordWrap(False)
        self.stepsTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        right_layout.addWidget(self.stepsTable)

        divider = QFrame(right_widget)
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        right_layout.addWidget(divider)

        summary_label = QLabel(
            f"Fine Step DAC: {recommended_fine_step_dac}   |   Rate Stable: {recommended_rate_stable_sec:.1f}s",
            right_widget,
        )
        summary_label.setStyleSheet("font-size: 10px; color: #666;")
        right_layout.addWidget(summary_label)

        splitter.addWidget(right_widget)
        splitter.setSizes([350, 430])

        root.addWidget(splitter, 1)

        # ③ 하단 버튼 영역
        bottom_layout = QHBoxLayout()
        self.applyConfigCheck = QCheckBox("Config 파라미터도 함께 적용 (권장)", self)
        self.applyConfigCheck.setChecked(True)
        self.applyConfigCheck.setToolTip(
            "fine_step_dac, rate_stable_sec 등 세부 파라미터도 함께 업데이트합니다.\n"
            "체크 해제 시 Ramp Steps 형태만 적용됩니다."
        )
        bottom_layout.addWidget(self.applyConfigCheck)
        bottom_layout.addStretch(1)

        apply_btn = QPushButton("이 설정 사용하기", self)
        apply_btn.clicked.connect(self._on_apply_clicked)
        cancel_btn = QPushButton("취소", self)
        cancel_btn.clicked.connect(self.reject)
        bottom_layout.addWidget(apply_btn)
        bottom_layout.addWidget(cancel_btn)
        root.addLayout(bottom_layout)

        # 테이블 데이터 채우기
        self._populate_runs_table()
        self._populate_steps_table()

        # 첫 번째 행 자동 선택
        if self.runsTable.rowCount() > 0:
            self.runsTable.selectRow(0)

    def _set_table_item(self, table: QTableWidget, row: int, col: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        table.setItem(row, col, item)

    def _populate_runs_table(self) -> None:
        runs = self._representative_runs
        self.runsTable.setRowCount(len(runs))
        for row_idx, run in enumerate(runs):
            try:
                ts = float(run.get("timestamp", 0) or 0)
                time_text = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts > 0 else "---"
            except Exception:
                time_text = "---"
            try:
                rate_text = f"{float(run.get('target_rate', 0.0) or 0.0):.3f}"
            except Exception:
                rate_text = "---"
            try:
                thickness_text = f"{float(run.get('target_thickness', 0.0) or 0.0):.1f}"
            except Exception:
                thickness_text = "---"
            try:
                sdac = run.get('stable_dac_mean')
                stable_dac_text = f"{float(sdac):.1f}" if sdac is not None and float(sdac) > 0 else "---"
            except Exception:
                stable_dac_text = "---"
            self._set_table_item(self.runsTable, row_idx, 0, time_text)
            self._set_table_item(self.runsTable, row_idx, 1, rate_text)
            self._set_table_item(self.runsTable, row_idx, 2, thickness_text)
            self._set_table_item(self.runsTable, row_idx, 3, stable_dac_text)

    def _populate_steps_table(self) -> None:
        ramp_steps = list(self._recommendation.get("recommended_ramp_steps") or [])
        self.stepsTable.setRowCount(len(ramp_steps))
        for row_idx, step in enumerate(ramp_steps):
            self._set_table_item(self.stepsTable, row_idx, 0, str(row_idx + 1))
            try:
                target_adc_text = f"{float(step.get('target_adc', 0.0) or 0.0):.1f}"
            except Exception:
                target_adc_text = "---"
            try:
                dac_step_text = str(int(step.get("dac_step", 0) or 0))
            except Exception:
                dac_step_text = "---"
            try:
                interval_text = f"{float(step.get('dac_interval_sec', 0.0) or 0.0):.1f}"
            except Exception:
                interval_text = "---"
            try:
                hold_text = f"{float(step.get('hold_sec', 0.0) or 0.0):.1f}"
            except Exception:
                hold_text = "---"
            self._set_table_item(self.stepsTable, row_idx, 1, target_adc_text)
            self._set_table_item(self.stepsTable, row_idx, 2, dac_step_text)
            self._set_table_item(self.stepsTable, row_idx, 3, interval_text)
            self._set_table_item(self.stepsTable, row_idx, 4, hold_text)

    def _on_run_selected(self) -> None:
        pass

    def _on_apply_clicked(self) -> None:
        self._accepted_apply = True
        self.accept()

    def applied(self) -> bool:
        return bool(self._accepted_apply)

    def recommended_process_config(self) -> Optional[dict]:
        cfg = self._recommendation.get("recommended_process_config")
        if not isinstance(cfg, dict):
            return None
        result = dict(cfg)
        if not self.applyConfigCheck.isChecked():
            # Config 파라미터 미적용 시 ramp_steps 형태만 반환: ProcessRecipeDialog가 나머지 config를 현재 값으로 유지
            result = {
                "ramp_steps": result.get("ramp_steps"),
                "step_count": result.get("step_count"),
            }
        return result

