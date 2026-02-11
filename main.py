# main.py
from __future__ import annotations

import gc
import sys
from pathlib import Path
from typing import Optional, Any

# ✅ 어디서 실행하든(import 깨짐 방지) 가장 먼저 보정
_BASE_DIR = Path(__file__).resolve().parent
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

from PySide6.QtWidgets import QApplication, QWidget, QMessageBox, QDialog, QVBoxLayout
from PySide6.QtCore import Qt, QTimer

# ✅ 그래프 위젯(별도 파일)
from ui.rt_plot_widget import DepositionPlotWidget
from ui.mainWindow import Ui_Form
from ui.config_dialog import ConfigDialog
from config.plc_config import load_plc_settings
from controller.hmi_plc_binder import HmiPlcBinder
from utils.device_manager import DeviceManager
from ui.material_catalog_dialog import MaterialCatalogDialog


# ============================================================
# (선택) 신규 공정/서비스 import
# - 없으면 기존 main.py처럼 HMI/PLC만 돌아가도록 자동 fallback
# ============================================================
try:
    from controller.process_controller import ProcessController  # type: ignore
except ImportError:
    ProcessController = None  # type: ignore

try:
    from services.log_service import LogService  # type: ignore
except ImportError:
    LogService = None  # type: ignore

try:
    from services.stm_service import STMService  # type: ignore
except ImportError:
    STMService = None  # type: ignore

try:
    from services.acs_service import ACSService  # type: ignore
except ImportError:
    ACSService = None  # type: ignore


# ============================================================
# UI 텍스트 출력 유틸 (QPlainTextEdit / QTextEdit / QLabel 등 방어)
# ============================================================
def _append_text(widget: Any, text: str) -> None:
    if widget is None:
        return
    s = str(text).rstrip("\n")
    try:
        # QPlainTextEdit
        if hasattr(widget, "appendPlainText"):
            widget.appendPlainText(s)
            return
        # QTextEdit
        if hasattr(widget, "append"):
            widget.append(s)
            return
        # QLabel / QLineEdit
        if hasattr(widget, "setText") and hasattr(widget, "text"):
            prev = widget.text() or ""
            widget.setText((prev + "\n" + s).strip())
            return
    except Exception:
        pass


# ============================================================
# 공정용 PLC 어댑터
# - HMI 버튼 동작(미연결 팝업/원복)은 기존처럼 HmiPlcBinder가 계속 담당
# - 공정 엔진은 PLC 포트를 새로 열지 않고 Binder의 워커 큐를 "공유"해서 사용
# ============================================================
class PlcAdapterFromBinder:
    def __init__(self, binder: HmiPlcBinder):
        self._binder = binder

    def start(self) -> None:
        # binder.start()는 main에서 이미 하지만, 중복 호출 방어
        try:
            self._binder.start()
        except Exception:
            pass

    def stop(self) -> None:
        try:
            self._binder.stop()
        except Exception:
            pass

    def is_running(self) -> bool:
        try:
            w = getattr(self._binder, "_worker", None)
            return bool(w and w.isRunning())
        except Exception:
            return False

    def is_connected(self) -> bool:
        try:
            return bool(self._binder.is_connected())
        except Exception:
            return False

    # --- 공정에서 상태 조회가 필요할 때(폴링 캐시 기반) ---
    def get_states(self) -> dict[str, bool]:
        try:
            return dict(self._binder.get_states() or {})
        except Exception:
            return {}

    def read_coil(self, coil_name: str, default: bool = False) -> bool:
        try:
            return bool(self.get_states().get(str(coil_name), bool(default)))
        except Exception:
            return bool(default)

    # --- 공정에서 write ---
    def enqueue_write(self, coil_name: str, on: bool, momentary: bool = False) -> None:
        # ✅ Binder가 제공하는 연결체크/방어 로직을 그대로 타게 함
        self._binder.enqueue_write(str(coil_name), bool(on), momentary=bool(momentary))

    # alias(엔진 구현 차이 방어)
    def write_coil(self, coil_name: str, on: bool) -> None:
        self.enqueue_write(coil_name, on, momentary=False)

    def set_coil(self, coil_name: str, on: bool) -> None:
        self.enqueue_write(coil_name, on, momentary=False)

    def pulse_coil(self, coil_name: str) -> None:
        self.enqueue_write(coil_name, True, momentary=True)


