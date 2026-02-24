# ui/rt_plot_widget.py
# -*- coding: utf-8 -*-
"""
DepositionPlotWidget
- 단일 그래프
  - X: time(s)
  - Y(left): Dep.rate (A/s)
  - Y(right): Power (DAC)  # 고정 범위(기본 0~4000)
- main.py에서는 append(rate=..., power=...)만 호출
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QBrush, QColor, QPen
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
        # Power 축은 DAC 고정 범위로 쓰므로 power_buf 불필요 -> 삭제

        from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis  # type: ignore

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._rate_series = QLineSeries()
        self._power_series = QLineSeries()

        self._rate_series.setName("Dep. Rate (Å/s)")
        self._power_series.setName(self._power_title)

        self._chart = QChart()

        # ✅ 아래 legend 박스가 거슬리면 숨김(축 라벨 색으로 구분)
        self._chart.legend().hide()
        # 만약 legend도 같이 쓰고 싶으면 hide() 대신:
        # self._chart.legend().setVisible(True)
        # self._chart.legend().setAlignment(Qt.AlignBottom)

        self._chart.setTitle("Dep. Rate / Power")
        self._chart.setAnimationOptions(QChart.NoAnimation)
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
        self._view.setRenderHint(QPainter.Antialiasing, True)
        lay.addWidget(self._view, 1)

        # view 생성 직전 또는 _reset_axes() 직전에 호출
        self._apply_tick_density(segments=10)  # ✅ 10칸(=tick 11개) 정도
        self._sync_axis_label_colors()         # ✅ 축 라벨을 선 색으로

        self._reset_axes()

    def clear(self) -> None:
        self._t0 = None
        self._rate_buf.clear()
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
            if r < 0:
                r = 0.0

            self._rate_buf.append((t, r))
            self._rate_series.append(t, r)
            if self._rate_series.count() > self._max_points:
                self._rate_series.removePoints(0, self._rate_series.count() - self._max_points)

        if power is not None:
            p = float(power)
            if p < 0:
                p = 0.0

            self._power_series.append(t, p)
            if self._power_series.count() > self._max_points:
                self._power_series.removePoints(0, self._power_series.count() - self._max_points)

        self._update_axes(t)

    def _reset_axes(self) -> None:
        self._ax_x.setRange(0.0, max(10.0, self._window_s))
        self._ax_rate.setRange(0.0, 1.0)
        # Power 축은 DAC 고정 범위
        self._ax_power.setRange(self._p_def_min, self._p_def_max)

    def _update_axes(self, t_now: float) -> None:
        x1 = max(0.0, t_now - self._window_s)
        x2 = max(x1 + 1.0, t_now)
        self._ax_x.setRange(x1, x2)

        if self._rate_buf:
            ys = [v for _, v in self._rate_buf]
            y_max = max(ys)

            # ✅ Dep.rate는 보통 0 이상이므로 0부터 시작
            # ✅ 최소 상한 1.0은 유지하되, 1 넘으면 자동 확장
            y_upper = max(1.0, y_max * 1.10)

            self._ax_rate.setRange(0.0, y_upper)

            # 범위에 따라 라벨 포맷 자동 조정(눈금 11개면 너무 많은 소수는 지저분해짐)
            try:
                if y_upper < 2.0:
                    self._ax_rate.setLabelFormat("%.3f")
                elif y_upper < 10.0:
                    self._ax_rate.setLabelFormat("%.2f")
                else:
                    self._ax_rate.setLabelFormat("%.1f")
            except Exception:
                pass

    def _apply_tick_density(self, *, segments: int = 10) -> None:
        """축 눈금(칸) 수를 늘림. segments=10이면 tickCount=11."""
        ticks = max(2, int(segments) + 1)
        try:
            self._ax_x.setTickCount(ticks)
        except Exception:
            pass
        try:
            self._ax_rate.setTickCount(ticks)
        except Exception:
            pass
        try:
            self._ax_power.setTickCount(ticks)
        except Exception:
            pass

        # minor tick은 과하면 복잡해져서 기본 0 권장 (원하면 1~2로 늘리면 더 촘촘해짐)
        for ax in (self._ax_x, self._ax_rate, self._ax_power):
            try:
                ax.setMinorTickCount(0)
            except Exception:
                pass

    def _sync_axis_label_colors(self) -> None:
        """선 색상에 맞춰 축 제목(라벨) 색을 칠해서 legend 없이도 구분되게."""
        rate_color = self._rate_series.pen().color()
        power_color = self._power_series.pen().color()

        # 혹시 둘 다 기본 검정으로 잡히는 환경이면(테마 영향) 안전하게 지정
        if rate_color == power_color:
            rate_color = QColor("#1f77b4")  # 파란 계열
            power_color = QColor("#2ca02c") # 초록 계열
            try:
                self._rate_series.setPen(QPen(rate_color, 2))
            except Exception:
                pass
            try:
                self._power_series.setPen(QPen(power_color, 2))
            except Exception:
                pass

        # ✅ 축 제목만 색칠(눈금 숫자까지 색칠하면 가독성이 떨어질 수 있어서)
        try:
            self._ax_rate.setTitleBrush(QBrush(rate_color))
        except Exception:
            pass
        try:
            self._ax_power.setTitleBrush(QBrush(power_color))
        except Exception:
            pass

        # 축 라인까지 같이 칠하고 싶으면(더 직관적):
        try:
            self._ax_rate.setLinePen(QPen(rate_color))
        except Exception:
            pass
        try:
            self._ax_power.setLinePen(QPen(power_color))
        except Exception:
            pass
