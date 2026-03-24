# process_window.py
from __future__ import annotations

import gc
import time
import warnings
import contextlib
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, TYPE_CHECKING

from PySide6.QtWidgets import QWidget, QMessageBox, QVBoxLayout, QApplication
from PySide6.QtCore import QTimer

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

# ✅ 파일 위치가 root든 ui 폴더든 둘 다 버티도록
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
# Process 창
# ============================================================
class ProcessWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.ui = Ui_Form()
        self.ui.setupUi(self)

        # ✅ 공정별 ProcessWindowLog 파일 식별자
        self._active_run_id: Optional[str] = None

        self.setWindowTitle("Process")
        self.ui.stackedWidget.setCurrentIndex(1)  # Process page

        self.hmi_window: Optional[HmiWindow] = None
        self._close_stop_guard = False   # ✅ closeEvent에서 stop 중복 실행 방지

        self._process_controller: Any = None
        self._log_service: Any = None
        self._stm_service: Any = None
        self._acs_service: Any = None

        # ✅ Start preflight 상태
        self._start_worker: Optional[ProcessStartWorker] = None
        self._start_in_progress: bool = False
        self._pending_run_cfg: Optional[dict[str, Any]] = None

        # ✅ UI에 "실제로 connect된 STM 인스턴스" 추적용(경고/중복 connect 방지)
        self._stm_ui_bound: bool = False
        self._stm_ui_stm: Any = None

        self._material_1 = None
        self._material_2 = None

        # 공정 시작 시점 power 선택 상태 latch
        self._run_use_power1: Optional[bool] = None
        self._run_use_power2: Optional[bool] = None

        # 현재 하드웨어 임시 우회 정책
        # - Power2는 현재 장비 문제로 start path에서 비활성
        # - Power1 actual feedback은 임시로 ADC2를 사용
        self._power2_temporarily_disabled: bool = True
        self._power1_feedback_uses_adc2: bool = True

        self.ui.materialEdit.clicked.connect(lambda: self._open_material_dialog(1))
        self.ui.materialEdit2.clicked.connect(lambda: self._open_material_dialog(2))

        self.ui.startProcess.clicked.connect(self._on_start_clicked)
        self.ui.stopProcess.clicked.connect(self._on_stop_clicked)

        # ✅ Process Config 버튼 연결
        cfg_btn = getattr(self.ui, "processConfigBtn", None)
        if cfg_btn is not None and hasattr(cfg_btn, "clicked"):
            cfg_btn.clicked.connect(self._open_process_config_dialog)

        # ✅ 새 프로세스 설정(ADC step 기반) 보관
        self._process_cfg: dict[str, Any] = self._default_process_config()

        # =========================
        # ✅ RT 표시(1초) + 그래프
        # =========================
        self._last_rate: Optional[float] = None
        self._last_thickness: Optional[float] = None

        # ✅ 이제 _last_power 는 "그래프 오른쪽 축에 넣을 값"
        #    현재 단계에서는 ADC total 우선값으로 사용
        self._last_power: Optional[float] = None

        self._plot: Optional[DepositionPlotWidget] = None
        self._init_rt_plot()  # graphWidget 자리에 plot 삽입

        self._rt_timer = QTimer(self)
        self._rt_timer.setInterval(1000)  # ✅ 1초
        self._rt_timer.timeout.connect(self._tick_rt_ui)

    def set_hmi_window(self, hmi_window: HmiWindow):
        self.hmi_window = hmi_window

    def set_runtime_objects(self, plc_binder, ini_path: Path, *, process_controller=None, log_service=None,
                            stm_service=None, acs_service=None) -> None:
        self._plc_binder = plc_binder
        self._ini_path = Path(ini_path)

        self._process_controller = process_controller
        self._log_service = log_service
        self._stm_service = stm_service
        self._acs_service = acs_service

        # ✅ Process 로그 위젯을 LogService에 연결 (run별 로그 저장)
        if self._log_service is not None and hasattr(self._log_service, "attach_text_widget"):
            with contextlib.suppress(Exception):
                self._log_service.attach_text_widget(getattr(self.ui, "logWindow", None), channel="PROCESS")

        # ProcessController 시그널 → UI 업데이트
        pc = self._process_controller
        if pc is not None:
            try:
                pc.sig_ui_log.connect(lambda s: _append_text(getattr(self.ui, "logWindow", None), s))
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

    def _bind_stm_ui(self, stm):
        """
        ✅ 핵심: disconnect는 '현재 self._stm_service'가 아니라
                '실제로 UI에 connect 되었던 STM 인스턴스'에서만 해야 한다.
        """
        if stm is None:
            self._unbind_stm_ui()
            return

        # 이미 같은 stm에 바인딩되어 있으면 중복 connect 방지
        if self._stm_ui_bound and (self._stm_ui_stm is stm):
            return

        # 이전에 UI에 연결돼 있던 stm만 정확히 해제
        self._unbind_stm_ui()

        # 새 stm에 연결
        stm.sig_rate.connect(self._on_stm_rate)
        stm.sig_thickness.connect(self._on_stm_thickness)

        # 바인딩 상태 기록
        self._stm_ui_stm = stm
        self._stm_ui_bound = True


    def _unbind_stm_ui(self):
        """
        ✅ '연결했던 적이 없는 객체'에서 disconnect를 시도하면
        Failed to disconnect RuntimeWarning이 뜬다.
        ✅ 그래서: UI에 연결했던 stm을 따로 기억해두고 그 stm만 disconnect한다.
        """
        stm = self._stm_ui_stm
        if (not self._stm_ui_bound) or (stm is None):
            self._stm_ui_stm = None
            self._stm_ui_bound = False
            return

        # 안전장치: PySide가 disconnect 실패를 RuntimeWarning으로 찍는 경우까지 억제
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
        # ✅ 값만 저장 (UI/그래프는 1초 타이머에서)
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
        Start 버튼 눌렀을 때 PLC 연결 상태를 먼저 확인한다.
        PLC가 끊긴 상태면 STM 연결/공정 시작으로 넘어가지 않게 막는다.
        """
        def _abort(msg: str) -> bool:
            _append_text(getattr(self.ui, "logWindow", None), f"[PRECHECK][BLOCK] PLC: {msg}")
            QMessageBox.warning(self, "PLC Pre-check", msg)
            return False

        if self.hmi_window is None:
            return _abort("HMI window가 없습니다. PLC 상태를 확인할 수 없습니다.")

        binder = getattr(self.hmi_window, "_plc_binder", None)
        if binder is None:
            return _abort("plc_binder가 없습니다. PLC 연결 상태를 확인할 수 없습니다.")

        try:
            plc = binder.get_plc_service()
        except Exception as e:
            return _abort(f"PLC 서비스 조회 실패: {e!r}")

        if plc is None:
            return _abort("PLC 서비스가 없습니다.")

        try:
            connected = False

            # 1순위: plc_service.is_connected()가 있으면 사용
            is_connected_fn = getattr(plc, "is_connected", None)
            if callable(is_connected_fn):
                connected = bool(is_connected_fn())
            else:
                # 2순위: latest snapshot의 connected 값 사용
                snap = plc.get_last_snapshot() if hasattr(plc, "get_last_snapshot") else None
                connected = bool(getattr(snap, "connected", False))

            if not connected:
                return _abort("PLC가 연결되지 않았습니다.\nPLC 연결 후 다시 시작하세요.")

        except Exception as e:
            return _abort(f"PLC 연결 상태 확인 실패: {e!r}")

        _append_text(getattr(self.ui, "logWindow", None), "[PRECHECK] PLC OK: connected")
        return True

    def _prepare_stm_service_for_start(self) -> bool:
        """
        Start 직후 UI thread에서는
        - FTM ON 요청
        - STMService 객체 생성/start
        - ProcessController에 runtime device 주입
        까지만 수행한다.

        실제 연결 대기 / crystal health check는
        ProcessStartWorker(QThread)에서 처리한다.
        """
        if self.hmi_window is None:
            _append_text(getattr(self.ui, "logWindow", None), "[DEV][ERR] hmi_window is None -> cannot prepare STM")
            return False

        ini_path = getattr(self, "_ini_path", None) or getattr(self.hmi_window, "_ini_path", None)
        if not ini_path:
            _append_text(getattr(self.ui, "logWindow", None), "[DEV][ERR] ini_path is None -> cannot prepare STM")
            return False

        # 이전 STM 정리 (start 직전이므로 FTM OFF까지 포함한 best-effort cleanup)
        self._shutdown_stm_with_ftm_off_best_effort()

        binder = getattr(self.hmi_window, "_plc_binder", None)
        if binder is None:
            _append_text(getattr(self.ui, "logWindow", None), "[DEV][ERR] plc_binder is None (cannot turn on FTM_SW)")
            return False

        try:
            binder.enqueue_write("FTM_SW", True)
            _append_text(getattr(self.ui, "logWindow", None), "[DEV] FTM_SW -> ON (before STM preflight)")

            stm = STMService(ini_path=ini_path)
            stm.start()

            self._stm_service = stm
            self.hmi_window._stm_service = stm

            self._acs_service = getattr(self.hmi_window, "_acs_service", None)

            pc = self._process_controller
            if pc is not None and hasattr(pc, "replace_runtime_devices"):
                pc.replace_runtime_devices(stm=stm, acs=self._acs_service)
            else:
                _append_text(getattr(self.ui, "logWindow", None), "[DEV][WARN] ProcessController.replace_runtime_devices() 없음")

            _append_text(getattr(self.ui, "logWindow", None), "[DEV] STM service prepared (health/preflight pending)")
            return True

        except Exception as e:
            _append_text(getattr(self.ui, "logWindow", None), f"[DEV][ERR] STM prepare failed: {e!r}")

            with contextlib.suppress(Exception):
                binder.enqueue_write("FTM_SW", False)
                _append_text(getattr(self.ui, "logWindow", None), "[DEV] FTM_SW -> OFF (STM prepare failed)")

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

        monitor = getattr(self.ui, "processMonitor_Process", None)
        if monitor is not None and hasattr(monitor, "setText") and busy:
            with contextlib.suppress(Exception):
                monitor.setText("Preparing STM...")

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
            QMessageBox.warning(self, "STM", "STM 서비스가 준비되지 않았습니다.")
            self._clear_run_power_flags()
            self._active_run_id = None
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
        _append_text(getattr(self.ui, "logWindow", None), f"[PRECHECK] {text}")

        monitor = getattr(self.ui, "processMonitor_Process", None)
        if monitor is not None and hasattr(monitor, "setText"):
            with contextlib.suppress(Exception):
                monitor.setText(str(text))

    def _abort_start_preflight(self, *, show_warning: bool, title: str, message: str) -> None:
        with contextlib.suppress(Exception):
            self._rt_stop()

        with contextlib.suppress(Exception):
            self._shutdown_stm_with_ftm_off_best_effort()

        # ✅ 여기서는 사용자 입력값을 지우지 않는다.
        #    preflight 실패했다고 process name / thickness / rate까지 초기화하면 UX가 나빠진다.
        self._pending_run_cfg = None
        self._active_run_id = None
        self._clear_run_power_flags()

        self._cleanup_start_worker()
        self._set_start_busy(False)

        monitor = getattr(self.ui, "processMonitor_Process", None)
        if monitor is not None and hasattr(monitor, "setText"):
            with contextlib.suppress(Exception):
                monitor.setText("---")

        if message:
            _append_text(getattr(self.ui, "logWindow", None), f"[PRECHECK][ABORT] {message}")

        if show_warning:
            QMessageBox.warning(self, title, message)

    def _on_start_preflight_result(self, result: STMPreflightResult) -> None:
        self._cleanup_start_worker()

        if bool(getattr(result, "cancelled", False)):
            self._abort_start_preflight(
                show_warning=False,
                title="Process",
                message="STM preflight가 취소되었습니다.",
            )
            return

        if not bool(getattr(result, "ok", False)):
            self._abort_start_preflight(
                show_warning=True,
                title="STM Pre-check",
                message=str(getattr(result, "message", "STM preflight 실패")),
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
                message="start_from_ui가 구현되어 있지 않습니다.",
            )
            return

        try:
            pc.start_from_ui(run_cfg, run_id=self._active_run_id)
            _append_text(getattr(self.ui, "logWindow", None), "[PRECHECK] STM preflight 성공 -> 공정 시작")
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
                _append_text(getattr(self.ui, "logWindow", None), "[DEV] STM stop() called")
            except Exception as e:
                _append_text(getattr(self.ui, "logWindow", None), f"[DEV][WARN] STM stop failed: {e!r}")

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
        _append_text(getattr(self.ui, "logWindow", None), "[DEV] STM released + gc.collect() (ACS kept alive)")

    def _shutdown_stm_with_ftm_off_best_effort(self) -> None:
        self._release_stm_runtime_only()

        try:
            binder = getattr(self.hmi_window, "_plc_binder", None) if self.hmi_window else None
            if binder is not None:
                binder.enqueue_write("FTM_SW", False)
                _append_text(getattr(self.ui, "logWindow", None), "[DEV] FTM_SW -> OFF")
        except Exception as e:
            _append_text(getattr(self.ui, "logWindow", None), f"[DEV][WARN] FTM_SW off failed: {e!r}")

    def _on_start_clicked(self) -> None:
        if self._start_in_progress:
            _append_text(getattr(self.ui, "logWindow", None), "[UI] Start ignored: preflight already running")
            return

        pc = self._process_controller
        if pc is None:
            QMessageBox.warning(self, "Process", "ProcessController가 연결되지 않았습니다.")
            return

        try:
            if hasattr(pc, "is_running") and pc.is_running():
                QMessageBox.information(self, "Process", "이미 공정이 실행 중입니다.")
                return
        except Exception:
            pass

        # 1) UI 입력 검증
        run_cfg = self._collect_ui_run_cfg()
        if run_cfg is None:
            return

        self._latch_run_power_flags(run_cfg)

        proc_cfg = run_cfg.get("process_config") or {}
        steps = proc_cfg.get("ramp_steps") or []
        if steps:
            step_desc = " | ".join(
                f"S{idx+1}: ADC {float(s.get('target_adc', 0.0)):.1f}, "
                f"DAC+{int(s.get('dac_step', 0))}, "
                f"{float(s.get('dac_interval_sec', 0.0)):.1f}s, "
                f"hold {float(s.get('hold_sec', 0.0)):.1f}s"
                for idx, s in enumerate(steps)
            )
            _append_text(getattr(self.ui, "logWindow", None), f"[CFG] {step_desc}")

        _append_text(
            getattr(self.ui, "logWindow", None),
            "[CFG] "
            f"dac_max={proc_cfg.get('dac_max')} | "
            f"rate_tol_ratio={proc_cfg.get('rate_tol_ratio')} | "
            f"fine_step_dac={proc_cfg.get('fine_step_dac')}"
        )

        # 2) run_id 생성
        self._active_run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 3) PLC precheck
        if not self._check_plc_ready_before_start():
            self._clear_run_power_flags()
            self._active_run_id = None
            return

        # 4) STM service 준비(여기서는 blocking wait 안 함)
        if not self._prepare_stm_service_for_start():
            QMessageBox.warning(self, "Device Connect Failed", "STM 준비 실패")
            with contextlib.suppress(Exception):
                self._shutdown_stm_with_ftm_off_best_effort()
            self._clear_run_power_flags()
            self._active_run_id = None
            return

        # 5) 실제 연결 대기 / crystal health는 worker로 넘긴다
        self._start_async_preflight(run_cfg)
            
    def _on_stop_clicked(self) -> None:
        # 0) Start preflight 중이면 우선 취소
        if self._has_active_start_preflight():
            worker = self._start_worker
            if worker is not None:
                with contextlib.suppress(Exception):
                    worker.request_cancel()
            _append_text(getattr(self.ui, "logWindow", None), "[UI] STM preflight 취소 요청")
            return

        pc = self._process_controller

        # 1) 엔진/컨트롤러가 살아 있으면 "정지 요청"만 보낸다.
        if pc is not None:
            try:
                pc.stop()
                _append_text(getattr(self.ui, "logWindow", None), "[UI] 공정 정지 요청 -> engine safety shutdown 대기")
                return
            except Exception as e:
                _append_text(getattr(self.ui, "logWindow", None), f"[STOP FAIL] controller stop failed: {e!r}")

        # 2) controller stop 요청이 안 되는 경우에만 emergency fallback
        try:
            self._emergency_safe_shutdown_plc_best_effort()
            _append_text(getattr(self.ui, "logWindow", None), "[SAFE] emergency fallback shutdown executed")
        except Exception as e:
            _append_text(getattr(self.ui, "logWindow", None), f"[SAFE][FAIL] emergency shutdown failed: {e!r}")

        with contextlib.suppress(Exception):
            self._rt_stop()
        with contextlib.suppress(Exception):
            self._shutdown_stm_with_ftm_off_best_effort()
        with contextlib.suppress(Exception):
            self._reset_process_ui()

        self._active_run_id = None

    def _on_status(self, st: Any) -> None:
        # ✅ 그래프용 power 추출(기존 유지)
        self._try_update_last_power(st)

        w = getattr(self.ui, "processMonitor_Process", None)
        if w is None or not hasattr(w, "setText"):
            return

        try:
            # ✅ engine.py가 보내는 “스텝별 표시 메시지” 우선
            m = str(getattr(st, "message", "") or "").strip()
            if m:
                w.setText(m)
                return

            # fallback: message가 없으면 기존처럼 phase/step
            phase = getattr(st, "phase", None)
            step = getattr(st, "step_name", None) or getattr(st, "step", None)

            if phase or step:
                w.setText(f"{phase} | {step}")
            else:
                w.setText("---")

        except Exception:
            pass

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

        y_max = max(candidates, default=300.0)
        y_max = max(100.0, y_max * 1.10)
        return (0.0, y_max)

    def _try_update_last_power(self, st: Any) -> None:
        """
        그래프용 power는 이제 ADC total 우선.
        아직 engine.py 가 adc를 status에 안 넣는 동안은 DAC fallback 허용.
        """
        def _set_pair(v1: Any, v2: Any) -> bool:
            total = self._sum_selected_pair(
                self._clamp_nonneg(v1),
                self._clamp_nonneg(v2),
            )
            if total is None:
                return False
            self._last_power = float(total)
            return True

        try:
            # dict 형태
            if isinstance(st, dict):
                for k in ("adc_total", "power_actual", "actual_power", "power_read", "adc"):
                    if k in st and st[k] is not None:
                        self._last_power = float(st[k])
                        return

                if ("adc1" in st) or ("adc2" in st):
                    if _set_pair(st.get("adc1"), st.get("adc2")):
                        return

                # fallback: 아직 adc 미구현이면 dac 사용
                for k in ("power", "power_dac", "dac", "dac_power", "power_cmd", "dac_cmd"):
                    if k in st and st[k] is not None:
                        self._last_power = float(st[k])
                        return

                if ("dac1" in st) or ("dac2" in st):
                    if _set_pair(st.get("dac1"), st.get("dac2")):
                        return
                return

            # 객체 형태
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
            where = getattr(err, "where", "")
            msg = getattr(err, "message", "")
            _append_text(getattr(self.ui, "logWindow", None), f"[ERROR] {where} | {msg}")
        except Exception:
            _append_text(getattr(self.ui, "logWindow", None), f"[ERROR] {err!r}")

    def _on_finished(self, result: Any) -> None:
        try:
            # 1) worker가 끝났으니 이제 RT 정지
            with contextlib.suppress(Exception):
                self._rt_stop()

            # 2) 여기서는 PLC 종료를 직접 하지 않는다.
            #    stop/error 경로의 safety shutdown은 engine.py가 이미 담당한다.

            # 3) worker 종료 후에만 센서/메모리 정리
            with contextlib.suppress(Exception):
                self._release_stm_runtime_only()

            try:
                ok = bool(getattr(result, "ok", False))
                rid = getattr(result, "run_id", "")
                _append_text(
                    getattr(self.ui, "logWindow", None),
                    f"[FINISHED] ok={ok} run_id={rid}"
                )
            except Exception:
                _append_text(getattr(self.ui, "logWindow", None), "[FINISHED]")

        finally:
            self._active_run_id = None
            self._clear_run_power_flags()

            with contextlib.suppress(Exception):
                self._reset_process_ui()

    def _wait_process_stop(self, timeout_s: float = 3.0) -> bool:
        """
        closeEvent에서 stop 요청 후 공정 worker가 실제로 멈췄는지
        짧게 기다린다.
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
        closeEvent에서 stop 요청 후 얼마나 기다릴지 계산.
        engine shutdown ramp 기준:
        - 1초마다 100 감소
        - max(dac1, dac2) 기준으로 대기시간 추정
        - 여유 버퍼 추가
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
        ramp_s = max_dac / 100.0   # 1초에 100 감소 기준
        timeout_s = ramp_s + 5.0   # 버퍼 5초
        return max(3.0, min(timeout_s, 30.0))

    def closeEvent(self, event):
        # 1) 먼저 stop / cancel 요청
        if not getattr(self, "_close_stop_guard", False):
            self._close_stop_guard = True
            try:
                self._on_stop_clicked()
            except Exception:
                pass

        # 2) Start preflight가 살아 있으면 먼저 기다린다
        if self._has_active_start_preflight():
            stopped = True
            try:
                stopped = self._wait_start_preflight_stop(timeout_s=5.0)
            except Exception:
                stopped = True

            if not stopped:
                _append_text(
                    getattr(self.ui, "logWindow", None),
                    "[UI][WARN] STM preflight stop wait timeout -> close canceled"
                )
                QMessageBox.warning(
                    self,
                    "Process",
                    "STM 준비 작업이 아직 종료되지 않았습니다.\n"
                    "잠시 후 다시 닫아주세요."
                )
                self._close_stop_guard = False
                event.ignore()
                return

        # 3) 실제 공정이 돌고 있었다면 기존 shutdown wait
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
            _append_text(
                getattr(self.ui, "logWindow", None),
                f"[UI][WARN] Process stop wait timeout ({timeout_s:.1f}s) -> close canceled"
            )
            QMessageBox.warning(
                self,
                "Process",
                "공정 정지 완료를 아직 확인하지 못했습니다.\n"
                "DAC ramp-down 종료 후 다시 닫아주세요."
            )
            self._close_stop_guard = False
            event.ignore()
            return

        _append_text(
            getattr(self.ui, "logWindow", None),
            "[UI] Process window closed -> engine/preflight stop finished"
        )

        if self.hmi_window is not None:
            self.hmi_window.process_window = None

        event.accept()

    def _open_material_dialog(self, channel: int) -> None:
        sel = MaterialCatalogDialog.pick(base_dir=_BASE_DIR, parent=self)
        if not sel:
            return

        # ✅ source 버튼은 물질 정보만 관리
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
                hold_src = d.get("hold_sec", d.get("delay_s", 0.0))   # delay_s만 호환 허용
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

        return {
            "step_count": len(steps),
            "ramp_steps": steps,
            "dac_max": _as_int(src, "dac_max", 4000, 1),
            "rate_tol_ratio": _as_float(src, "rate_tol_ratio", 0.05, 0.001, 1.0),
            "rate_stable_sec": _as_float(src, "rate_stable_sec", 3.0, 0.0),
            "hold_control_interval_s": _as_float(src, "hold_control_interval_s", 1.0, 0.1),
            "fine_step_dac": _as_int(src, "fine_step_dac", 10, 1),
            "rate_abort_ratio": _as_float(src, "rate_abort_ratio", 0.30, 0.001, 1.0),
            "rate_abort_sec": _as_float(src, "rate_abort_sec", 5.0, 0.0),
            "sensor_none_abort_s": _as_float(src, "sensor_none_abort_s", 5.0, 0.0),
            "adc_none_abort_s": _as_float(src, "adc_none_abort_s", 5.0, 0.0),
        }

    def _open_process_config_dialog(self) -> None:
        """
        아직 ui/process_config_dialog.py 가 없더라도
        프로그램이 바로 죽지 않게 lazy import 로 처리한다.
        """
        try:
            from ui.process_config_dialog import ProcessConfigDialog
        except Exception as e:
            QMessageBox.warning(
                self,
                "Process Config",
                f"Process Config dialog import 실패:\n{e!r}"
            )
            _append_text(getattr(self.ui, "logWindow", None), f"[CFG][ERR] dialog import failed: {e!r}")
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

        _append_text(
            getattr(self.ui, "logWindow", None),
            "[CFG] Process config updated | "
            f"steps={len(steps)} | {step_desc}"
        )

    # ================== 그래프 설정 ==================
    def _init_rt_plot(self) -> None:
        """ui.graphWidget 자리에 DepositionPlotWidget을 삽입"""
        host = getattr(self.ui, "graphWidget", None)
        if host is None:
            return

        # 이미 layout이 없으면 생성
        lay = host.layout()
        if lay is None:
            lay = QVBoxLayout(host)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
            host.setLayout(lay)

        # 기존 위젯 제거(중복 방지)
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
        """1초마다 lineedit + 그래프 갱신"""
        rate = self._clamp_nonneg(self._last_rate)

        # ✅ DAC / ADC 둘 다 읽기
        dac1, dac2 = self._read_plc_power_dac_pair()
        adc1, adc2 = self._read_plc_power_actual_pair()

        # MODIFIED: 하드웨어 채널 매핑 — Power1 단독 사용 시 ADC2가 실제 feedback
        # Power1 선택 시: ADC2 값을 그래프/표시에 사용 (ADC1은 노이즈)
        graph_power, display_adc1, display_adc2 = self._resolve_power_feedback_for_ui(adc1, adc2)

        if graph_power is not None:
            self._last_power = graph_power
        else:
            self._last_power = None

        show_th = self._is_main_deposition()
        th = self._clamp_nonneg(self._last_thickness) if show_th else None

        try:
            self.ui.currentRateEdit.setText(f"{rate:.3f}" if rate is not None else "---")
        except Exception:
            pass

        try:
            self.ui.currentThicknessEdit.setText(f"{th:.2f}" if th is not None else "---")
        except Exception:
            pass

        # ✅ 신규 DAC 표시
        self._update_dac_power_ui(dac1, dac2)

        # MODIFIED: actualPower 표시 — Power1 단독 시 ADC2 값으로 표시
        self._update_actual_power_ui(display_adc1, display_adc2)

        # ✅ 그래프는 ADC 기준으로 append
        if self._plot is not None:
            try:
                self._plot.append(rate=rate, power=graph_power)
            except Exception:
                pass

    def _resolve_power_feedback_for_ui(
        self,
        adc1: Optional[float],
        adc2: Optional[float],
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """
        반환값:
            graph_power, display_adc1, display_adc2
        """

        use1, use2 = self._selected_power_flags()

        # 현재 하드웨어 임시 우회:
        # Power1 단독 사용 시 실제 feedback은 ADC2를 사용한다.
        if use1 and not use2 and self._power1_feedback_uses_adc2:
            fb = self._clamp_nonneg(adc2)
            return fb, fb, None

        graph_power = self._clamp_nonneg(self._sum_selected_pair(adc1, adc2))
        return graph_power, self._clamp_nonneg(adc1), self._clamp_nonneg(adc2)
    # ================== 그래프 설정 ==================

    @staticmethod
    def _clamp_nonneg(v: Optional[float]) -> Optional[float]:
        """음수는 0으로 클램프. (None은 그대로)"""
        if v is None:
            return None
        try:
            fv = float(v)
        except Exception:
            return None
        return 0.0 if fv < 0 else fv

    def _is_main_deposition(self) -> bool:
        """메인 공정(메인 셔터 OPEN)일 때만 True."""
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
        공정/preflight 중에는 시작 시점에 latch된 power 선택 상태를 사용한다.
        idle 상태에서만 현재 UI 체크박스 값을 읽는다.
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
        선택된 power 기준으로 합산.
        - 1개 선택: 해당 채널 값
        - 2개 선택: 합계
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
        PLC snapshot에서 DAC command 2채널을 각각 읽는다.
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
        DAC 표시칸(currentDac1Edit/currentDac2Edit)에 값 출력.
        공정/preflight 중에는 현재 체크박스 상태가 아니라 run 시작 시점 latch 기준으로 표시한다.
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
        plc_service.py에서 이미 sanitize + scale 된 값을 그대로 사용한다.
        예:
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
        PLC snapshot에서 actual power readback 2채널을 읽는다.
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
        기존 actualPower1/2 는 ADC 표시칸으로 사용.
        공정/preflight 중에는 현재 체크박스 상태가 아니라 run 시작 시점 latch 기준으로 표시한다.
        """
        use1, use2 = self._selected_power_flags()

        t1 = "---"
        t2 = "---"

        if use1:
            t1 = f"{p1:.1f}" if p1 is not None else "---"
        if use2:
            t2 = f"{p2:.1f}" if p2 is not None else "---"

        try:
            self.ui.actualPower1Edit.setText(t1)
        except Exception:
            pass

        try:
            self.ui.actualPower2Edit.setText(t2)
        except Exception:
            pass

    def _reset_process_ui(self) -> None:
        """공정 종료 후: 입력값/물질 선택/그래프/표시값 초기화."""
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

        # ✅ 현재값 표시 초기화
        for wname in ("currentRateEdit", "currentThicknessEdit"):
            w = getattr(self.ui, wname, None)
            if w is not None and hasattr(w, "setText"):
                with contextlib.suppress(Exception):
                    w.setText("---")

        for wname in ("currentDac1Edit", "currentDac2Edit", "actualPower1Edit", "actualPower2Edit"):
            w = getattr(self.ui, wname, None)
            if w is not None and hasattr(w, "setText"):
                with contextlib.suppress(Exception):
                    w.setText("---")

        w = getattr(self.ui, "processMonitor_Process", None)
        if w is not None and hasattr(w, "setText"):
            with contextlib.suppress(Exception):
                w.setText("---")

        if self._plot is not None:
            with contextlib.suppress(Exception):
                self._plot.clear()

        self._last_rate = None
        self._last_thickness = None
        self._last_power = None

    # ================== UI 값 파싱 ==================
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

    def _collect_ui_run_cfg(self) -> Optional[dict[str, Any]]:
        # ✅ Power 선택: 1개 또는 2개 허용
        p1 = bool(getattr(getattr(self.ui, "sourcePower1", None), "isChecked", lambda: False)())
        p2 = bool(getattr(getattr(self.ui, "sourcePower2", None), "isChecked", lambda: False)())
        if not (p1 or p2):
            QMessageBox.warning(self, "Input", "Power1/Power2 중 최소 1개는 선택해야 합니다.")
            return None

        # 아래 Power2 / dual-power 분기는 현재 하드웨어 문제로 인해
        # 위의 임시 차단에 걸려 실제로는 진입하지 않는다.
        # 다만 장비 수리 후 다시 활성화할 복구 경로이므로 삭제하지 않고 유지한다.
        if self._power2_temporarily_disabled and p2:
            QMessageBox.warning(
                self,
                "Input",
                "현재 장비 상태에서는 Power 2를 사용할 수 없습니다.\n"
                "임시로 Power 1만 사용해 주세요.\n"
                "(장비 수리 후 Power2/dual-power 경로를 다시 활성화할 예정)"
            )
            return None

        # ✅ Target Dep.rate: 선택된 power에 해당하는 입력칸만 사용 (fallback 없음)
        rate1 = self._read_float("deprateEdit")
        rate2 = self._read_float("deprateEdit2")

        if p1 and not p2:
            # Power1만 선택 → Dep.rate 1만 본다
            if rate1 is None:
                QMessageBox.warning(self, "Input", "Power1 선택 시 Target Dep.rate 1을 입력하세요.")
                return None
            if rate1 <= 0:
                QMessageBox.warning(self, "Input", "Target Dep.rate 1은 0보다 커야 합니다.")
                return None
            target_rate = rate1

        elif p2 and not p1:
            # Power2만 선택 → Dep.rate 2만 본다
            if rate2 is None:
                QMessageBox.warning(self, "Input", "Power2 선택 시 Target Dep.rate 2를 입력하세요.")
                return None
            if rate2 <= 0:
                QMessageBox.warning(self, "Input", "Target Dep.rate 2는 0보다 커야 합니다.")
                return None
            target_rate = rate2

        else:
            # Power1 + Power2 동시 선택
            # 현재 ProcessController는 target_rate가 1개만 들어가도록 설계되어 있어서(= 엔진도 1개 목표로 동작),
            # 두 값을 다르게 받으면 이후 단계에서 모순/오동작 가능 → 둘 다 입력 + 동일값 강제
            if rate1 is None:
                QMessageBox.warning(self, "Input", "Power1+Power2 동시 사용 시 Target Dep.rate 1을 입력하세요.")
                return None
            if rate2 is None:
                QMessageBox.warning(self, "Input", "Power1+Power2 동시 사용 시 Target Dep.rate 2를 입력하세요.")
                return None
            if rate1 <= 0 or rate2 <= 0:
                QMessageBox.warning(self, "Input", "Target Dep.rate 1/2는 0보다 커야 합니다.")
                return None
            if abs(rate1 - rate2) > 1e-9:
                QMessageBox.warning(self, "Input", "Power1+Power2 동시 사용 시 Target Dep.rate 1과 2는 동일해야 합니다.")
                return None

            target_rate = rate1

        # 공통 입력값
        target_thk = self._read_float("thicknessEdit")
        delay_min = self._read_float("delayEdit")
        if target_thk is None:
            QMessageBox.warning(self, "Input", "Target Thickness를 입력하세요.")
            return None
        if target_thk <= 0:
            QMessageBox.warning(self, "Input", "Target Thickness는 0보다 커야 합니다.")
            return None
        if delay_min is None:
            delay_min = 0.0
        if delay_min < 0:
            QMessageBox.warning(self, "Input", "Delay(min)은 0 이상이어야 합니다.")
            return None

        if p1 and p2:
            if not (self._material_1 or self._material_2):
                QMessageBox.warning(self, "Input", "Power1+Power2 동시 사용 시 Material을 최소 1개는 선택해야 합니다.")
                return None
            base_mat = self._material_1 or self._material_2

            # 둘 다 선택되어 있을 때 둘 다 material이 존재하면 동일성 체크
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
                        "Power1+Power2 동시 사용은 '동일 물질' 가정입니다.\n"
                        "Material1/Material2가 서로 다릅니다. 동일하게 맞춰주세요.",
                    )
                    return None
        else:
            base_mat = self._material_1 if p1 else self._material_2

        mat_name = str((base_mat or {}).get("material", "")).strip()
        den = float((base_mat or {}).get("density_g_cm3", 0.0) or 0.0)
        zf = float((base_mat or {}).get("z_factor", 0.0) or 0.0)

        if not mat_name:
            QMessageBox.warning(self, "Input", "Material 이름이 비어있습니다. Material을 다시 선택하세요.")
            return None
        if den <= 0 or zf <= 0:
            QMessageBox.warning(self, "Input", "Material density/z-factor 값이 올바르지 않습니다.")
            return None

        # ✅ Process Name (필수)
        pname = ""
        w = getattr(self.ui, "processNameEdit", None)
        if w is not None and hasattr(w, "text"):
            pname = str(w.text()).strip()
        if not pname:
            QMessageBox.warning(self, "Input", "Process Name을 입력하세요.")
            return None

        proc_cfg = self._normalize_process_config(getattr(self, "_process_cfg", None))
        steps = proc_cfg.get("ramp_steps") or []
        if not steps:
            QMessageBox.warning(self, "Input", "Process Config의 step이 비어 있습니다.")
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
    
    # ================== UI 값 파싱 ==================

    # ================== 공정 종료 함수 ==================
    def _emergency_safe_shutdown_plc_best_effort(self) -> None:
        """
        비상 fallback 전용 PLC 종료.
        정상 Stop/Abort/Error 종료는 engine.py 의 safety shutdown 경로가 담당한다.
        이 함수는 controller/worker stop 요청이 불가능할 때만 사용한다.
        """
        if self.hmi_window is None:
            return
        binder = getattr(self.hmi_window, "_plc_binder", None)
        if binder is None:
            _append_text(self.ui.logWindow, "[PLC][WARN] plc_binder is None")
            return

        # 1) main shutter close
        try:
            binder.enqueue_write("MAIN_SHUTTER_SW", False)
            _append_text(self.ui.logWindow, "[SAFE] MAIN_SHUTTER -> CLOSE")
        except Exception as e:
            _append_text(self.ui.logWindow, f"[SAFE][WARN] MAIN_SHUTTER close failed: {e!r}")

        # 2) DAC=0은 무조건 시도(미구현이면 그게 버그로 드러나야 함)
        try:
            binder.enqueue_write_reg("DAC_POWER_1", 0)
            binder.enqueue_write_reg("DAC_POWER_2", 0)
            _append_text(self.ui.logWindow, "[SAFE] DAC_POWER_1/2 -> 0")
        except Exception as e:
            _append_text(self.ui.logWindow, f"[SAFE][ERROR] DAC=0 FAILED (MUST FIX): {e!r}")

        # 3) power off
        try:
            binder.enqueue_write("POWER_1_SW", False)
            binder.enqueue_write("POWER_2_SW", False)
            _append_text(self.ui.logWindow, "[SAFE] POWER_1/2 -> OFF")
        except Exception as e:
            _append_text(self.ui.logWindow, f"[SAFE][WARN] POWER off failed: {e!r}")

        self._last_power = 0.0
    # ================== 공정 종료 함수 ==================
