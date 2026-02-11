# ui/rt_plot_widget.py
# -*- coding: utf-8 -*-
"""
DepositionPlotWidget
- Dep.rate(A/s), Thickness(A) 실시간 플롯 위젯
- main.py에서는 append(rate, thickness)만 호출
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout


class DepositionPlotWidget(QWidget):
    """Dep.rate/thickness 실시간 플롯(상: rate, 하: thickness)."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        max_points: int = 600,         # 1초 샘플 기준 10분
        window_seconds: float = 300.0  # x축 표시 윈도우(최근 N초)
    ):
        super().__init__(parent)
        self._max_points = int(max_points)
        self._window_s = float(window_seconds)

        self._t0: Optional[float] = None
        self._rate_buf: Deque[Tuple[float, float]] = deque(maxlen=self._max_points)
        self._th_buf: Deque[Tuple[float, float]] = deque(maxlen=self._max_points)

        # QtCharts (optional)
        from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis  # type: ignore

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        # --- rate chart ---
        self._rate_series = QLineSeries()
        self._rate_chart = QChart()
        self._rate_chart.legend().hide()
        self._rate_chart.addSeries(self._rate_series)
        self._rate_chart.setTitle("Dep. Rate (A/s)")
        self._rate_ax_x = QValueAxis()
        self._rate_ax_y = QValueAxis()
        self._rate_ax_x.setLabelFormat("%.0f")
        self._rate_ax_y.setLabelFormat("%.3f")
        self._rate_chart.addAxis(self._rate_ax_x, Qt.AlignBottom)
        self._rate_chart.addAxis(self._rate_ax_y, Qt.AlignLeft)
        self._rate_series.attachAxis(self._rate_ax_x)
        self._rate_series.attachAxis(self._rate_ax_y)
        self._rate_view = QChartView(self._rate_chart)
        lay.addWidget(self._rate_view, 1)

        # --- thickness chart ---
        self._th_series = QLineSeries()
        self._th_chart = QChart()
        self._th_chart.legend().hide()
        self._th_chart.addSeries(self._th_series)
        self._th_chart.setTitle("Thickness (A)")
        self._th_ax_x = QValueAxis()
        self._th_ax_y = QValueAxis()
        self._th_ax_x.setLabelFormat("%.0f")
        self._th_ax_y.setLabelFormat("%.1f")
        self._th_chart.addAxis(self._th_ax_x, Qt.AlignBottom)
        self._th_chart.addAxis(self._th_ax_y, Qt.AlignLeft)
        self._th_series.attachAxis(self._th_ax_x)
        self._th_series.attachAxis(self._th_ax_y)
        self._th_view = QChartView(self._th_chart)
        lay.addWidget(self._th_view, 1)

        self._reset_axes()

    def clear(self) -> None:
        self._t0 = None
        self._rate_buf.clear()
        self._th_buf.clear()
        self._rate_series.clear()
        self._th_series.clear()
        self._reset_axes()

    def append(self, *, rate: Optional[float], thickness: Optional[float]) -> None:
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        t = now - self._t0

        if rate is not None:
            r = float(rate)
            self._rate_buf.append((t, r))
            self._rate_series.append(t, r)
            if self._rate_series.count() > self._max_points:
                self._rate_series.removePoints(0, self._rate_series.count() - self._max_points)

        if thickness is not None:
            th = float(thickness)
            self._th_buf.append((t, th))
            self._th_series.append(t, th)
            if self._th_series.count() > self._max_points:
                self._th_series.removePoints(0, self._th_series.count() - self._max_points)

        self._update_axes(t)

    def _reset_axes(self) -> None:
        self._rate_ax_x.setRange(0.0, max(10.0, self._window_s))
        self._th_ax_x.setRange(0.0, max(10.0, self._window_s))
        self._rate_ax_y.setRange(0.0, 1.0)
        self._th_ax_y.setRange(0.0, 100.0)

    def _update_axes(self, t_now: float) -> None:
        x1 = max(0.0, t_now - self._window_s)
        x2 = max(x1 + 1.0, t_now)
        self._rate_ax_x.setRange(x1, x2)
        self._th_ax_x.setRange(x1, x2)

        if self._rate_buf:
            ys = [v for _, v in self._rate_buf]
            y_min, y_max = min(ys), max(ys)
            pad = max(0.01, (y_max - y_min) * 0.1)
            self._rate_ax_y.setRange(y_min - pad, y_max + pad)

        if self._th_buf:
            ys = [v for _, v in self._th_buf]
            y_min, y_max = min(ys), max(ys)
            pad = max(0.1, (y_max - y_min) * 0.1)
            self._th_ax_y.setRange(y_min - pad, y_max + pad)
