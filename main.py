# main.py
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Any

# ✅ 어디서 실행하든(import 깨짐 방지) 가장 먼저 보정
_BASE_DIR = Path(__file__).resolve().parent
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

from PySide6.QtWidgets import QApplication, QWidget, QMessageBox, QDialog
from PySide6.QtCore import Qt, QTimer

from ui.mainWindow import Ui_Form
from ui.config_dialog import ConfigDialog
from config.plc_config import load_plc_settings
from controller.hmi_plc_binder import HmiPlcBinder
from utils.device_manager import DeviceManager


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
            return bool(getattr(self._binder, "_connected", False))
        except Exception:
            return False

    # --- 공정에서 상태 조회가 필요할 때(폴링 캐시 기반) ---
    def get_states(self) -> dict[str, bool]:
        try:
            d = getattr(self._binder, "_last_states", {}) or {}
            return dict(d)
        except Exception:
            return {}

    def read_coil(self, coil_name: str, default: bool = False) -> bool:
        try:
            return bool(self.get_states().get(str(coil_name), bool(default)))
        except Exception:
            return bool(default)

    # --- 공정에서 write ---
    def enqueue_write(self, coil_name: str, on: bool, momentary: bool = False) -> None:
        w = getattr(self._binder, "_worker", None)
        if w is None:
            raise RuntimeError("PLC worker not initialized")
        w.enqueue_write(str(coil_name), bool(on), momentary=bool(momentary))

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

    def open_config_dialog(self) -> None:
        """Config 팝업 → Save(=Accepted)면 즉시 장비 재연결 (기존 유지)"""
        dlg = ConfigDialog(ini_path=self._ini_path, parent=self)
        ret = dlg.exec()
        if ret == QDialog.Accepted:
            self._apply_config_and_reconnect()

    def _apply_config_and_reconnect(self) -> None:
        errors: list[str] = []

        # 1) PLC 재연결(워커 재시작) - 기존 유지
        try:
            if self._plc_binder:
                new_plc_settings = load_plc_settings(self._ini_path)
                self._plc_binder.reload_settings(new_plc_settings)
        except Exception as e:
            errors.append(f"PLC reconnect failed: {e}")

        # 2) 센서 재연결 - (신규 서비스가 있으면 그쪽 reload, 없으면 기존 DeviceManager reload)
        try:
            if self._stm_service is not None and hasattr(self._stm_service, "reload_from_ini"):
                self._stm_service.reload_from_ini(self._ini_path)
            if self._acs_service is not None and hasattr(self._acs_service, "reload_from_ini"):
                self._acs_service.reload_from_ini(self._ini_path)

            if (self._stm_service is None or self._acs_service is None) and self._dev_mgr:
                dev_errs = self._dev_mgr.reload_from_ini(self._ini_path, connect=False)
                for k, v in dev_errs.items():
                    errors.append(f"{k}: {v}")
        except Exception as e:
            errors.append(f"Sensor reconnect failed: {e}")

        if errors:
            QMessageBox.warning(self, "Reconnect", "일부 장비 재연결 실패:\n" + "\n".join(errors))
        else:
            QMessageBox.information(self, "Reconnect", "저장 완료 (PLC만 즉시 적용). STM/ACS는 Start에서 연결됩니다.")

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

        self.ui.hmiBtn.clicked.connect(self.goto_hmi_window)

        self.ui.startProcess.clicked.connect(self._on_start_clicked)   
        self.ui.stopProcess.clicked.connect(self._on_stop_clicked)   

        # ✅ UI에 버튼이 있는 경우에만 연결 (없으면 기존처럼 “아무 것도 안 함”)
        if hasattr(self.ui, "startProcess"):
            self.ui.startProcess.clicked.connect(self._on_start_clicked)
        if hasattr(self.ui, "stopProcess"):
            self.ui.stopProcess.clicked.connect(self._on_stop_clicked)
        if hasattr(self.ui, "pauseProcess"):
            self.ui.pauseProcess.clicked.connect(self._on_pause_clicked)
        if hasattr(self.ui, "resumeProcess"):
            self.ui.resumeProcess.clicked.connect(self._on_resume_clicked)

    def _on_start_clicked(self):
        if not self.hmi_window or not getattr(self.hmi_window, "_dev_mgr", None):
            return

        dev_mgr = self.hmi_window._dev_mgr
        ini_path = self.hmi_window._ini_path

        # 최신 ini 기준으로 reload + connect
        dev_errs = dev_mgr.reload_from_ini(ini_path, connect=True)

        if dev_errs:
            QMessageBox.warning(self, "Device Connect", "STM/ACS 연결 실패:\n" + "\n".join([f"{k}: {v}" for k, v in dev_errs.items()]))
        else:
            QMessageBox.information(self, "Device Connect", "STM/ACS 연결 성공")

    def _on_stop_clicked(self):
        if not self.hmi_window or not getattr(self.hmi_window, "_dev_mgr", None):
            return
        self.hmi_window._dev_mgr.close_all()
        QMessageBox.information(self, "Device Disconnect", "STM/ACS 연결 종료")

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

    def _on_start_clicked(self) -> None:
        pc = self._process_controller
        if pc is None:
            QMessageBox.warning(self, "Process", "ProcessController가 연결되지 않았습니다.")
            return
        try:
            # 레시피가 없으면 샘플 생성(해당 메서드가 있을 때만)
            try:
                r = pc.get_recipe()
            except Exception:
                r = None
            if r is None and hasattr(pc, "make_and_set_sample_recipe"):
                pc.make_and_set_sample_recipe()

            pc.start()
            _append_text(getattr(self.ui, "logWindow", None), "[UI] 공정 시작 요청")
        except Exception as e:
            QMessageBox.warning(self, "Process Start Failed", f"{e!r}")
            _append_text(getattr(self.ui, "logWindow", None), f"[START FAIL] {e!r}")

    def _on_stop_clicked(self) -> None:
        pc = self._process_controller
        if pc is None:
            return
        try:
            pc.stop()
            _append_text(getattr(self.ui, "logWindow", None), "[UI] 공정 정지 요청")
        except Exception as e:
            _append_text(getattr(self.ui, "logWindow", None), f"[STOP FAIL] {e!r}")

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

    # ------------------------------
    # 센서 연결
    # - 신규 STMService/ACSService가 있으면 우선 사용
    # - 둘 중 하나라도 실패하면 기존 DeviceManager로 fallback
    # ------------------------------
    dev_mgr: Optional[DeviceManager] = None
    stm_service: Any = None
    acs_service: Any = None

    if STMService is not None and ACSService is not None:
        try:
            stm_service = STMService.from_ini(ini_path) if hasattr(STMService, "from_ini") else STMService(ini_path)
            acs_service = ACSService.from_ini(ini_path) if hasattr(ACSService, "from_ini") else ACSService(ini_path)
            if hasattr(stm_service, "start"):
                stm_service.start()
            if hasattr(acs_service, "start"):
                acs_service.start()
        except Exception:
            stm_service = None
            acs_service = None

    # fallback: 기존 DeviceManager 유지(현재 main.py의 흐름 그대로)
    if stm_service is None or acs_service is None:
        # ✅ STM/ACS 매니저는 만들되, HMI 시작 시에는 연결하지 않음
        #    (Start 버튼 눌렀을 때 연결)
        dev_mgr = DeviceManager.from_ini(ini_path)

        app.aboutToQuit.connect(dev_mgr.close_all)

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
        except Exception:
            process_controller = None

    # 종료 시 신규 서비스 정리
    def _shutdown_new_services() -> None:
        # 공정 실행 중이면 stop 시도
        if process_controller is not None:
            try:
                if hasattr(process_controller, "is_running") and process_controller.is_running():
                    process_controller.stop()
            except Exception:
                pass

        for svc in (stm_service, acs_service, log_service):
            if svc is None:
                continue
            for name in ("stop", "close", "shutdown"):
                fn = getattr(svc, name, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
                    break

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
