# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'EvaporatorZApNgy.ui'
##
## Created by: Qt User Interface Compiler version 6.9.1
##
## WARNING! All changes made in this file will be lost when recompiling UI file!
################################################################################

from PySide6.QtCore import (
    QCoreApplication, QDate, QDateTime, QLocale,
    QMetaObject, QObject, QPoint, QRect,
    QSize, QTime, QUrl, Qt, QTimer
)
from PySide6.QtGui import (
    QBrush, QColor, QConicalGradient, QCursor,
    QFont, QFontDatabase, QGradient, QIcon,
    QImage, QKeySequence, QLinearGradient, QPainter,
    QPalette, QPixmap, QRadialGradient, QTransform
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QLabel,
    QLineEdit, QPushButton, QSizePolicy, QStackedWidget,
    QWidget
)


class Ui_Form(object):
    def setupUi(self, Form):
        if not Form.objectName():
            Form.setObjectName(u"Form")
        Form.resize(1121, 631)
        Form.setAutoFillBackground(True)

        # ✅ 추후 레이아웃 보정 시 참조
        self._form = Form

        self.stackedWidget = QStackedWidget(Form)
        self.stackedWidget.setObjectName(u"stackedWidget")
        self.stackedWidget.setGeometry(QRect(10, 0, 1101, 631))

        # =========================
        # PAGE 0 (HMI)
        # =========================
        self.page = QWidget()
        self.page.setObjectName(u"page")

        self.frame_19 = QFrame(self.page)
        self.frame_19.setObjectName(u"frame_19")
        self.frame_19.setGeometry(QRect(1070, 240, 16, 161))
        self.frame_19.setAutoFillBackground(True)
        self.frame_19.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_19.setFrameShadow(QFrame.Shadow.Raised)

        self.pushButton_13 = QPushButton(self.page)
        self.pushButton_13.setObjectName(u"pushButton_13")
        self.pushButton_13.setGeometry(QRect(740, 260, 101, 71))

        self.frame_7 = QFrame(self.page)
        self.frame_7.setObjectName(u"frame_7")
        self.frame_7.setGeometry(QRect(90, 290, 71, 16))
        self.frame_7.setAutoFillBackground(True)
        self.frame_7.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_7.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_9 = QFrame(self.page)
        self.frame_9.setObjectName(u"frame_9")
        self.frame_9.setGeometry(QRect(160, 360, 16, 141))
        self.frame_9.setAutoFillBackground(True)
        self.frame_9.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_9.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_8 = QFrame(self.page)
        self.frame_8.setObjectName(u"frame_8")
        self.frame_8.setGeometry(QRect(370, 350, 16, 141))
        self.frame_8.setAutoFillBackground(True)
        self.frame_8.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_8.setFrameShadow(QFrame.Shadow.Raised)

        self.ms2powerBtn = QPushButton(self.page)
        self.ms2powerBtn.setObjectName(u"ms2powerBtn")
        self.ms2powerBtn.setGeometry(QRect(430, 450, 101, 71))

        self.ftmBtn = QPushButton(self.page)
        self.ftmBtn.setObjectName(u"ftmBtn")
        self.ftmBtn.setGeometry(QRect(220, 10, 101, 71))

        self.rpBtn = QPushButton(self.page)
        self.rpBtn.setObjectName(u"rpBtn")
        self.rpBtn.setGeometry(QRect(740, 360, 101, 71))

        self.frame_13 = QFrame(self.page)
        self.frame_13.setObjectName(u"frame_13")
        self.frame_13.setGeometry(QRect(590, 290, 421, 16))
        self.frame_13.setAutoFillBackground(True)
        self.frame_13.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_13.setFrameShadow(QFrame.Shadow.Raised)

        self.mvBtn = QPushButton(self.page)
        self.mvBtn.setObjectName(u"mvBtn")
        self.mvBtn.setGeometry(QRect(620, 260, 101, 71))

        self.widget = QWidget(self.page)
        self.widget.setObjectName(u"widget")
        self.widget.setGeometry(QRect(140, 140, 261, 261))
        self.widget.setAutoFillBackground(True)

        # ✅ 중앙 "Chamber" 글자(Designer에 없으면 코드로 생성)
        self.chamberLabel = QLabel(self.widget)
        self.chamberLabel.setObjectName("chamberLabel")
        self.chamberLabel.setGeometry(QRect(0, 0, 261, 261))
        self.chamberLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.frame_2 = QFrame(self.page)
        self.frame_2.setObjectName(u"frame_2")
        self.frame_2.setGeometry(QRect(100, 580, 120, 16))
        self.frame_2.setAutoFillBackground(True)
        self.frame_2.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_2.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_6 = QFrame(self.page)
        self.frame_6.setObjectName(u"frame_6")
        self.frame_6.setGeometry(QRect(310, 360, 16, 231))
        self.frame_6.setAutoFillBackground(True)
        self.frame_6.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_6.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_16 = QFrame(self.page)
        self.frame_16.setObjectName(u"frame_16")
        self.frame_16.setGeometry(QRect(1000, 180, 16, 121))
        self.frame_16.setAutoFillBackground(True)
        self.frame_16.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_16.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_4 = QFrame(self.page)
        self.frame_4.setObjectName(u"frame_4")
        self.frame_4.setGeometry(QRect(319, 580, 121, 16))
        self.frame_4.setAutoFillBackground(True)
        self.frame_4.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_4.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_3 = QFrame(self.page)
        self.frame_3.setObjectName(u"frame_3")
        self.frame_3.setGeometry(QRect(370, 480, 71, 16))
        self.frame_3.setAutoFillBackground(True)
        self.frame_3.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_3.setFrameShadow(QFrame.Shadow.Raised)

        self.fvBtn = QPushButton(self.page)
        self.fvBtn.setObjectName(u"fvBtn")
        self.fvBtn.setGeometry(QRect(880, 260, 101, 71))

        self.frame_5 = QFrame(self.page)
        self.frame_5.setObjectName(u"frame_5")
        self.frame_5.setGeometry(QRect(220, 365, 16, 231))
        self.frame_5.setAutoFillBackground(True)
        self.frame_5.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_5.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_10 = QFrame(self.page)
        self.frame_10.setObjectName(u"frame_10")
        self.frame_10.setGeometry(QRect(90, 180, 71, 16))
        self.frame_10.setAutoFillBackground(True)
        self.frame_10.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_10.setFrameShadow(QFrame.Shadow.Raised)

        # ✅ Process 버튼은 "기본 버튼 스타일" 유지 (QSS 적용하지 않음)
        self.processBtn = QPushButton(self.page)
        self.processBtn.setObjectName(u"processBtn")
        self.processBtn.setGeometry(QRect(10, 10, 101, 71))

        self.frame_18 = QFrame(self.page)
        self.frame_18.setObjectName(u"frame_18")
        self.frame_18.setGeometry(QRect(830, 390, 251, 16))
        self.frame_18.setAutoFillBackground(True)
        self.frame_18.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_18.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_14 = QFrame(self.page)
        self.frame_14.setObjectName(u"frame_14")
        self.frame_14.setGeometry(QRect(590, 180, 421, 16))
        self.frame_14.setAutoFillBackground(True)
        self.frame_14.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_14.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_11 = QFrame(self.page)
        self.frame_11.setObjectName(u"frame_11")
        self.frame_11.setGeometry(QRect(380, 180, 71, 16))
        self.frame_11.setAutoFillBackground(True)
        self.frame_11.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_11.setFrameShadow(QFrame.Shadow.Raised)

        self.frame = QFrame(self.page)
        self.frame.setObjectName(u"frame")
        self.frame.setGeometry(QRect(100, 480, 71, 16))
        self.frame.setAutoFillBackground(True)
        self.frame.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame.setFrameShadow(QFrame.Shadow.Raised)

        self.vvBtn = QPushButton(self.page)
        self.vvBtn.setObjectName(u"vvBtn")
        self.vvBtn.setGeometry(QRect(10, 150, 101, 71))

        self.frame_20 = QFrame(self.page)
        self.frame_20.setObjectName(u"frame_20")
        self.frame_20.setGeometry(QRect(1010, 230, 71, 16))
        self.frame_20.setAutoFillBackground(True)
        self.frame_20.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_20.setFrameShadow(QFrame.Shadow.Raised)

        self.doorBtn = QPushButton(self.page)
        self.doorBtn.setObjectName(u"doorBtn")
        self.doorBtn.setGeometry(QRect(10, 260, 101, 71))

        self.ms2shutterBtn = QPushButton(self.page)
        self.ms2shutterBtn.setObjectName(u"ms2shutterBtn")
        self.ms2shutterBtn.setGeometry(QRect(430, 550, 101, 71))

        self.frame_15 = QFrame(self.page)
        self.frame_15.setObjectName(u"frame_15")
        self.frame_15.setGeometry(QRect(590, 180, 16, 131))
        self.frame_15.setAutoFillBackground(True)
        self.frame_15.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_15.setFrameShadow(QFrame.Shadow.Raised)

        self.ms1shutterBtn = QPushButton(self.page)
        self.ms1shutterBtn.setObjectName(u"ms1shutterBtn")
        self.ms1shutterBtn.setGeometry(QRect(10, 550, 101, 71))

        self.frame_12 = QFrame(self.page)
        self.frame_12.setObjectName(u"frame_12")
        self.frame_12.setGeometry(QRect(260, 60, 16, 101))
        self.frame_12.setAutoFillBackground(True)
        self.frame_12.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_12.setFrameShadow(QFrame.Shadow.Raised)

        self.mainshutterBtn = QPushButton(self.page)
        self.mainshutterBtn.setObjectName(u"mainshutterBtn")
        self.mainshutterBtn.setGeometry(QRect(430, 150, 101, 71))

        self.frame_17 = QFrame(self.page)
        self.frame_17.setObjectName(u"frame_17")
        self.frame_17.setGeometry(QRect(360, 290, 241, 16))
        self.frame_17.setAutoFillBackground(True)
        self.frame_17.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_17.setFrameShadow(QFrame.Shadow.Raised)

        self.ms1powerBtn = QPushButton(self.page)
        self.ms1powerBtn.setObjectName(u"ms1powerBtn")
        self.ms1powerBtn.setGeometry(QRect(10, 450, 101, 71))

        self.processMonitor_HMI = QLineEdit(self.page)
        self.processMonitor_HMI.setObjectName(u"processMonitor_HMI")
        self.processMonitor_HMI.setGeometry(QRect(570, 450, 511, 171))

        self.allstopBtn = QPushButton(self.page)
        self.allstopBtn.setObjectName(u"allstopBtn")
        self.allstopBtn.setGeometry(QRect(590, 10, 91, 81))

        self.label = QLabel(self.page)
        self.label.setObjectName(u"label")
        self.label.setGeometry(QRect(720, 70, 61, 20))
        font = QFont()
        font.setPointSize(13)
        self.label.setFont(font)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.label_2 = QLabel(self.page)
        self.label_2.setObjectName(u"label_2")
        self.label_2.setGeometry(QRect(820, 70, 61, 20))
        self.label_2.setFont(font)
        self.label_2.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.label_3 = QLabel(self.page)
        self.label_3.setObjectName(u"label_3")
        self.label_3.setGeometry(QRect(920, 70, 61, 20))
        self.label_3.setFont(font)
        self.label_3.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.label_4 = QLabel(self.page)
        self.label_4.setObjectName(u"label_4")
        self.label_4.setGeometry(QRect(1020, 70, 61, 20))
        self.label_4.setFont(font)
        self.label_4.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ✅ 인디케이터 4개(Designer에서 배치 완료)
        self.g2_indicator_2 = QWidget(self.page)
        self.g2_indicator_2.setObjectName(u"g2_indicator_2")
        self.g2_indicator_2.setGeometry(QRect(720, 10, 61, 61))
        self.g2_indicator_2.setAutoFillBackground(True)

        self.g2_indicator_3 = QWidget(self.page)
        self.g2_indicator_3.setObjectName(u"g2_indicator_3")
        self.g2_indicator_3.setGeometry(QRect(820, 10, 61, 61))
        self.g2_indicator_3.setAutoFillBackground(True)

        self.g2_indicator_4 = QWidget(self.page)
        self.g2_indicator_4.setObjectName(u"g2_indicator_4")
        self.g2_indicator_4.setGeometry(QRect(920, 10, 61, 61))
        self.g2_indicator_4.setAutoFillBackground(True)

        self.g2_indicator_5 = QWidget(self.page)
        self.g2_indicator_5.setObjectName(u"g2_indicator_5")
        self.g2_indicator_5.setGeometry(QRect(1020, 10, 61, 61))
        self.g2_indicator_5.setAutoFillBackground(True)

        self.rvBtn = QPushButton(self.page)
        self.rvBtn.setObjectName(u"rvBtn")
        self.rvBtn.setGeometry(QRect(740, 150, 101, 71))

        self.stackedWidget.addWidget(self.page)

        # =========================
        # PAGE 1 (Process)
        # =========================
        self.page_2 = QWidget()
        self.page_2.setObjectName(u"page_2")

        self.materialEdit = QLineEdit(self.page_2)
        self.materialEdit.setObjectName(u"materialEdit")
        self.materialEdit.setGeometry(QRect(0, 170, 191, 26))

        self.graphWidget = QWidget(self.page_2)
        self.graphWidget.setObjectName(u"graphWidget")
        self.graphWidget.setGeometry(QRect(209, 50, 891, 431))
        self.graphWidget.setAutoFillBackground(True)

        self.deprateEdit = QLineEdit(self.page_2)
        self.deprateEdit.setObjectName(u"deprateEdit")
        self.deprateEdit.setGeometry(QRect(0, 220, 191, 26))

        self.powerEdit = QLineEdit(self.page_2)
        self.powerEdit.setObjectName(u"powerEdit")
        self.powerEdit.setGeometry(QRect(0, 270, 191, 26))

        self.thicknessEdit = QLineEdit(self.page_2)
        self.thicknessEdit.setObjectName(u"thicknessEdit")
        self.thicknessEdit.setGeometry(QRect(0, 370, 191, 26))

        self.delayEdit = QLineEdit(self.page_2)
        self.delayEdit.setObjectName(u"delayEdit")
        self.delayEdit.setGeometry(QRect(0, 320, 191, 26))

        self.materialLabel = QLabel(self.page_2)
        self.materialLabel.setObjectName(u"materialLabel")
        self.materialLabel.setGeometry(QRect(0, 150, 181, 20))
        self.materialLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.stopProcess = QPushButton(self.page_2)
        self.stopProcess.setObjectName(u"stopProcess")
        self.stopProcess.setGeometry(QRect(0, 550, 91, 71))

        self.startProcess = QPushButton(self.page_2)
        self.startProcess.setObjectName(u"startProcess")
        self.startProcess.setGeometry(QRect(100, 550, 91, 71))

        self.logWindow = QLineEdit(self.page_2)
        self.logWindow.setObjectName(u"logWindow")
        self.logWindow.setGeometry(QRect(210, 490, 891, 131))

        self.thicknessLabel = QLabel(self.page_2)
        self.thicknessLabel.setObjectName(u"thicknessLabel")
        self.thicknessLabel.setGeometry(QRect(0, 350, 181, 20))
        self.thicknessLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.delayLabel = QLabel(self.page_2)
        self.delayLabel.setObjectName(u"delayLabel")
        self.delayLabel.setGeometry(QRect(0, 300, 181, 20))
        self.delayLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.pwoerLabel = QLabel(self.page_2)
        self.pwoerLabel.setObjectName(u"pwoerLabel")
        self.pwoerLabel.setGeometry(QRect(0, 250, 181, 20))
        self.pwoerLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.deprateLabel = QLabel(self.page_2)
        self.deprateLabel.setObjectName(u"deprateLabel")
        self.deprateLabel.setGeometry(QRect(0, 200, 181, 20))
        self.deprateLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.evaporatorLabel = QLabel(self.page_2)
        self.evaporatorLabel.setObjectName(u"evaporatorLabel")
        self.evaporatorLabel.setGeometry(QRect(0, 10, 191, 31))
        font1 = QFont()
        font1.setPointSize(19)
        self.evaporatorLabel.setFont(font1)
        self.evaporatorLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ✅ Process 페이지 HMI 버튼도 "기본 버튼 스타일" 유지 (QSS 적용하지 않음)
        self.hmiBtn = QPushButton(self.page_2)
        self.hmiBtn.setObjectName(u"hmiBtn")
        self.hmiBtn.setGeometry(QRect(0, 50, 191, 61))

        self.sourcePower1 = QCheckBox(self.page_2)
        self.sourcePower1.setObjectName(u"sourcePower1")
        self.sourcePower1.setGeometry(QRect(0, 120, 81, 24))

        self.sourcePower2 = QCheckBox(self.page_2)
        self.sourcePower2.setObjectName(u"sourcePower2")
        self.sourcePower2.setGeometry(QRect(110, 120, 81, 24))

        self.processMonitor_Process = QLineEdit(self.page_2)
        self.processMonitor_Process.setObjectName(u"processMonitor_Process")
        self.processMonitor_Process.setGeometry(QRect(210, 5, 891, 41))

        self.stackedWidget.addWidget(self.page_2)

        self.retranslateUi(Form)
        self.stackedWidget.setCurrentIndex(0)
        QMetaObject.connectSlotsByName(Form)

        # ✅ 인디케이터 위젯 별칭(이름이 g2_indicator_* 로 생성되어서 의미있는 이름으로 alias)
        self.g1_indicator = self.g2_indicator_2
        self.g2_indicator = self.g2_indicator_3
        self.air_indicator = self.g2_indicator_4
        self.water_indicator = self.g2_indicator_5

        # ✅ UI 커스텀(한 번 + 이벤트루프 1회 후 한 번 더)
        self._apply_custom_ui()
        QTimer.singleShot(0, self._apply_custom_ui)

    # setupUi

    def retranslateUi(self, Form):
        Form.setWindowTitle(QCoreApplication.translate("Form", u"Form", None))
        self.pushButton_13.setText(QCoreApplication.translate("Form", u"T . M . P", None))
        self.ms2powerBtn.setText(QCoreApplication.translate("Form", u"M.S 2\nPower", None))
        self.ftmBtn.setText(QCoreApplication.translate("Form", u"F . T . M", None))
        self.rpBtn.setText(QCoreApplication.translate("Form", u"R / P", None))
        self.mvBtn.setText(QCoreApplication.translate("Form", u"M / V", None))
        self.fvBtn.setText(QCoreApplication.translate("Form", u"F / V", None))
        self.processBtn.setText(QCoreApplication.translate("Form", u"Process", None))
        self.vvBtn.setText(QCoreApplication.translate("Form", u"V / V", None))
        self.doorBtn.setText(QCoreApplication.translate("Form", u"Door", None))
        self.ms2shutterBtn.setText(QCoreApplication.translate("Form", u"M.S 2\nShutter", None))
        self.ms1shutterBtn.setText(QCoreApplication.translate("Form", u"M.S 1\nShutter", None))
        self.mainshutterBtn.setText(QCoreApplication.translate("Form", u"Main\nShutter", None))
        self.ms1powerBtn.setText(QCoreApplication.translate("Form", u"M.S 1\nPower", None))
        self.allstopBtn.setText(QCoreApplication.translate("Form", u"ALL\nSTOP", None))
        self.label.setText(QCoreApplication.translate("Form", u"G1", None))
        self.label_2.setText(QCoreApplication.translate("Form", u"G2", None))
        self.label_3.setText(QCoreApplication.translate("Form", u"Air", None))
        self.label_4.setText(QCoreApplication.translate("Form", u"Water", None))
        self.rvBtn.setText(QCoreApplication.translate("Form", u"R / V", None))

        # ✅ 중앙 "Chamber" 표기
        self.chamberLabel.setText(QCoreApplication.translate("Form", u"Chamber", None))

        self.materialLabel.setText(QCoreApplication.translate("Form", u"Material Name", None))
        self.stopProcess.setText(QCoreApplication.translate("Form", u"Start", None))
        self.startProcess.setText(QCoreApplication.translate("Form", u"Stop", None))
        self.thicknessLabel.setText(QCoreApplication.translate("Form", u"Thickness", None))
        self.delayLabel.setText(QCoreApplication.translate("Form", u"Delay", None))
        self.pwoerLabel.setText(QCoreApplication.translate("Form", u"Power Ramp", None))
        self.deprateLabel.setText(QCoreApplication.translate("Form", u"Dep.Rate", None))
        self.evaporatorLabel.setText(QCoreApplication.translate("Form", u"Evaporator", None))
        self.hmiBtn.setText(QCoreApplication.translate("Form", u"HMI", None))
        self.sourcePower1.setText(QCoreApplication.translate("Form", u"Power 1", None))
        self.sourcePower2.setText(QCoreApplication.translate("Form", u"Power 2", None))

    # retranslateUi

    # =========================================================================
    # Custom UI (HMI styles only)
    # =========================================================================

    def _apply_custom_ui(self):
        """
        요구사항:
        - HMI 페이지 설비 버튼 스타일 적용 (토글/그레이)
        - 선(Frame) 스타일/굵기 적용 + ㄱ/ㄴ 코너 딱 맞게 스냅
        - 인디케이터 4개 LED 스타일 적용
        - Process 버튼 / Process 페이지 HMI 버튼은 기본 스타일 유지(건드리지 않음)
        """

        # -------------------------
        # (1) HMI 버튼 스타일 (설비 버튼만)
        # -------------------------
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
        QPushButton:pressed { background: #808080; border-color: #333333; }
        """

        # ✅ 설비 버튼만 토글 스타일 적용
        equip_buttons = [
            self.vvBtn, self.doorBtn, self.ftmBtn, self.mainshutterBtn,
            self.ms1powerBtn, self.ms1shutterBtn, self.ms2powerBtn, self.ms2shutterBtn,
            self.rvBtn, self.mvBtn, self.pushButton_13, self.fvBtn, self.rpBtn
        ]
        for b in equip_buttons:
            b.setCheckable(True)
            b.setStyleSheet(TOGGLE_QSS)

        # ✅ ALL STOP만 별도 스타일
        self.allstopBtn.setCheckable(False)
        self.allstopBtn.setStyleSheet(ALLSTOP_QSS)

        # ✅ processBtn / hmiBtn는 "기본 스타일" 유지 (절대 setStyleSheet 하지 않음)
        # self.processBtn.setStyleSheet(...)  # ❌ 금지
        # self.hmiBtn.setStyleSheet(...)      # ❌ 금지

        # -------------------------
        # (2) 선(Frame) 스타일 + 두께
        # -------------------------
        frames = [
            self.frame_19, self.frame_7, self.frame_9, self.frame_8,
            self.frame_13, self.frame_2, self.frame_6, self.frame_16,
            self.frame_4, self.frame_3, self.frame_5, self.frame_10,
            self.frame_18, self.frame_14, self.frame_11, self.frame,
            self.frame_20, self.frame_15, self.frame_12, self.frame_17
        ]

        for f in frames:
            # 프레임 외곽선 제거 + 회색 파이프 느낌
            f.setFrameShape(QFrame.Shape.NoFrame)
            f.setStyleSheet("background-color: rgb(170,170,170); border: none;")
            f.lower()  # ✅ 버튼보다 항상 뒤로

        # ✅ 파이프(선) 굵기
        PIPE_T = 26   # 24~30 사이에서 조절(요구: 더 두껍게)
        self._set_frame_thickness(frames, PIPE_T)

        # ✅ ㄱ/ㄴ 모서리 틈/튀어나옴 보정
        self._snap_hmi_frames(PIPE_T)

        # -------------------------
        # (3) 중앙 Chamber 스타일
        # -------------------------
        self.widget.setStyleSheet("background-color: rgb(220,220,220); border: none;")
        ch_font = QFont()
        ch_font.setPointSize(22)
        ch_font.setBold(True)
        self.chamberLabel.setFont(ch_font)
        self.chamberLabel.setStyleSheet("color: rgb(90,90,90); background: transparent;")
        self.chamberLabel.setGeometry(0, 0, self.widget.width(), self.widget.height())
        self.chamberLabel.raise_()

        # -------------------------
        # (4) 인디케이터 LED 스타일
        # -------------------------
        # 기본 상태: OFF(빨강)으로 시작(원하면 True로 바꿔서 초록 시작 가능)
        self._style_indicator_widget(self.g1_indicator, on=False)
        self._style_indicator_widget(self.g2_indicator, on=False)
        self._style_indicator_widget(self.air_indicator, on=False)
        self._style_indicator_widget(self.water_indicator, on=False)

        for lab in (self.label, self.label_2, self.label_3, self.label_4):
            lab.setStyleSheet("color: black; background: transparent; font-weight: bold;")

        # -------------------------
        # (5) 텍스트가 잘리면 최소 크기 보장
        # -------------------------
        for b in equip_buttons:
            b.setMinimumSize(101, 71)
        self.allstopBtn.setMinimumSize(91, 81)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _set_frame_thickness(self, frames, thickness: int):
        """
        선(Frame)의 굵기를 키워도 '중심선'이 유지되도록 재배치.
        """
        t = int(thickness)
        for f in frames:
            g = f.geometry()

            # 가로선(폭이 높이보다 큰 경우)
            if g.width() >= g.height():
                old_h = g.height()
                new_y = g.y() + int((old_h - t) / 2)
                f.setGeometry(QRect(g.x(), new_y, g.width(), t))
            # 세로선
            else:
                old_w = g.width()
                new_x = g.x() + int((old_w - t) / 2)
                f.setGeometry(QRect(new_x, g.y(), t, g.height()))

    def _style_indicator_widget(self, w: QWidget, on: bool):
        """
        QWidget를 원형 LED처럼 보이게 스타일링.
        """
        bg = "#38d62f" if on else "#d82c2c"  # green / red
        size = min(w.width(), w.height())
        r = max(1, size // 2 - 2)  # radius
        w.setStyleSheet(
            f"background: {bg}; border-radius: {r}px; border: 2px solid #333333;"
        )

    def set_indicator_state(self, name: str, on: bool):
        """
        외부에서 인디케이터 상태 변경용.
        name: 'g1' | 'g2' | 'air' | 'water'
        """
        k = (name or "").strip().lower()
        if k == "g1":
            self._style_indicator_widget(self.g1_indicator, on)
        elif k == "g2":
            self._style_indicator_widget(self.g2_indicator, on)
        elif k == "air":
            self._style_indicator_widget(self.air_indicator, on)
        elif k == "water":
            self._style_indicator_widget(self.water_indicator, on)

    def _safe_set_rect(self, w: QWidget, rect: QRect):
        """geometry set을 안전하게(최소 폭/높이 1)"""
        r = QRect(rect)
        if r.width() < 1:
            r.setWidth(1)
        if r.height() < 1:
            r.setHeight(1)
        w.setGeometry(r)

    def _cy(self, w: QWidget) -> int:
        g = w.geometry()
        return g.y() + g.height() // 2

    def _cx(self, w: QWidget) -> int:
        g = w.geometry()
        return g.x() + g.width() // 2

    def _snap_hmi_frames(self, t: int):
        """
        두껍게 만들면 ㄱ/ㄴ 코너에서
        - 살짝 튀어나오거나
        - 1~2px 비는 현상
        을 막기 위한 '자동 스냅' 보정.
        (Qt Designer에서 배치한 구조를 기준으로, 연결되는 끝점을 맞춘다.)
        """
        overlap = max(2, t // 2)

        # ---------- (A) FTM 세로선(frame_12): FTM 버튼 아래 ~ Chamber 위젯 위까지 ----------
        ftm = self.ftmBtn.geometry()
        ch  = self.widget.geometry()
        # 세로선 x를 FTM 버튼 중앙에 정렬
        x = self._cx(self.ftmBtn) - t // 2
        y1 = ftm.y() + ftm.height() - overlap
        y2 = ch.y() + overlap
        self._safe_set_rect(self.frame_12, QRect(x, y1, t, max(1, y2 - y1)))

        # ---------- (B) VV / Door -> Chamber 왼쪽 연결(가로선: frame_10, frame_7) ----------
        # frame_10: VV 라인 (VV 버튼 중앙 높이에 맞춤)
        vv = self.vvBtn.geometry()
        y = self._cy(self.vvBtn) - t // 2
        x1 = vv.x() + vv.width() - overlap
        x2 = ch.x() + overlap
        self._safe_set_rect(self.frame_10, QRect(x1, y, max(1, x2 - x1), t))

        # frame_7: Door 라인 (Door 버튼 중앙 높이에 맞춤)
        door = self.doorBtn.geometry()
        y = self._cy(self.doorBtn) - t // 2
        x1 = door.x() + door.width() - overlap
        x2 = ch.x() + overlap
        self._safe_set_rect(self.frame_7, QRect(x1, y, max(1, x2 - x1), t))

        # ---------- (C) Chamber -> Main Shutter (frame_11) ----------
        ms = self.mainshutterBtn.geometry()
        y = self._cy(self.mainshutterBtn) - t // 2
        x1 = ch.x() + ch.width() - overlap
        x2 = ms.x() + overlap
        self._safe_set_rect(self.frame_11, QRect(x1, y, max(1, x2 - x1), t))

        # ---------- (D) Chamber -> Right pipe start (frame_17) ----------
        # frame_17은 Chamber 오른쪽에서 오른쪽 네트워크 시작점(frame_15)까지
        y = self._cy(self.mvBtn) - t // 2  # 우측 배관의 중심 높이(=MV 라인)로 맞추는게 자연스러움
        x1 = ch.x() + ch.width() - overlap
        x2 = self.frame_15.geometry().x() + t  # frame_15 영역까지 겹치게
        self._safe_set_rect(self.frame_17, QRect(x1, y, max(1, x2 - x1), t))

        # ---------- (E) Right rectangle top/bottom (frame_14, frame_13) ----------
        v15 = self.frame_15.geometry()
        v16 = self.frame_16.geometry()
        # top: frame_14 (RV 높이)
        y = self._cy(self.rvBtn) - t // 2
        x1 = v15.x()
        x2 = v16.x() + t
        self._safe_set_rect(self.frame_14, QRect(x1, y, max(1, x2 - x1), t))

        # mid/bottom: frame_13 (MV/TMP/FV 높이)
        y = self._cy(self.mvBtn) - t // 2
        x1 = v15.x()
        x2 = v16.x() + t
        self._safe_set_rect(self.frame_13, QRect(x1, y, max(1, x2 - x1), t))

        # ---------- (F) Right connector (frame_20): frame_16 -> frame_19 ----------
        v19 = self.frame_19.geometry()
        y = self.frame_20.geometry().y() + self.frame_20.geometry().height() // 2 - t // 2
        x1 = v16.x()
        x2 = v19.x() + t
        self._safe_set_rect(self.frame_20, QRect(x1, y, max(1, x2 - x1), t))

        # ---------- (G) RP bottom line (frame_18): RP 버튼 중앙 높이로 맞추고, 우측 끝까지 ----------
        rp = self.rpBtn.geometry()
        y = self._cy(self.rpBtn) - t // 2
        x1 = rp.x() + rp.width() - overlap
        x2 = v19.x() + t
        self._safe_set_rect(self.frame_18, QRect(x1, y, max(1, x2 - x1), t))

        # ---------- (H) Bottom left (M.S1 Power) line (frame) ----------
        ms1p = self.ms1powerBtn.geometry()
        y = self._cy(self.ms1powerBtn) - t // 2
        x1 = ms1p.x() + ms1p.width() - overlap
        x2 = self.frame_9.geometry().x() + t
        self._safe_set_rect(self.frame, QRect(x1, y, max(1, x2 - x1), t))

        # ---------- (I) Bottom mid (M.S2 Power) line (frame_3) ----------
        ms2p = self.ms2powerBtn.geometry()
        y = self._cy(self.ms2powerBtn) - t // 2
        x1 = self.frame_8.geometry().x()
        x2 = ms2p.x() + overlap
        self._safe_set_rect(self.frame_3, QRect(x1, y, max(1, x2 - x1), t))

        # ---------- (J) Bottom shutter lines (frame_2, frame_4) ----------
        ms1s = self.ms1shutterBtn.geometry()
        y = self._cy(self.ms1shutterBtn) - t // 2
        x1 = ms1s.x() + ms1s.width() - overlap
        x2 = self.frame_5.geometry().x() + t
        self._safe_set_rect(self.frame_2, QRect(x1, y, max(1, x2 - x1), t))

        ms2s = self.ms2shutterBtn.geometry()
        y = self._cy(self.ms2shutterBtn) - t // 2
        x1 = self.frame_6.geometry().x()
        x2 = ms2s.x() + overlap
        self._safe_set_rect(self.frame_4, QRect(x1, y, max(1, x2 - x1), t))

        # ---------- (K) 세로선들도 연결되는 가로선까지 '약간' 커버되도록 높이 보정 ----------
        # frame_15: frame_14(frame_13, frame_17) 범위를 모두 커버
        top = min(self.frame_14.geometry().y(), self.frame_13.geometry().y(), self.frame_17.geometry().y()) - overlap
        bot = max(self.frame_14.geometry().y(), self.frame_13.geometry().y(), self.frame_17.geometry().y()) + t + overlap
        self._safe_set_rect(self.frame_15, QRect(v15.x(), top, t, max(1, bot - top)))

        # frame_16: frame_14/frame_13/frame_20을 커버
        v16 = self.frame_16.geometry()
        top = min(self.frame_14.geometry().y(), self.frame_13.geometry().y(), self.frame_20.geometry().y()) - overlap
        bot = max(self.frame_14.geometry().y(), self.frame_13.geometry().y(), self.frame_20.geometry().y()) + t + overlap
        self._safe_set_rect(self.frame_16, QRect(v16.x(), top, t, max(1, bot - top)))

        # frame_19: frame_20/frame_18을 커버
        v19 = self.frame_19.geometry()
        top = min(self.frame_20.geometry().y(), self.frame_18.geometry().y()) - overlap
        bot = max(self.frame_20.geometry().y(), self.frame_18.geometry().y()) + t + overlap
        self._safe_set_rect(self.frame_19, QRect(v19.x(), top, t, max(1, bot - top)))
