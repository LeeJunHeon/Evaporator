# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


def _float_text(value: Any, *, digits: int = 3, suffix: str = "") -> str:
    try:
        number = float(value)
    except Exception:
        return "---"
    return f"{number:.{digits}f}{suffix}"


def _timestamp_text(value: Any) -> str:
    try:
        ts = float(value)
        if ts <= 0:
            return "---"
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "---"


class HistoryRecommendationDialog(QDialog):
    def __init__(
        self,
        *,
        recommendation: Optional[dict[str, Any]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Recommendation")
        self.setModal(True)
        self.resize(760, 700)
        self.setMinimumSize(820, 720)

        self._recommendation = dict(recommendation or {})
        self._accepted_apply = False
        self._representative_runs = list(self._recommendation.get("representative_runs") or [])

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        if not self._recommendation:
            empty = QLabel("추천 데이터가 없습니다.")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            root.addWidget(empty, 1)

            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
            buttons.rejected.connect(self.reject)
            buttons.accepted.connect(self.accept)
            root.addWidget(buttons)
            return

        summary_form = QFormLayout()
        root.addLayout(summary_form)

        confidence = float(self._recommendation.get("confidence", 0.0) or 0.0)
        basis_run_count = int(self._recommendation.get("basis_run_count", 0) or 0)
        recommended_start_dac = int(self._recommendation.get("recommended_start_dac", 0) or 0)
        recommended_fine_step_dac = int(self._recommendation.get("recommended_fine_step_dac", 0) or 0)
        recommended_rate_stable_sec = float(self._recommendation.get("recommended_rate_stable_sec", 0.0) or 0.0)

        summary_form.addRow("Confidence", QLabel(f"{confidence * 100.0:.1f}%"))
        summary_form.addRow("Basis Runs", QLabel(str(basis_run_count)))
        summary_form.addRow("Recommended Start DAC", QLabel(str(recommended_start_dac)))
        summary_form.addRow("Recommended Fine Step DAC", QLabel(str(recommended_fine_step_dac)))
        summary_form.addRow("Recommended Rate Stable Sec", QLabel(f"{recommended_rate_stable_sec:.2f}"))

        basis_runs = QPlainTextEdit(self)
        basis_runs.setReadOnly(True)
        basis_runs.setMaximumHeight(70)
        basis_runs.setPlainText("\n".join(self._recommendation.get("representative_run_ids", []) or []))
        summary_form.addRow("Representative Runs", basis_runs)

        root.addWidget(QLabel("Recommended Ramp Steps"))

        self.stepsTable = QTableWidget(self)
        self.stepsTable.setColumnCount(4)
        self.stepsTable.setHorizontalHeaderLabels([
            "Target ADC",
            "DAC Step",
            "Interval (s)",
            "Hold (s)",
        ])
        self.stepsTable.verticalHeader().setVisible(False)
        self.stepsTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stepsTable.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.stepsTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.stepsTable.setWordWrap(False)
        self.stepsTable.setMinimumHeight(170)
        root.addWidget(self.stepsTable)

        ramp_steps = list(self._recommendation.get("recommended_ramp_steps") or [])
        self.stepsTable.setRowCount(len(ramp_steps))
        for row_idx, step in enumerate(ramp_steps):
            self._set_table_item(self.stepsTable, row_idx, 0, f"{float(step.get('target_adc', 0.0) or 0.0):.3f}")
            self._set_table_item(self.stepsTable, row_idx, 1, str(int(step.get("dac_step", 0) or 0)))
            self._set_table_item(self.stepsTable, row_idx, 2, f"{float(step.get('dac_interval_sec', 0.0) or 0.0):.3f}")
            self._set_table_item(self.stepsTable, row_idx, 3, f"{float(step.get('hold_sec', 0.0) or 0.0):.3f}")

        root.addWidget(QLabel("Representative Run Details"))

        self.runTable = QTableWidget(self)
        self.runTable.setColumnCount(8)
        self.runTable.setHorizontalHeaderLabels([
            "Run ID",
            "Time",
            "Rate",
            "Thickness",
            "Stable DAC",
            "Overshoot",
            "Spikes",
            "Error",
        ])
        self.runTable.verticalHeader().setVisible(False)
        self.runTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.runTable.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.runTable.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.runTable.setAlternatingRowColors(True)
        self.runTable.setWordWrap(False)
        self.runTable.setMinimumHeight(240)
        self.runTable.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.runTable.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(2, 8):
            self.runTable.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.runTable, 1)

        self.runDetail = QPlainTextEdit(self)
        self.runDetail.setReadOnly(True)
        self.runDetail.setMinimumHeight(150)
        root.addWidget(self.runDetail)

        self._populate_representative_runs()
        self.runTable.itemSelectionChanged.connect(self._on_run_selection_changed)
        if self.runTable.rowCount() > 0:
            self.runTable.selectRow(0)
            self._update_run_detail(0)
        else:
            self.runDetail.setPlainText("대표 run 정보가 없습니다.")

        buttons = QDialogButtonBox(parent=self)
        self.applyButton = buttons.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
        self.closeButton = buttons.addButton(QDialogButtonBox.StandardButton.Close)

        self.applyButton.clicked.connect(self._on_apply_clicked)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _set_table_item(self, table: QTableWidget, row: int, col: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        table.setItem(row, col, item)

    def _populate_representative_runs(self) -> None:
        runs = list(self._representative_runs)
        self.runTable.setRowCount(len(runs))
        for row_idx, run in enumerate(runs):
            self._set_table_item(self.runTable, row_idx, 0, str(run.get("run_id", "") or ""))
            self._set_table_item(self.runTable, row_idx, 1, _timestamp_text(run.get("timestamp")))
            self._set_table_item(self.runTable, row_idx, 2, _float_text(run.get("target_rate"), digits=3))
            self._set_table_item(self.runTable, row_idx, 3, _float_text(run.get("target_thickness"), digits=1))
            self._set_table_item(self.runTable, row_idx, 4, _float_text(run.get("stable_dac_mean"), digits=1))
            self._set_table_item(self.runTable, row_idx, 5, _float_text(run.get("overshoot_ratio_peak"), digits=3))
            self._set_table_item(self.runTable, row_idx, 6, str(run.get("spike_count", "---") if run.get("spike_count") is not None else "---"))
            self._set_table_item(
                self.runTable,
                row_idx,
                7,
                _float_text(run.get("thickness_error_A"), digits=1),
            )

    def _on_run_selection_changed(self) -> None:
        row = self.runTable.currentRow()
        self._update_run_detail(row)

    def _update_run_detail(self, row: int) -> None:
        if row < 0 or row >= len(self._representative_runs):
            self.runDetail.setPlainText("대표 run 정보가 없습니다.")
            return

        run = dict(self._representative_runs[row] or {})
        lines = [
            f"run_id: {run.get('run_id', '') or '---'}",
            f"time: {_timestamp_text(run.get('timestamp'))}",
            f"material: {run.get('material_name', '') or '---'}",
            f"result: {run.get('result_status', '') or '---'}",
            f"target_rate: {_float_text(run.get('target_rate'), digits=3)} A/s",
            f"target_thickness: {_float_text(run.get('target_thickness'), digits=1)} A",
            f"stable_rate_mean: {_float_text(run.get('stable_rate_mean'), digits=3)} A/s",
            f"stable_dac_mean: {_float_text(run.get('stable_dac_mean'), digits=1)}",
            f"overshoot_ratio_peak: {_float_text(run.get('overshoot_ratio_peak'), digits=3)}",
            f"spike_count: {run.get('spike_count', '---') if run.get('spike_count') is not None else '---'}",
            f"final_thickness_A: {_float_text(run.get('final_thickness_A'), digits=1)}",
            f"thickness_error_A: {_float_text(run.get('thickness_error_A'), digits=1)}",
            f"thickness_error_ratio: {_float_text(run.get('thickness_error_ratio'), digits=4)}",
            f"similarity_score: {_float_text(run.get('score'), digits=3)}",
        ]
        self.runDetail.setPlainText("\n".join(lines))

    def _on_apply_clicked(self) -> None:
        self._accepted_apply = True
        self.accept()

    def applied(self) -> bool:
        return bool(self._accepted_apply)

    def recommended_process_config(self) -> Optional[dict[str, Any]]:
        cfg = self._recommendation.get("recommended_process_config")
        if not isinstance(cfg, dict):
            return None
        return dict(cfg)

    def recommended_runtime_overrides(self) -> Optional[dict[str, Any]]:
        overrides = self._recommendation.get("recommended_runtime_overrides")
        if isinstance(overrides, dict):
            return dict(overrides)

        start_dac = self._recommendation.get("recommended_start_dac")
        try:
            initial_dac = int(round(float(start_dac)))
        except Exception:
            return None
        if initial_dac <= 0:
            return None
        return {
            "initial_dac": initial_dac,
            "initial_dac_source": "recommendation",
            "applied_recommended_start_dac": True,
        }
