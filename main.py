# main.py
import sys
from PySide6.QtWidgets import QApplication, QWidget, QMessageBox

from ui.mainWindow import Ui_Form


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

        # Process 버튼: Process 창 앞으로
        self.ui.processBtn.clicked.connect(self.goto_process_window)

    def set_process_window(self, process_window: "ProcessWindow"):
        self.process_window = process_window

    def goto_process_window(self):
        if not self.process_window:
            return
        self.process_window.show()
        self.process_window.raise_()
        self.process_window.activateWindow()

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
    ini_path = Path(__file__).resolve().parent / "config" / "devices.ini"
    plc_settings = load_plc_settings(ini_path)
    plc_binder = HmiPlcBinder(hmi.ui, plc_settings)
    plc_binder.start()

    # 프로그램 종료 시 워커 스레드 정리
    app.aboutToQuit.connect(plc_binder.stop)

    hmi.set_process_window(proc)
    proc.set_hmi_window(hmi)

    hmi.show()
    proc.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
