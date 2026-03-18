# process_window.py
from __future__ import annotations

import gc
import time
import warnings
import contextlib
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, TYPE_CHECKING
from concurrent.futures import TimeoutError as FuturesTimeoutError

from PySide6.QtWidgets import QWidget, QMessageBox, QVBoxLayout, QApplication
from PySide6.QtCore import QTimer

from ui.windows.mainWindow import Ui_Form
from ui.rt_plot_widget import DepositionPlotWidget
from ui.material_catalog_dialog import MaterialCatalogDialog
from services.stm_service import STMService

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
    # ✅ Material catalog에서 가져올 ramp 파라미터 키 목록
    _RAMP_KEYS = (
        "ramp_step_dac",
        "ramp_seg1_max_dac",
        "ramp_interval_seg1_s",
        "ramp_seg2_max_dac",
        "ramp_interval_seg2_s",
        "ramp_interval_after_seg2_s",
        "ignite_dac",
        "ignite_rate_min",
        "ignite_timeout_s",
        "pre_rate",
        "pre_hold_s",
        "pre_hold_adjust_interval_s",
        "pre_drop_ratio",
        "pre_drop_count",
        "dac_adjust_interval_s",
        "fine_step_dac",
        "material_shortage_dac",
        "material_shortage_rate_max",
        "material_shortage_time_s",
    )

    # ✅ float 비교가 필요한 키(듀얼 파워 동일성 검사에 사용)
    _RAMP_FLOAT_KEYS = {
        "ramp_interval_seg1_s",
        "ramp_interval_seg2_s",
        "ramp_interval_after_seg2_s",
        "ignite_rate_min",
        "ignite_timeout_s",
        "pre_rate",
        "pre_hold_s",
        "pre_hold_adjust_interval_s",
        "pre_drop_ratio",
        "dac_adjust_interval_s",
        "material_shortage_rate_max",
        "material_shortage_time_s",
    }

    def __init__(self):
        super().__init__()
        self.ui = Ui_Form()
        self.ui.setupUi(self)

        # ✅ 공정별 ProcessWindowLog 파일 식별자
        self._active_run_id: Optional[str] = None
        self._active_recipe_name: str = ""

        self.setWindowTitle("Process")
        self.ui.stackedWidget.setCurrentIndex(1)  # Process page

        self.hmi_window: Optional[HmiWindow] = None
        self._closing_all = False
        self._close_stop_guard = False   # ✅ closeEvent에서 stop 중복 실행 방지

        self._process_controller: Any = None
        self._log_service: Any = None
        self._stm_service: Any = None
        self._acs_service: Any = None

        # ✅ UI에 "실제로 connect된 STM 인스턴스" 추적용(경고/중복 connect 방지)
        self._stm_ui_bound: bool = False
        self._stm_ui_stm: Any = None

        self._material_1 = None
        self._material_2 = None

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

    def _check_stm_crystal_health_before_start(self) -> bool:
        """
        Start 버튼 눌렀을 때:
        - STM 연결된 후(U/V 등) LIFE/FREQ/CRYSTAL FAIL을 읽어서
        - 조건 만족할 때만 공정을 시작하도록 gate.
        """
        # --- 설정값(원하면 나중에 config로 빼도 됨) ---
        LIFE_MIN_PERCENT = 80.0

        # STM-100 스펙: 6 MHz crystal, max freq shift 1 MHz
        # → 정상적인 U(Hz) 값 sanity 범위(너무 타이트하게 잡지 않음)
        FREQ_MIN_MHZ = 5.0
        FREQ_MAX_MHZ = 6.1

        def _abort(title: str, msg: str) -> bool:
            # ✅ 1) 먼저 UI에 로그 출력
            _append_text(getattr(self.ui, "logWindow", None), f"[PRECHECK][BLOCK] {title}: {msg}")

            # ✅ 2) 프리체크 때문에 켜둔 장비/상태 정리(FTM OFF 포함)
            with contextlib.suppress(Exception):
                self._shutdown_sensors_and_release_memory()  # 여기 안에 FTM OFF + PC detach 포함

            # ✅ 3) 사용자 경고
            QMessageBox.warning(self, title, msg)

            # ✅ 4) run 생명주기(open/close)는 main.py가 아니라 ProcessController/Engine이 담당
            self._active_run_id = None
            self._active_recipe_name = ""
            return False

        stm = getattr(self, "_stm_service", None)
        if stm is None:
            return _abort("STM", "STM 서비스가 없습니다. (STM 연결 실패)")

        if not hasattr(stm, "submit_read_crystal_health"):
            return _abort(
                "STM",
                "STM 서비스에 submit_read_crystal_health()가 없습니다.\n"
                "services/stm_service.py 수정이 반영됐는지 확인하세요."
            )

        # 1) LIFE/FREQ 스냅샷 읽기
        try:
            fut = stm.submit_read_crystal_health()
            snap = fut.result(timeout=3.0)
        except FuturesTimeoutError:
            return _abort("STM", "STM LIFE/FREQ 읽기 Timeout (통신/전원/케이블/FTM_SW 상태 확인 필요)")
        except Exception as e:
            return _abort("STM", f"STM LIFE/FREQ 읽기 실패: {e!r}")

        # 2) 값 파싱
        crystal_fail_raw = snap.get("crystal_fail", None)
        life = snap.get("life_percent", None)
        freq_mhz = snap.get("freq_mhz", None)

        reasons = []

        # ✅ 0) stm_service가 준 errors도 이유에 포함
        errs = snap.get("errors", None)
        if errs:
            if isinstance(errs, (list, tuple)):
                for e in errs:
                    reasons.append(f"STM 오류: {e}")
            else:
                reasons.append(f"STM 오류: {errs}")

        # ✅ 1) crystal_fail = None(확인불가)면 차단
        if crystal_fail_raw is None:
            reasons.append("CRYSTAL FAIL 상태 확인 불가")
        elif bool(crystal_fail_raw):
            reasons.append("CRYSTAL FAIL 상태")

        try:
            if life is None:
                reasons.append("LIFE 값을 읽지 못함")
            else:
                life_f = float(life)
                if life_f < LIFE_MIN_PERCENT:
                    reasons.append(f"LIFE {life_f:.1f}% < {LIFE_MIN_PERCENT:.1f}%")
        except Exception:
            reasons.append(f"LIFE 값이 숫자가 아님: {life!r}")

        try:
            if freq_mhz is None:
                reasons.append("FREQ 값을 읽지 못함")
            else:
                fmhz = float(freq_mhz)
                if not (FREQ_MIN_MHZ <= fmhz <= FREQ_MAX_MHZ):
                    reasons.append(f"FREQ {fmhz:.3f} MHz (정상범위 {FREQ_MIN_MHZ}~{FREQ_MAX_MHZ} MHz 밖)")
        except Exception:
            reasons.append(f"FREQ 값이 숫자가 아님: {freq_mhz!r}")

        # 4) 실패면 공정 시작 차단
        if reasons:
            def _fmt_pct(v) -> str:
                try:
                    return f"{float(v):.1f}%"
                except Exception:
                    return "None" if v is None else f"{v!r}"

            def _fmt_mhz(v) -> str:
                try:
                    return f"{float(v):.3f} MHz"
                except Exception:
                    return "None" if v is None else f"{v!r}"

            life_str = _fmt_pct(life)
            freq_str = _fmt_mhz(freq_mhz)

            msg = (
                "STM 크리스탈 상태 불량으로 공정을 시작하지 않습니다.\n\n"
                f"- LIFE: {life_str}\n"
                f"- FREQ: {freq_str}\n\n"
                "사유:\n- " + "\n- ".join(reasons)
            )
            return _abort("STM Pre-check", msg)

        # 5) 통과 로그
        try:
            _append_text(
                getattr(self.ui, "logWindow", None),
                f"[PRECHECK] STM OK: LIFE {float(life):.1f}% | FREQ {float(freq_mhz):.3f} MHz"
            )
        except Exception:
            _append_text(getattr(self.ui, "logWindow", None), f"[PRECHECK] STM OK: {snap!r}")

        return True
    
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

    def _ensure_sensors_connected_fresh(self) -> bool:
        if self.hmi_window is None:
            _append_text(getattr(self.ui, "logWindow", None), "[DEV][ERR] hmi_window is None -> cannot connect STM")
            return False

        ini_path = self.hmi_window._ini_path

        # ✅ 이전 STM 정리 (여기서도 FTM OFF까지 되도록 2번 수정이 전제)
        self._shutdown_sensors_and_release_memory()

        binder = getattr(self.hmi_window, "_plc_binder", None)
        if binder is None:
            _append_text(self.ui.logWindow, "[DEV][ERR] plc_binder is None (cannot turn on FTM_SW) -> abort STM connect")
            return False

        try:
            binder.enqueue_write("FTM_SW", True)
            _append_text(self.ui.logWindow, "[DEV] FTM_SW -> ON (before STM connect)")

            time.sleep(1.5)

            stm = STMService(ini_path=ini_path)
            stm.start()

            self._stm_service = stm
            self.hmi_window._stm_service = stm

            self._acs_service = getattr(self.hmi_window, "_acs_service", None)
            self.hmi_window._set_process_controller_devices(stm, self._acs_service)

            _append_text(self.ui.logWindow, "[DEV] STM connected (ACS kept alive).")
            return True

        except Exception as e:
            _append_text(self.ui.logWindow, f"[DEV][ERR] STM start failed: {e!r}")

            # ✅ 실패했으면 FTM 다시 OFF
            with contextlib.suppress(Exception):
                binder.enqueue_write("FTM_SW", False)
                _append_text(self.ui.logWindow, "[DEV] FTM_SW -> OFF (STM start failed)")

            self._stm_service = None
            self.hmi_window._stm_service = None
            return False

    def _shutdown_sensors_and_release_memory(self) -> None:
        """Stop에서 호출: STM 연결 해제 + 객체 해제 + gc.collect()
        ✅ ACS는 항상 켜져있는 장비이므로 여기서 stop하지 않는다.
        """

        # ✅ 먼저 UI 시그널 해제
        try:
            self._unbind_stm_ui()
        except Exception:
            pass

        # ✅ 1) STM만 stop()
        if self._stm_service is not None:
            try:
                self._stm_service.stop()
                _append_text(self.ui.logWindow, "[DEV] STM stop() called")
            except Exception as e:
                _append_text(self.ui.logWindow, f"[DEV][WARN] STM stop failed: {e!r}")

        # ✅ 2) ProcessController에서 STM 참조/시그널까지 정리(ACS는 유지)
        try:
            if self.hmi_window is not None:
                self.hmi_window._set_process_controller_devices(None, self._acs_service)
        except Exception:
            pass

        # ✅ 3) 참조 제거(ACS는 건드리지 않음)
        self._stm_service = None
        if self.hmi_window is not None:
            self.hmi_window._stm_service = None

        # ✅ 4) FTM OFF (S`TM을 더 이상 쓰지 않으면 반드시 끈다)
        try:
            binder = getattr(self.hmi_window, "_plc_binder", None) if self.hmi_window else None
            if binder is not None:
                binder.enqueue_write("FTM_SW", False)
                _append_text(self.ui.logWindow, "[DEV] FTM_SW -> OFF (after STM stop)")
        except Exception as e:
            _append_text(self.ui.logWindow, f"[DEV][WARN] FTM_SW off failed: {e!r}")
            
        # 5) GC
        gc.collect()
        _append_text(self.ui.logWindow, "[DEV] STM released + gc.collect() (ACS kept alive)")

    def _on_start_clicked(self) -> None:
        pc = self._process_controller
        if pc is None:
            QMessageBox.warning(self, "Process", "ProcessController가 연결되지 않았습니다.")
            return

        # ✅ 1) UI 입력값 먼저 검증
        run_cfg = self._collect_ui_run_cfg()
        if run_cfg is None:
            return
        
        proc_cfg = run_cfg.get("process_config") or {}
        steps = proc_cfg.get("ramp_steps") or []
        if steps:
            step_desc = " | ".join(
                f"S{idx+1}: ADC {float(s.get('target_adc', 0.0)):.1f}, Delay {float(s.get('delay_s', 0.0)):.1f}s"
                for idx, s in enumerate(steps)
            )
            _append_text(getattr(self.ui, "logWindow", None), f"[CFG] {step_desc}")

        _append_text(
            getattr(self.ui, "logWindow", None),
            "[CFG] "
            f"after_last_step_policy={proc_cfg.get('after_last_step_policy')} | "
            f"extra_ramp={proc_cfg.get('extra_ramp')}"
        )

        # ✅ run_id만 생성해서 ProcessController로 전달
        #    실제 open_run()/close_run()는 ProcessController/Engine 쪽에서 담당
        self._active_run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        pname = str(run_cfg.get("process_name", "") or "").strip()
        mat = str(run_cfg.get("material_name", "") or "").strip()

        # 현재 main.py에서는 recipe_name을 파일 open 용도로 직접 쓰지 않음
        self._active_recipe_name = pname if pname else (f"EVAP_{mat}" if mat else "EVAP")

        # ✅ 2) PLC 연결 상태 먼저 확인
        if not self._check_plc_ready_before_start():
            self._active_run_id = None
            self._active_recipe_name = ""
            return

        # ✅ 3) 그 다음 FTM ON → STM 연결
        if not self._ensure_sensors_connected_fresh():
            QMessageBox.warning(self, "Device Connect Failed", "STM 연결 실패")

            # ✅ 한 곳으로 정리 통일 (FTM OFF 포함)
            with contextlib.suppress(Exception):
                self._shutdown_sensors_and_release_memory()
            with contextlib.suppress(Exception):
                self._reset_process_ui()

            self._active_run_id = None
            self._active_recipe_name = ""
            return
        
        # ✅ 2.5) STM 크리스탈 상태(LIFE/FREQ) 사전 점검: 통과 시에만 공정 진행
        if not self._check_stm_crystal_health_before_start():
            return

        # ✅ 3) STM UI 바인딩 + RT 시작
        try:
            if self._stm_service is not None:
                self._bind_stm_ui(self._stm_service)
        except Exception:
            pass
        self._rt_start()

        # ✅ 4) 공정 시작
        if not hasattr(pc, "start_from_ui"):
            QMessageBox.warning(self, "Process", "start_from_ui가 구현되어 있지 않습니다.\nProcessController에 start_from_ui를 추가하세요.")
            self._rt_stop()
            self._shutdown_sensors_and_release_memory()
            self._reset_process_ui()

            self._active_run_id = None
            self._active_recipe_name = ""
            return
        
        try:
            # ✅ (변경) run_id를 ProcessController로 전달 (process log/csv와 묶이게)
            pc.start_from_ui(run_cfg, run_id=self._active_run_id)

        except Exception as e:
            with contextlib.suppress(Exception):
                self._safe_shutdown_plc_best_effort()
            with contextlib.suppress(Exception):
                self._rt_stop()
            with contextlib.suppress(Exception):
                self._shutdown_sensors_and_release_memory()
            with contextlib.suppress(Exception):
                self._reset_process_ui()

            self._active_run_id = None
            self._active_recipe_name = ""

            QMessageBox.warning(self, "Process Start Failed", f"{e!r}")
            _append_text(self.ui.logWindow, f"[START FAIL] {e!r}")
            return
            
    def _on_stop_clicked(self) -> None:
        try:
            # ✅ 1) 제일 먼저 하드웨어 안전종료(셔터→DAC0→파워OFF)
            self._safe_shutdown_plc_best_effort()

            # ✅ 2) 그 다음 UI RT 멈춤
            self._rt_stop()

            # ✅ 3) 공정 로직 stop
            pc = self._process_controller
            if pc is not None:
                try:
                    pc.stop()
                    _append_text(self.ui.logWindow, "[UI] 공정 정지 요청")
                except Exception as e:
                    _append_text(self.ui.logWindow, f"[STOP FAIL] {e!r}")

            # ✅ 4) Stop에서 STM/ACS 끊고 메모리 해제
            self._shutdown_sensors_and_release_memory()

            # ✅ UI 초기화
            self._reset_process_ui()

        finally:
            # ✅ run close는 ProcessController/Engine이 담당
            self._active_run_id = None
            self._active_recipe_name = ""

    def _on_pause_clicked(self) -> None:
        pc = self._process_controller
        if pc is None:
            return
        try:
            pc.pause()
            _append_text(getattr(self.ui, "logWindow", None), "[UI] 일시정지 요청")
        except Exception as e:
            _append_text(getattr(self.ui, "logWindow", None), f"[PAUSE FAIL] {e!r}")

    def _on_resume_clicked(self) -> None:
        pc = self._process_controller
        if pc is None:
            return
        try:
            pc.resume()
            _append_text(getattr(self.ui, "logWindow", None), "[UI] 재개 요청")
        except Exception as e:
            _append_text(getattr(self.ui, "logWindow", None), f"[RESUME FAIL] {e!r}")

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

        extra = dict(cfg.get("extra_ramp") or {})
        if bool(extra.get("enabled", False)):
            try:
                v = float(extra.get("max_adc", 0.0) or 0.0)
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
            # ✅ UI RT 정지
            with contextlib.suppress(Exception):
                self._rt_stop()

            # ✅ 운영 안전: best-effort 안전정지
            with contextlib.suppress(Exception):
                self._safe_shutdown_plc_best_effort()

            # ✅ 센서/메모리 정리
            with contextlib.suppress(Exception):
                self._shutdown_sensors_and_release_memory()

            # ✅ finished 로그(파일에 남기기 위해 close_run 전에 출력)
            try:
                ok = bool(getattr(result, "ok", False))
                rid = getattr(result, "run_id", "")
                _append_text(getattr(self.ui, "logWindow", None), f"[FINISHED] ok={ok} run_id={rid}")
            except Exception:
                _append_text(getattr(self.ui, "logWindow", None), "[FINISHED]")

        finally:
            # ✅ run close는 ProcessController/Engine이 담당
            self._active_run_id = None
            self._active_recipe_name = ""

            # ✅ 성공/실패/예외 상관없이 UI 초기화 보장
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

    def closeEvent(self, event):
        # ✅ Process 창 닫기(X) = Stop 버튼과 동일하게 동작
        if not getattr(self, "_close_stop_guard", False):
            self._close_stop_guard = True
            try:
                self._on_stop_clicked()
            except Exception:
                pass

        stopped = True
        try:
            stopped = self._wait_process_stop(timeout_s=3.0)
        except Exception:
            stopped = True

        if not stopped:
            _append_text(
                getattr(self.ui, "logWindow", None),
                "[UI][WARN] Process stop wait timeout -> close canceled"
            )
            QMessageBox.warning(
                self,
                "Process",
                "공정 정지 완료를 아직 확인하지 못했습니다.\n"
                "잠시 후 다시 닫아주세요."
            )
            self._close_stop_guard = False
            event.ignore()
            return

        _append_text(
            getattr(self.ui, "logWindow", None),
            "[UI] Process window closed -> SAFE STOP confirmed"
        )

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
        }

        # ✅ ramp 파라미터들까지 함께 저장 (구버전 sel에도 안전하게 getattr)
        for k in getattr(self, "_RAMP_KEYS", ()):
            if hasattr(sel, k):
                data[k] = getattr(sel, k)

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
        """
        공정용 기본 설정.
        - step은 최소 1개
        - 지금은 ADC 기준 램프업 step 구조를 미리 보관만 한다.
        """
        return {
            "step_count": 1,
            "ramp_steps": [
                {
                    "target_adc": 100.0,
                    "delay_s": 0.0,
                }
            ],
            # step 중 dep.rate 도달 시 즉시 메인 공정 진입
            "reach_main_on_rate": True,

            # 모든 step 종료 후 dep.rate 미도달 시 정책
            # "extra_ramp" | "stop"
            "after_last_step_policy": "extra_ramp",

            # 추가 ramp 정책
            "extra_ramp": {
                "enabled": True,
                "max_adc": 300.0,
                "step_max": 50.0,     # 동적 증가폭 상한(100 이하)
                "interval_s": 5.0,
            },
        }


    def _normalize_process_config(self, cfg: Any) -> dict[str, Any]:
        """
        dialog / json / 임시 dict 어떤 형태로 와도
        최소한 engine/controller로 넘길 수 있는 표준 형태로 맞춘다.
        """
        default = self._default_process_config()
        src = dict(cfg or {})

        raw_steps = src.get("ramp_steps") or src.get("steps") or default["ramp_steps"]
        steps: list[dict[str, float]] = []

        for item in list(raw_steps)[:10]:
            try:
                target_adc = max(0.0, float((item or {}).get("target_adc", 0.0)))
                delay_s = max(0.0, float((item or {}).get("delay_s", 0.0)))
            except Exception:
                continue
            steps.append({
                "target_adc": target_adc,
                "delay_s": delay_s,
            })

        if not steps:
            steps = list(default["ramp_steps"])

        step_count = int(src.get("step_count", len(steps)) or len(steps))
        step_count = max(1, min(10, step_count))
        steps = steps[:step_count]

        extra_ramp_src = dict(src.get("extra_ramp") or default["extra_ramp"])
        extra_ramp = {
            "enabled": bool(extra_ramp_src.get("enabled", True)),
            "max_adc": max(0.0, float(extra_ramp_src.get("max_adc", 300.0) or 0.0)),
            "step_max": min(100.0, max(1.0, float(extra_ramp_src.get("step_max", 50.0) or 50.0))),
            "interval_s": max(0.1, float(extra_ramp_src.get("interval_s", 5.0) or 5.0)),
        }

        policy = str(src.get("after_last_step_policy", "extra_ramp") or "extra_ramp").strip().lower()
        if policy not in {"extra_ramp", "stop"}:
            policy = "extra_ramp"

        return {
            "step_count": len(steps),
            "ramp_steps": steps,
            "reach_main_on_rate": bool(src.get("reach_main_on_rate", True)),
            "after_last_step_policy": policy,
            "extra_ramp": extra_ramp,
        }


    def _open_process_config_dialog(self) -> None:
        """
        아직 ui/process_config_dialog.py 가 없더라도
        프로그램이 바로 죽지 않게 lazy import 로 처리한다.
        """
        try:
            from ui.process_config_dialog import ProcessConfigDialog
        except Exception as e:
            QMessageBox.information(
                self,
                "Process Config",
                "process_config_dialog.py 가 아직 준비되지 않았습니다.\n"
                "다음 단계에서 추가할 예정입니다."
            )
            _append_text(getattr(self.ui, "logWindow", None), f"[CFG][WARN] dialog import failed: {e!r}")
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
            f"{idx+1}:{float(s.get('target_adc', 0.0)):.1f}/{float(s.get('delay_s', 0.0)):.1f}s"
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
            f"steps={len(steps)} | {step_desc} | "
            f"policy={self._process_cfg.get('after_last_step_policy')}"
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

        # ✅ 그래프는 이제 ADC 기준
        graph_power = self._sum_selected_pair(adc1, adc2)
        graph_power = self._clamp_nonneg(graph_power)

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

        # ✅ 기존 actualPower = ADC 표시
        self._update_actual_power_ui(adc1, adc2)

        # ✅ 그래프는 ADC 기준으로 append
        if self._plot is not None:
            try:
                self._plot.append(rate=rate, power=graph_power)
            except Exception:
                pass
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
        use1 = bool(getattr(getattr(self.ui, "sourcePower1", None), "isChecked", lambda: False)())
        use2 = bool(getattr(getattr(self.ui, "sourcePower2", None), "isChecked", lambda: False)())
        return use1, use2


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

    def _read_plc_power_dac(self) -> Optional[float]:
        """선택된 power 기준 DAC 합계/단일값."""
        p1, p2 = self._read_plc_power_dac_pair()
        return self._sum_selected_pair(p1, p2)
        
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

                # ✅ (추가) ramp 파라미터 동일성 검사
                diff_keys: list[str] = []
                for k in getattr(self, "_RAMP_KEYS", ()):
                    v1 = (self._material_1 or {}).get(k, None)
                    v2 = (self._material_2 or {}).get(k, None)

                    # 둘 다 없으면 OK(구버전 JSON 등)
                    if v1 is None and v2 is None:
                        continue
                    # 한쪽만 있으면 불일치
                    if (v1 is None) != (v2 is None):
                        diff_keys.append(k)
                        continue

                    try:
                        if k in getattr(self, "_RAMP_FLOAT_KEYS", set()):
                            if abs(float(v1) - float(v2)) > 1e-9:
                                diff_keys.append(k)
                        else:
                            if int(float(v1)) != int(float(v2)):
                                diff_keys.append(k)
                    except Exception:
                        if v1 != v2:
                            diff_keys.append(k)

                if diff_keys:
                    QMessageBox.warning(
                        self,
                        "Input",
                        "Power1+Power2 동시 사용은 '동일 Ramp 설정' 가정입니다.\n"
                        "Material1/Material2의 ramp 파라미터가 서로 다릅니다:\n"
                        + ", ".join(diff_keys),
                    )
                    return None
                
        else:
            base_mat = self._material_1 if p1 else self._material_2

        mat_name = str((base_mat or {}).get("material", "")).strip()
        den = float((base_mat or {}).get("density_g_cm3", 0.0) or 0.0)
        zf = float((base_mat or {}).get("z_factor", 0.0) or 0.0)

        # ✅ (추가) base_mat에서 ramp 파라미터 묶어서 run_cfg로 전달
        ramp_cfg: dict[str, Any] = {}
        for k in getattr(self, "_RAMP_KEYS", ()):
            v = (base_mat or {}).get(k, None)
            if v is not None:
                ramp_cfg[k] = v

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

        cfg: dict[str, Any] = {
            "process_name": pname,

            "use_power1": p1,
            "use_power2": p2,

            "material_name": mat_name,
            "density": den,
            "z_factor": zf,

            # 기존 material catalog 기반 ramp 정보
            "ramp": ramp_cfg,

            "target_rate": float(target_rate),
            "target_thickness": float(target_thk),
            "delay_min": float(delay_min),

            "material_1": self._material_1,
            "material_2": self._material_2,

            # ✅ 신규 process config
            "adc_control_mode": "adc",
            "process_config": proc_cfg,
            "ramp_steps": list(steps),
            "after_last_step_policy": proc_cfg.get("after_last_step_policy", "extra_ramp"),
            "extra_ramp": dict(proc_cfg.get("extra_ramp") or {}),
        }
        return cfg
    
    # ================== UI 값 파싱 ==================

    # ================== 공정 종료 함수 ==================
    def _safe_shutdown_plc_best_effort(self) -> None:
        """
        Stop 시 안전종료 순서:
        1) MAIN_SHUTTER close
        2) DAC 0
        3) POWER off
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