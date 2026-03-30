# process_window.py
from __future__ import annotations

import gc
import re
import time
import warnings
import contextlib
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget,
    QMessageBox,
    QGridLayout,
    QVBoxLayout,
    QApplication,
    QLabel,
    QPushButton,
    QSizePolicy,
)
from PySide6.QtCore import QTimer, Qt

from ui.windows.mainWindow import Ui_Form
from ui.rt_plot_widget import DepositionPlotWidget
from ui.material_catalog_dialog import MaterialCatalogDialog
from services.stm_service import STMService
from controller.process_start_worker import (
    ProcessStartWorker,
    STMPreflightConfig,
    STMPreflightResult,
)

if TYPE_CHECKING:
    from ui.windows.hmi_window import HmiWindow


_BASE_DIR = Path(__file__).resolve().parent
while not (_BASE_DIR / "config").exists() and _BASE_DIR != _BASE_DIR.parent:
    _BASE_DIR = _BASE_DIR.parent


def _append_text(widget: Any, text: str) -> None:
    if widget is None:
        return
    s = str(text).rstrip("\n")
    try:
        if hasattr(widget, "appendPlainText"):
            widget.appendPlainText(s)
            return
        if hasattr(widget, "append"):
            widget.append(s)
            return
        if hasattr(widget, "setText") and hasattr(widget, "text"):
            prev = widget.text() or ""
            widget.setText((prev + "\n" + s).strip())
            return
    except Exception:
        pass

