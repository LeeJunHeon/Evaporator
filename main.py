# main.py
from __future__ import annotations

import sys
from pathlib import Path

# ✅ 어디서 실행하든(import 깨짐 방지) 가장 먼저 보정
_BASE_DIR = Path(__file__).resolve().parent
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

from PySide6.QtWidgets import QApplication, QWidget, QMessageBox, QDialog
from PySide6.QtCore import QTimer

from ui.mainWindow import Ui_Form
from ui.config_dialog import ConfigDialog
from config.plc_config import load_plc_settings
from controller.hmi_plc_binder import HmiPlcBinder
from utils.device_manager import DeviceManager


# 하얀색 "일반 버튼" (초록/체크 상태 없음)
NAV_BTN_QSS = """
QPushButton {
    background-color: rgb(255, 255, 255);
    color: rgb(0, 0, 0);
    border: 1px solid rgb(180, 180, 180);
    border-radius: 6px;
}
QPushButton:hover {
    background-color: rgb(245, 245, 245);
}
QPushButton:pressed {
    background-color: rgb(235, 235, 235);
}
QPushButton:disabled {
    background-color: rgb(230, 230, 230);
    color: rgb(140, 140, 140);
}
"""


class HmiWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.ui = Ui_Form()
        self.ui.setupUi(self)

        self.setWindowTitle("HMI")
        self.ui.stackedWidget.setCurrentIndex(0)  # HMI page

        self.process_window = None
        self._closing_all = False

        # ✅ Config / runtime objects
        self._ini_path = _BASE_DIR / "config" / "devices.ini"
        self._plc_binder: HmiPlcBinder | None = None
        self._dev_mgr: DeviceManager | None = None

        # Process 버튼: Process 창 앞으로
        self.ui.processBtn.clicked.connect(self.goto_process_window)

        # ✅ Config 버튼: 팝업 열기
        if hasattr(self.ui, "configBtn"):
            self.ui.configBtn.clicked.connect(self.open_config_dialog)

    def set_process_window(self, process_window: "ProcessWindow"):
        self.process_window = process_window

    def goto_process_window(self):
        if not self.process_window:
            return
        self.process_window.show()
        self.process_window.raise_()
        self.process_window.activateWindow()

    def set_runtime_objects(self, plc_binder: HmiPlcBinder, dev_mgr: DeviceManager, ini_path: Path) -> None:
        """main()에서 만든 런타임 객체 주입(PLC/STM/ACS 재연결에 사용)"""
        self._plc_binder = plc_binder
        self._dev_mgr = dev_mgr
        self._ini_path = Path(ini_path)

    def open_config_dialog(self) -> None:
        """Config 팝업 → Save(=Accepted)면 즉시 3개 장비 재연결"""
        dlg = ConfigDialog(ini_path=self._ini_path, parent=self)
        ret = dlg.exec()

        # ✅ ConfigDialog는 Save에서 accept(), Cancel은 reject()이므로
        #    Accepted만 확인하면 "저장했을 때"가 정확히 걸린다.
        if ret == QDialog.Accepted:
            self._apply_config_and_reconnect()

    def _apply_config_and_reconnect(self) -> None:
        errors: list[str] = []

        # 1) PLC 재연결(워커 재시작)
        try:
            if self._plc_binder:
                new_plc_settings = load_plc_settings(self._ini_path)
                self._plc_binder.reload_settings(new_plc_settings)
        except Exception as e:
            errors.append(f"PLC reconnect failed: {e}")

        # 2) STM/ACS 재연결(ini 재로딩 + connect)
        try:
            if self._dev_mgr:
                dev_errs = self._dev_mgr.reload_from_ini(self._ini_path, connect=True)
                for k, v in dev_errs.items():
                    errors.append(f"{k}: {v}")
        except Exception as e:
            errors.append(f"STM/ACS reconnect failed: {e}")

        if errors:
            QMessageBox.warning(self, "Reconnect", "일부 장비 재연결 실패:\n" + "\n".join(errors))
        else:
            QMessageBox.information(self, "Reconnect", "저장 완료 + 3개 장비 재연결 성공")


class ProcessWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.ui = Ui_Form()
        self.ui.setupUi(self)

        self.setWindowTitle("Process")
        self.ui.stackedWidget.setCurrentIndex(1)  # Process page

        self.hmi_window = None
        self._closing_all = False  # 두 창 동시 종료용 플래그

        self.ui.hmiBtn.clicked.connect(self.goto_hmi_window)

    def set_hmi_window(self, hmi_window: HmiWindow):
        self.hmi_window = hmi_window

    def goto_hmi_window(self):
        if not self.hmi_window:
            return
        self.hmi_window.show()
        self.hmi_window.raise_()
        self.hmi_window.activateWindow()

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
        # 이미 "둘 다 닫는 중"이면 그냥 닫히게
        if self._closing_all:
            event.accept()
            return

        # 사용자 X 클릭 -> 종료 확인
        if not self._confirm_exit():
            event.ignore()
            return

        # Yes -> 둘 다 같이 종료
        self._closing_all = True
        if self.hmi_window:
            self.hmi_window._closing_all = True
            self.hmi_window.close()
        event.accept()


def main():
    app = QApplication(sys.argv)

    hmi = HmiWindow()
    proc = ProcessWindow()

    # ------------------------------
    # PLC 바인딩 시작
    # ------------------------------
    ini_path = _BASE_DIR / "config" / "devices.ini"
    plc_settings = load_plc_settings(ini_path)

    # ✅ STM/ACS 매니저 생성 및 최초 연결(실패해도 프로그램은 유지)
    dev_mgr = DeviceManager.from_ini(ini_path)
    dev_errors = dev_mgr.connect_all()  # 실패한 것만 dict로 옴
    if dev_errors:
        # 필요하면 여기서 QMessageBox로 알려도 됨(원하면)
        pass

    plc_binder = HmiPlcBinder(hmi.ui, plc_settings)
    plc_binder.start()

    app.aboutToQuit.connect(plc_binder.stop)
    app.aboutToQuit.connect(dev_mgr.close_all)  # ✅ 추가

    # ✅ HMI가 Config 저장 후 재연결할 수 있도록 주입
    hmi.set_runtime_objects(plc_binder, dev_mgr, ini_path)

    hmi.set_process_window(proc)
    proc.set_hmi_window(hmi)

    # ✅ 두 창을 모두 띄우되, HMI가 항상 앞으로 오도록
    #proc.show()
    hmi.show()

    def _focus_hmi():
        hmi.raise_()
        hmi.activateWindow()

    # 이벤트 루프 시작 직후 포커싱(Windows에서도 잘 먹힘)
    QTimer.singleShot(0, _focus_hmi)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
