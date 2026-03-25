# hmi_window.py
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Optional, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ui.windows.process_window import ProcessWindow

from PySide6.QtWidgets import QWidget, QMessageBox, QDialog
from PySide6.QtCore import Qt

from ui.windows.mainWindow import Ui_Form
from ui.config_dialog import ConfigDialog
from config.plc_config import load_plc_settings
from controller.hmi_plc_binder import HmiPlcBinder

# ✅ 파일 위치가 root든 ui 폴더든 둘 다 버티도록
_BASE_DIR = Path(__file__).resolve().parent
while not (_BASE_DIR / "config").exists() and _BASE_DIR != _BASE_DIR.parent:
    _BASE_DIR = _BASE_DIR.parent


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

        # ✅ 신규 공정/서비스 (있으면 주입)
        self._process_controller: Any = None
        self._log_service: Any = None
        self._stm_service: Any = None
        self._acs_service: Any = None
        self._turbovac_service: Any = None
        self._run_summary_service: Any = None
        self._recommendation_service: Any = None

        # Process 버튼: Process 창 앞으로 (기존 유지)
        self.ui.processBtn.clicked.connect(self.goto_process_window)

        # Config 버튼: 팝업 열기 (기존 유지)
        if hasattr(self.ui, "configBtn"):
            self.ui.configBtn.clicked.connect(self.open_config_dialog)

    def goto_process_window(self):
        # ✅ 순환 import 방지: 필요할 때만 import
        from ui.windows.process_window import ProcessWindow

        # ✅ 닫혀있거나 아직 없으면 새로 생성
        if self.process_window is None:
            self.process_window = ProcessWindow()
            self.process_window.set_hmi_window(self)

            self.process_window.setAttribute(Qt.WA_DeleteOnClose, True)
            self.process_window.destroyed.connect(self._on_process_destroyed)

            # 최초 1회만 정식 주입
            self.process_window.set_runtime_objects(
                self._plc_binder,
                self._ini_path,
                process_controller=self._process_controller,
                log_service=self._log_service,
                stm_service=self._stm_service,
                acs_service=self._acs_service,
                run_summary_service=self._run_summary_service,
                recommendation_service=self._recommendation_service,
            )

        # ✅ 이미 떠 있는 창도 최신 참조를 보게 함
        self._refresh_process_window_runtime_refs()

        self.process_window.show()
        self.process_window.raise_()
        self.process_window.activateWindow()

    def _on_process_destroyed(self, *args):
        self.process_window = None

    def _refresh_process_window_runtime_refs(self) -> None:
        """
        이미 떠 있는 ProcessWindow가 최신 runtime object를 보도록
        참조만 갱신한다.

        주의:
        - process_window.set_runtime_objects()를 다시 호출하면
        ProcessController signal 중복 연결 가능성이 있으므로
        여기서는 참조 필드만 갱신한다.
        """
        pw = self.process_window
        if pw is None:
            return

        try:
            pw.hmi_window = self
        except Exception:
            pass

        try:
            pw._plc_binder = self._plc_binder
        except Exception:
            pass

        try:
            pw._ini_path = Path(self._ini_path)
        except Exception:
            pass

        try:
            pw._process_controller = self._process_controller
        except Exception:
            pass

        try:
            pw._log_service = self._log_service
        except Exception:
            pass

        try:
            pw._stm_service = self._stm_service
        except Exception:
            pass

        try:
            pw._acs_service = self._acs_service
        except Exception:
            pass

        try:
            pw._turbovac_service = self._turbovac_service
        except Exception:
            pass

        try:
            pw._run_summary_service = self._run_summary_service
        except Exception:
            pass

        try:
            pw._recommendation_service = self._recommendation_service
        except Exception:
            pass

    def _sync_process_controller_runtime_refs(self) -> None:
        """
        HMI가 들고 있는 최신 runtime object를
        ProcessController의 공식 API로 동기화한다.

        주의:
        - 공정 실행 중이면 controller 쪽에서 교체를 거부할 수 있다.
        - startup / idle 상태에서만 실질 반영되는 용도다.
        """
        pc = self._process_controller
        if pc is None or not hasattr(pc, "replace_runtime_devices"):
            return

        try:
            if hasattr(pc, "is_running") and pc.is_running():
                return
        except Exception:
            pass

        with contextlib.suppress(Exception):
            pc.replace_runtime_devices(
                stm=self._stm_service,
                acs=self._acs_service,
                turbovac=self._turbovac_service,
            )

    def set_runtime_objects(
        self,
        plc_binder: HmiPlcBinder,
        ini_path: Path,
        *,
        process_controller: Any = None,
        log_service: Any = None,
        stm_service: Any = None,
        acs_service: Any = None,
        turbovac_service: Any = None,
        run_summary_service: Any = None,
        recommendation_service: Any = None,
    ) -> None:
        """main()에서 만든 런타임 객체 주입(PLC/센서 재연결에 사용)"""
        self._plc_binder = plc_binder
        self._ini_path = Path(ini_path)

        self._process_controller = process_controller
        self._log_service = log_service
        self._stm_service = stm_service
        self._acs_service = acs_service
        self._turbovac_service = turbovac_service
        self._run_summary_service = run_summary_service
        self._recommendation_service = recommendation_service

        # ✅ HMI 로그 위젯을 LogService에 연결 (일별 로그 저장)
        if self._log_service is not None and hasattr(self._log_service, "attach_text_widget"):
            with contextlib.suppress(Exception):
                self._log_service.attach_text_widget(getattr(self.ui, "hmiLogWindow", None), channel="HMI")

        # ✅ controller도 공식 API 기준으로 최신 device 참조 동기화
        self._sync_process_controller_runtime_refs()

        # ✅ 이미 떠 있는 ProcessWindow가 있으면 최신 참조 반영
        self._refresh_process_window_runtime_refs()

    def open_config_dialog(self) -> None:
        """Config 팝업 → Save(=Accepted)면 즉시 장비 재연결 (기존 유지)"""
        dlg = ConfigDialog(ini_path=self._ini_path, parent=self)
        ret = dlg.exec()
        if ret == QDialog.Accepted:
            self._apply_config_and_reconnect()

    def _apply_config_and_reconnect(self) -> None:
        errors: list[str] = []

        # ✅ 1) PLC 즉시 재연결
        try:
            if self._plc_binder:
                new_plc_settings = load_plc_settings(self._ini_path)
                self._plc_binder.reload_settings(new_plc_settings)
        except Exception as e:
            errors.append(f"PLC reconnect failed: {e}")

        # ✅ 2) ACS는 "항상 켜져있는 장비"이므로 config 저장 시 즉시 reload 적용
        try:
            if self._acs_service is not None:
                # devices.ini 반영
                if hasattr(self._acs_service, "reload_from_ini"):
                    self._acs_service.reload_from_ini(self._ini_path)
                # CON 모드 유지(1초)
                if hasattr(self._acs_service, "set_stream_mode"):
                    self._acs_service.set_stream_mode(stream=True, stream_interval_a=1)
        except Exception as e:
            errors.append(f"ACS reconnect failed: {e}")

        # ✅ 3) TMP(TURBOVAC)도 config 저장 시 즉시 reload 적용
        try:
            if self._turbovac_service is not None:
                if hasattr(self._turbovac_service, "reload_from_ini"):
                    self._turbovac_service.reload_from_ini(self._ini_path)
                elif hasattr(self._turbovac_service, "reload_config"):
                    self._turbovac_service.reload_config()
                else:
                    raise RuntimeError("TurbovacService에 reload_from_ini/reload_config가 없습니다.")
        except Exception as e:
            errors.append(f"TMP reconnect failed: {e}")

        # ✅ 4) STM은 Start에서만(F.T.M ON 후) 연결 (기존 규칙 유지)
        if errors:
            QMessageBox.warning(self, "Reconnect", "일부 장비 재연결 실패:\n" + "\n".join(errors))
        else:
            QMessageBox.information(
                self,
                "Reconnect",
                "저장 완료. (PLC/ACS/TMP 즉시 적용)\nSTM은 다음 Start에서 FTM ON 후 새로 연결됩니다.",
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

        # Process 창 같이 닫기
        if self.process_window is not None:
            closed = False
            try:
                self.process_window._closing_all = True
                closed = bool(self.process_window.close())
            except Exception:
                closed = False

            # ProcessWindow가 아직 닫히지 못한 경우(HW stop/preflight wait 등)
            # HMI도 닫지 말고 종료를 취소해야 한다.
            if not closed:
                self._closing_all = False
                event.ignore()
                return

            # ✅ 여기서 self.process_window = None 으로 끊지 않는다.
            #    실제 삭제 완료 시 destroyed -> _on_process_destroyed()가 정리하도록 맡긴다.

        # ✅ LogService stop은 main()의 aboutToQuit(_shutdown_new_services)에서 처리
        event.accept()
