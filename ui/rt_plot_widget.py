# ui/rt_plot_widget.py
# -*- coding: utf-8 -*-
"""
DepositionPlotWidget
- 단일 그래프
  - X: time(s)
  - Y(left): Dep.rate (A/s)
  - Y(right): Power (DAC)
- main.py에서는 append(rate=..., power=...)만 호출
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout


class DepositionPlotWidget(QWidget):
    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        max_points: int = 600,
        window_seconds: float = 300.0,
        power_title: str = "Power (DAC)",
        power_default_range: tuple[float, float] = (0.0, 4000.0),
    ):
        super().__init__(parent)
        self._max_points = int(max_points)
        self._window_s = float(window_seconds)
        self._power_title = str(power_title)
        self._p_def_min, self._p_def_max = float(power_default_range[0]), float(power_default_range[1])

        self._t0: Optional[float] = None
        self._rate_buf: Deque[Tuple[float, float]] = deque(maxlen=self._max_points)
        self._power_buf: Deque[Tuple[float, float]] = deque(maxlen=self._max_points)

        from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis  # type: ignore

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._rate_series = QLineSeries()
        self._power_series = QLineSeries()

        self._chart = QChart()
        self._chart.legend().hide()
        self._chart.setTitle("Dep. Rate / Power")
        self._chart.addSeries(self._rate_series)
        self._chart.addSeries(self._power_series)

        # X axis (time)
        self._ax_x = QValueAxis()
        self._ax_x.setLabelFormat("%.0f")
        self._chart.addAxis(self._ax_x, Qt.AlignBottom)

        # Left Y axis (rate)
        self._ax_rate = QValueAxis()
        self._ax_rate.setLabelFormat("%.3f")
        self._ax_rate.setTitleText("Dep. Rate (A/s)")
        self._chart.addAxis(self._ax_rate, Qt.AlignLeft)

        # Right Y axis (power)
        self._ax_power = QValueAxis()
        self._ax_power.setLabelFormat("%.0f")
        self._ax_power.setTitleText(self._power_title)
        self._chart.addAxis(self._ax_power, Qt.AlignRight)

        # Attach axes
        self._rate_series.attachAxis(self._ax_x)
        self._rate_series.attachAxis(self._ax_rate)

        self._power_series.attachAxis(self._ax_x)
        self._power_series.attachAxis(self._ax_power)

        self._view = QChartView(self._chart)
        lay.addWidget(self._view, 1)

        self._reset_axes()

    def clear(self) -> None:
        self._t0 = None
        self._rate_buf.clear()
        self._power_buf.clear()
        self._rate_series.clear()
        self._power_series.clear()
        self._reset_axes()

    def append(self, *, rate: Optional[float], power: Optional[float]) -> None:
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

        if power is not None:
            p = float(power)
            self._power_buf.append((t, p))
            self._power_series.append(t, p)
            if self._power_series.count() > self._max_points:
                self._power_series.removePoints(0, self._power_series.count() - self._max_points)

        self._update_axes(t)

    def _reset_axes(self) -> None:
        self._ax_x.setRange(0.0, max(10.0, self._window_s))
        self._ax_rate.setRange(0.0, 1.0)
        self._ax_power.setRange(self._p_def_min, self._p_def_max)

    def _update_axes(self, t_now: float) -> None:
        x1 = max(0.0, t_now - self._window_s)
        x2 = max(x1 + 1.0, t_now)
        self._ax_x.setRange(x1, x2)

        if self._rate_buf:
            ys = [v for _, v in self._rate_buf]
            y_min, y_max = min(ys), max(ys)
            pad = max(0.01, (y_max - y_min) * 0.1)
            self._ax_rate.setRange(y_min - pad, y_max + pad)

        if self._power_buf:
            ys = [v for _, v in self._power_buf]
            y_min, y_max = min(ys), max(ys)
            pad = max(1.0, (y_max - y_min) * 0.1)
            self._ax_power.setRange(y_min - pad, y_max + pad)
