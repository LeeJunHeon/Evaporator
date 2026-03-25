# -*- coding: utf-8 -*-
from __future__ import annotations

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
        self.resize(620, 520)

        self._recommendation = dict(recommendation or {})
        self._accepted_apply = False

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
        basis_runs.setMaximumHeight(80)
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
        root.addWidget(self.stepsTable, 1)

        ramp_steps = list(self._recommendation.get("recommended_ramp_steps") or [])
        self.stepsTable.setRowCount(len(ramp_steps))
        for row_idx, step in enumerate(ramp_steps):
            self._set_table_item(row_idx, 0, f"{float(step.get('target_adc', 0.0) or 0.0):.3f}")
            self._set_table_item(row_idx, 1, str(int(step.get("dac_step", 0) or 0)))
            self._set_table_item(row_idx, 2, f"{float(step.get('dac_interval_sec', 0.0) or 0.0):.3f}")
            self._set_table_item(row_idx, 3, f"{float(step.get('hold_sec', 0.0) or 0.0):.3f}")

        buttons = QDialogButtonBox(parent=self)
        self.applyButton = buttons.addButton("Apply", QDialogButtonBox.ButtonRole.AcceptRole)
        self.closeButton = buttons.addButton(QDialogButtonBox.StandardButton.Close)

        self.applyButton.clicked.connect(self._on_apply_clicked)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _set_table_item(self, row: int, col: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stepsTable.setItem(row, col, item)

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