# ============================================================
# HMI 창
# ============================================================
class HmiWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.ui = Ui_Form()
        self.ui.setupUi(self)

        self.setWindowTitle("HMI")
        self.ui.stackedWidget.setCurrentIndex(0)  # HMI page

        self.process_window: Optional["ProcessWindow"] = None
        self._closing_all = False

        # ✅ Config / runtime objects (기존 유지)
        self._ini_path = _BASE_DIR / "config" / "devices.ini"
        self._plc_binder: HmiPlcBinder | None = None
        self._dev_mgr: DeviceManager | None = None

        # ✅ 신규 공정/서비스 (있으면 주입)
        self._process_controller: Any = None
        self._log_service: Any = None
        self._stm_service: Any = None
        self._acs_service: Any = None

        # Process 버튼: Process 창 앞으로 (기존 유지)
        self.ui.processBtn.clicked.connect(self.goto_process_window)

        # Config 버튼: 팝업 열기 (기존 유지)
        if hasattr(self.ui, "configBtn"):
            self.ui.configBtn.clicked.connect(self.open_config_dialog)

    def goto_process_window(self):
        # ✅ 닫혀있거나 아직 없으면 새로 생성 (기존 유지)
        if self.process_window is None:
            self.process_window = ProcessWindow()
            self.process_window.set_hmi_window(self)

            self.process_window.setAttribute(Qt.WA_DeleteOnClose, True)
            self.process_window.destroyed.connect(self._on_process_destroyed)

            # ✅ 신규 공정/로그/센서 주입
            self.process_window.set_runtime_objects(
                process_controller=self._process_controller,
                log_service=self._log_service,
                stm_service=self._stm_service,
                acs_service=self._acs_service,
            )

        self.process_window.show()
        self.process_window.raise_()
        self.process_window.activateWindow()

    def _on_process_destroyed(self, *args):
        self.process_window = None

    def set_runtime_objects(
        self,
        plc_binder: HmiPlcBinder,
        dev_mgr: Optional[DeviceManager],
        ini_path: Path,
        *,
        process_controller: Any = None,
        log_service: Any = None,
        stm_service: Any = None,
        acs_service: Any = None,
    ) -> None:
        """main()에서 만든 런타임 객체 주입(PLC/센서 재연결에 사용)"""
        self._plc_binder = plc_binder
        self._dev_mgr = dev_mgr
        self._ini_path = Path(ini_path)

        self._process_controller = process_controller
        self._log_service = log_service
        self._stm_service = stm_service
        self._acs_service = acs_service

    def _set_process_controller_devices(self, stm: Any, acs: Any) -> None:
        """ProcessController가 stm/acs를 참조한다면 여기서 갈아끼움."""
        pc = self._process_controller
        if pc is None:
            return
        for attr, val in (("stm", stm), ("acs", acs)):
            try:
                if hasattr(pc, attr):
                    setattr(pc, attr, val)
                elif hasattr(pc, f"_{attr}"):
                    setattr(pc, f"_{attr}", val)
            except Exception:
                pass

    def open_config_dialog(self) -> None:
        """Config 팝업 → Save(=Accepted)면 즉시 장비 재연결 (기존 유지)"""
        dlg = ConfigDialog(ini_path=self._ini_path, parent=self)
        ret = dlg.exec()
        if ret == QDialog.Accepted:
            self._apply_config_and_reconnect()

    def _apply_config_and_reconnect(self) -> None:
        errors: list[str] = []

        # ✅ 1) PLC만 즉시 재연결
        try:
            if self._plc_binder:
                new_plc_settings = load_plc_settings(self._ini_path)
                self._plc_binder.reload_settings(new_plc_settings)
        except Exception as e:
            errors.append(f"PLC reconnect failed: {e}")

        # ✅ 2) STM/ACS는 여기서 절대 재연결/재생성하지 않음
        #     (Stop→메모리해제, Start→재생성 규칙 유지)

        if errors:
            QMessageBox.warning(self, "Reconnect", "일부 장비 재연결 실패:\n" + "\n".join(errors))
        else:
            QMessageBox.information(
                self,
                "Reconnect",
                "저장 완료. (PLC만 즉시 적용)\nSTM/ACS는 다음 Start에서 새로 생성/연결됩니다.",
            )

    def _confirm_exit(self) -> bool:
        ret = QMessageBox.question(
            self,
            "종료 확인",
            "정말 종료하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return ret == QMessageBox.Yes

    def closeEvent(self, event):
        if getattr(self, "_closing_all", False):
            event.accept()
            return

        if not self._confirm_exit():
            event.ignore()
            return

        self._closing_all = True

        # ✅ Process가 열려있으면 같이 닫기 (기존 유지)
        if self.process_window is not None:
            try:
                self.process_window._closing_all = True
                self.process_window.close()
            except Exception:
                pass
            self.process_window = None

        event.accept()


# ============================================================
# Process 창
# ============================================================
class ProcessWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.ui = Ui_Form()
        self.ui.setupUi(self)

        self.setWindowTitle("Process")
        self.ui.stackedWidget.setCurrentIndex(1)  # Process page

        self.hmi_window: Optional[HmiWindow] = None
        self._closing_all = False

        self._process_controller: Any = None
        self._log_service: Any = None
        self._stm_service: Any = None
        self._acs_service: Any = None

        self._material_1 = None
        self._material_2 = None

        self.ui.materialEdit.clicked.connect(lambda: self._open_material_dialog(1))
        self.ui.materialEdit2.clicked.connect(lambda: self._open_material_dialog(2))

        self.ui.hmiBtn.clicked.connect(self.goto_hmi_window)

        self.ui.startProcess.clicked.connect(self._on_start_clicked)   
        self.ui.stopProcess.clicked.connect(self._on_stop_clicked)   

        # =========================
        # ✅ RT 표시(1초) + 그래프
        # =========================
        self._last_rate: Optional[float] = None
        self._last_thickness: Optional[float] = None
        self._last_power: Optional[float] = None   # ✅ 추가

        self._plot: Optional[DepositionPlotWidget] = None
        self._init_rt_plot()  # graphWidget 자리에 plot 삽입

        self._rt_timer = QTimer(self)
        self._rt_timer.setInterval(1000)  # ✅ 1초
        self._rt_timer.timeout.connect(self._tick_rt_ui)

    def set_hmi_window(self, hmi_window: HmiWindow):
        self.hmi_window = hmi_window

    def set_runtime_objects(self, *, process_controller: Any, log_service: Any, stm_service: Any, acs_service: Any) -> None:
        self._process_controller = process_controller
        self._log_service = log_service
        self._stm_service = stm_service
        self._acs_service = acs_service

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

        # LogService가 있으면 추가로 같이 출력(선택)
        lg = self._log_service
        if lg is not None and hasattr(lg, "sig_line"):
            try:
                lg.sig_line.connect(lambda s: _append_text(getattr(self.ui, "logWindow", None), s))
            except Exception:
                pass

    def goto_hmi_window(self):
        if not self.hmi_window:
            return
        self.hmi_window.show()
        self.hmi_window.raise_()
        self.hmi_window.activateWindow()

    def _bind_stm_ui(self, stm):
        self._unbind_stm_ui()
        stm.sig_rate.connect(self._on_stm_rate)
        stm.sig_thickness.connect(self._on_stm_thickness)

    def _unbind_stm_ui(self):
        if not self._stm_service:
            return
        try: self._stm_service.sig_rate.disconnect(self._on_stm_rate)
        except: pass
        try: self._stm_service.sig_thickness.disconnect(self._on_stm_thickness)
        except: pass

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

    def _ensure_sensors_connected_fresh(self) -> None:
        """Start에서 호출: STM/ACS가 없으면 '새로 생성하고 연결(start)'"""
        if self.hmi_window is None:
            return
        ini_path = self.hmi_window._ini_path

        # 이미 살아있으면 재사용(Stop을 안 눌렀다면 여기로 들어올 수 있음)
        if (self._stm_service is not None) or (self._acs_service is not None) or (self.hmi_window._dev_mgr is not None):
            _append_text(self.ui.logWindow, "[DEV] STM/ACS already allocated (reuse).")
            return

        # 1) 신규 서비스(STMService/ACSService)가 있으면: Start에서 생성+start()
        if (STMService is not None) and (ACSService is not None):
            try:
                stm = STMService.from_ini(ini_path) if hasattr(STMService, "from_ini") else STMService(ini_path)
                acs = ACSService.from_ini(ini_path) if hasattr(ACSService, "from_ini") else ACSService(ini_path)

                # ✅ 연결은 Start에서만
                stm.start()
                acs.start()
                self._bind_stm_ui(stm)

                self._stm_service = stm
                self._acs_service = acs
                self.hmi_window._stm_service = stm
                self.hmi_window._acs_service = acs

                # ✅ ProcessController에 새 STM/ACS 주입
                self.hmi_window._set_process_controller_devices(stm, acs)

                _append_text(self.ui.logWindow, "[DEV] STM/ACS connected (fresh instance).")
                return
            except Exception as e:
                _append_text(self.ui.logWindow, f"[DEV][WARN] STM/ACS start failed: {e!r}")
                self._stm_service = None
                self._acs_service = None
                self.hmi_window._stm_service = None
                self.hmi_window._acs_service = None

        # 2) fallback: DeviceManager로 연결(서비스가 없을 때만)
        try:
            dev_mgr = DeviceManager.from_ini(ini_path)
            errs = dev_mgr.reload_from_ini(ini_path, connect=True)

            # 여기서 HMI에 dev_mgr를 “연결된 상태로” 잠깐 보관했다가 Stop에서 제거
            self.hmi_window._dev_mgr = dev_mgr

            if errs:
                _append_text(self.ui.logWindow, "[DEV][WARN] DeviceManager connect errors:")
                for k, v in errs.items():
                    _append_text(self.ui.logWindow, f"  - {k}: {v}")
            else:
                _append_text(self.ui.logWindow, "[DEV] STM/ACS connected (DeviceManager).")
        except Exception as e:
            _append_text(self.ui.logWindow, f"[DEV][WARN] DeviceManager connect exception: {e!r}")

    def _shutdown_sensors_and_release_memory(self) -> None:
        """Stop에서 호출: STM/ACS 연결 해제 + 객체 해제 + gc.collect()"""
        # 1) 서비스 기반이면 stop()만 호출 (후보 3개 금지)
        if self._stm_service is not None:
            try:
                self._stm_service.stop()
                _append_text(self.ui.logWindow, "[DEV] STM stop() called")
            except Exception as e:
                _append_text(self.ui.logWindow, f"[DEV][WARN] STM stop failed: {e!r}")

        if self._acs_service is not None:
            try:
                self._acs_service.stop()
                _append_text(self.ui.logWindow, "[DEV] ACS stop() called")
            except Exception as e:
                _append_text(self.ui.logWindow, f"[DEV][WARN] ACS stop failed: {e!r}")

        # 2) DeviceManager fallback이면 close_all()만 호출
        if self.hmi_window is not None and self.hmi_window._dev_mgr is not None:
            try:
                self.hmi_window._dev_mgr.close_all()
                _append_text(self.ui.logWindow, "[DEV] DeviceManager.close_all() called")
            except Exception as e:
                _append_text(self.ui.logWindow, f"[DEV][WARN] DeviceManager close failed: {e!r}")

        # ✅ ProcessController가 stm/acs 참조를 잡고 있으면 같이 끊어줘야 메모리 정리가 확실함
        try:
            pc = self._process_controller
            if pc is not None:
                if hasattr(pc, "stm"): setattr(pc, "stm", None)
                if hasattr(pc, "acs"): setattr(pc, "acs", None)
                if hasattr(pc, "_stm"): setattr(pc, "_stm", None)
                if hasattr(pc, "_acs"): setattr(pc, "_acs", None)
        except Exception:
            pass

        # 3) 참조 제거 → 메모리 회수
        self._stm_service = None
        self._acs_service = None
        if self.hmi_window is not None:
            self.hmi_window._stm_service = None
            self.hmi_window._acs_service = None
            self.hmi_window._dev_mgr = None

        # 4) GC
        gc.collect()
        _append_text(self.ui.logWindow, "[DEV] sensors released + gc.collect()")

    def _on_start_clicked(self) -> None:
        # ✅ Start에서만 STM/ACS 생성/연결
        self._ensure_sensors_connected_fresh()

        pc = self._process_controller
        if pc is None:
            QMessageBox.warning(self, "Process", "ProcessController가 연결되지 않았습니다.")
            return

        try:
            run_cfg = self._collect_ui_run_cfg()
            if run_cfg is None:
                return

            # ✅ 1초 RT 시작
            self._rt_start()

            # ✅ ProcessController에 UI 기반 시작 API가 있으면 그걸 사용
            if hasattr(pc, "start_from_ui"):
                pc.start_from_ui(run_cfg)
            else:
                # 레시피 자동 생성은 금지. 레시피가 없으면 시작 못한다고 알림.
                r = pc.get_recipe() if hasattr(pc, "get_recipe") else None
                if r is None:
                    QMessageBox.warning(self, "Process", "레시피가 없고 start_from_ui도 없습니다.\n(샘플 레시피 자동 생성은 제거됨)")
                    self._rt_stop()
                    return
                pc.start()

            _append_text(self.ui.logWindow, "[UI] 공정 시작 요청")
        except Exception as e:
            self._rt_stop()  # ✅ 추가
            QMessageBox.warning(self, "Process Start Failed", f"{e!r}")
            _append_text(self.ui.logWindow, f"[START FAIL] {e!r}")

    def _on_stop_clicked(self) -> None:
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
        # ✅ (추가) status에서 power/dac 값을 최대한 추출해서 그래프에 사용
        self._try_update_last_power(st)

        # UI에 표시용 위젯이 있으면 업데이트(없으면 무시)
        w = getattr(self.ui, "processMonitor_Process", None)
        if w is None:
            return
        try:
            phase = getattr(st, "phase", None)
            step = getattr(st, "step_name", None) or getattr(st, "step", None)
            msg = f"{phase} | {step}" if (phase or step) else str(st)
            if hasattr(w, "setText"):
                w.setText(msg)
        except Exception:
            pass

    def _try_update_last_power(self, st: Any) -> None:
        """
        ProcessController가 status에 power/dac 관련 값을 넣어줄 수도 있고,
        dict로 올 수도 있어서 최대한 방어적으로 추출한다.
        - 우선순위: total/combined -> (dac1+dac2) -> single
        """
        try:
            # dict 형태
            if isinstance(st, dict):
                for k in ("power", "power_dac", "dac", "dac_power", "power_cmd", "dac_cmd"):
                    if k in st and st[k] is not None:
                        self._last_power = float(st[k])
                        return
                # 2채널
                if ("dac1" in st) or ("dac2" in st):
                    d1 = float(st.get("dac1") or 0.0)
                    d2 = float(st.get("dac2") or 0.0)
                    self._last_power = d1 + d2
                    return
                return

            # 객체 형태
            for name in ("power", "power_dac", "dac", "dac_power", "power_cmd", "dac_cmd"):
                if hasattr(st, name):
                    v = getattr(st, name)
                    if v is not None:
                        self._last_power = float(v)
                        return

            # 2채널 객체 후보
            if hasattr(st, "dac1") or hasattr(st, "dac2"):
                d1 = float(getattr(st, "dac1", 0.0) or 0.0)
                d2 = float(getattr(st, "dac2", 0.0) or 0.0)
                self._last_power = d1 + d2
                return
        except Exception:
            # power는 없어도 공정은 돌 수 있으니 무시
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
            ok = bool(getattr(result, "ok", False))
            rid = getattr(result, "run_id", "")
            _append_text(getattr(self.ui, "logWindow", None), f"[FINISHED] ok={ok} run_id={rid}")
        except Exception:
            _append_text(getattr(self.ui, "logWindow", None), "[FINISHED]")

    def closeEvent(self, event):
        if getattr(self, "_closing_all", False):
            event.accept()
            return

        if self.hmi_window is not None:
            self.hmi_window.process_window = None

        event.accept()

    def _open_material_dialog(self, channel: int) -> None:
        sel = MaterialCatalogDialog.pick(base_dir=_BASE_DIR, parent=self)
        if not sel:
            return
        self._apply_material(channel, {
            "material": sel.material,
            "density_g_cm3": sel.density_g_cm3,
            "z_factor": sel.z_factor,
        })

    def _apply_material(self, channel: int, data: dict[str, Any]) -> None:
        mat = str(data.get("material", "")).strip()
        den = data.get("density_g_cm3")
        z = data.get("z_factor")

        if channel == 1:
            self._material_1 = dict(data)
            self.ui.materialEdit.setText(mat or "Select")
            self.ui.materialDensityEdit1.setText(f"{float(den):.4f}")
            self.ui.materialZfactorEdit1.setText(f"{float(z):.4f}")
        else:
            self._material_2 = dict(data)
            self.ui.materialEdit2.setText(mat or "Select")
            self.ui.materialDensityEdit2.setText(f"{float(den):.4f}")
            self.ui.materialZfactorEdit2.setText(f"{float(z):.4f}")

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

        self._plot = DepositionPlotWidget(parent=host)
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
        rate = self._last_rate
        th = self._last_thickness
        power = self._last_power

        try:
            self.ui.currentRateEdit.setText(f"{rate:.3f}" if rate is not None else "")
        except Exception:
            pass

        try:
            self.ui.currentThicknessEdit.setText(f"{th:.2f}" if th is not None else "")
        except Exception:
            pass

        if self._plot is not None:
            try:
                self._plot.append(rate=rate, power=power)
            except Exception:
                pass
    # ================== 그래프 설정 ==================

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
        # Power 선택(동시 가능)
        p1 = bool(getattr(getattr(self.ui, "sourcePower1", None), "isChecked", lambda: False)())
        p2 = bool(getattr(getattr(self.ui, "sourcePower2", None), "isChecked", lambda: False)())
        if not (p1 or p2):
            QMessageBox.warning(self, "Input", "Power1/Power2 중 최소 1개는 선택해야 합니다.")
            return None

        # 입력값
        target_rate = self._read_float("depositionRateEdit")  # UI objectName이 다르면 여기만 맞춰주면 됨
        target_thk  = self._read_float("thicknessEdit")       # "
        delay_min   = self._read_float("delayEdit")           # 분, 소수 허용

        if target_rate is None:
            QMessageBox.warning(self, "Input", "Target Dep.rate를 입력하세요.")
            return None
        if target_thk is None:
            QMessageBox.warning(self, "Input", "Target Thickness를 입력하세요.")
            return None
        if delay_min is None:
            delay_min = 0.0

        cfg: dict[str, Any] = {
            "use_power1": p1,
            "use_power2": p2,
            "material_1": self._material_1,
            "material_2": self._material_2,
            "target_dep_rate": float(target_rate),
            "target_thickness": float(target_thk),
            "delay_min": float(delay_min),
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

        # 2) DAC = 0 (binder가 reg write 지원하는 경우에만)
        try:
            if hasattr(binder, "enqueue_write_reg"):
                binder.enqueue_write_reg("DAC_POWER_1", 0)
                binder.enqueue_write_reg("DAC_POWER_2", 0)
                _append_text(self.ui.logWindow, "[SAFE] DAC_POWER_1/2 -> 0")
            else:
                _append_text(self.ui.logWindow, "[SAFE][WARN] binder has no enqueue_write_reg(). DAC=0 미지원")
        except Exception as e:
            _append_text(self.ui.logWindow, f"[SAFE][WARN] DAC=0 failed: {e!r}")

        # 3) power off
        try:
            binder.enqueue_write("POWER_1_SW", False)
            binder.enqueue_write("POWER_2_SW", False)
            _append_text(self.ui.logWindow, "[SAFE] POWER_1/2 -> OFF")
        except Exception as e:
            _append_text(self.ui.logWindow, f"[SAFE][WARN] POWER off failed: {e!r}")

        self._last_power = 0.0
    # ================== 공정 종료 함수 ==================


# ============================================================
# main
# ============================================================
def main():
    app = QApplication(sys.argv)

    hmi = HmiWindow()

    # ------------------------------
    # PLC 바인딩 시작 (✅ 기존 로직 유지)
    # ------------------------------
    ini_path = _BASE_DIR / "config" / "devices.ini"
    plc_settings = load_plc_settings(ini_path)

    # ✅ PLC 연결/버튼 팝업/원복 로직은 HmiPlcBinder가 계속 담당 (그대로 유지)
    plc_binder = HmiPlcBinder(hmi.ui, plc_settings)
    plc_binder.start()
    app.aboutToQuit.connect(plc_binder.stop)

    # ✅ STM/ACS는 Start에서만 생성/연결한다.
    dev_mgr: Optional[DeviceManager] = None
    stm_service: Any = None
    acs_service: Any = None

    # ------------------------------
    # LogService (신규)
    # ------------------------------
    log_service: Any = None
    if LogService is not None:
        try:
            # 생성자 시그니처 차이 방어
            try:
                log_service = LogService(base_dir=_BASE_DIR)
            except TypeError:
                log_service = LogService()
            if hasattr(log_service, "start"):
                log_service.start()
        except Exception:
            log_service = None

    # ------------------------------
    # ProcessController (신규)
    # - PLC는 포트 중복 방지를 위해 binder 공유 어댑터로 주입
    # ------------------------------
    process_controller: Any = None
    if ProcessController is not None:
        try:
            plc_for_process = PlcAdapterFromBinder(plc_binder)
            process_controller = ProcessController(
                plc=plc_for_process,
                log=log_service,
                stm=stm_service,
                acs=acs_service,
            )
        except Exception as e:
            process_controller = None
            _append_text(getattr(hmi.ui, "logWindow", None), f"[BOOT][WARN] ProcessController init failed: {e!r}")

    # 종료 시 신규 서비스 정리
    def _shutdown_new_services() -> None:
        if process_controller is not None:
            try:
                if process_controller.is_running():
                    process_controller.stop()
            except Exception:
                pass

        # ✅ stop()만 시도
        for svc in (getattr(hmi, "_stm_service", None), getattr(hmi, "_acs_service", None), log_service):
            if svc is None:
                continue
            fn = getattr(svc, "stop", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

        # 혹시 DeviceManager가 Start에서 생성되었을 수 있으니 HMI쪽도 정리
        try:
            if getattr(hmi, "_dev_mgr", None) is not None:
                hmi._dev_mgr.close_all()
        except Exception:
            pass

    app.aboutToQuit.connect(_shutdown_new_services)

    # ✅ HMI가 Config 저장 후 재연결할 수 있도록 주입(기존 기능 유지 + 신규 기능 추가)
    hmi.set_runtime_objects(
        plc_binder=plc_binder,
        dev_mgr=dev_mgr,
        ini_path=ini_path,
        process_controller=process_controller,
        log_service=log_service,
        stm_service=stm_service,
        acs_service=acs_service,
    )

    # ✅ HMI 표시 (기존 유지)
    hmi.show()

    def _focus_hmi():
        hmi.raise_()
        hmi.activateWindow()

    QTimer.singleShot(0, _focus_hmi)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
