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

            # ✅ 신규 공정/로그/센서 주입
            self.process_window.set_runtime_objects(
                self._plc_binder,           # ✅ positional 1
                self._ini_path,             # ✅ positional 2
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
        ini_path: Path,
        *,
        process_controller: Any = None,
        log_service: Any = None,
        stm_service: Any = None,
        acs_service: Any = None,
    ) -> None:
        """main()에서 만든 런타임 객체 주입(PLC/센서 재연결에 사용)"""
        self._plc_binder = plc_binder
        self._ini_path = Path(ini_path)

        self._process_controller = process_controller
        self._log_service = log_service
        self._stm_service = stm_service
        self._acs_service = acs_service

        # ✅ HMI 로그 위젯을 LogService에 연결 (일별 로그 저장)
        if self._log_service is not None and hasattr(self._log_service, "attach_text_widget"):
            with contextlib.suppress(Exception):
                self._log_service.attach_text_widget(getattr(self.ui, "hmiLogWindow", None), channel="HMI")

    def _set_process_controller_devices(self, stm: Any, acs: Any) -> None:
        """
        ProcessController가 stm/acs를 참조한다면 여기서 갈아끼움.

        ✅ 목표:
        - dep.rate/thickness 폴링(S/T)은 기존처럼 로그에서 제외
        - 그 외 STM 명령(E/F/C/B/i/j/k...)은 ProcessWindowLog에 출력
        - STM 연결/해제/오류도 ProcessWindowLog에 출력
        """
        pc = self._process_controller
        if pc is None:
            return

        # ---------- 1) 기존 STM 시그널 disconnect ----------
        prev_stm = None
        try:
            prev_stm = getattr(pc, "stm", None) if hasattr(pc, "stm") else getattr(pc, "_stm", None)
        except Exception:
            prev_stm = None

        if prev_stm is not None:
            # io trace
            if hasattr(prev_stm, "sig_io_trace") and hasattr(pc, "_on_stm_io_trace"):
                with contextlib.suppress(Exception):
                    prev_stm.sig_io_trace.disconnect(pc._on_stm_io_trace)

            # error
            if hasattr(prev_stm, "sig_error") and hasattr(pc, "_on_stm_error"):
                with contextlib.suppress(Exception):
                    prev_stm.sig_error.disconnect(pc._on_stm_error)

            # connected
            if hasattr(prev_stm, "sig_connected") and hasattr(pc, "_on_stm_connected"):
                with contextlib.suppress(Exception):
                    prev_stm.sig_connected.disconnect(pc._on_stm_connected)

        # ---------- 2) 참조 교체 ----------
        for attr, val in (("stm", stm), ("acs", acs)):
            try:
                if hasattr(pc, attr):
                    setattr(pc, attr, val)
                elif hasattr(pc, f"_{attr}"):
                    setattr(pc, f"_{attr}", val)
            except Exception:
                pass

        # ---------- 3) 새 STM 시그널 connect ----------
        if stm is not None:
            if hasattr(stm, "sig_io_trace") and hasattr(pc, "_on_stm_io_trace"):
                with contextlib.suppress(Exception):
                    stm.sig_io_trace.connect(pc._on_stm_io_trace)

            if hasattr(stm, "sig_error") and hasattr(pc, "_on_stm_error"):
                with contextlib.suppress(Exception):
                    stm.sig_error.connect(pc._on_stm_error)

            if hasattr(stm, "sig_connected") and hasattr(pc, "_on_stm_connected"):
                with contextlib.suppress(Exception):
                    stm.sig_connected.connect(pc._on_stm_connected)

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

        # ✅ 3) STM은 Start에서만(F.T.M ON 후) 연결 (기존 규칙 유지)
        if errors:
            QMessageBox.warning(self, "Reconnect", "일부 장비 재연결 실패:\n" + "\n".join(errors))
        else:
            QMessageBox.information(
                self,
                "Reconnect",
                "저장 완료. (PLC/ACS 즉시 적용)\nSTM은 다음 Start에서 FTM ON 후 새로 연결됩니다.",
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
            try:
                self.process_window._closing_all = True
                self.process_window.close()
            except Exception:
                pass
            self.process_window = None

        # ✅ LogService stop은 main()의 aboutToQuit(_shutdown_new_services)에서 처리
        event.accept()