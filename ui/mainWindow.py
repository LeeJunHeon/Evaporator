# -*- coding: utf-8 -*-

"""
✅ Qt Designer로 잡아둔 "좌표/선 정렬"은 그대로 두고
- HMI 페이지 버튼 스타일
- 선(QFrame) 스타일
- 인디케이터(원형 LED) 스타일
만 적용한 "가벼운" 버전입니다.

※ Process 버튼(processBtn) / HMI 버튼(hmiBtn)은 기본 스타일 그대로 둡니다.
"""

from PySide6.QtCore import QCoreApplication, QMetaObject, QRect, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QLabel, QLineEdit,
    QPushButton, QStackedWidget, QWidget, QRadioButton,
)


class Ui_Form(object):
    def setupUi(self, Form):
        if not Form.objectName():
            Form.setObjectName("Form")
        Form.resize(1121, 631)
        Form.setAutoFillBackground(True)

        self.stackedWidget = QStackedWidget(Form)
        self.stackedWidget.setObjectName("stackedWidget")
        self.stackedWidget.setGeometry(QRect(10, 0, 1101, 631))

        # =========================
        # PAGE 0 (HMI)
        # =========================
        self.page = QWidget()
        self.page.setObjectName("page")

        self.pushButton_13 = QPushButton(self.page)
        self.pushButton_13.setObjectName("pushButton_13")
        self.pushButton_13.setGeometry(QRect(770, 260, 101, 71))

        self.ms2powerBtn = QPushButton(self.page)
        self.ms2powerBtn.setObjectName("ms2powerBtn")
        self.ms2powerBtn.setGeometry(QRect(430, 450, 101, 71))

        self.ftmBtn = QPushButton(self.page)
        self.ftmBtn.setObjectName("ftmBtn")
        self.ftmBtn.setGeometry(QRect(220, 100, 101, 71))

        self.rpBtn = QPushButton(self.page)
        self.rpBtn.setObjectName("rpBtn")
        self.rpBtn.setGeometry(QRect(770, 370, 101, 71))

        self.mvBtn = QPushButton(self.page)
        self.mvBtn.setObjectName("mvBtn")
        self.mvBtn.setGeometry(QRect(620, 260, 101, 71))

        self.widget = QWidget(self.page)
        self.widget.setObjectName("widget")
        self.widget.setGeometry(QRect(140, 200, 261, 201))
        self.widget.setAutoFillBackground(True)

        # (선택) 중앙 Chamber 글자 (원치 않으면 아래 4줄 삭제)
        self.chamberLabel = QLabel(self.widget)
        self.chamberLabel.setObjectName("chamberLabel")
        self.chamberLabel.setGeometry(QRect(0, 0, 261, 201))
        self.chamberLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.fvBtn = QPushButton(self.page)
        self.fvBtn.setObjectName("fvBtn")
        self.fvBtn.setGeometry(QRect(920, 260, 101, 71))

        # ✅ 기본 스타일 유지
        self.processBtn = QPushButton(self.page)
        self.processBtn.setObjectName("processBtn")
        self.processBtn.setGeometry(QRect(10, 20, 101, 71))

        # ✅ Config 버튼 (Process 아래, 동일 크기/스타일)
        self.configBtn = QPushButton(self.page)
        self.configBtn.setObjectName("configBtn")
        self.configBtn.setGeometry(QRect(10, 110, 101, 71))

        self.vvBtn = QPushButton(self.page)
        self.vvBtn.setObjectName("vvBtn")
        self.vvBtn.setGeometry(QRect(10, 200, 101, 71))

        self.doorBtn = QPushButton(self.page)
        self.doorBtn.setObjectName("doorBtn")
        self.doorBtn.setGeometry(QRect(10, 330, 101, 71))

        self.ms2shutterBtn = QPushButton(self.page)
        self.ms2shutterBtn.setObjectName("ms2shutterBtn")
        self.ms2shutterBtn.setGeometry(QRect(430, 550, 101, 71))

        self.ms1shutterBtn = QPushButton(self.page)
        self.ms1shutterBtn.setObjectName("ms1shutterBtn")
        self.ms1shutterBtn.setGeometry(QRect(10, 550, 101, 71))

        self.mainshutterBtn = QPushButton(self.page)
        self.mainshutterBtn.setObjectName("mainshutterBtn")
        self.mainshutterBtn.setGeometry(QRect(430, 200, 101, 71))

        # ---- PIPES (frames) ----
        self.frame_17 = QFrame(self.page)
        self.frame_17.setObjectName("frame_17")
        self.frame_17.setGeometry(QRect(360, 285, 721, 21))
        self.frame_17.setAutoFillBackground(True)
        self.frame_17.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_17.setFrameShadow(QFrame.Shadow.Raised)

        self.ms1powerBtn = QPushButton(self.page)
        self.ms1powerBtn.setObjectName("ms1powerBtn")
        self.ms1powerBtn.setGeometry(QRect(10, 450, 101, 71))

        self.hmiLogWindow = QLineEdit(self.page)
        self.hmiLogWindow.setObjectName("hmiLogWindow")
        self.hmiLogWindow.setGeometry(QRect(570, 450, 511, 171))
        font = QFont()
        font.setPointSize(11)
        self.hmiLogWindow.setFont(font)

        self.allstopBtn = QPushButton(self.page)
        self.allstopBtn.setObjectName("allstopBtn")
        self.allstopBtn.setGeometry(QRect(610, 20, 91, 71))

        self.label = QLabel(self.page)
        self.label.setObjectName("label")
        self.label.setGeometry(QRect(720, 80, 61, 20))
        font1 = QFont()
        font1.setPointSize(13)
        self.label.setFont(font1)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.label_2 = QLabel(self.page)
        self.label_2.setObjectName("label_2")
        self.label_2.setGeometry(QRect(820, 80, 61, 20))
        self.label_2.setFont(font1)
        self.label_2.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.label_3 = QLabel(self.page)
        self.label_3.setObjectName("label_3")
        self.label_3.setGeometry(QRect(920, 80, 61, 20))
        self.label_3.setFont(font1)
        self.label_3.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.label_4 = QLabel(self.page)
        self.label_4.setObjectName("label_4")
        self.label_4.setGeometry(QRect(1020, 80, 61, 20))
        self.label_4.setFont(font1)
        self.label_4.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ---- Indicators (QWidget) ----
        self.g2_indicator_2 = QWidget(self.page)
        self.g2_indicator_2.setObjectName("g2_indicator_2")
        self.g2_indicator_2.setGeometry(QRect(720, 20, 61, 61))
        self.g2_indicator_2.setAutoFillBackground(True)

        self.g2_indicator_3 = QWidget(self.page)
        self.g2_indicator_3.setObjectName("g2_indicator_3")
        self.g2_indicator_3.setGeometry(QRect(820, 20, 61, 61))
        self.g2_indicator_3.setAutoFillBackground(True)

        self.g2_indicator_4 = QWidget(self.page)
        self.g2_indicator_4.setObjectName("g2_indicator_4")
        self.g2_indicator_4.setGeometry(QRect(920, 20, 61, 61))
        self.g2_indicator_4.setAutoFillBackground(True)

        self.g2_indicator_5 = QWidget(self.page)
        self.g2_indicator_5.setObjectName("g2_indicator_5")
        self.g2_indicator_5.setGeometry(QRect(1020, 20, 61, 61))
        self.g2_indicator_5.setAutoFillBackground(True)

        self.rvBtn = QPushButton(self.page)
        self.rvBtn.setObjectName("rvBtn")
        self.rvBtn.setGeometry(QRect(770, 150, 101, 71))

        self.frame_20 = QFrame(self.page)
        self.frame_20.setObjectName("frame_20")
        self.frame_20.setGeometry(QRect(570, 180, 21, 121))
        self.frame_20.setAutoFillBackground(True)
        self.frame_20.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_20.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_21 = QFrame(self.page)
        self.frame_21.setObjectName("frame_21")
        self.frame_21.setGeometry(QRect(570, 180, 511, 21))
        self.frame_21.setAutoFillBackground(True)
        self.frame_21.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_21.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_22 = QFrame(self.page)
        self.frame_22.setObjectName("frame_22")
        self.frame_22.setGeometry(QRect(1060, 180, 21, 231))
        self.frame_22.setAutoFillBackground(True)
        self.frame_22.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_22.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_18 = QFrame(self.page)
        self.frame_18.setObjectName("frame_18")
        self.frame_18.setGeometry(QRect(850, 390, 231, 21))
        self.frame_18.setAutoFillBackground(True)
        self.frame_18.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_18.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_23 = QFrame(self.page)
        self.frame_23.setObjectName("frame_23")
        self.frame_23.setGeometry(QRect(90, 230, 61, 21))
        self.frame_23.setAutoFillBackground(True)
        self.frame_23.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_23.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_24 = QFrame(self.page)
        self.frame_24.setObjectName("frame_24")
        self.frame_24.setGeometry(QRect(390, 230, 61, 21))
        self.frame_24.setAutoFillBackground(True)
        self.frame_24.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_24.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_25 = QFrame(self.page)
        self.frame_25.setObjectName("frame_25")
        self.frame_25.setGeometry(QRect(90, 360, 61, 21))
        self.frame_25.setAutoFillBackground(True)
        self.frame_25.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_25.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_26 = QFrame(self.page)
        self.frame_26.setObjectName("frame_26")
        self.frame_26.setGeometry(QRect(100, 480, 81, 21))
        self.frame_26.setAutoFillBackground(True)
        self.frame_26.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_26.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_27 = QFrame(self.page)
        self.frame_27.setObjectName("frame_27")
        self.frame_27.setGeometry(QRect(160, 390, 21, 111))
        self.frame_27.setAutoFillBackground(True)
        self.frame_27.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_27.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_28 = QFrame(self.page)
        self.frame_28.setObjectName("frame_28")
        self.frame_28.setGeometry(QRect(220, 390, 21, 211))
        self.frame_28.setAutoFillBackground(True)
        self.frame_28.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_28.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_29 = QFrame(self.page)
        self.frame_29.setObjectName("frame_29")
        self.frame_29.setGeometry(QRect(100, 580, 141, 21))
        self.frame_29.setAutoFillBackground(True)
        self.frame_29.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_29.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_30 = QFrame(self.page)
        self.frame_30.setObjectName("frame_30")
        self.frame_30.setGeometry(QRect(300, 390, 21, 211))
        self.frame_30.setAutoFillBackground(True)
        self.frame_30.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_30.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_31 = QFrame(self.page)
        self.frame_31.setObjectName("frame_31")
        self.frame_31.setGeometry(QRect(300, 580, 141, 21))
        self.frame_31.setAutoFillBackground(True)
        self.frame_31.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_31.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_32 = QFrame(self.page)
        self.frame_32.setObjectName("frame_32")
        self.frame_32.setGeometry(QRect(360, 480, 81, 21))
        self.frame_32.setAutoFillBackground(True)
        self.frame_32.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_32.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_33 = QFrame(self.page)
        self.frame_33.setObjectName("frame_33")
        self.frame_33.setGeometry(QRect(360, 390, 21, 111))
        self.frame_33.setAutoFillBackground(True)
        self.frame_33.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_33.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_34 = QFrame(self.page)
        self.frame_34.setObjectName("frame_34")
        self.frame_34.setGeometry(QRect(260, 140, 21, 71))
        self.frame_34.setAutoFillBackground(True)
        self.frame_34.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_34.setFrameShadow(QFrame.Shadow.Raised)

        self.processMonitor_HMI = QLineEdit(self.page)
        self.processMonitor_HMI.setObjectName("processMonitor_HMI")
        self.processMonitor_HMI.setGeometry(QRect(120, 20, 481, 71))

        self.stackedWidget.addWidget(self.page)

        # =========================
        # PAGE 1 (Process)
        # =========================
        self.page_2 = QWidget()
        self.page_2.setObjectName("page_2")

        self.materialEdit = QLineEdit(self.page_2)
        self.materialEdit.setObjectName("materialEdit")
        self.materialEdit.setGeometry(QRect(0, 170, 91, 26))
        self.materialEdit2 = QLineEdit(self.page_2)
        self.materialEdit2.setObjectName("materialEdit2")
        self.materialEdit2.setGeometry(QRect(100, 170, 91, 26))

        self.graphWidget = QWidget(self.page_2)
        self.graphWidget.setObjectName("graphWidget")
        self.graphWidget.setGeometry(QRect(209, 50, 891, 431))
        self.graphWidget.setAutoFillBackground(True)

        self.deprateEdit = QLineEdit(self.page_2)
        self.deprateEdit.setObjectName("deprateEdit")
        self.deprateEdit.setGeometry(QRect(0, 220, 91, 26))
        self.deprateEdit2 = QLineEdit(self.page_2)
        self.deprateEdit2.setObjectName("deprateEdit2")
        self.deprateEdit2.setGeometry(QRect(100, 220, 91, 26))

        self.powerEdit = QLineEdit(self.page_2)
        self.powerEdit.setObjectName("powerEdit")
        self.powerEdit.setGeometry(QRect(0, 270, 91, 26))
        self.powerEdit2 = QLineEdit(self.page_2)
        self.powerEdit2.setObjectName("powerEdit2")
        self.powerEdit2.setGeometry(QRect(100, 270, 91, 26))

        self.thicknessEdit = QLineEdit(self.page_2)
        self.thicknessEdit.setObjectName("thicknessEdit")
        self.thicknessEdit.setGeometry(QRect(0, 370, 191, 26))

        self.delayEdit = QLineEdit(self.page_2)
        self.delayEdit.setObjectName("delayEdit")
        self.delayEdit.setGeometry(QRect(0, 320, 191, 26))

        self.materialLabel = QLabel(self.page_2)
        self.materialLabel.setObjectName("materialLabel")
        self.materialLabel.setGeometry(QRect(0, 150, 181, 20))
        self.materialLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.stopProcess = QPushButton(self.page_2)
        self.stopProcess.setObjectName("stopProcess")
        self.stopProcess.setGeometry(QRect(0, 550, 91, 71))

        self.startProcess = QPushButton(self.page_2)
        self.startProcess.setObjectName("startProcess")
        self.startProcess.setGeometry(QRect(100, 550, 91, 71))

        self.logWindow = QLineEdit(self.page_2)
        self.logWindow.setObjectName("logWindow")
        self.logWindow.setGeometry(QRect(210, 490, 891, 131))

        self.thicknessLabel = QLabel(self.page_2)
        self.thicknessLabel.setObjectName("thicknessLabel")
        self.thicknessLabel.setGeometry(QRect(0, 350, 181, 20))
        self.thicknessLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.delayLabel = QLabel(self.page_2)
        self.delayLabel.setObjectName("delayLabel")
        self.delayLabel.setGeometry(QRect(0, 300, 181, 20))
        self.delayLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.pwoerLabel = QLabel(self.page_2)
        self.pwoerLabel.setObjectName("pwoerLabel")
        self.pwoerLabel.setGeometry(QRect(0, 250, 181, 20))
        self.pwoerLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.deprateLabel = QLabel(self.page_2)
        self.deprateLabel.setObjectName("deprateLabel")
        self.deprateLabel.setGeometry(QRect(0, 200, 181, 20))
        self.deprateLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.evaporatorLabel = QLabel(self.page_2)
        self.evaporatorLabel.setObjectName("evaporatorLabel")
        self.evaporatorLabel.setGeometry(QRect(0, 10, 191, 31))
        font2 = QFont()
        font2.setPointSize(19)
        self.evaporatorLabel.setFont(font2)
        self.evaporatorLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ✅ 기본 스타일 유지
        self.hmiBtn = QPushButton(self.page_2)
        self.hmiBtn.setObjectName("hmiBtn")
        self.hmiBtn.setGeometry(QRect(0, 50, 191, 61))

        self.sourcePower1 = QRadioButton(self.page_2)
        self.sourcePower1.setObjectName("sourcePower1")
        self.sourcePower1.setGeometry(QRect(0, 120, 81, 24))

        self.sourcePower2 = QRadioButton(self.page_2)
        self.sourcePower2.setObjectName("sourcePower2")
        self.sourcePower2.setGeometry(QRect(110, 120, 81, 24))

        # 기본 선택(무조건 하나 선택되게)
        self.sourcePower1.setChecked(True)

        self.processMonitor_Process = QLineEdit(self.page_2)
        self.processMonitor_Process.setObjectName("processMonitor_Process")
        self.processMonitor_Process.setGeometry(QRect(210, 5, 891, 41))

        self.stackedWidget.addWidget(self.page_2)

        # ---- translation ----
        self.retranslateUi(Form)
        self.stackedWidget.setCurrentIndex(0)
        QMetaObject.connectSlotsByName(Form)

        # ✅ 스타일만 적용
        self._apply_styles()

    def retranslateUi(self, Form):
        Form.setWindowTitle(QCoreApplication.translate("Form", "Form", None))
        self.pushButton_13.setText(QCoreApplication.translate("Form", "T. M. P", None))
        self.ms2powerBtn.setText(QCoreApplication.translate("Form", "M.S 2\nPower", None))
        self.ftmBtn.setText(QCoreApplication.translate("Form", "F. T. M", None))
        self.rpBtn.setText(QCoreApplication.translate("Form", "R / P", None))
        self.mvBtn.setText(QCoreApplication.translate("Form", "M / V", None))
        self.fvBtn.setText(QCoreApplication.translate("Form", "F / V", None))
        self.processBtn.setText(QCoreApplication.translate("Form", "Process", None))
        self.configBtn.setText(QCoreApplication.translate("Form", "Config", None))
        self.vvBtn.setText(QCoreApplication.translate("Form", "V / V", None))
        self.doorBtn.setText(QCoreApplication.translate("Form", "Door", None))
        self.ms2shutterBtn.setText(QCoreApplication.translate("Form", "M.S 2\nShutter", None))
        self.ms1shutterBtn.setText(QCoreApplication.translate("Form", "M.S 1\nShutter", None))
        self.mainshutterBtn.setText(QCoreApplication.translate("Form", "Main\nShutter", None))
        self.ms1powerBtn.setText(QCoreApplication.translate("Form", "M.S 1\nPower", None))
        self.allstopBtn.setText(QCoreApplication.translate("Form", "ALL\nSTOP", None))
        self.label.setText(QCoreApplication.translate("Form", "G1", None))
        self.label_2.setText(QCoreApplication.translate("Form", "G2", None))
        self.label_3.setText(QCoreApplication.translate("Form", "Air", None))
        self.label_4.setText(QCoreApplication.translate("Form", "Water", None))
        self.rvBtn.setText(QCoreApplication.translate("Form", "R / V", None))

        # (선택) 중앙 글자 (원치 않으면 아래 1줄 삭제)
        self.chamberLabel.setText(QCoreApplication.translate("Form", "Chamber", None))

        self.materialLabel.setText(QCoreApplication.translate("Form", "Material Name", None))
        self.stopProcess.setText(QCoreApplication.translate("Form", "Start", None))
        self.startProcess.setText(QCoreApplication.translate("Form", "Stop", None))
        self.thicknessLabel.setText(QCoreApplication.translate("Form", "Thickness", None))
        self.delayLabel.setText(QCoreApplication.translate("Form", "Delay", None))
        self.pwoerLabel.setText(QCoreApplication.translate("Form", "Power Ramp", None))
        self.deprateLabel.setText(QCoreApplication.translate("Form", "Dep.Rate", None))
        self.evaporatorLabel.setText(QCoreApplication.translate("Form", "Evaporator", None))
        self.hmiBtn.setText(QCoreApplication.translate("Form", "HMI", None))
        self.sourcePower1.setText(QCoreApplication.translate("Form", "Power 1", None))
        self.sourcePower2.setText(QCoreApplication.translate("Form", "Power 2", None))

    # =========================
    # Style only
    # =========================
    def _apply_styles(self):
        # ---- button styles ----
        TOGGLE_QSS = """
        QPushButton {
            background: #A0A0A0;
            color: white;
            font-weight: bold;
            font-size: 16pt;
            border-radius: 10px;
            border: 2px solid #555555;
        }
        QPushButton:checked {
            background: #32FF32;
            color: black;
            border: 2px solid #229b12;
        }
        QPushButton:pressed {
            background: #32FF32;
            color: black;
            border: 2px solid #229b12;
        }
        """

        ALLSTOP_QSS = """
        QPushButton {
            background: #A0A0A0;
            color: red;
            font-weight: bold;
            font-size: 16pt;
            border-radius: 10px;
            border: 2px solid #555555;
        }
        QPushButton:pressed {
            background: #808080;
            border-color: #333333;
        }
        """

        # HMI에서 토글처럼 보일 버튼들만 스타일 적용
        toggle_buttons = [
            self.vvBtn, self.doorBtn, self.ftmBtn, self.mainshutterBtn,
            self.ms1powerBtn, self.ms1shutterBtn, self.ms2powerBtn, self.ms2shutterBtn,
            self.rvBtn, self.mvBtn, self.pushButton_13, self.fvBtn, self.rpBtn,
        ]
        for b in toggle_buttons:
            b.setCheckable(True)
            b.setStyleSheet(TOGGLE_QSS)

        # ALL STOP만 별도
        self.allstopBtn.setCheckable(False)
        self.allstopBtn.setStyleSheet(ALLSTOP_QSS)

        # ✅ processBtn / hmiBtn 은 기본 스타일 유지 (건드리지 않음)

        # ---- frames (pipes) ----
        pipe_frames = [
            self.frame_17, self.frame_18,
            self.frame_20, self.frame_21, self.frame_22,
            self.frame_23, self.frame_24, self.frame_25, self.frame_26,
            self.frame_27, self.frame_28, self.frame_29, self.frame_30,
            self.frame_31, self.frame_32, self.frame_33, self.frame_34
        ]
        for f in pipe_frames:
            f.setFrameShape(QFrame.Shape.NoFrame)
            f.setStyleSheet("background-color: rgb(170,170,170); border: none;")
            f.lower()  # 항상 버튼보다 뒤로

        # ---- chamber block ----
        self.widget.setStyleSheet("background-color: rgb(220,220,220); border: none;")
        if hasattr(self, "chamberLabel"):
            ch_font = QFont()
            ch_font.setPointSize(22)
            ch_font.setBold(True)
            self.chamberLabel.setFont(ch_font)
            self.chamberLabel.setStyleSheet("color: rgb(90,90,90); background: transparent;")

        # ---- indicators ----
        self._style_indicator(self.g2_indicator_2, on=False)  # G1
        self._style_indicator(self.g2_indicator_3, on=False)  # G2
        self._style_indicator(self.g2_indicator_4, on=False)  # Air
        self._style_indicator(self.g2_indicator_5, on=False)  # Water

        for lab in (self.label, self.label_2, self.label_3, self.label_4):
            lab.setStyleSheet("color: black; background: transparent; font-weight: bold;")

        # ---- log windows (optional: 살짝 깔끔하게) ----
        self.hmiLogWindow.setStyleSheet("background: white; border: 1px solid #d0d0d0;")
        self.processMonitor_HMI.setStyleSheet("background: white; border: 1px solid #d0d0d0;")

    def _style_indicator(self, w: QWidget, on: bool):
        bg = "#38d62f" if on else "#d82c2c"
        # 61x61 기준 radius 30
        w.setStyleSheet(f"background: {bg}; border-radius: 30px; border: 2px solid #333333;")

    # 외부에서 상태 바꾸고 싶으면 이거 사용
    def set_indicator_state(self, name: str, on: bool):
        key = name.strip().lower()
        if key == "g1":
            self._style_indicator(self.g2_indicator_2, on)
        elif key == "g2":
            self._style_indicator(self.g2_indicator_3, on)
        elif key == "air":
            self._style_indicator(self.g2_indicator_4, on)
        elif key == "water":
            self._style_indicator(self.g2_indicator_5, on)


# (선택) 단독 실행 테스트용
if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    w = QWidget()
    ui = Ui_Form()
    ui.setupUi(w)
    w.show()
    sys.exit(app.exec())
