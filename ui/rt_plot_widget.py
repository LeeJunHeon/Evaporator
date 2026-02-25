# ui/rt_plot_widget.py
# -*- coding: utf-8 -*-
"""
DepositionPlotWidget
- 단일 그래프
  - X: time(s)
  - Y(left): Dep.rate (Å/s)
  - Y(right): Power (DAC)  # 고정 범위(기본 0~4000)
- main.py에서는 append(rate=..., power=...)만 호출
"""

from __future__ import annotations

import time
import math
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
        window_seconds: float = 150.0,
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

        # legend는 숨기고(축 라벨 색으로 구분)
        self._chart.legend().hide()

        self._chart.setTitle("Dep. Rate / Power")
        self._chart.setAnimationOptions(QChart.NoAnimation)
        self._chart.addSeries(self._rate_series)
        self._chart.addSeries(self._power_series)

        # X axis (time)
        self._ax_x = QValueAxis()
        self._ax_x.setLabelFormat("%.0f")
        self._ax_x.setTitleText("Time (s)")  # ✅ 하단 값 의미 표시(초)
        self._chart.addAxis(self._ax_x, Qt.AlignBottom)

        # Left Y axis (rate)
        self._ax_rate = QValueAxis()
        self._ax_rate.setLabelFormat("%.3f")
        self._ax_rate.setTitleText("Dep. Rate (Å/s)")
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

        # ✅ X축: 큰 눈금 5초, 작은 눈금 1초
        self._configure_x_axis_ticks(major_s=5.0, minor_s=1.0)

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
        x2 = max(10.0, self._window_s)

        major = float(getattr(self, "_x_major_s", 0.0) or 0.0)
        if major > 0:
            x2 = float(math.ceil(x2 / major) * major)

        self._ax_x.setRange(0.0, x2)
        self._apply_x_tickcount_for_range(0.0, x2)

        self._ax_rate.setRange(0.0, 1.0)
        self._ax_power.setRange(self._p_def_min, self._p_def_max)

    def _update_axes(self, t_now: float) -> None:
        major = float(getattr(self, "_x_major_s", 0.0) or 0.0)

        # ✅ Dep.rate Y축 스케일링을 "현재 보이는 X-window" 기준으로 하려면,
        #    아래에서 최종 x1/x2를 반드시 계산해 둬야 함.
        x1: float
        x2: float

        if major > 0:
            # ✅ 초반에는 0~window 고정(눈금/라벨 안정)
            if t_now < self._window_s:
                x1 = 0.0
                x2 = float(self._window_s)
            else:
                x2 = float(t_now)
                x1 = max(0.0, x2 - self._window_s)

            # ✅ x2는 항상 t_now를 포함하도록 "올림" 후 5초 경계로 맞춤
            x2 = float(math.ceil(x2 / major) * major)

            # ✅ x1도 5초 경계로 내림
            x1 = max(0.0, x2 - self._window_s)
            x1 = float(math.floor(x1 / major) * major)

            if x2 <= x1:
                x2 = x1 + major

            self._ax_x.setRange(x1, x2)
            self._apply_x_tickcount_for_range(x1, x2)
        else:
            x1 = max(0.0, t_now - self._window_s)
            x2 = max(x1 + 1.0, t_now)
            self._ax_x.setRange(x1, x2)

        # ---- Dep.rate Y축 자동 스케일: "현재 보이는 구간(x1~x2)"만 기준 ----
        if self._rate_buf:
            ys = [v for tt, v in self._rate_buf if x1 <= tt <= x2]
            if not ys:
                ys = [v for _, v in self._rate_buf]  # fallback
            y_max = max(ys)
            y_upper = max(1.0, y_max * 1.10)
            self._ax_rate.setRange(0.0, y_upper)
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

    def _configure_x_axis_ticks(self, *, major_s: float = 5.0, minor_s: float = 1.0) -> None:
        """X축 눈금: major=5s, minor=1s"""
        self._x_major_s = float(major_s)
        self._x_minor_s = float(minor_s)
        self._x_use_tick_interval = False

        # minor tick: 5초 사이에 1초 간격이면 4개
        minor_cnt = 0
        if self._x_minor_s > 0 and self._x_major_s > self._x_minor_s:
            div = int(round(self._x_major_s / self._x_minor_s))
            minor_cnt = max(0, div - 1)

        try:
            self._ax_x.setMinorTickCount(minor_cnt)
        except Exception:
            pass

        # minor grid line이 보이면 “작은 눈금(1초)” 느낌이 훨씬 확실
        try:
            if hasattr(self._ax_x, "setMinorGridLineVisible"):
                self._ax_x.setMinorGridLineVisible(True)
            if hasattr(self._ax_x, "setGridLineVisible"):
                self._ax_x.setGridLineVisible(True)
        except Exception:
            pass

        # 가능하면 tickInterval(5초)로 고정 (환경에 따라 없을 수 있어 fallback 준비)
        try:
            if hasattr(self._ax_x, "setTickType") and hasattr(self._ax_x, "setTickInterval"):
                TickType = getattr(type(self._ax_x), "TickType", None)
                ticks_fixed = getattr(TickType, "TicksFixed", None) if TickType else None
                if ticks_fixed is not None:
                    self._ax_x.setTickType(ticks_fixed)
                    self._ax_x.setTickInterval(self._x_major_s)
                    self._x_use_tick_interval = True
        except Exception:
            self._x_use_tick_interval = False

    def _apply_x_tickcount_for_range(self, x1: float, x2: float) -> None:
        """tickInterval을 못 쓰는 환경이면 tickCount로 5초 간격을 맞춤"""
        if bool(getattr(self, "_x_use_tick_interval", False)):
            return
        major = float(getattr(self, "_x_major_s", 0.0) or 0.0)
        if major <= 0:
            return
        span = max(0.0, float(x2) - float(x1))
        ticks = int(round(span / major)) + 1
        ticks = max(2, min(200, ticks))  # 과도 방지
        try:
            self._ax_x.setTickCount(ticks)
        except Exception:
            pass