# ============================================================
# Process 李?
# ============================================================
class ProcessWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.ui = Ui_Form()
        self.ui.setupUi(self)


        self._active_run_id: Optional[str] = None

        self.setWindowTitle("Process")
        self.ui.stackedWidget.setCurrentIndex(1)  # Process page

        self.hmi_window: Optional[HmiWindow] = None
        self._close_stop_guard = False

        self._process_controller: Any = None
        self._log_service: Any = None
        self._stm_service: Any = None
        self._acs_service: Any = None
        self._run_summary_service: Any = None
        self._recommendation_service: Any = None


        self._start_worker: Optional[ProcessStartWorker] = None
        self._start_in_progress: bool = False
        self._pending_run_cfg: Optional[dict[str, Any]] = None
        self._active_run_cfg: Optional[dict[str, Any]] = None
        self._active_run_profile: Optional[dict[str, Any]] = None
        self._last_recommendation: Optional[dict[str, Any]] = None


        self._stm_ui_bound: bool = False
        self._stm_ui_stm: Any = None

        self._material_1 = None
        self._material_2 = None

        # 공정 시작 ?쒖젏 power ?좏깮 ?곹깭 latch
        self._run_use_power1: Optional[bool] = None
        self._run_use_power2: Optional[bool] = None

        # ?꾩옱 ?섎뱶?⑥뼱 ?꾩떆 ?고쉶 ?뺤콉


        self._power2_temporarily_disabled: bool = True
        self._power1_feedback_uses_adc2: bool = True

        self.ui.materialEdit.clicked.connect(lambda: self._open_material_dialog(1))
        self.ui.materialEdit2.clicked.connect(lambda: self._open_material_dialog(2))

        self.ui.startProcess.clicked.connect(self._on_start_clicked)
        self.ui.stopProcess.clicked.connect(self._on_stop_clicked)


        cfg_btn = getattr(self.ui, "processConfigBtn", None)
        if cfg_btn is not None and hasattr(cfg_btn, "clicked"):
            cfg_btn.clicked.connect(self._open_process_config_dialog)
        recipe_btn = getattr(self.ui, "recipeBtn", None)
        if recipe_btn is not None and hasattr(recipe_btn, "clicked"):
            recipe_btn.clicked.connect(self._open_process_recipe_dialog)
        self._setup_process_action_panel()


        self._process_cfg: dict[str, Any] = self._default_process_config()

        # =========================

        # =========================
        self._last_rate: Optional[float] = None
        self._last_thickness: Optional[float] = None


        self._last_power: Optional[float] = None

        self._plot: Optional[DepositionPlotWidget] = None
        self._init_rt_plot()  # graphWidget ?먮━??plot ?쎌엯

        self._rt_timer = QTimer(self)
        self._rt_timer.setInterval(1000)
        self._rt_timer.timeout.connect(self._tick_rt_ui)

        self._setup_process_monitor_ui()

    def set_hmi_window(self, hmi_window: HmiWindow):
        self.hmi_window = hmi_window

    def set_runtime_objects(self, plc_binder, ini_path: Path, *, process_controller=None, log_service=None,
                            stm_service=None, acs_service=None, run_summary_service=None,
                            recommendation_service=None) -> None:
        self._plc_binder = plc_binder
        self._ini_path = Path(ini_path)

        self._process_controller = process_controller
        self._log_service = log_service
        self._stm_service = stm_service
        self._acs_service = acs_service
        self._run_summary_service = run_summary_service
        self._recommendation_service = recommendation_service


        pc = self._process_controller
        if pc is not None:
            try:
                pc.sig_ui_log.connect(self._append_process_log)
            except Exception:
                pass
            try:
                pc.sig_status.connect(self._on_status)
            except Exception:
                pass
            try:
                pc.sig_error.connect(self._on_error)
            except Exception:
                pass
            try:
                pc.sig_finished.connect(self._on_finished)
            except Exception:
                pass

    def _setup_process_monitor_ui(self) -> None:
        w = getattr(self.ui, "processMonitor_Process", None)
        if w is None:
            return

        try:
            if hasattr(w, "setStyleSheet"):
                w.setStyleSheet(
                    "background: white;"
                    "border: 1px solid #d0d0d0;"
                    "border-radius: 2px;"
                    "color: #111111;"
                    "padding: 4px 10px;"
                    "font-size: 16px;"
                    "font-weight: 700;"
                )

            if hasattr(w, "setWordWrap"):
                w.setWordWrap(True)
            if hasattr(w, "setAlignment"):
                w.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            if hasattr(w, "setMargin"):
                w.setMargin(10)

        except Exception:
            pass
        self._adjust_process_page_layout()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        with contextlib.suppress(Exception):
            self.ui.adjust_window_layout()
        with contextlib.suppress(Exception):
            self._adjust_process_page_layout()

    def _adjust_process_page_layout(self) -> None:
        self._ensure_process_left_layout()
        self._update_process_left_geometry()
        self._ensure_process_content_layout()
        self._update_process_content_geometry()
        self._update_process_action_panel_geometry()

    def _ensure_process_left_layout(self) -> None:
        parent = getattr(self.ui, "page_2", None)
        action_panel = getattr(self, "_process_action_panel", None)
        names = (
            "evaporatorLabel",
            "processNameLabel",
            "processNameEdit",
            "sourcePower1",
            "sourcePower2",
            "materialLabel",
            "materialEdit",
            "materialEdit2",
            "deprateLabel",
            "deprateEdit",
            "deprateEdit2",
            "thicknessLabel",
            "thicknessEdit",
            "delayLabel",
            "delayEdit",
            "currentRateLabel",
            "currentRateEdit",
            "currentThicknessLabel",
            "currentThicknessEdit",
            "currentDac1Label",
            "currentDac2Label",
            "currentDac1Edit",
            "currentDac2Edit",
            "actualPower1Label",
            "actualPower2Label",
            "actualPower1Edit",
            "actualPower2Edit",
        )
        widgets = {name: getattr(self.ui, name, None) for name in names}
        if parent is None or action_panel is None or any(widget is None for widget in widgets.values()):
            return

        panel = getattr(self, "_process_left_panel", None)
        if panel is not None:
            return

        panel = QWidget(parent)
        panel.setObjectName("processLeftPanel")
        root = QVBoxLayout(panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        self._process_left_panel = panel
        self._process_left_layout = root

        def _make_two_col_row(*items: QWidget) -> QWidget:
            row = QWidget(panel)
            grid = QGridLayout(row)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(4)
            for idx, item in enumerate(items):
                with contextlib.suppress(Exception):
                    item.setParent(row)
                grid.addWidget(item, 0, idx)
                grid.setColumnStretch(idx, 1)
            return row

        def _make_two_line_grid(
            label1: QWidget,
            label2: QWidget,
            edit1: QWidget,
            edit2: QWidget,
        ) -> QWidget:
            row = QWidget(panel)
            grid = QGridLayout(row)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(2)
            for item in (label1, label2, edit1, edit2):
                with contextlib.suppress(Exception):
                    item.setParent(row)
            grid.addWidget(label1, 0, 0)
            grid.addWidget(label2, 0, 1)
            grid.addWidget(edit1, 1, 0)
            grid.addWidget(edit2, 1, 1)
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            return row

        for widget in widgets.values():
            with contextlib.suppress(Exception):
                widget.setParent(panel)
                widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        with contextlib.suppress(Exception):
            action_panel.setParent(panel)
            action_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        power_row = _make_two_col_row(widgets["sourcePower1"], widgets["sourcePower2"])
        material_row = _make_two_col_row(widgets["materialEdit"], widgets["materialEdit2"])
        rate_row = _make_two_col_row(widgets["deprateEdit"], widgets["deprateEdit2"])
        dac_row = _make_two_line_grid(
            widgets["currentDac1Label"],
            widgets["currentDac2Label"],
            widgets["currentDac1Edit"],
            widgets["currentDac2Edit"],
        )
        adc_row = _make_two_line_grid(
            widgets["actualPower1Label"],
            widgets["actualPower2Label"],
            widgets["actualPower1Edit"],
            widgets["actualPower2Edit"],
        )

        for key in ("processNameEdit", "thicknessEdit", "delayEdit", "currentRateEdit", "currentThicknessEdit"):
            with contextlib.suppress(Exception):
                widgets[key].setMinimumHeight(32)
        for key in ("materialEdit", "materialEdit2"):
            with contextlib.suppress(Exception):
                widgets[key].setMinimumHeight(36)
        for key in ("deprateEdit", "deprateEdit2", "currentDac1Edit", "currentDac2Edit", "actualPower1Edit", "actualPower2Edit"):
            with contextlib.suppress(Exception):
                widgets[key].setMinimumHeight(30)

        root.addWidget(widgets["evaporatorLabel"])
        root.addWidget(widgets["processNameLabel"])
        root.addWidget(widgets["processNameEdit"])
        root.addWidget(power_row)
        root.addWidget(widgets["materialLabel"])
        root.addWidget(material_row)
        root.addWidget(widgets["deprateLabel"])
        root.addWidget(rate_row)
        root.addWidget(widgets["thicknessLabel"])
        root.addWidget(widgets["thicknessEdit"])
        root.addWidget(widgets["delayLabel"])
        root.addWidget(widgets["delayEdit"])
        root.addWidget(widgets["currentRateLabel"])
        root.addWidget(widgets["currentRateEdit"])
        root.addWidget(widgets["currentThicknessLabel"])
        root.addWidget(widgets["currentThicknessEdit"])
        root.addWidget(dac_row)
        root.addWidget(adc_row)
        root.addWidget(action_panel)

    def _update_process_left_geometry(self) -> None:
        panel = getattr(self, "_process_left_panel", None)
        parent = getattr(self.ui, "page_2", None)
        if panel is None or parent is None:
            return

        x = 4
        y = 6
        width = 191
        available_h = max(200, int(parent.height()) - y - 8)
        panel_h = available_h
        panel.setGeometry(x, y, width, panel_h)
        self._sync_process_left_layout(panel_h)

    def _sync_process_left_layout(self, panel_height: int) -> None:
        layout = getattr(self, "_process_left_layout", None)
        action_panel = getattr(self, "_process_action_panel", None)
        if layout is None or action_panel is None:
            return

        total_h = max(560, int(panel_height or 0))
        base_spacing = 10
        hint_h = 0
        gap_count = max(1, layout.count() - 1)
        with contextlib.suppress(Exception):
            hint_h = int(layout.sizeHint().height())
        extra = max(0, total_h - hint_h)
        spacing = min(14, base_spacing + (extra // gap_count))

        with contextlib.suppress(Exception):
            layout.setSpacing(spacing)
        with contextlib.suppress(Exception):
            action_panel.setMinimumHeight(132)
            action_panel.setMaximumHeight(140)

    def _ensure_process_content_layout(self) -> None:
        monitor = getattr(self.ui, "processMonitor_Process", None)
        graph = getattr(self.ui, "graphWidget", None)
        log_window = getattr(self.ui, "logWindow", None)
        parent = getattr(self.ui, "page_2", None)
        if parent is None or any(widget is None for widget in (monitor, graph, log_window)):
            return

        panel = getattr(self, "_process_content_panel", None)
        if panel is None:
            widgets = (monitor, graph, log_window)
            rects = [widget.geometry() for widget in widgets]
            left = min(int(rect.x()) for rect in rects)
            top = min(int(rect.y()) for rect in rects)
            right = max(int(rect.x()) + int(rect.width()) for rect in rects)
            bottom = max(int(rect.y()) + int(rect.height()) for rect in rects)
            self._process_content_panel_base = {
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
                "right_margin": 8,
                "bottom_margin": 8,
            }
            panel = QWidget(parent)
            panel.setObjectName("processContentPanel")
            layout = QVBoxLayout(panel)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            self._process_content_panel = panel
        else:
            layout = panel.layout()
            if not isinstance(layout, QVBoxLayout):
                layout = QVBoxLayout(panel)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(8)

        for widget in (monitor, graph, log_window):
            with contextlib.suppress(Exception):
                widget.setParent(panel)

        while layout.count():
            item = layout.takeAt(0)
            child = item.widget()
            if child is not None:
                child.hide()

        if isinstance(monitor, QLabel):
            with contextlib.suppress(Exception):
                monitor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        with contextlib.suppress(Exception):
            graph.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        with contextlib.suppress(Exception):
            log_window.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)

        layout.addWidget(monitor, 0)
        layout.addWidget(graph, 5)
        layout.addWidget(log_window, 2)
        for widget in (monitor, graph, log_window):
            with contextlib.suppress(Exception):
                widget.show()

    def _update_process_content_geometry(self) -> None:
        panel = getattr(self, "_process_content_panel", None)
        parent = getattr(self.ui, "page_2", None)
        base = getattr(self, "_process_content_panel_base", None) or {}
        if panel is None or parent is None or not base:
            return

        x = int(base.get("x", 210))
        y = int(base.get("y", 5))
        right_margin = max(0, int(base.get("right_margin", 0)))
        bottom_margin = max(0, int(base.get("bottom_margin", 0)))
        width = max(760, int(parent.width()) - x - right_margin)
        height = max(520, int(parent.height()) - y - bottom_margin)
        panel.setGeometry(x, y, width, height)
        self._sync_process_content_sizing(height)

    def _sync_process_content_sizing(self, panel_height: int) -> None:
        monitor = getattr(self.ui, "processMonitor_Process", None)
        graph = getattr(self.ui, "graphWidget", None)
        log_window = getattr(self.ui, "logWindow", None)
        if any(widget is None for widget in (monitor, graph, log_window)):
            return

        total_h = max(520, int(panel_height or 0))
        status_h = max(92, min(122, int(total_h * 0.13)))
        log_h = max(180, min(250, int(total_h * 0.26)))
        graph_h = max(220, total_h - status_h - log_h - 16)

        with contextlib.suppress(Exception):
            monitor.setMinimumHeight(status_h)
            monitor.setMaximumHeight(max(status_h + 16, int(total_h * 0.19)))
        with contextlib.suppress(Exception):
            graph.setMinimumHeight(graph_h)
        with contextlib.suppress(Exception):
            log_window.setMinimumHeight(log_h)
            log_window.setMaximumHeight(max(log_h + 24, int(total_h * 0.34)))

    def _update_process_action_panel_geometry(self) -> None:
        if getattr(self, "_process_left_panel", None) is not None:
            return
        panel = getattr(self, "_process_action_panel", None)
        parent = getattr(self.ui, "page_2", None)
        base = getattr(self, "_process_action_panel_base", None) or {}
        if panel is None or parent is None or not base:
            return

        page_w = max(int(base.get("page_width", parent.width() or 1)), int(parent.width() or 1))
        page_h = max(int(base.get("page_height", parent.height() or 1)), int(parent.height() or 1))
        x = int(base.get("x", 0))
        y = int(base.get("y", 0))
        right_margin = max(0, int(base.get("right_margin", 0)))
        bottom_margin = max(0, int(base.get("bottom_margin", 0)))
        width = max(180, min(int(base.get("width", 191)), page_w - x - right_margin))
        available_h = max(int(base.get("height", 143)), page_h - y - bottom_margin)

        layout = panel.layout()
        desired_h = int(base.get("compact_height", base.get("height", 143)))
        if layout is not None:
            with contextlib.suppress(Exception):
                desired_h = max(desired_h, int(layout.sizeHint().height()) + 10)

        panel_h = min(max(desired_h, int(base.get("min_height", desired_h))), available_h)
        anchored_y = max(0, page_h - bottom_margin - panel_h)
        panel.setGeometry(x, anchored_y, width, panel_h)

    def _setup_process_action_panel(self) -> None:
        parent = getattr(self.ui, "page_2", None)
        cfg_btn = getattr(self.ui, "processConfigBtn", None)
        recipe_btn = getattr(self.ui, "recipeBtn", None)
        start_btn = getattr(self.ui, "startProcess", None)
        stop_btn = getattr(self.ui, "stopProcess", None)
        if parent is None or any(btn is None for btn in (cfg_btn, recipe_btn, start_btn, stop_btn)):
            return

        panel = getattr(self, "_process_action_panel", None)
        if panel is None:
            rects = [btn.geometry() for btn in (cfg_btn, recipe_btn, start_btn, stop_btn)]
            left = min(int(rect.x()) for rect in rects)
            top = min(int(rect.y()) for rect in rects)
            right = max(int(rect.x()) + int(rect.width()) for rect in rects)
            bottom = max(int(rect.y()) + int(rect.height()) for rect in rects)
            self._process_action_panel_base = {
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
                "compact_height": 132,
                "right_margin": 8,
                "bottom_margin": 8,
                "page_width": max(1, int(parent.width())),
                "page_height": max(1, int(parent.height())),
                "min_height": 132,
            }
            panel = QWidget(parent)
            panel.setObjectName("processActionPanel")
            layout = QGridLayout(panel)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setHorizontalSpacing(8)
            layout.setVerticalSpacing(8)
            self._process_action_panel = panel
        else:
            layout = panel.layout()
            if not isinstance(layout, QGridLayout):
                layout = QGridLayout(panel)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setHorizontalSpacing(8)
                layout.setVerticalSpacing(8)

        buttons = (cfg_btn, recipe_btn, start_btn, stop_btn)
        for button in buttons:
            with contextlib.suppress(Exception):
                button.setParent(panel)
                button.setAutoDefault(False)
                button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        for action_btn in (cfg_btn, recipe_btn, start_btn, stop_btn):
            with contextlib.suppress(Exception):
                action_btn.setMinimumHeight(56)
                action_btn.setMaximumHeight(60)

        while layout.count():
            item = layout.takeAt(0)
            child = item.widget()
            if child is not None:
                child.hide()

        layout.addWidget(cfg_btn, 0, 0)
        layout.addWidget(recipe_btn, 0, 1)
        layout.addWidget(start_btn, 1, 0)
        layout.addWidget(stop_btn, 1, 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)

        for button in buttons:
            with contextlib.suppress(Exception):
                button.show()

        self._update_process_action_panel_geometry()

        with contextlib.suppress(Exception):
            panel.raise_()

    def _set_process_monitor_text(self, text: str, *, fallback: str = "---") -> None:
        w = getattr(self.ui, "processMonitor_Process", None)
        if w is None or not hasattr(w, "setText"):
            return

        s = str(text or "").strip()
        if not s:
            s = fallback

        try:
            w.setText(s)
        except Exception:
            pass

    def _set_process_status(self, title: str, detail: str = "") -> None:
        title = str(title or "").strip() or "---"
        detail = str(detail or "").strip()
        if detail:
            self._set_process_monitor_text(f"{title}\n{detail}")
        else:
            self._set_process_monitor_text(title)

    @staticmethod
    def _strip_process_log_tag_prefix(body: str, tag: str) -> str:
        text = str(body or "").strip()
        if not text or not tag:
            return text
        pattern = rf"^(?:\[{re.escape(str(tag).strip())}\]\s*)+"
        return re.sub(pattern, "", text, flags=re.IGNORECASE).strip()

    def _format_process_log_line(self, raw: str) -> str:
        # 이미 [HH:MM:SS] 또는 [YYYY-MM-DD HH:MM:SS] 접두사가 있으면 시간 부분 추출하여 재포맷
        text = str(raw or "").strip()
        if not text:
            return ""

        time_text = datetime.now().strftime("%H:%M:%S")
        payload = text

        m = re.match(r"^\[(?:\d{4}-\d{2}-\d{2}\s+)?(\d{2}:\d{2}:\d{2})\]\s+(.*)$", text)
        if m:
            time_text = str(m.group(1) or "").strip() or time_text
            payload = str(m.group(2) or "").strip()

        level = ""
        tag = ""

        level_match = re.match(r"^\[([A-Z]+)\]\s*(.*)$", payload)
        if level_match and str(level_match.group(1) or "").upper() in ("INFO", "WARN", "WARNING", "ERROR", "DEBUG"):
            level = str(level_match.group(1) or "").strip().upper()
            payload = str(level_match.group(2) or "").strip()

        tag_match = re.match(r"^\[([^\]]+)\]\s*(.*)$", payload)
        if tag_match:
            tag = str(tag_match.group(1) or "").strip()
            payload = str(tag_match.group(2) or "").strip()

        if tag:
            payload = self._strip_process_log_tag_prefix(payload, tag)
        if level in ("WARN", "WARNING", "ERROR"):
            level_pattern = "WARN|WARNING" if level == "WARNING" else re.escape(level)
            payload = re.sub(rf"^\[(?:{level_pattern})\]\s*", "", payload, flags=re.IGNORECASE)
        elif level == "INFO":
            payload = re.sub(r"^\[INFO\]\s*", "", payload, flags=re.IGNORECASE)

        prefix = ""
        if tag:
            prefix = f"[{tag}]"
            if level in ("WARN", "WARNING", "ERROR"):
                prefix += f"[{'WARN' if level == 'WARNING' else level}]"
        elif level in ("WARN", "WARNING", "ERROR"):
            prefix = f"[{'WARN' if level == 'WARNING' else level}]"

        if prefix and payload:
            return f"[{time_text}] {prefix} {payload}"
        if prefix:
            return f"[{time_text}] {prefix}"
        if payload:
            return f"[{time_text}] {payload}"
        return f"[{time_text}]"

    def _append_process_log(self, text: str) -> None:
        raw = str(text or "").strip()
        if not raw:
            return

        line = self._format_process_log_line(raw)

        _append_text(getattr(self.ui, "logWindow", None), line)

        ls = self._log_service
        if self._active_run_id and ls is not None and hasattr(ls, "run_line"):
            with contextlib.suppress(Exception):
                ls.run_line(line)

    def _build_run_profile(self, run_cfg: dict[str, Any]) -> Optional[dict[str, Any]]:
        pc = self._process_controller
        if pc is None or not hasattr(pc, "build_run_profile"):
            return None
        return dict(pc.build_run_profile(run_cfg) or {})

    def _apply_process_config(self, new_cfg: Any, *, log_prefix: str) -> None:
        self._process_cfg = self._normalize_process_config(new_cfg)

        steps = self._process_cfg.get("ramp_steps") or []
        step_desc = ", ".join(
            f"{idx+1}: ADC {float(s.get('target_adc', 0.0)):.1f}, "
            f"DAC+{int(s.get('dac_step', 0))}, "
            f"{float(s.get('dac_interval_sec', 0.0)):.1f}s, "
            f"hold {float(s.get('hold_sec', 0.0)):.1f}s"
            for idx, s in enumerate(steps)
        )

        if self._plot is not None and hasattr(self._plot, "set_power_default_range"):
            try:
                adc_range = self._get_plot_adc_default_range()
                self._plot.set_power_default_range(*adc_range)
            except Exception:
                pass

        self._append_process_log(f"{log_prefix} steps={len(steps)} | {step_desc}")

    def _open_process_run_log(self, run_id: str, run_cfg: dict[str, Any], run_profile: Optional[dict[str, Any]] = None) -> None:
        self._active_run_id = str(run_id)

        ls = self._log_service
        if ls is None or not hasattr(ls, "open_run"):
            return

        recipe_name = str(run_cfg.get("process_name", "") or "").strip()
        profile = dict(run_profile or {})
        meta = {
            "process_name": recipe_name,
            "material_name": str(run_cfg.get("material_name", "") or "").strip(),
            "density": run_cfg.get("density"),
            "z_factor": run_cfg.get("z_factor"),
            "use_power1": bool(run_cfg.get("use_power1", False)),
            "use_power2": bool(run_cfg.get("use_power2", False)),
            "target_rate": run_cfg.get("target_rate"),
            "target_thickness": run_cfg.get("target_thickness"),
            "delay_min": run_cfg.get("delay_min"),
            "process_config": dict(run_cfg.get("process_config") or {}),
            "process_config_hash": str(profile.get("process_config_hash", "") or ""),
            "hw_mapping": dict(profile.get("hw_mapping") or {}),
            "configured_start_dac": profile.get("configured_start_dac"),
            "initial_dac": profile.get("initial_dac"),
            "initial_dac_source": str(profile.get("initial_dac_source", "") or ""),
            "applied_recommended_start_dac": bool(profile.get("applied_recommended_start_dac", False)),
        }

        with contextlib.suppress(Exception):
            ls.open_run(run_id=run_id, recipe_name=recipe_name, meta=meta)

    def _close_process_run_log(self) -> None:
        ls = self._log_service
        if ls is None or not hasattr(ls, "close_run"):
            return

        with contextlib.suppress(Exception):
            ls.close_run()

    def _store_finished_run_summary(self, *, run_profile: dict[str, Any], result: Any) -> None:
        svc = self._run_summary_service
        if not run_profile or svc is None or not hasattr(svc, "store_finished_run"):
            return

        try:
            svc.store_finished_run(run_profile=dict(run_profile), result=result)
        except Exception as exc:
            self._append_process_log(f"[HISTORY][WARN] summary save failed: {exc!r}")

    def _bind_stm_ui(self, stm):
        """
        ???듭떖: disconnect??'?꾩옱 self._stm_service'媛 ?꾨땲??
                '?ㅼ젣濡?UI??connect ?섏뿀??STM ?몄뒪?댁뒪'?먯꽌留??댁빞 ?쒕떎.
        """
        if stm is None:
            self._unbind_stm_ui()
            return


        if self._stm_ui_bound and (self._stm_ui_stm is stm):
            return


        self._unbind_stm_ui()


        stm.sig_rate.connect(self._on_stm_rate)
        stm.sig_thickness.connect(self._on_stm_thickness)


        self._stm_ui_stm = stm
        self._stm_ui_bound = True


    def _unbind_stm_ui(self):
        """
        ??'?곌껐?덈뜕 ?곸씠 ?녿뒗 媛앹껜'?먯꽌 disconnect瑜??쒕룄?섎㈃
        Failed to disconnect RuntimeWarning???щ떎.
        ??洹몃옒?? UI???곌껐?덈뜕 stm???곕줈 湲곗뼲?대몢怨?洹?stm留?disconnect?쒕떎.
        """
        stm = self._stm_ui_stm
        if (not self._stm_ui_bound) or (stm is None):
            self._stm_ui_stm = None
            self._stm_ui_bound = False
            return


        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)

            try:
                stm.sig_rate.disconnect(self._on_stm_rate)
            except Exception:
                pass

            try:
                stm.sig_thickness.disconnect(self._on_stm_thickness)
            except Exception:
                pass

        self._stm_ui_stm = None
        self._stm_ui_bound = False

    def _on_stm_rate(self, rate):

        try:
            self._last_rate = float(rate)
        except Exception:
            self._last_rate = None

    def _on_stm_thickness(self, th):
        try:
            self._last_thickness = float(th)
        except Exception:
            self._last_thickness = None
    
    def _check_plc_ready_before_start(self) -> bool:
        """
        Start 踰꾪듉 ?뚮?????PLC ?곌껐 ?곹깭瑜?癒쇱? ?뺤씤?쒕떎.
        PLC媛 ?딄릿 ?곹깭硫?STM ?곌껐/공정 시작?쇰줈 ?섏뼱媛吏 ?딄쾶 留됰뒗??
        """
        def _abort(msg: str) -> bool:
            self._append_process_log(f"[PRECHECK][BLOCK] PLC: {msg}")
            QMessageBox.warning(self, "PLC Pre-check", msg)
            return False

        if self.hmi_window is None:
            return _abort("HMI window媛 ?놁뒿?덈떎. PLC ?곹깭瑜??뺤씤?????놁뒿?덈떎.")

        binder = getattr(self.hmi_window, "_plc_binder", None)
        if binder is None:
            return _abort("plc_binder媛 ?놁뒿?덈떎. PLC ?곌껐 ?곹깭瑜??뺤씤?????놁뒿?덈떎.")

        try:
            plc = binder.get_plc_service()
        except Exception as e:
            return _abort(f"PLC ?쒕퉬??議고쉶 ?ㅽ뙣: {e!r}")

        if plc is None:
            return _abort("PLC ?쒕퉬?ㅺ? ?놁뒿?덈떎.")

        try:
            connected = False


            is_connected_fn = getattr(plc, "is_connected", None)
            if callable(is_connected_fn):
                connected = bool(is_connected_fn())
            else:

                snap = plc.get_last_snapshot() if hasattr(plc, "get_last_snapshot") else None
                connected = bool(getattr(snap, "connected", False))

            if not connected:
                return _abort("PLC媛 ?곌껐?섏? ?딆븯?듬땲??\nPLC ?곌껐 ???ㅼ떆 ?쒖옉?섏꽭??")

        except Exception as e:
            return _abort(f"PLC ?곌껐 ?곹깭 ?뺤씤 ?ㅽ뙣: {e!r}")

        self._append_process_log("[PRECHECK] PLC OK: connected")
        return True

    def _prepare_stm_service_for_start(self) -> bool:
        """
        Start 吏곹썑 UI thread?먯꽌??
        - FTM ON ?붿껌
        - STMService 媛앹껜 ?앹꽦/start
        - ProcessController??runtime device 二쇱엯
        源뚯?留??섑뻾?쒕떎.

        ?ㅼ젣 ?곌껐 ?湲?/ crystal health check??
        ProcessStartWorker(QThread)?먯꽌 泥섎━?쒕떎.
        """
        if self.hmi_window is None:
            self._append_process_log("[DEV][ERR] hmi_window is None -> cannot prepare STM")
            return False

        ini_path = getattr(self, "_ini_path", None) or getattr(self.hmi_window, "_ini_path", None)
        if not ini_path:
            self._append_process_log("[DEV][ERR] ini_path is None -> cannot prepare STM")
            return False


        self._shutdown_stm_with_ftm_off_best_effort()

        binder = getattr(self.hmi_window, "_plc_binder", None)
        if binder is None:
            self._append_process_log("[DEV][ERR] plc_binder is None (cannot turn on FTM_SW)")
            return False

        try:
            binder.enqueue_write("FTM_SW", True)
            self._append_process_log("[DEV] FTM_SW -> ON (before STM preflight)")

            stm = STMService(ini_path=ini_path)
            stm.start()

            self._stm_service = stm
            self.hmi_window._stm_service = stm

            self._acs_service = getattr(self.hmi_window, "_acs_service", None)

            pc = self._process_controller
            if pc is not None and hasattr(pc, "replace_runtime_devices"):
                pc.replace_runtime_devices(stm=stm, acs=self._acs_service)
            else:
                self._append_process_log("[DEV][WARN] ProcessController.replace_runtime_devices() ?놁쓬")

            self._append_process_log("[DEV] STM service prepared (health/preflight pending)")
            return True

        except Exception as e:
            self._append_process_log(f"[DEV][ERR] STM prepare failed: {e!r}")

            with contextlib.suppress(Exception):
                binder.enqueue_write("FTM_SW", False)
                self._append_process_log("[DEV] FTM_SW -> OFF (STM prepare failed)")

            self._stm_service = None
            if self.hmi_window is not None:
                self.hmi_window._stm_service = None
            return False
        
    def _set_start_busy(self, busy: bool) -> None:
        self._start_in_progress = bool(busy)

        btn = getattr(self.ui, "startProcess", None)
        if btn is not None and hasattr(btn, "setEnabled"):
            with contextlib.suppress(Exception):
                btn.setEnabled(not busy)

        stop_btn = getattr(self.ui, "stopProcess", None)
        if stop_btn is not None and hasattr(stop_btn, "setEnabled"):
            with contextlib.suppress(Exception):
                stop_btn.setEnabled(True)

        if busy:
            self._set_process_status("STM 점검중", "센서 연결 및 crystal 상태 확인")

    def _cleanup_start_worker(self) -> None:
        worker = self._start_worker
        if worker is None:
            return

        with contextlib.suppress(Exception):
            worker.sig_progress.disconnect(self._on_start_preflight_progress)
        with contextlib.suppress(Exception):
            worker.sig_result.disconnect(self._on_start_preflight_result)

        with contextlib.suppress(Exception):
            if worker.isRunning():
                worker.quit()
                worker.wait(200)

        self._start_worker = None

    def _start_async_preflight(self, run_cfg: dict[str, Any]) -> None:
        stm = self._stm_service
        if stm is None:
            QMessageBox.warning(self, "STM", "STM ?쒕퉬?ㅺ? 以鍮꾨릺吏 ?딆븯?듬땲??")
            self._close_process_run_log()
            self._clear_run_power_flags()
            self._active_run_id = None
            self._active_run_cfg = None
            self._active_run_profile = None
            return

        self._pending_run_cfg = dict(run_cfg)
        self._set_start_busy(True)

        cfg = STMPreflightConfig(
            ftm_settle_s=1.5,
            connected_timeout_s=8.0,
            health_timeout_s=3.0,
            poll_interval_ms=100,
            require_health_check=True,
            allow_skip_if_not_supported=True,
        )

        worker = ProcessStartWorker(stm=stm, config=cfg, parent=self)
        worker.sig_progress.connect(self._on_start_preflight_progress)
        worker.sig_result.connect(self._on_start_preflight_result)

        self._start_worker = worker
        worker.start()

    def _on_start_preflight_progress(self, text: str) -> None:
        self._append_process_log(f"[PRECHECK] {text}")
        self._set_process_status("STM 점검중", text)

    def _abort_start_preflight(self, *, show_warning: bool, title: str, message: str) -> None:
        with contextlib.suppress(Exception):
            self._rt_stop()

        with contextlib.suppress(Exception):
            self._shutdown_stm_with_ftm_off_best_effort()


        self._pending_run_cfg = None

        self._cleanup_start_worker()
        self._set_start_busy(False)

        if message:
            self._append_process_log(f"[PRECHECK][ABORT] {message}")
            self._set_process_status("STM 점검 실패", message)
        else:
            self._set_process_status("STM 점검 실패")

        self._close_process_run_log()
        self._active_run_id = None
        self._clear_run_power_flags()
        self._active_run_cfg = None
        self._active_run_profile = None

        if show_warning:
            QMessageBox.warning(self, title, message)

    def _on_start_preflight_result(self, result: STMPreflightResult) -> None:
        self._cleanup_start_worker()

        if bool(getattr(result, "cancelled", False)):
            self._abort_start_preflight(
                show_warning=False,
                title="Process",
                message="STM preflight媛 痍⑥냼?섏뿀?듬땲??",
            )
            return

        if not bool(getattr(result, "ok", False)):
            self._abort_start_preflight(
                show_warning=True,
                title="STM Pre-check",
                message=str(getattr(result, "message", "STM preflight ?ㅽ뙣")),
            )
            return

        try:
            if self._stm_service is not None:
                self._bind_stm_ui(self._stm_service)
        except Exception:
            pass

        self._rt_start()

        pc = self._process_controller
        run_cfg = self._pending_run_cfg or {}

        if pc is None or not hasattr(pc, "start_from_ui"):
            self._abort_start_preflight(
                show_warning=True,
                title="Process",
                message="start_from_ui媛 援ы쁽?섏뼱 ?덉? ?딆뒿?덈떎.",
            )
            return

        if self._plot is not None:
            try:
                adc_range = self._get_plot_adc_default_range()
                if hasattr(self._plot, 'set_power_default_range'):
                    self._plot.set_power_default_range(*adc_range)
                if hasattr(self._plot, 'reset_plot'):
                    self._plot.reset_plot()
            except Exception:
                pass

        try:
            pc.start_from_ui(run_cfg, run_id=self._active_run_id)
            self._append_process_log("[PRECHECK] STM preflight 완료 -> 공정 시작")
            self._set_process_status("공정 시작", "STM preflight 완료")
            self._pending_run_cfg = None
            self._set_start_busy(False)
        except Exception as e:
            with contextlib.suppress(Exception):
                self._emergency_safe_shutdown_plc_best_effort()

            self._abort_start_preflight(
                show_warning=True,
                title="Process Start Failed",
                message=f"{e!r}",
            )

    def _has_active_start_preflight(self) -> bool:
        worker = self._start_worker
        return bool(worker is not None and worker.isRunning())

    def _wait_start_preflight_stop(self, timeout_s: float = 3.0) -> bool:
        worker = self._start_worker
        if worker is None:
            return True

        deadline = time.monotonic() + max(0.1, float(timeout_s))

        while time.monotonic() < deadline:
            try:
                if not worker.isRunning():
                    return True
            except Exception:
                return True

            with contextlib.suppress(Exception):
                QApplication.processEvents()

            time.sleep(0.05)

        return not worker.isRunning()

    def _release_stm_runtime_only(self) -> None:
        try:
            self._unbind_stm_ui()
        except Exception:
            pass

        if self._stm_service is not None:
            try:
                self._stm_service.stop()
                self._append_process_log("[DEV] STM stop() called")
            except Exception as e:
                self._append_process_log(f"[DEV][WARN] STM stop failed: {e!r}")

        try:
            pc = self._process_controller
            if pc is not None and hasattr(pc, "replace_runtime_devices"):
                pc.replace_runtime_devices(stm=None, acs=self._acs_service)
        except Exception:
            pass

        self._stm_service = None
        if self.hmi_window is not None:
            self.hmi_window._stm_service = None

        gc.collect()
        self._append_process_log("[DEV] STM released + gc.collect() (ACS kept alive)")

    def _shutdown_stm_with_ftm_off_best_effort(self) -> None:
        self._release_stm_runtime_only()

        try:
            binder = getattr(self.hmi_window, "_plc_binder", None) if self.hmi_window else None
            if binder is not None:
                binder.enqueue_write("FTM_SW", False)
                self._append_process_log("[DEV] FTM_SW -> OFF")
        except Exception as e:
            self._append_process_log(f"[DEV][WARN] FTM_SW off failed: {e!r}")

    def _on_start_clicked(self) -> None:
        if self._start_in_progress:
            self._append_process_log("[UI] Start ignored: preflight already running")
            return

        pc = self._process_controller
        if pc is None:
            QMessageBox.warning(self, "Process", "ProcessController媛 ?곌껐?섏? ?딆븯?듬땲??")
            return

        try:
            if hasattr(pc, "is_running") and pc.is_running():
                QMessageBox.information(self, "Process", "?대? 怨듭젙???ㅽ뻾 以묒엯?덈떎.")
                return
        except Exception:
            pass


        run_cfg = self._collect_ui_run_cfg(require_process_name=True)
        if run_cfg is None:
            return

        try:
            run_profile = self._build_run_profile(run_cfg)
        except Exception as exc:
            QMessageBox.warning(self, "Process", f"Run profile build failed:\n{exc!r}")
            return

        if not run_profile:
            QMessageBox.warning(self, "Process", "Run profile is not available.")
            return
        
        self._set_process_status("공정 시작 ?붿껌", "PLC / STM pre-check 吏꾪뻾")

        self._latch_run_power_flags(run_cfg)
        self._active_run_cfg = dict(run_cfg)
        self._active_run_profile = dict(run_profile)


        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._open_process_run_log(run_id, run_cfg, run_profile)

        proc_cfg = run_cfg.get("process_config") or {}
        steps = proc_cfg.get("ramp_steps") or []

        step_desc = ""
        if steps:
            step_desc = " | ".join(
                f"S{idx+1}: ADC {float(s.get('target_adc', 0.0)):.1f}, "
                f"DAC+{int(s.get('dac_step', 0))}, "
                f"{float(s.get('dac_interval_sec', 0.0)):.1f}s, "
                f"hold {float(s.get('hold_sec', 0.0)):.1f}s"
                for idx, s in enumerate(steps)
            )

        if step_desc:
            self._append_process_log(f"[CFG] {step_desc}")

        self._append_process_log(
            "[CFG] "
            f"dac_max={proc_cfg.get('dac_max')} | "
            f"rate_tol_ratio={proc_cfg.get('rate_tol_ratio')} | "
            f"fine_step_dac={proc_cfg.get('fine_step_dac')} | "
            f"hold_mode={proc_cfg.get('hold_control_mode', 'PID')} | "
            f"hold_max_delta={proc_cfg.get('hold_max_dac_delta', proc_cfg.get('fine_step_dac'))}"
        )
        if run_profile.get("initial_dac") is not None:
            self._append_process_log(
                "[CFG] "
                f"initial_dac={run_profile.get('initial_dac')} | "
                f"source={run_profile.get('initial_dac_source') or 'runtime'}"
            )

        # 공정 시작 순서: PLC precheck → STM 서비스 준비 → async preflight(crystal health) → pc.start_from_ui
        if not self._check_plc_ready_before_start():
            self._close_process_run_log()
            self._clear_run_power_flags()
            self._active_run_id = None
            self._active_run_cfg = None
            self._active_run_profile = None
            return


        if not self._prepare_stm_service_for_start():
            QMessageBox.warning(self, "Device Connect Failed", "STM 점검 실패")
            with contextlib.suppress(Exception):
                self._shutdown_stm_with_ftm_off_best_effort()
            self._close_process_run_log()
            self._clear_run_power_flags()
            self._active_run_id = None
            self._active_run_cfg = None
            self._active_run_profile = None
            return


        self._start_async_preflight(run_cfg)
            
    def _on_stop_clicked(self) -> None:
        if self._has_active_start_preflight():
            worker = self._start_worker
            if worker is not None:
                with contextlib.suppress(Exception):
                    worker.request_cancel()
            self._append_process_log("[UI] STM preflight cancel requested")
            self._set_process_status("STM preflight cancel requested")
            return

        pc = self._process_controller

        if pc is not None:
            try:
                is_running = bool(pc.is_running()) if hasattr(pc, "is_running") else False
                if is_running:
                    pc.stop()
                    self._append_process_log("[UI] Stop requested -> waiting for engine safety shutdown")
                    self._set_process_status("Stop requested", "engine safety shutdown in progress")
                    return
            except Exception as e:
                self._append_process_log(f"[STOP FAIL] controller stop failed: {e!r}")

        try:
            self._emergency_safe_shutdown_plc_best_effort()
            self._append_process_log("[SAFE] emergency fallback shutdown executed")
            self._set_process_status("Emergency stop executed", "PLC fallback shutdown")
        except Exception as e:
            self._append_process_log(f"[SAFE][FAIL] emergency shutdown failed: {e!r}")
            self._set_process_status("Emergency stop failed", f"{e!r}")

        with contextlib.suppress(Exception):
            self._rt_stop()
        with contextlib.suppress(Exception):
            self._shutdown_stm_with_ftm_off_best_effort()
        with contextlib.suppress(Exception):
            self._reset_process_ui(reset_monitor=False)

        self._close_process_run_log()
        self._active_run_id = None
        self._active_run_cfg = None
        self._active_run_profile = None

    def _on_status(self, st: Any) -> None:
        self._try_update_last_power(st)

        try:
            self._set_process_monitor_text(self._build_process_status_summary(st))
        except Exception:
            pass

    @staticmethod
    def _status_value(st: Any, name: str, default: Any = None) -> Any:
        if isinstance(st, dict):
            return st.get(name, default)
        return getattr(st, name, default)

    def _split_status_message(self, st: Any) -> tuple[str, list[str], list[str], list[str]]:
        msg = str(self._status_value(st, "message", "") or "").strip()
        if not msg:
            return "", [], [], []

        time_parts: list[str] = []
        while True:
            m = re.match(r"^\[([^\]]+)\]\s*(.*)$", msg)
            if not m:
                break
            time_text = str(m.group(1) or "").strip()
            if time_text:
                time_parts.append(time_text)
            msg = str(m.group(2) or "").strip()
            if not msg:
                break

        action = ""
        value_parts: list[str] = []
        extra_parts: list[str] = []

        for raw_part in [part.strip() for part in msg.split("|") if str(part).strip()]:
            part = str(raw_part or "").strip()
            if not part:
                continue
            if self._is_status_time_part(part):
                cleaned = re.sub(r"^\[(.*)\]$", r"\1", part).strip()
                time_parts.append(cleaned or part)
                continue
            if self._is_status_value_part(part):
                value_parts.append(part)
                continue
            if not action:
                action = part
            else:
                extra_parts.append(part)

        return action, value_parts, time_parts, extra_parts

    @staticmethod
    def _format_status_phase(phase: Any) -> str:
        raw = str(getattr(phase, "value", phase) or "").strip().upper()
        return {
            "IDLE": "대기",
            "RUNNING": "실행중",
            "STOPPING": "정지중",
            "FINISHED": "완료",
            "ERROR": "오류",
        }.get(raw, raw.replace("_", " ").title() if raw else "---")

    @staticmethod
    def _format_status_step(step_idx: Any, step_name: Any) -> str:
        name = str(step_name or "").strip()
        try:
            idx = int(step_idx)
        except Exception:
            idx = -1

        if idx >= 0 and name:
            return f"#{idx + 1} {name}"
        if idx >= 0:
            return f"#{idx + 1}"
        return name

    @staticmethod
    def _same_status_text(left: str, right: str) -> bool:
        def _norm(text: str) -> str:
            s = str(text or "").strip().upper().replace("_", " ")
            return re.sub(r"\s+", " ", s)

        return bool(left and right and _norm(left) == _norm(right))

    @staticmethod
    def _is_status_value_part(part: str) -> bool:
        text = str(part or "").strip()
        if not text:
            return False
        upper = text.upper()
        return bool(
            re.match(r"^(ADC\d*|DAC\d*|RATE|THICKNESS|PRESSURE)\b", upper)
            or re.match(r"^(ADC\d*|DAC\d*|RATE|THICKNESS|PRESSURE)\s*[:=]", upper)
        )

    @staticmethod
    def _is_status_time_part(part: str) -> bool:
        text = str(part or "").strip()
        if not text:
            return False
        if re.fullmatch(r"\[[^\]]+\]", text):
            return True
        if not re.search(r"\d", text):
            return False
        keywords = ("남은", "경과", "다음", "초", "sec", "secs", "second", "seconds", "ms", "min", "mins", "minute", "minutes", "분")
        lower = text.lower()
        return any(keyword in text or keyword in lower for keyword in keywords)

    def _build_status_value_parts(self, st: Any, message_value_parts: list[str]) -> list[str]:
        if message_value_parts:
            return [str(part).strip() for part in message_value_parts if str(part).strip()][:4]

        parts: list[str] = []

        pressure = self._to_float_or_none(self._status_value(st, "pressure", None))
        thickness = self._to_float_or_none(self._status_value(st, "thickness_a", None))
        rate = self._to_float_or_none(self._status_value(st, "rate_a_s", None))

        if pressure is not None:
            parts.append(f"pressure={pressure:.2e} Torr")
        if thickness is not None:
            parts.append(f"thickness={thickness:.1f} A")
        if rate is not None:
            parts.append(f"rate={rate:.3f} A/s")

        for label, value, fmt in (
            ("ADC1", self._status_value(st, "adc1", None), "{:.1f}"),
            ("ADC2", self._status_value(st, "adc2", None), "{:.1f}"),
            ("DAC1", self._status_value(st, "dac1", None), "{:.0f}"),
            ("DAC2", self._status_value(st, "dac2", None), "{:.0f}"),
        ):
            number = self._to_float_or_none(value)
            if number is not None:
                parts.append(f"{label}={fmt.format(number)}")

        return parts[:4]

    def _build_process_status_summary(self, st: Any) -> str:
        phase_text = self._format_status_phase(self._status_value(st, "phase", ""))
        step_text = self._format_status_step(
            self._status_value(st, "step_idx", -1),
            self._status_value(st, "step_name", self._status_value(st, "step", "")),
        )
        action, value_parts, time_parts, extra_parts = self._split_status_message(st)

        lines: list[str] = [phase_text or "---"]

        if step_text:
            lines.append(f"단계: {step_text}")

        action_parts: list[str] = []
        if action and not self._same_status_text(action, phase_text) and not self._same_status_text(action, step_text):
            action_parts.append(action)
        for extra in extra_parts:
            if len(action_parts) >= 3:
                break
            if not self._same_status_text(extra, phase_text) and not self._same_status_text(extra, step_text):
                action_parts.append(extra)

        if action_parts:
            lines.append(f"동작: {' | '.join(action_parts)}")

        current_parts = self._build_status_value_parts(st, value_parts)
        tail_parts: list[str] = []
        if current_parts:
            tail_parts.append(f"현재값: {' | '.join(current_parts[:4])}")
        if time_parts:
            tail_parts.append(f"시간: {' | '.join([part for part in time_parts if part][:2])}")

        if tail_parts:
            lines.append(" | ".join(tail_parts))

        if len(lines) == 1:
            raw_msg = str(self._status_value(st, "message", "") or "").strip()
            if raw_msg:
                lines.append(f"동작: {raw_msg}")

        return "\n".join(lines[:4])

    def _get_plot_adc_default_range(self) -> tuple[float, float]:
        cfg = self._normalize_process_config(self._process_cfg)

        candidates: list[float] = []
        for step in cfg.get("ramp_steps") or []:
            try:
                v = float((step or {}).get("target_adc", 0.0) or 0.0)
                if v > 0:
                    candidates.append(v)
            except Exception:
                pass

        y_max = max(candidates, default=100.0)
        # adc_max가 설정되어 있으면 그 값도 참고
        adc_max_cfg = float(self._process_cfg.get("adc_max", 200) or 200)
        y_max = max(100.0, y_max * 1.10, adc_max_cfg)
        return (0.0, y_max)

    def _try_update_last_power(self, st: Any) -> None:
        """
        洹몃옒?꾩슜 power???댁젣 ADC total ?곗꽑.
        ?꾩쭅 engine.py 媛 adc瑜?status?????ｋ뒗 ?숈븞? DAC fallback ?덉슜.
        """
        def _set_pair(v1: Any, v2: Any) -> bool:
            graph_power, _display_adc1, _display_adc2 = self._resolve_power_feedback_for_ui(
                self._to_float_or_none(v1),
                self._to_float_or_none(v2),
            )
            if graph_power is None:
                return False
            self._last_power = float(graph_power)
            return True

        try:
            # dict ?뺥깭
            if isinstance(st, dict):
                for k in ("adc_total", "power_actual", "actual_power", "power_read", "adc"):
                    if k in st and st[k] is not None:
                        self._last_power = float(st[k])
                        return

                if ("adc1" in st) or ("adc2" in st):
                    if _set_pair(st.get("adc1"), st.get("adc2")):
                        return


                for k in ("power", "power_dac", "dac", "dac_power", "power_cmd", "dac_cmd"):
                    if k in st and st[k] is not None:
                        self._last_power = float(st[k])
                        return

                if ("dac1" in st) or ("dac2" in st):
                    if _set_pair(st.get("dac1"), st.get("dac2")):
                        return
                return


            for name in ("adc_total", "power_actual", "actual_power", "power_read", "adc"):
                if hasattr(st, name):
                    v = getattr(st, name)
                    if v is not None:
                        self._last_power = float(v)
                        return

            if hasattr(st, "adc1") or hasattr(st, "adc2"):
                if _set_pair(getattr(st, "adc1", None), getattr(st, "adc2", None)):
                    return

            # fallback
            for name in ("power", "power_dac", "dac", "dac_power", "power_cmd", "dac_cmd"):
                if hasattr(st, name):
                    v = getattr(st, name)
                    if v is not None:
                        self._last_power = float(v)
                        return

            if hasattr(st, "dac1") or hasattr(st, "dac2"):
                _set_pair(getattr(st, "dac1", None), getattr(st, "dac2", None))

        except Exception:
            return

    def _on_error(self, err: Any) -> None:
        try:
            where = str(getattr(err, "where", "") or "").strip()
            msg = str(getattr(err, "message", "") or "").strip()

            self._append_process_log(f"[ERROR] {where} | {msg}")

            detail = f"{where} | {msg}" if where and msg else (where or msg or "상세 메시지 없음")
            self._set_process_status("에러 발생", detail)

        except Exception:
            self._append_process_log(f"[ERROR] {err!r}")
            self._set_process_status("에러 발생", f"{err!r}")

    def _on_finished(self, result: Any) -> None:
        run_profile = dict(self._active_run_profile or {})

        try:
            with contextlib.suppress(Exception):
                self._rt_stop()

            with contextlib.suppress(Exception):
                self._release_stm_runtime_only()

            try:
                ok = bool(getattr(result, "ok", False))
                rid = str(getattr(result, "run_id", "") or "").strip()

                current_status = ""
                try:
                    w = getattr(self.ui, "processMonitor_Process", None)
                    if w is not None and hasattr(w, "text"):
                        current_status = str(w.text() or "").strip()
                except Exception:
                    current_status = ""

                if ok:
                    self._append_process_log(f"[FINISHED][OK] run_id={rid}")
                    self._set_process_status("공정 완료", f"run_id={rid}" if rid else "")
                    ls = self._log_service
                    if ls is not None and hasattr(ls, "mark_run_success"):
                        with contextlib.suppress(Exception):
                            _run_cfg = self._active_run_cfg or {}
                            ls.mark_run_success(
                                material_name=_run_cfg.get("material_name", ""),
                                target_rate=float(_run_cfg.get("target_rate", 0.0) or 0.0),
                            )
                            self._append_process_log("[FINISHED] 성공 로그 파일 SUCCESS prefix 적용")
                else:
                    self._append_process_log(f"[FINISHED][ABNORMAL] run_id={rid}")


                    if not current_status.startswith("에러 발생"):
                        if rid:
                            self._set_process_status("怨듭젙 醫낅즺", f"run_id={rid}")
                        else:
                            self._set_process_status("怨듭젙 醫낅즺", "?뺤? ?먮뒗 鍮꾩젙??醫낅즺")

            except Exception as e:
                self._append_process_log(f"[FINISHED][WARN] finalize failed: {e!r}")
                self._set_process_status("怨듭젙 醫낅즺")

        finally:
            with contextlib.suppress(Exception):
                self._close_process_run_log()

            with contextlib.suppress(Exception):
                ls = self._log_service
                if ls is not None and hasattr(ls, "flush"):
                    ls.flush(timeout_ms=3000)

            with contextlib.suppress(Exception):
                self._store_finished_run_summary(run_profile=run_profile, result=result)

            self._active_run_id = None
            self._active_run_cfg = None
            self._active_run_profile = None
            self._clear_run_power_flags()

            with contextlib.suppress(Exception):
                self._reset_process_ui(reset_monitor=False)

    def _wait_process_stop(self, timeout_s: float = 3.0) -> bool:
        """
        closeEvent?먯꽌 stop ?붿껌 ??怨듭젙 worker媛 ?ㅼ젣濡?硫덉톬?붿?
        吏㏐쾶 湲곕떎由곕떎.
        """
        pc = self._process_controller
        if pc is None:
            return True

        is_running_fn = getattr(pc, "is_running", None)
        if not callable(is_running_fn):
            return True

        deadline = time.monotonic() + max(0.1, float(timeout_s))

        while time.monotonic() < deadline:
            try:
                if not bool(is_running_fn()):
                    return True
            except Exception:
                return True

            with contextlib.suppress(Exception):
                QApplication.processEvents()

            time.sleep(0.05)

        return False
    
    def _estimate_stop_wait_timeout_s(self) -> float:
        """
        closeEvent?먯꽌 stop ?붿껌 ???쇰쭏??湲곕떎由댁? 怨꾩궛.
        engine shutdown ramp 湲곗?:
        - 1珥덈쭏??100 媛먯냼
        - max(dac1, dac2) 湲곗??쇰줈 ?湲곗떆媛?異붿젙
        - ?ъ쑀 踰꾪띁 異붽?
        """
        dac1 = 0.0
        dac2 = 0.0

        try:
            d1, d2 = self._read_plc_power_dac_pair()
            dac1 = float(d1 or 0.0)
            dac2 = float(d2 or 0.0)
        except Exception:
            pass

        max_dac = max(dac1, dac2, 0.0)
        ramp_s = max_dac / 100.0
        timeout_s = ramp_s + 5.0
        return max(3.0, min(timeout_s, 30.0))

    def closeEvent(self, event):

        if not getattr(self, "_close_stop_guard", False):
            self._close_stop_guard = True
            try:
                self._on_stop_clicked()
            except Exception:
                pass


        if self._has_active_start_preflight():
            stopped = True
            try:
                stopped = self._wait_start_preflight_stop(timeout_s=5.0)
            except Exception:
                stopped = True

            if not stopped:
                self._append_process_log("[UI][WARN] STM preflight stop wait timeout -> close canceled")
                QMessageBox.warning(
                    self,
                    "Process",
                    "STM 以鍮??묒뾽???꾩쭅 醫낅즺?섏? ?딆븯?듬땲??\n"
                    "?좎떆 ???ㅼ떆 ?レ븘二쇱꽭??"
                )
                self._close_stop_guard = False
                event.ignore()
                return


        timeout_s = 3.0
        try:
            timeout_s = self._estimate_stop_wait_timeout_s()
        except Exception:
            timeout_s = 8.0

        stopped = True
        try:
            stopped = self._wait_process_stop(timeout_s=timeout_s)
        except Exception:
            stopped = True

        if not stopped:
            self._append_process_log(f"[UI][WARN] Process stop wait timeout ({timeout_s:.1f}s) -> close canceled")
            QMessageBox.warning(
                self,
                "Process",
                "怨듭젙 ?뺤? ?꾨즺瑜??꾩쭅 ?뺤씤?섏? 紐삵뻽?듬땲??\n"
                "DAC ramp-down 醫낅즺 ???ㅼ떆 ?レ븘二쇱꽭??"
            )
            self._close_stop_guard = False
            event.ignore()
            return

        if self.hmi_window is not None:
            self.hmi_window.process_window = None

        event.accept()

    def _open_material_dialog(self, channel: int) -> None:
        sel = MaterialCatalogDialog.pick(base_dir=_BASE_DIR, parent=self)
        if not sel:
            return


        data = {
            "material": getattr(sel, "material", ""),
            "density_g_cm3": getattr(sel, "density_g_cm3", 0.0),
            "z_factor": getattr(sel, "z_factor", 0.0),
            "note": getattr(sel, "note", ""),
        }

        self._apply_material(channel, data)

    def _apply_material(self, channel: int, data: dict[str, Any]) -> None:
        mat = str(data.get("material", "")).strip()

        if channel == 1:
            self._material_1 = dict(data)
            self.ui.materialEdit.setText(mat or "Select")
        else:
            self._material_2 = dict(data)
            self.ui.materialEdit2.setText(mat or "Select")

    def _default_process_config(self) -> dict[str, Any]:
        return {
            "step_count": 1,
            "ramp_steps": [
                {
                    "target_adc": 100.0,
                    "dac_step": 10,
                    "dac_interval_sec": 30.0,
                    "hold_sec": 0.0,
                }
            ],
            "dac_max": 4000,
            "rate_tol_ratio": 0.05,
            "rate_stable_sec": 3.0,
            "hold_control_interval_s": 1.0,
            "fine_step_dac": 10,
            "hold_control_mode": "PID",
            "hold_pi_kp": 50.0,
            "hold_pi_ki": 8.0,
            "hold_integral_limit": 2.5,
            "rate_filter_alpha": 0.35,
            "rate_jump_guard_ratio": 0.50,
            "rate_jump_guard_abs": 0.15,
            "hold_max_dac_delta": 10,
            "rate_abort_ratio": 0.30,
            "rate_abort_sec": 5.0,
            "sensor_none_abort_s": 5.0,
            "adc_none_abort_s": 5.0,
        }

    def _normalize_process_config(self, cfg: Any) -> dict[str, Any]:
        def _as_int(src: dict[str, Any], name: str, default_val: int,
                    min_v: int | None = None, max_v: int | None = None) -> int:
            try:
                v = int(src.get(name, default_val))
            except Exception:
                v = int(default_val)
            if min_v is not None:
                v = max(min_v, v)
            if max_v is not None:
                v = min(max_v, v)
            return v

        def _as_float(src: dict[str, Any], name: str, default_val: float,
                    min_v: float | None = None, max_v: float | None = None) -> float:
            try:
                v = float(src.get(name, default_val))
            except Exception:
                v = float(default_val)
            if min_v is not None:
                v = max(min_v, v)
            if max_v is not None:
                v = min(max_v, v)
            return v

        default = self._default_process_config()
        src = dict(cfg or {})

        raw_steps = src.get("ramp_steps") or default["ramp_steps"]
        steps: list[dict[str, Any]] = []

        for item in list(raw_steps)[:10]:
            d = dict(item or {})
            try:
                target_adc = max(0.0, float(d.get("target_adc", 0.0)))
            except Exception:
                target_adc = 0.0

            try:
                dac_step = max(1, int(d.get("dac_step", 10)))
            except Exception:
                dac_step = 10

            try:
                dac_interval_sec = max(0.1, float(d.get("dac_interval_sec", 30.0)))
            except Exception:
                dac_interval_sec = 30.0

            try:
                hold_src = d.get("hold_sec", d.get("delay_s", 0.0))   # delay_s留??명솚 ?덉슜
                hold_sec = max(0.0, float(hold_src))
            except Exception:
                hold_sec = 0.0

            steps.append({
                "target_adc": target_adc,
                "dac_step": dac_step,
                "dac_interval_sec": dac_interval_sec,
                "hold_sec": hold_sec,
            })

        if not steps:
            steps = list(default["ramp_steps"])

        step_count = src.get("step_count", len(steps))
        try:
            step_count = int(step_count)
        except Exception:
            step_count = len(steps)

        step_count = max(1, min(10, step_count))
        steps = steps[:step_count]

        fine_step_dac = _as_int(src, "fine_step_dac", 10, 1)
        hold_max_dac_delta = _as_int(src, "hold_max_dac_delta", fine_step_dac, 1)
        hold_pi_kp = _as_float(src, "hold_pi_kp", max(1.0, hold_max_dac_delta * 5.0), 0.0)
        hold_pi_ki = _as_float(src, "hold_pi_ki", max(0.0, hold_max_dac_delta * 0.8), 0.0)
        hold_integral_limit = _as_float(
            src,
            "hold_integral_limit",
            max(1.0, (2.0 * hold_max_dac_delta) / max(hold_pi_ki, 1e-6)),
            0.1,
        )
        hold_control_mode = str(src.get("hold_control_mode", default["hold_control_mode"]) or "").strip().upper() or "PID"
        if hold_control_mode not in {"PI", "PID", "STEP"}:
            hold_control_mode = "PID"

        return {
            "step_count": len(steps),
            "ramp_steps": steps,
            "dac_max": _as_int(src, "dac_max", 4000, 1),
            "rate_tol_ratio": _as_float(src, "rate_tol_ratio", 0.05, 0.001, 1.0),
            "rate_stable_sec": _as_float(src, "rate_stable_sec", 3.0, 0.0),
            "hold_control_interval_s": _as_float(src, "hold_control_interval_s", 1.0, 0.1),
            "fine_step_dac": fine_step_dac,
            "hold_control_mode": hold_control_mode,
            "hold_pi_kp": hold_pi_kp,
            "hold_pi_ki": hold_pi_ki,
            "hold_integral_limit": hold_integral_limit,
            "rate_filter_alpha": _as_float(src, "rate_filter_alpha", 0.35, 0.01, 1.0),
            "rate_jump_guard_ratio": _as_float(src, "rate_jump_guard_ratio", 0.50, 0.0),
            "rate_jump_guard_abs": _as_float(src, "rate_jump_guard_abs", 0.15, 0.0),
            "hold_max_dac_delta": hold_max_dac_delta,
            "rate_abort_ratio": _as_float(src, "rate_abort_ratio", 0.30, 0.001, 1.0),
            "rate_abort_sec": _as_float(src, "rate_abort_sec", 5.0, 0.0),
            "sensor_none_abort_s": _as_float(src, "sensor_none_abort_s", 5.0, 0.0),
            "adc_none_abort_s": _as_float(src, "adc_none_abort_s", 5.0, 0.0),
        }

    def _open_process_config_dialog(self) -> None:
        try:
            from ui.process_config_dialog import ProcessConfigDialog
        except Exception as e:
            QMessageBox.warning(
                self,
                "Process Config",
                f"Process Config dialog import failed:\n{e!r}"
            )
            self._append_process_log(f"[CFG][ERR] dialog import failed: {e!r}")
            return

        try:
            dlg = ProcessConfigDialog(initial_config=dict(self._process_cfg), parent=self)
        except TypeError:
            dlg = ProcessConfigDialog(parent=self, initial_config=dict(self._process_cfg))

        if not dlg.exec():
            return

        getter = getattr(dlg, "get_config", None)
        if callable(getter):
            new_cfg = getter()
        else:
            new_cfg = getattr(dlg, "config", None)
        self._clear_recommendation_runtime_overrides()
        self._apply_process_config(new_cfg, log_prefix="[CFG] Process config updated |")

    def _open_process_recipe_dialog(self) -> None:
        try:
            from ui.process_recipe_dialog import ProcessRecipeDialog
        except Exception as exc:
            QMessageBox.warning(self, "Recipe", f"Recipe dialog import failed:\n{exc!r}")
            self._append_process_log(f"[RECIPE][ERR] dialog import failed: {exc!r}")
            return

        dlg = ProcessRecipeDialog(
            initial_config=dict(self._process_cfg),
            recommend_callback=self._request_recommendation_for_config,
            history_callback=self._run_history_rebuild,
            parent=self,
        )
        if not dlg.exec():
            return

        self._apply_process_config(dlg.get_config(), log_prefix="[RECIPE] Recipe updated |")
        self._last_recommendation = dlg.last_recommendation()

    def _request_recommendation_for_config(
        self,
        process_config: dict[str, Any],
        parent: Optional[QWidget] = None,
    ) -> Optional[dict[str, Any]]:
        owner = parent or self
        if self._recommendation_service is None or not hasattr(self._recommendation_service, "recommend"):
            QMessageBox.information(owner, "Recommendation", "Recommendation service is not available.")
            return None

        run_cfg = self._collect_ui_run_cfg(require_process_name=False, require_thickness=False, process_cfg_override=process_config)
        if run_cfg is None:
            return None

        try:
            run_profile = self._build_run_profile(run_cfg)
        except Exception as exc:
            QMessageBox.warning(owner, "Recommendation", f"Run profile build failed:\n{exc!r}")
            return None

        if not run_profile:
            QMessageBox.warning(owner, "Recommendation", "Run profile is not available.")
            return None

        try:
            recommendation = self._recommendation_service.recommend(run_profile)
        except Exception as exc:
            QMessageBox.warning(owner, "Recommendation", f"Recommendation query failed:\n{exc!r}")
            self._append_process_log(f"[RECO][ERR] query failed: {exc!r}")
            return None

        if not recommendation:
            self._append_process_log("[RECO] no recommendation data")
            material_name = str((run_profile or {}).get("material_name", "") or "미지정").strip()
            reply = QMessageBox.question(
                owner,
                "이전 공정 기록 없음",
                f"'{material_name}' 소재의 공정 기록을 찾을 수 없습니다.\n\n"
                "로그 스캔을 실행하면 NAS에 저장된 공정 기록을 불러올 수 있습니다.\n"
                "지금 로그 스캔을 실행하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._run_history_rebuild(parent=owner)
            return None

        try:
            from ui.history_recommendation_dialog import HistoryRecommendationDialog
        except Exception as exc:
            QMessageBox.warning(owner, "Recommendation", f"Dialog import failed:\n{exc!r}")
            return None

        dlg = HistoryRecommendationDialog(recommendation=recommendation, parent=owner)
        if not dlg.exec():
            return None

        if not dlg.applied():
            self._append_process_log("[RECO] recommendation viewed without apply")
            return None

        recommended_cfg = dlg.recommended_process_config()
        if not recommended_cfg:
            QMessageBox.warning(owner, "Recommendation", "Recommended config is empty.")
            return None

        self._append_process_log("[RECO] recommendation prepared for recipe apply")
        return {
            "process_config": self._normalize_process_config(recommended_cfg),
            "recommendation": dict(recommendation),
        }

    def _run_history_rebuild(self, parent: Optional[QWidget] = None) -> Optional[dict[str, Any]]:
        owner = parent or self
        svc = self._run_summary_service
        if svc is None or not hasattr(svc, "backfill_history"):
            QMessageBox.information(owner, "History Rebuild", "History rebuild service is not available.")
            return None

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = dict(svc.backfill_history() or {})
        except Exception as exc:
            QMessageBox.warning(owner, "History Rebuild", f"History rebuild failed:\n{exc!r}")
            self._append_process_log(f"[HISTORY][ERR] rebuild failed: {exc!r}")
            return None
        finally:
            QApplication.restoreOverrideCursor()

        scanned = int(result.get("scanned", 0) or 0)
        unchanged_skipped = int(result.get("unchanged_skipped", result.get("skipped", 0)) or 0)
        changed = int(result.get("changed", 0) or 0)
        stored = int(result.get("stored", 0) or 0)
        updated = int(result.get("updated", 0) or 0)
        failed = int(result.get("failed", 0) or 0)

        self._append_process_log(
            "[HISTORY] sync complete | "
            f"scanned={scanned} | unchanged={unchanged_skipped} | changed={changed} | "
            f"stored={stored} | updated={updated} | failed={failed}"
        )

        errors = list(result.get("errors") or [])
        detail = ""
        if errors:
            detail = "\n\nErrors:\n" + "\n".join(str(err) for err in errors[:10])

        if failed == 0:
            msg = (
                f"로그 스캔이 완료되었습니다.\n\n"
                f"새로 추가: {stored}건  |  갱신: {updated}건  |  변동없음: {unchanged_skipped}건\n"
                f"(전체 스캔: {scanned}건)\n\n"
                "이제 Recipe 창에서 '이전 설정 불러오기'를 눌러보세요."
            )
        else:
            msg = (
                f"로그 스캔이 완료되었습니다. (일부 오류)\n\n"
                f"새로 추가: {stored}건  |  갱신: {updated}건\n"
                f"실패: {failed}건 (일부 로그 파일을 읽지 못했습니다)"
            )
        QMessageBox.information(owner, "로그 스캔 완료", msg)
        return result

    def _init_rt_plot(self) -> None:
        """ui.graphWidget ?먮━??DepositionPlotWidget???쎌엯"""
        host = getattr(self.ui, "graphWidget", None)
        if host is None:
            return


        lay = host.layout()
        if lay is None:
            lay = QVBoxLayout(host)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
            host.setLayout(lay)


        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        adc_range = self._get_plot_adc_default_range()
        self._plot = DepositionPlotWidget(
            parent=host,
            power_title="ADC",
            power_default_range=adc_range,
        )
        lay.addWidget(self._plot)

    def _rt_start(self) -> None:
        if self._plot is not None:
            try:
                self._plot.clear()
            except Exception:
                pass
        if not self._rt_timer.isActive():
            self._rt_timer.start()

    def _rt_stop(self) -> None:
        if self._rt_timer.isActive():
            self._rt_timer.stop()

    def _tick_rt_ui(self) -> None:
        """1珥덈쭏??lineedit + 洹몃옒??媛깆떊"""
        rate = self._to_float_or_none(self._last_rate)


        dac1, dac2 = self._read_plc_power_dac_pair()
        adc1, adc2 = self._read_plc_power_actual_pair()

        graph_power, display_adc1, display_adc2 = self._resolve_power_feedback_for_ui(adc1, adc2)

        if graph_power is not None:
            self._last_power = graph_power
        else:
            self._last_power = None

        show_th = self._is_main_deposition()
        th = self._to_float_or_none(self._last_thickness) if show_th else None

        # 1) rate/thickness ?쒖떆
        try:
            self.ui.currentRateEdit.setText(f"{rate:.3f}" if rate is not None else "---")
        except Exception:
            pass

        try:
            self.ui.currentThicknessEdit.setText(f"{th:.1f}" if th is not None else "---")
        except Exception:
            pass

        # 2) DAC / actual power ?쒖떆
        self._update_dac_power_ui(dac1, dac2)
        self._update_actual_power_ui(display_adc1, display_adc2)


        if self._plot is not None:
            try:
                self._plot.append(
                    rate=rate,
                    power=self._last_power,
                )
            except Exception:
                pass

    def _resolve_power_feedback_for_ui(
        self,
        adc1: Optional[float],
        adc2: Optional[float],
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """
        諛섑솚媛?
            graph_power, display_adc1, display_adc2
        """
        use1, use2 = self._selected_power_flags()

        # ?꾩옱 ?섎뱶?⑥뼱 ?꾩떆 ?고쉶:

        if use1 and not use2 and self._power1_feedback_uses_adc2:
            fb = self._to_float_or_none(adc2)
            return fb, None, fb

        graph_power = self._sum_selected_pair(
            self._to_float_or_none(adc1),
            self._to_float_or_none(adc2),
        )
        return graph_power, self._to_float_or_none(adc1), self._to_float_or_none(adc2)


    @staticmethod
    def _clamp_nonneg(v: Optional[float]) -> Optional[float]:
        """?뚯닔??0?쇰줈 ?대옩?? (None? 洹몃?濡?"""
        if v is None:
            return None
        try:
            fv = float(v)
        except Exception:
            return None
        return 0.0 if fv < 0 else fv

    @staticmethod
    def _to_float_or_none(v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        try:
            return float(v)
        except Exception:
            return None

    def _is_main_deposition(self) -> bool:
        """硫붿씤 怨듭젙(硫붿씤 ?뷀꽣 OPEN)???뚮쭔 True."""
        pc = self._process_controller
        try:
            if pc is None or (not pc.is_running()):
                return False
        except Exception:
            return False

        if self.hmi_window is None:
            return False
        binder = getattr(self.hmi_window, "_plc_binder", None)
        if binder is None:
            return False

        try:
            return bool(binder.read_coil("MAIN_SHUTTER_SW", default=False))
        except Exception:
            return False
        
    def _selected_power_flags(self) -> tuple[bool, bool]:
        """
        怨듭젙/preflight 以묒뿉???쒖옉 ?쒖젏??latch??power ?좏깮 ?곹깭瑜??ъ슜?쒕떎.
        idle ?곹깭?먯꽌留??꾩옱 UI 泥댄겕諛뺤뒪 媛믪쓣 ?쎈뒗??
        """
        if self._run_use_power1 is not None and self._run_use_power2 is not None:
            return bool(self._run_use_power1), bool(self._run_use_power2)

        use1 = bool(getattr(getattr(self.ui, "sourcePower1", None), "isChecked", lambda: False)())
        use2 = bool(getattr(getattr(self.ui, "sourcePower2", None), "isChecked", lambda: False)())
        return use1, use2
    
    def _latch_run_power_flags(self, run_cfg: dict[str, Any]) -> None:
        self._run_use_power1 = bool(run_cfg.get("use_power1", False))
        self._run_use_power2 = bool(run_cfg.get("use_power2", False))

    def _clear_run_power_flags(self) -> None:
        self._run_use_power1 = None
        self._run_use_power2 = None

    def _sum_selected_pair(self, p1: Optional[float], p2: Optional[float]) -> Optional[float]:
        """
        ?좏깮??power 湲곗??쇰줈 ?⑹궛.
        - 1媛??좏깮: ?대떦 梨꾨꼸 媛?
        - 2媛??좏깮: ?⑷퀎
        """
        use1, use2 = self._selected_power_flags()

        vals: list[float] = []
        if use1 and p1 is not None:
            vals.append(float(p1))
        if use2 and p2 is not None:
            vals.append(float(p2))

        if not vals:
            return None

        return sum(vals)


    def _read_plc_power_dac_pair(self) -> tuple[Optional[float], Optional[float]]:
        """
        PLC snapshot?먯꽌 DAC command 2梨꾨꼸??媛곴컖 ?쎈뒗??
        """
        if self.hmi_window is None:
            return None, None

        binder = getattr(self.hmi_window, "_plc_binder", None)
        if binder is None:
            return None, None

        try:
            plc = binder.get_plc_service()
            snap = plc.get_last_snapshot() if plc is not None else None
            if snap is None:
                return None, None

            if not bool(getattr(snap, "connected", False)):
                return None, None

            regs = getattr(snap, "regs", None) or {}
            p1 = self._clamp_nonneg(regs.get("DAC_POWER_1", None))
            p2 = self._clamp_nonneg(regs.get("DAC_POWER_2", None))
            return p1, p2

        except Exception:
            return None, None


    def _update_dac_power_ui(self, p1: Optional[float], p2: Optional[float]) -> None:
        """
        DAC ?쒖떆移?currentDac1Edit/currentDac2Edit)??媛?異쒕젰.
        怨듭젙/preflight 以묒뿉???꾩옱 泥댄겕諛뺤뒪 ?곹깭媛 ?꾨땲??run ?쒖옉 ?쒖젏 latch 湲곗??쇰줈 ?쒖떆?쒕떎.
        """
        use1, use2 = self._selected_power_flags()

        t1 = "---"
        t2 = "---"

        if use1:
            t1 = f"{p1:.0f}" if p1 is not None else "---"
        if use2:
            t2 = f"{p2:.0f}" if p2 is not None else "---"

        try:
            self.ui.currentDac1Edit.setText(t1)
        except Exception:
            pass

        try:
            self.ui.currentDac2Edit.setText(t2)
        except Exception:
            pass
        
    def _convert_power_read_to_amp(self, raw: Optional[float]) -> Optional[float]:
        """
        plc_service.py?먯꽌 ?대? sanitize + scale ??媛믪쓣 洹몃?濡??ъ슜?쒕떎.
        ??
            0.0
            100.0
            253.4
        """
        if raw is None:
            return None
        try:
            return float(raw)
        except Exception:
            return None

    def _read_plc_power_actual_pair(self) -> tuple[Optional[float], Optional[float]]:
        """
        PLC snapshot?먯꽌 actual power readback 2梨꾨꼸???쎈뒗??
        """
        if self.hmi_window is None:
            return None, None

        binder = getattr(self.hmi_window, "_plc_binder", None)
        if binder is None:
            return None, None

        try:
            plc = binder.get_plc_service()
            snap = plc.get_last_snapshot() if plc is not None else None
            if snap is None:
                return None, None

            if not bool(getattr(snap, "connected", False)):
                return None, None

            regs = getattr(snap, "regs", None) or {}
            raw1 = regs.get("POWER_READ_1", None)
            raw2 = regs.get("POWER_READ_2", None)

            p1 = self._convert_power_read_to_amp(raw1)
            p2 = self._convert_power_read_to_amp(raw2)
            return p1, p2

        except Exception:
            return None, None

    def _update_actual_power_ui(self, p1: Optional[float], p2: Optional[float]) -> None:
        """
        湲곗〈 actualPower1/2 ??ADC ?쒖떆移몄쑝濡??ъ슜.
        怨듭젙/preflight 以묒뿉???꾩옱 泥댄겕諛뺤뒪 ?곹깭媛 ?꾨땲??run ?쒖옉 ?쒖젏 latch 湲곗??쇰줈 ?쒖떆?쒕떎.
        """
        use1, use2 = self._selected_power_flags()

        t1 = "---"
        t2 = "---"

        if use1:
            t1 = f"{p1:.1f}" if p1 is not None else "---"
        if use2 or p2 is not None:
            t2 = f"{p2:.1f}" if p2 is not None else "---"

        try:
            self.ui.actualPower1Edit.setText(t1)
        except Exception:
            pass

        try:
            self.ui.actualPower2Edit.setText(t2)
        except Exception:
            pass

    def _reset_process_ui(self, *, reset_monitor: bool = True) -> None:
        """怨듭젙 醫낅즺 ?? ?낅젰媛?臾쇱쭏 ?좏깮/洹몃옒???쒖떆媛?珥덇린??"""
        for wname in ("processNameEdit", "deprateEdit", "deprateEdit2", "thicknessEdit", "delayEdit"):
            w = getattr(self.ui, wname, None)
            if w is not None and hasattr(w, "setText"):
                with contextlib.suppress(Exception):
                    w.setText("")

        for wname in ("sourcePower1", "sourcePower2"):
            w = getattr(self.ui, wname, None)
            if w is not None and hasattr(w, "setChecked"):
                with contextlib.suppress(Exception):
                    w.setChecked(False)

        self._material_1 = None
        self._material_2 = None
        self._clear_run_power_flags()

        with contextlib.suppress(Exception):
            self.ui.materialEdit.setText("Select")
        with contextlib.suppress(Exception):
            self.ui.materialEdit2.setText("Select")


        for wname in (
            "currentRateEdit",
            "currentThicknessEdit",
            "currentDac1Edit",
            "currentDac2Edit",
            "actualPower1Edit",
            "actualPower2Edit",
        ):
            w = getattr(self.ui, wname, None)
            if w is not None and hasattr(w, "setText"):
                with contextlib.suppress(Exception):
                    w.setText("---")

        if reset_monitor:
            self._set_process_monitor_text("---")

        if self._plot is not None:
            with contextlib.suppress(Exception):
                self._plot.clear()

        self._last_rate = None
        self._last_thickness = None
        self._last_power = None


    def _read_float(self, wname: str) -> Optional[float]:
        w = getattr(self.ui, wname, None)
        if w is None or not hasattr(w, "text"):
            return None
        s = str(w.text()).strip()
        if not s:
            return None
        try:
            return float(s)
        except Exception:
            return None

    def _collect_ui_run_cfg(
        self,
        *,
        require_process_name: bool = True,
        require_thickness: bool = True,
        process_cfg_override: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:

        p1 = bool(getattr(getattr(self.ui, "sourcePower1", None), "isChecked", lambda: False)())
        p2 = bool(getattr(getattr(self.ui, "sourcePower2", None), "isChecked", lambda: False)())
        if not (p1 or p2):
            QMessageBox.warning(self, "Input", "Power1/Power2 중 최소 1개는 선택해야 합니다.")
            return None


        if self._power2_temporarily_disabled and p2:
            QMessageBox.warning(
                self,
                "Input",
                "현재 장비 상태에서는 Power 2를 사용할 수 없습니다.\n"
                "임시로 Power 1만 사용해 주세요.\n"
                "(장비 수리 후 Power2/dual-power 경로를 다시 활성화할 예정입니다.)"
            )
            return None


        rate1 = self._read_float("deprateEdit")
        rate2 = self._read_float("deprateEdit2")

        if p1 and not p2:

            if rate1 is None:
                QMessageBox.warning(self, "Input", "Power1 선택 시 Target Dep.rate 1을 입력해 주세요.")
                return None
            if rate1 <= 0:
                QMessageBox.warning(self, "Input", "Target Dep.rate 1은 0보다 커야 합니다.")
                return None
            target_rate = rate1

        elif p2 and not p1:

            if rate2 is None:
                QMessageBox.warning(self, "Input", "Power2 선택 시 Target Dep.rate 2를 입력해 주세요.")
                return None
            if rate2 <= 0:
                QMessageBox.warning(self, "Input", "Target Dep.rate 2는 0보다 커야 합니다.")
                return None
            target_rate = rate2

        else:
            # Power1 + Power2 ?숈떆 ?좏깮


            if rate1 is None:
                QMessageBox.warning(self, "Input", "Power1+Power2 동시 사용 시 Target Dep.rate 1을 입력해 주세요.")
                return None
            if rate2 is None:
                QMessageBox.warning(self, "Input", "Power1+Power2 동시 사용 시 Target Dep.rate 2를 입력해 주세요.")
                return None
            if rate1 <= 0 or rate2 <= 0:
                QMessageBox.warning(self, "Input", "Target Dep.rate 1/2는 모두 0보다 커야 합니다.")
                return None
            if abs(rate1 - rate2) > 1e-9:
                QMessageBox.warning(self, "Input", "Power1+Power2 동시 사용 시 Target Dep.rate 1과 2는 같아야 합니다.")
                return None

            target_rate = rate1


        target_thk = self._read_float("thicknessEdit")
        delay_min = self._read_float("delayEdit")
        if require_thickness:
            if target_thk is None:
                QMessageBox.warning(self, "Input", "Target Thickness를 입력해 주세요.")
                return None
            if target_thk <= 0:
                QMessageBox.warning(self, "Input", "Target Thickness는 0보다 커야 합니다.")
                return None
        if target_thk is None or target_thk <= 0:
            target_thk = 0.0
        if delay_min is None:
            delay_min = 0.0
        if delay_min < 0:
            QMessageBox.warning(self, "Input", "Delay(min)은 0 이상이어야 합니다.")
            return None

        if p1 and p2:
            if not (self._material_1 or self._material_2):
                QMessageBox.warning(self, "Input", "Power1+Power2 동시 사용 시 Material은 최소 1개 선택해야 합니다.")
                return None
            base_mat = self._material_1 or self._material_2


            if self._material_1 and self._material_2:
                m1 = str(self._material_1.get("material", "")).strip()
                m2 = str(self._material_2.get("material", "")).strip()
                d1 = float(self._material_1.get("density_g_cm3", 0.0) or 0.0)
                d2 = float(self._material_2.get("density_g_cm3", 0.0) or 0.0)
                z1 = float(self._material_1.get("z_factor", 0.0) or 0.0)
                z2 = float(self._material_2.get("z_factor", 0.0) or 0.0)

                if (m1 != m2) or (abs(d1 - d2) > 1e-9) or (abs(z1 - z2) > 1e-9):
                    QMessageBox.warning(
                        self,
                        "Input",
                        "Power1+Power2 동시 사용은 동일 재료를 가정합니다.\n"
                        "Material1/Material2 설정이 서로 다릅니다. 동일하게 맞춰 주세요.",
                    )
                    return None
        else:
            base_mat = self._material_1 if p1 else self._material_2

        mat_name = str((base_mat or {}).get("material", "")).strip()
        den = float((base_mat or {}).get("density_g_cm3", 0.0) or 0.0)
        zf = float((base_mat or {}).get("z_factor", 0.0) or 0.0)

        if not mat_name:
            QMessageBox.warning(self, "Input", "Material 이름이 비어 있습니다. Material을 다시 선택해 주세요.")
            return None
        if den <= 0 or zf <= 0:
            QMessageBox.warning(self, "Input", "Material density/z-factor 값이 올바르지 않습니다.")
            return None


        pname = ""
        w = getattr(self.ui, "processNameEdit", None)
        if w is not None and hasattr(w, "text"):
            pname = str(w.text()).strip()
        if require_process_name and not pname:
            QMessageBox.warning(self, "Input", "Process Name을 입력해 주세요.")
            return None

        proc_cfg = self._normalize_process_config(
            process_cfg_override if process_cfg_override is not None else getattr(self, "_process_cfg", None)
        )
        steps = proc_cfg.get("ramp_steps") or []
        if not steps:
            QMessageBox.warning(
                self,
                "Ramp Step 없음",
                "Ramp Step이 설정되지 않았습니다.\n\n"
                "Recipe 버튼을 눌러 Step을 먼저 추가한 후 공정을 시작하세요."
            )
            return None

        last_adc = -1.0
        for idx, step in enumerate(steps, start=1):
            target_adc = float(step.get("target_adc", 0.0) or 0.0)
            dac_step = int(step.get("dac_step", 0) or 0)
            dac_interval_sec = float(step.get("dac_interval_sec", 0.0) or 0.0)
            hold_sec = float(step.get("hold_sec", 0.0) or 0.0)

            if target_adc <= 0:
                QMessageBox.warning(self, "Input", f"Process Config step {idx}의 target_adc는 0보다 커야 합니다.")
                return None
            if dac_step <= 0:
                QMessageBox.warning(self, "Input", f"Process Config step {idx}의 dac_step은 0보다 커야 합니다.")
                return None
            if dac_interval_sec <= 0:
                QMessageBox.warning(self, "Input", f"Process Config step {idx}의 dac_interval_sec는 0보다 커야 합니다.")
                return None
            if hold_sec < 0:
                QMessageBox.warning(self, "Input", f"Process Config step {idx}의 hold_sec는 0 이상이어야 합니다.")
                return None
            if last_adc >= 0 and target_adc < last_adc:
                QMessageBox.warning(self, "Input", f"Process Config step {idx}의 target_adc는 이전 step보다 크거나 같아야 합니다.")
                return None

            last_adc = target_adc

        cfg: dict[str, Any] = {
            "process_name": pname,

            "use_power1": p1,
            "use_power2": p2,

            "material_name": mat_name,
            "density": den,
            "z_factor": zf,

            "target_rate": float(target_rate),
            "target_thickness": float(target_thk),
            "delay_min": float(delay_min),

            "material_1": self._material_1,
            "material_2": self._material_2,

            "process_config": proc_cfg,
        }
        return cfg
    

    def _emergency_safe_shutdown_plc_best_effort(self) -> None:
        """
        鍮꾩긽 fallback ?꾩슜 PLC 醫낅즺.
        ?뺤긽 Stop/Abort/Error 醫낅즺??engine.py ??safety shutdown 寃쎈줈媛 ?대떦?쒕떎.
        ???⑥닔??controller/worker stop ?붿껌??遺덇??ν븷 ?뚮쭔 ?ъ슜?쒕떎.
        """
        if self.hmi_window is None:
            return
        binder = getattr(self.hmi_window, "_plc_binder", None)
        if binder is None:
            self._append_process_log("[PLC][WARN] plc_binder is None")
            return

        # 1) main shutter close
        try:
            binder.enqueue_write("MAIN_SHUTTER_SW", False)
            self._append_process_log("[SAFE] MAIN_SHUTTER -> CLOSE")
        except Exception as e:
            self._append_process_log(f"[SAFE][WARN] MAIN_SHUTTER close failed: {e!r}")


        try:
            binder.enqueue_write_reg("DAC_POWER_1", 0)
            binder.enqueue_write_reg("DAC_POWER_2", 0)
            self._append_process_log("[SAFE] DAC_POWER_1/2 -> 0")
        except Exception as e:
            self._append_process_log(f"[SAFE][ERROR] DAC=0 FAILED (MUST FIX): {e!r}")

        # 3) power off
        try:
            binder.enqueue_write("POWER_1_SW", False)
            binder.enqueue_write("POWER_2_SW", False)
            self._append_process_log("[SAFE] POWER_1/2 -> OFF")
        except Exception as e:
            self._append_process_log(f"[SAFE][WARN] POWER off failed: {e!r}")

        self._last_power = 0.0


