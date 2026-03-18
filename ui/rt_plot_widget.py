# ui/rt_plot_widget.py
# -*- coding: utf-8 -*-
"""
DepositionPlotWidget
- 단일 그래프
  - X: time(s)
  - Y(left): Dep.rate (Å/s)
  - Y(right): ADC
- main.py에서는 append(rate=..., power=...)만 호출
  ※ 여기서 power 인자는 DAC가 아니라 ADC 값으로 사용
"""

from __future__ import annotations

import time
import math
from collections import deque
from typing import Deque, Optional, Tuple, Callable

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPainter, QBrush, QColor, QPen
from PySide6.QtWidgets import QWidget, QVBoxLayout

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis  # type: ignore


class _InteractiveChartView(QChartView):
    """
    - 좌클릭 드래그: X축 팬(과거/미래 이동)
      (QChart.scroll(dx, 0) 사용 → 축 range가 실제로 이동)
    - 더블클릭: 라이브 팔로우 복귀
    """
    def __init__(
        self,
        chart: QChart,
        parent: Optional[QWidget] = None,
        *,
        on_user_interact: Optional[Callable[[], None]] = None,
        on_pan_finished: Optional[Callable[[], None]] = None,
        on_double_click: Optional[Callable[[], None]] = None,
    ):
        super().__init__(chart, parent)
        self._on_user_interact = on_user_interact
        self._on_pan_finished = on_pan_finished
        self._on_double_click = on_double_click

        self._panning = False
        self._last_pos = QPoint()

        try:
            self.setRubberBand(QChartView.NoRubberBand)
        except Exception:
            pass

        try:
            self.setMouseTracking(True)
        except Exception:
            pass

        try:
            self.setCursor(Qt.OpenHandCursor)
        except Exception:
            pass

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._panning = True
            self._last_pos = event.pos()
            try:
                self.setCursor(Qt.ClosedHandCursor)
            except Exception:
                pass

            if self._on_user_interact:
                self._on_user_interact()

            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._panning:
            delta = event.pos() - self._last_pos
            self._last_pos = event.pos()

            # dx를 반대로 주면 "드래그 방향"이 직관적으로 맞음
            try:
                self.chart().scroll(-delta.x(), 0.0)
            except Exception:
                pass

            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._panning:
            self._panning = False
            try:
                self.setCursor(Qt.OpenHandCursor)
            except Exception:
                pass

            if self._on_pan_finished:
                self._on_pan_finished()

            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._on_double_click:
            self._on_double_click()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class DepositionPlotWidget(QWidget):
    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        max_points: int = 600,
        window_seconds: float = 150.0,
        power_title: str = "ADC",
        power_default_range: tuple[float, float] = (0.0, 1.0),
    ):
        super().__init__(parent)
        self._max_points = int(max_points)
        self._window_s = float(window_seconds)
        self._power_title = str(power_title)
        self._p_def_min, self._p_def_max = float(power_default_range[0]), float(power_default_range[1])

        self._t0: Optional[float] = None
        self._last_t: float = 0.0

        # ✅ 핵심 상태: 사용자 조작이 없으면 라이브 팔로우, 과거 조회 중이면 화면 고정
        self._follow_live: bool = True

        self._rate_buf: Deque[Tuple[float, float]] = deque(maxlen=self._max_points)
        self._power_buf: Deque[Tuple[float, float]] = deque(maxlen=self._max_points)

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

        self._chart.setTitle("Dep. Rate / ADC")
        self._chart.setAnimationOptions(QChart.NoAnimation)
        self._chart.addSeries(self._rate_series)
        self._chart.addSeries(self._power_series)

        # X axis (time)
        self._ax_x = QValueAxis()
        self._ax_x.setLabelFormat("%.0f")
        self._ax_x.setTitleText("Time (s)")
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

        # ✅ 인터랙티브 뷰(드래그 팬 + 더블클릭 라이브복귀)
        self._view = _InteractiveChartView(
            self._chart,
            self,
            on_user_interact=self._on_user_interact,
            on_pan_finished=self._on_pan_finished,
            on_double_click=self.reset_to_live,
        )
        self._view.setRenderHint(QPainter.Antialiasing, True)
        lay.addWidget(self._view, 1)

        self._apply_tick_density(segments=10)
        self._sync_axis_label_colors()

        # ✅ X축: 큰 눈금 5초, 작은 눈금 1초
        self._configure_x_axis_ticks(major_s=5.0, minor_s=1.0)

        self._reset_axes()

    def clear(self) -> None:
        self._t0 = None
        self._last_t = 0.0
        self._follow_live = True

        self._rate_buf.clear()
        self._power_buf.clear()
        self._rate_series.clear()
        self._power_series.clear()
        self._reset_axes()

    def reset_to_live(self) -> None:
        """더블클릭 등으로 강제 라이브 복귀"""
        self._follow_live = True
        self._update_axes(self._last_t)

    def append(self, *, rate: Optional[float], power: Optional[float]) -> None:
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        t = now - self._t0
        self._last_t = float(t)

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

            self._power_buf.append((t, p))
            self._power_series.append(t, p)
            if self._power_series.count() > self._max_points:
                self._power_series.removePoints(0, self._power_series.count() - self._max_points)

        # ✅ 핵심:
        # - 사용자 조작 없으면 화면도 계속 앞으로(라이브)
        # - 과거 조회 중이면 화면은 고정(하지만 데이터는 계속 쌓임)
        if self._follow_live:
            self._update_axes(t)
        else:
            self._update_axes_manual(t)

    def _on_user_interact(self) -> None:
        # 사용자가 드래그 시작하면 라이브 팔로우 끔(화면 고정 모드)
        self._follow_live = False

    def _on_pan_finished(self) -> None:
        # 드래그가 끝난 시점에:
        # - range를 데이터 범위 안으로 클램프
        # - 오른쪽 끝(라이브)에 도달했으면 자동 라이브 복귀
        self._update_axes_manual(self._last_t)

    def _get_x_range(self) -> Tuple[float, float]:
        try:
            return float(self._ax_x.min()), float(self._ax_x.max())
        except Exception:
            return 0.0, float(self._window_s)

    def _set_x_range(self, x1: float, x2: float) -> None:
        self._ax_x.setRange(float(x1), float(x2))
        self._apply_x_tickcount_for_range(float(x1), float(x2))

    def _compute_live_range(self, t_now: float) -> Tuple[float, float]:
        major = float(getattr(self, "_x_major_s", 0.0) or 0.0)

        if major > 0:
            if t_now < self._window_s:
                x1 = 0.0
                x2 = float(self._window_s)
            else:
                x2 = float(t_now)
                x1 = max(0.0, x2 - self._window_s)

            x2 = float(math.ceil(x2 / major) * major)
            x1 = max(0.0, x2 - self._window_s)
            x1 = float(math.floor(x1 / major) * major)

            if x2 <= x1:
                x2 = x1 + major
            return x1, x2

        x1 = max(0.0, float(t_now) - self._window_s)
        x2 = max(x1 + 1.0, float(t_now))
        return x1, x2

    def _clamp_manual_range(self, x1: float, x2: float, live_x2: float) -> Tuple[float, float]:
        span = max(1.0, float(x2) - float(x1))

        # 미래로 너무 가면(live_x2 밖) 오른쪽 끝을 live_x2로 당김
        if x2 > live_x2:
            x2 = float(live_x2)
            x1 = x2 - span

        # 0보다 작아지면 0으로 당김
        if x1 < 0.0:
            x1 = 0.0
            x2 = min(float(live_x2), x1 + span)

        # span 유지 보정
        if x2 <= x1:
            x2 = min(float(live_x2), x1 + 1.0)

        return float(x1), float(x2)

    def _maybe_resume_live(self, current_x2: float, live_x2: float) -> None:
        if self._follow_live:
            return

        major = float(getattr(self, "_x_major_s", 0.0) or 0.0)
        eps = 1.0
        if major > 0:
            eps = max(0.5, major * 0.6)  # 5초 major면 약 3초 내면 "오른쪽 끝 도달"로 판단

        if abs(float(live_x2) - float(current_x2)) <= eps:
            self._follow_live = True
            # 즉시 라이브 윈도우로 복귀
            self._update_axes(self._last_t)

    def _update_rate_axis_for_range(self, x1: float, x2: float) -> None:
        if not self._rate_buf:
            return

        ys = [v for tt, v in self._rate_buf if float(x1) <= tt <= float(x2)]
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


    def _update_power_axis_for_range(self, x1: float, x2: float) -> None:
        if not self._power_buf:
            self._ax_power.setRange(self._p_def_min, self._p_def_max)
            return

        ys = [v for tt, v in self._power_buf if float(x1) <= tt <= float(x2)]
        if not ys:
            ys = [v for _, v in self._power_buf]  # fallback

        y_max = max(ys)
        y_upper = max(1.0, y_max * 1.10)
        self._ax_power.setRange(0.0, y_upper)

        try:
            if y_upper < 2.0:
                self._ax_power.setLabelFormat("%.3f")
            elif y_upper < 10.0:
                self._ax_power.setLabelFormat("%.2f")
            elif y_upper < 100.0:
                self._ax_power.setLabelFormat("%.1f")
            else:
                self._ax_power.setLabelFormat("%.0f")
        except Exception:
            pass

    def _reset_axes(self) -> None:
        x2 = max(10.0, self._window_s)

        major = float(getattr(self, "_x_major_s", 0.0) or 0.0)
        if major > 0:
            x2 = float(math.ceil(x2 / major) * major)

        self._set_x_range(0.0, x2)

        self._ax_rate.setRange(0.0, 1.0)
        self._ax_power.setRange(self._p_def_min, self._p_def_max)

    def _update_axes(self, t_now: float) -> None:
        # ✅ 라이브 팔로우
        x1, x2 = self._compute_live_range(float(t_now))
        self._set_x_range(x1, x2)
        self._update_rate_axis_for_range(x1, x2)
        self._update_power_axis_for_range(x1, x2)

    def _update_axes_manual(self, t_now: float) -> None:
        # ✅ 과거 조회 중:
        # - 화면 x range는 유지
        # - 다만 데이터 범위 밖으로 나가면 clamp
        # - y축 autoscale은 현재 보이는 구간 기준으로 계속 업데이트
        _, live_x2 = self._compute_live_range(float(t_now))
        x1, x2 = self._get_x_range()
        x1, x2 = self._clamp_manual_range(x1, x2, float(live_x2))
        self._set_x_range(x1, x2)
        self._update_rate_axis_for_range(x1, x2)
        self._update_power_axis_for_range(x1, x2)

        # ✅ 사용자가 다시 오른쪽 끝으로 이동하면 자동 라이브 복귀
        self._maybe_resume_live(current_x2=x2, live_x2=float(live_x2))

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

        for ax in (self._ax_x, self._ax_rate, self._ax_power):
            try:
                ax.setMinorTickCount(0)
            except Exception:
                pass

    def _sync_axis_label_colors(self) -> None:
        """선 색상에 맞춰 축 제목(라벨) 색을 칠해서 legend 없이도 구분되게."""
        rate_color = self._rate_series.pen().color()
        power_color = self._power_series.pen().color()

        if rate_color == power_color:
            rate_color = QColor("#1f77b4")
            power_color = QColor("#2ca02c")
            try:
                self._rate_series.setPen(QPen(rate_color, 2))
            except Exception:
                pass
            try:
                self._power_series.setPen(QPen(power_color, 2))
            except Exception:
                pass

        try:
            self._ax_rate.setTitleBrush(QBrush(rate_color))
        except Exception:
            pass
        try:
            self._ax_power.setTitleBrush(QBrush(power_color))
        except Exception:
            pass

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

        minor_cnt = 0
        if self._x_minor_s > 0 and self._x_major_s > self._x_minor_s:
            div = int(round(self._x_major_s / self._x_minor_s))
            minor_cnt = max(0, div - 1)

        try:
            self._ax_x.setMinorTickCount(minor_cnt)
        except Exception:
            pass

        try:
            if hasattr(self._ax_x, "setMinorGridLineVisible"):
                self._ax_x.setMinorGridLineVisible(True)
            if hasattr(self._ax_x, "setGridLineVisible"):
                self._ax_x.setGridLineVisible(True)
        except Exception:
            pass

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
        ticks = max(2, min(200, ticks))
        try:
            self._ax_x.setTickCount(ticks)
        except Exception:
            pass