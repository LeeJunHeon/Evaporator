# main.py
import sys
from pathlib import Path

# ✅ 어디서 실행하든(import 깨짐 방지) 가장 먼저 보정
_BASE_DIR = Path(__file__).resolve().parent
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

from PySide6.QtWidgets import QApplication, QWidget, QMessageBox
from PySide6.QtCore import QTimer

from ui.mainWindow import Ui_Form
from ui.config_dialog import ConfigDialog   # ✅ 추가
from config.plc_config import load_plc_settings
from controller.hmi_plc_binder import HmiPlcBinder


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
        self._closing_all = False  # 두 창 동시 종료용 플래그

        # ✅ Config에서 사용할 ini 경로/PLC 바인더 참조
        self._ini_path = _BASE_DIR / "config" / "devices.ini"
        self._plc_binder: HmiPlcBinder | None = None

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

    def set_plc_binder(self, binder: HmiPlcBinder, ini_path: Path) -> None:
        """main()에서 생성한 PLC 바인더/ini 경로를 HMI에 주입"""
        self._plc_binder = binder
        self._ini_path = Path(ini_path)

    def open_config_dialog(self) -> None:
        """Config 팝업 열기 (3개 장비 파라미터 수정/저장)"""
        dlg = ConfigDialog(
            ini_path=self._ini_path,
            parent=self,
            on_saved=self._on_config_saved,  # 저장 후 콜백(PLC 재적용)
        )
        dlg.exec()

    def _on_config_saved(self) -> None:
        """devices.ini 저장 후 즉시 PLC 설정 재적용(선택)"""
        if not self._plc_binder:
            return
        new_settings = load_plc_settings(self._ini_path)
        self._plc_binder.reload_settings(new_settings)


    def _confirm_exit(self) -> bool:
        """종료 확인: Yes면 종료, No면 취소"""
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
        if self.process_window:
            self.process_window._closing_all = True
            self.process_window.close()
        event.accept()


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
    plc_binder = HmiPlcBinder(hmi.ui, plc_settings)
    plc_binder.start()

    # 프로그램 종료 시 워커 스레드 정리
    app.aboutToQuit.connect(plc_binder.stop)

    # ✅ Config 팝업에서 저장 후 즉시 PLC 재적용하려면 바인더 참조를 HMI에 넘겨야 함
    hmi.set_plc_binder(plc_binder, ini_path)

    hmi.set_process_window(proc)
    proc.set_hmi_window(hmi)

    # ✅ 두 창을 모두 띄우되, HMI가 항상 앞으로 오도록
    proc.show()
    hmi.show()

    def _focus_hmi():
        hmi.raise_()
        hmi.activateWindow()

    # 이벤트 루프 시작 직후 포커싱(Windows에서도 잘 먹힘)
    QTimer.singleShot(0, _focus_hmi)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
