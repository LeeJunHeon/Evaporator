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
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QStackedWidget, QWidget,
)


class Ui_Form(object):
    def setupUi(self, Form):
        if not Form.objectName():
            Form.setObjectName("Form")
        Form.resize(1121, 700)
        Form.setAutoFillBackground(True)

        self.stackedWidget = QStackedWidget(Form)
        self.stackedWidget.setObjectName("stackedWidget")
        self.stackedWidget.setGeometry(QRect(10, 0, 1101, 700))

        # =========================
        # PAGE 0 (HMI)
        # =========================
        self.page = QWidget()
        self.page.setObjectName("page")

        self.pushButton_13 = QPushButton(self.page)
        self.pushButton_13.setObjectName("pushButton_13")
        self.pushButton_13.setGeometry(QRect(770, 310, 101, 71))

        self.ms2powerBtn = QPushButton(self.page)
        self.ms2powerBtn.setObjectName("ms2powerBtn")
        self.ms2powerBtn.setGeometry(QRect(430, 500, 101, 71))

        self.ftmBtn = QPushButton(self.page)
        self.ftmBtn.setObjectName("ftmBtn")
        self.ftmBtn.setGeometry(QRect(220, 150, 101, 71))

        self.rpBtn = QPushButton(self.page)
        self.rpBtn.setObjectName("rpBtn")
        self.rpBtn.setGeometry(QRect(770, 420, 101, 71))

        self.mvBtn = QPushButton(self.page)
        self.mvBtn.setObjectName("mvBtn")
        self.mvBtn.setGeometry(QRect(620, 310, 101, 71))

        self.widget = QWidget(self.page)
        self.widget.setObjectName("widget")
        self.widget.setGeometry(QRect(140, 250, 261, 201))
        self.widget.setAutoFillBackground(True)

        # ✅ Chamber 타이틀(상단)
        self.chamberLabel = QLabel(self.widget)
        self.chamberLabel.setObjectName("chamberLabel")
        self.chamberLabel.setGeometry(QRect(0, 14, 261, 50))
        self.chamberLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ✅ Pressure 캡션(중간)
        self.pressureCaption = QLabel(self.widget)
        self.pressureCaption.setObjectName("pressureCaption")
        self.pressureCaption.setGeometry(QRect(0, 68, 261, 18))
        self.pressureCaption.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ✅ Pressure 값(하단, 크게)
        self.pressureValue = QLabel(self.widget)
        self.pressureValue.setObjectName("pressureValue")
        self.pressureValue.setGeometry(QRect(0, 90, 261, 96))
        self.pressureValue.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 표시가 항상 위로 오도록
        self.pressureCaption.raise_()
        self.pressureValue.raise_()

        self.fvBtn = QPushButton(self.page)
        self.fvBtn.setObjectName("fvBtn")
        self.fvBtn.setGeometry(QRect(920, 310, 101, 71))

        # ✅ 기본 스타일 유지
        self.processBtn = QPushButton(self.page)
        self.processBtn.setObjectName("processBtn")
        self.processBtn.setGeometry(QRect(10, 20, 101, 71))

        # ✅ Vacuum ON (Process 아래, 동일 크기)
        self.vacuumOnBtn = QPushButton(self.page)
        self.vacuumOnBtn.setObjectName("vacuumOnBtn")
        self.vacuumOnBtn.setGeometry(QRect(10, 110, 101, 71))

        # ✅ Config 버튼 (Process 오른쪽, 동일 크기/스타일)
        self.configBtn = QPushButton(self.page)
        self.configBtn.setObjectName("configBtn")
        self.configBtn.setGeometry(QRect(120, 20, 101, 71))

        self.vvBtn = QPushButton(self.page)
        self.vvBtn.setObjectName("vvBtn")
        self.vvBtn.setGeometry(QRect(10, 250, 101, 71))

        self.doorBtn = QPushButton(self.page)
        self.doorBtn.setObjectName("doorBtn")
        self.doorBtn.setGeometry(QRect(10, 380, 101, 71))

        self.ms2shutterBtn = QPushButton(self.page)
        self.ms2shutterBtn.setObjectName("ms2shutterBtn")
        self.ms2shutterBtn.setGeometry(QRect(430, 600, 101, 71))

        self.ms1shutterBtn = QPushButton(self.page)
        self.ms1shutterBtn.setObjectName("ms1shutterBtn")
        self.ms1shutterBtn.setGeometry(QRect(10, 600, 101, 71))

        self.mainshutterBtn = QPushButton(self.page)
        self.mainshutterBtn.setObjectName("mainshutterBtn")
        self.mainshutterBtn.setGeometry(QRect(430, 250, 101, 71))

        # =========================
        # DAC manual set (Power1/Power2)
        # - Main Shutter 버튼 위에서 Power1/Power2 DAC 코드를 수동 입력/적용
        # - 실제 PLC write는 HmiPlcBinder가 처리
        # =========================
        self.dac1Edit = QLineEdit(self.page)
        self.dac1Edit.setObjectName("dac1Edit")
        self.dac1Edit.setGeometry(QRect(430, 190, 101, 26))
        self.dac1Edit.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.dac1ApplyBtn = QPushButton(self.page)
        self.dac1ApplyBtn.setObjectName("dac1ApplyBtn")
        self.dac1ApplyBtn.setGeometry(QRect(540, 190, 61, 26))

        self.dac2Edit = QLineEdit(self.page)
        self.dac2Edit.setObjectName("dac2Edit")
        self.dac2Edit.setGeometry(QRect(430, 220, 101, 26))
        self.dac2Edit.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.dac2ApplyBtn = QPushButton(self.page)
        self.dac2ApplyBtn.setObjectName("dac2ApplyBtn")
        self.dac2ApplyBtn.setGeometry(QRect(540, 220, 61, 26))

        # ---- PIPES (frames) ----
        self.frame_17 = QFrame(self.page)
        self.frame_17.setObjectName("frame_17")
        self.frame_17.setGeometry(QRect(360, 335, 721, 21))
        self.frame_17.setAutoFillBackground(True)
        self.frame_17.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_17.setFrameShadow(QFrame.Shadow.Raised)

        self.ms1powerBtn = QPushButton(self.page)
        self.ms1powerBtn.setObjectName("ms1powerBtn")
        self.ms1powerBtn.setGeometry(QRect(10, 500, 101, 71))

        self.hmiLogWindow = QPlainTextEdit(self.page)
        self.hmiLogWindow.setObjectName("hmiLogWindow")
        self.hmiLogWindow.setGeometry(QRect(570, 500, 511, 171))
        font = QFont()
        font.setPointSize(11)
        self.hmiLogWindow.setFont(font)

        # ✅ 로그 전용 세팅
        self.hmiLogWindow.setReadOnly(True)
        self.hmiLogWindow.setUndoRedoEnabled(False)
        self.hmiLogWindow.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)  # 안 잘리고 자동 줄바꿈
        self.hmiLogWindow.document().setMaximumBlockCount(2000)  # 너무 커지지 않게

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
        self.rvBtn.setGeometry(QRect(770, 200, 101, 71))

        self.frame_20 = QFrame(self.page)
        self.frame_20.setObjectName("frame_20")
        self.frame_20.setGeometry(QRect(570, 230, 21, 121))
        self.frame_20.setAutoFillBackground(True)
        self.frame_20.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_20.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_21 = QFrame(self.page)
        self.frame_21.setObjectName("frame_21")
        self.frame_21.setGeometry(QRect(570, 230, 511, 21))
        self.frame_21.setAutoFillBackground(True)
        self.frame_21.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_21.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_22 = QFrame(self.page)
        self.frame_22.setObjectName("frame_22")
        self.frame_22.setGeometry(QRect(1060, 230, 21, 231))
        self.frame_22.setAutoFillBackground(True)
        self.frame_22.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_22.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_18 = QFrame(self.page)
        self.frame_18.setObjectName("frame_18")
        self.frame_18.setGeometry(QRect(850, 440, 231, 21))
        self.frame_18.setAutoFillBackground(True)
        self.frame_18.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_18.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_23 = QFrame(self.page)
        self.frame_23.setObjectName("frame_23")
        self.frame_23.setGeometry(QRect(90, 280, 61, 21))
        self.frame_23.setAutoFillBackground(True)
        self.frame_23.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_23.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_24 = QFrame(self.page)
        self.frame_24.setObjectName("frame_24")
        self.frame_24.setGeometry(QRect(390, 280, 61, 21))
        self.frame_24.setAutoFillBackground(True)
        self.frame_24.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_24.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_25 = QFrame(self.page)
        self.frame_25.setObjectName("frame_25")
        self.frame_25.setGeometry(QRect(90, 410, 61, 21))
        self.frame_25.setAutoFillBackground(True)
        self.frame_25.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_25.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_26 = QFrame(self.page)
        self.frame_26.setObjectName("frame_26")
        self.frame_26.setGeometry(QRect(100, 530, 81, 21))
        self.frame_26.setAutoFillBackground(True)
        self.frame_26.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_26.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_27 = QFrame(self.page)
        self.frame_27.setObjectName("frame_27")
        self.frame_27.setGeometry(QRect(160, 440, 21, 111))
        self.frame_27.setAutoFillBackground(True)
        self.frame_27.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_27.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_28 = QFrame(self.page)
        self.frame_28.setObjectName("frame_28")
        self.frame_28.setGeometry(QRect(220, 440, 21, 211))
        self.frame_28.setAutoFillBackground(True)
        self.frame_28.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_28.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_29 = QFrame(self.page)
        self.frame_29.setObjectName("frame_29")
        self.frame_29.setGeometry(QRect(100, 630, 141, 21))
        self.frame_29.setAutoFillBackground(True)
        self.frame_29.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_29.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_30 = QFrame(self.page)
        self.frame_30.setObjectName("frame_30")
        self.frame_30.setGeometry(QRect(300, 440, 21, 211))
        self.frame_30.setAutoFillBackground(True)
        self.frame_30.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_30.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_31 = QFrame(self.page)
        self.frame_31.setObjectName("frame_31")
        self.frame_31.setGeometry(QRect(300, 630, 141, 21))
        self.frame_31.setAutoFillBackground(True)
        self.frame_31.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_31.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_32 = QFrame(self.page)
        self.frame_32.setObjectName("frame_32")
        self.frame_32.setGeometry(QRect(360, 530, 81, 21))
        self.frame_32.setAutoFillBackground(True)
        self.frame_32.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_32.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_33 = QFrame(self.page)
        self.frame_33.setObjectName("frame_33")
        self.frame_33.setGeometry(QRect(360, 440, 21, 111))
        self.frame_33.setAutoFillBackground(True)
        self.frame_33.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_33.setFrameShadow(QFrame.Shadow.Raised)

        self.frame_34 = QFrame(self.page)
        self.frame_34.setObjectName("frame_34")
        self.frame_34.setGeometry(QRect(260, 190, 21, 71))
        self.frame_34.setAutoFillBackground(True)
        self.frame_34.setFrameShape(QFrame.Shape.StyledPanel)
        self.frame_34.setFrameShadow(QFrame.Shadow.Raised)

        self.processMonitor_HMI = QLineEdit(self.page)
        self.processMonitor_HMI.setObjectName("processMonitor_HMI")
        # Process/Config 버튼을 상단에 가로 배치하기 위해 폭을 줄이고 오른쪽으로 이동
        self.processMonitor_HMI.setGeometry(QRect(230, 20, 371, 71))

        self.stackedWidget.addWidget(self.page)

        # =========================
        # PAGE 1 (Process)
        # =========================
        self.page_2 = QWidget()
        self.page_2.setObjectName("page_2")

        # ✅ Material name: QLineEdit -> QPushButton
        self.materialEdit = QPushButton(self.page_2)
        self.materialEdit.setObjectName("materialEdit")
        self.materialEdit.setGeometry(QRect(0, 170, 91, 32))
        self.materialEdit.setAutoDefault(False)

        self.materialEdit2 = QPushButton(self.page_2)
        self.materialEdit2.setObjectName("materialEdit2")
        self.materialEdit2.setGeometry(QRect(100, 170, 91, 32))
        self.materialEdit2.setAutoDefault(False)

        # ✅ material 하단에 Density / Z-factor 표시 (라벨은 위, Edit은 2칸)
        # - 기존 materialRhoLabel1/2, materialZLabel1/2는 "왼쪽 라벨" 용도였는데
        #   이제 "위 라벨"로 재배치한다.
        # - 오른쪽 라벨(materialRhoLabel2/materialZLabel2)은 안 쓰므로 숨긴다.

        # Density label (위에 1개로 통합)
        self.materialRhoLabel1 = QLabel(self.page_2)
        self.materialRhoLabel1.setObjectName("materialRhoLabel1")
        self.materialRhoLabel1.setGeometry(QRect(0, 200, 191, 20))
        self.materialRhoLabel1.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # 기존 오른쪽 ρ 라벨은 사용 안함 → 숨김(겹침 방지)
        self.materialRhoLabel2 = QLabel(self.page_2)
        self.materialRhoLabel2.setObjectName("materialRhoLabel2")
        self.materialRhoLabel2.setGeometry(QRect(0, 0, 0, 0))

        # Density edits (PowerRamp처럼 2칸, 높이 26)
        self.materialDensityEdit1 = QLineEdit(self.page_2)
        self.materialDensityEdit1.setObjectName("materialDensityEdit1")
        self.materialDensityEdit1.setGeometry(QRect(0, 220, 91, 26))
        self.materialDensityEdit1.setReadOnly(True)

        self.materialDensityEdit2 = QLineEdit(self.page_2)
        self.materialDensityEdit2.setObjectName("materialDensityEdit2")
        self.materialDensityEdit2.setGeometry(QRect(100, 220, 91, 26))
        self.materialDensityEdit2.setReadOnly(True)

        # Z-factor label (위에 1개로 통합)
        self.materialZLabel1 = QLabel(self.page_2)
        self.materialZLabel1.setObjectName("materialZLabel1")
        self.materialZLabel1.setGeometry(QRect(0, 250, 191, 20))
        self.materialZLabel1.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # 기존 오른쪽 Z 라벨은 사용 안함 → 숨김
        self.materialZLabel2 = QLabel(self.page_2)
        self.materialZLabel2.setObjectName("materialZLabel2")
        self.materialZLabel2.setGeometry(QRect(0, 0, 0, 0))

        # Z-factor edits (2칸, 높이 26)
        self.materialZfactorEdit1 = QLineEdit(self.page_2)
        self.materialZfactorEdit1.setObjectName("materialZfactorEdit1")
        self.materialZfactorEdit1.setGeometry(QRect(0, 270, 91, 26))
        self.materialZfactorEdit1.setReadOnly(True)

        self.materialZfactorEdit2 = QLineEdit(self.page_2)
        self.materialZfactorEdit2.setObjectName("materialZfactorEdit2")
        self.materialZfactorEdit2.setGeometry(QRect(100, 270, 91, 26))
        self.materialZfactorEdit2.setReadOnly(True)

        # ✅ Current: Rate는 재료별 2칸, Thick는 1칸(전체폭)
        self.currentRateLabel = QLabel(self.page_2)
        self.currentRateLabel.setGeometry(QRect(0, 500, 91, 20))

        self.currentRateLabel2 = QLabel(self.page_2)
        self.currentRateLabel2.setObjectName("currentRateLabel2")
        self.currentRateLabel2.setGeometry(QRect(100, 500, 91, 20))

        self.currentRateEdit = QLineEdit(self.page_2)
        self.currentRateEdit.setGeometry(QRect(0, 520, 91, 26))
        self.currentRateEdit.setReadOnly(True)

        self.currentRateEdit2 = QLineEdit(self.page_2)
        self.currentRateEdit2.setObjectName("currentRateEdit2")
        self.currentRateEdit2.setGeometry(QRect(100, 520, 91, 26))
        self.currentRateEdit2.setReadOnly(True)

        self.currentThicknessLabel = QLabel(self.page_2)
        self.currentThicknessLabel.setGeometry(QRect(0, 550, 191, 20))

        self.currentThicknessEdit = QLineEdit(self.page_2)
        self.currentThicknessEdit.setGeometry(QRect(0, 570, 191, 26))
        self.currentThicknessEdit.setReadOnly(True)

        self.graphWidget = QWidget(self.page_2)
        self.graphWidget.setObjectName("graphWidget")
        self.graphWidget.setGeometry(QRect(209, 50, 891, 431))
        self.graphWidget.setAutoFillBackground(True)

        self.deprateEdit = QLineEdit(self.page_2)
        self.deprateEdit.setObjectName("deprateEdit")
        self.deprateEdit.setGeometry(QRect(0, 320, 91, 26))
        self.deprateEdit2 = QLineEdit(self.page_2)
        self.deprateEdit2.setObjectName("deprateEdit2")
        self.deprateEdit2.setGeometry(QRect(100, 320, 91, 26))

        self.powerEdit = QLineEdit(self.page_2)
        self.powerEdit.setObjectName("powerEdit")
        self.powerEdit.setGeometry(QRect(0, 420, 91, 26))
        self.powerEdit2 = QLineEdit(self.page_2)
        self.powerEdit2.setObjectName("powerEdit2")
        self.powerEdit2.setGeometry(QRect(100, 420, 91, 26))

        self.thicknessEdit = QLineEdit(self.page_2)
        self.thicknessEdit.setObjectName("thicknessEdit")
        self.thicknessEdit.setGeometry(QRect(0, 370, 191, 26))

        self.delayEdit = QLineEdit(self.page_2)
        self.delayEdit.setObjectName("delayEdit")
        self.delayEdit.setGeometry(QRect(0, 470, 191, 26))

        self.materialLabel = QLabel(self.page_2)
        self.materialLabel.setObjectName("materialLabel")
        self.materialLabel.setGeometry(QRect(0, 150, 181, 20))
        self.materialLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.stopProcess = QPushButton(self.page_2)
        self.stopProcess.setObjectName("stopProcess")
        self.stopProcess.setGeometry(QRect(100, 610, 91, 71))  # ✅ 오른쪽으로 이동

        self.startProcess = QPushButton(self.page_2)
        self.startProcess.setObjectName("startProcess")
        self.startProcess.setGeometry(QRect(0, 610, 91, 71))    # ✅ 왼쪽으로 이동

        self.logWindow = QPlainTextEdit(self.page_2)
        self.logWindow.setObjectName("logWindow")
        self.logWindow.setGeometry(QRect(210, 490, 891, 191))

        self.logWindow.setReadOnly(True)
        self.logWindow.setUndoRedoEnabled(False)
        self.logWindow.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.logWindow.document().setMaximumBlockCount(2000)

        self.thicknessLabel = QLabel(self.page_2)
        self.thicknessLabel.setObjectName("thicknessLabel")
        self.thicknessLabel.setGeometry(QRect(0, 350, 181, 20))
        self.thicknessLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.delayLabel = QLabel(self.page_2)
        self.delayLabel.setObjectName("delayLabel")
        self.delayLabel.setGeometry(QRect(0, 450, 181, 20))
        self.delayLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.pwoerLabel = QLabel(self.page_2)
        self.pwoerLabel.setObjectName("pwoerLabel")
        self.pwoerLabel.setGeometry(QRect(0, 400, 181, 20))
        self.pwoerLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.deprateLabel = QLabel(self.page_2)
        self.deprateLabel.setObjectName("deprateLabel")
        self.deprateLabel.setGeometry(QRect(0, 300, 181, 20))
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

        self.sourcePower1 = QCheckBox(self.page_2)
        self.sourcePower1.setObjectName("sourcePower1")
        self.sourcePower1.setGeometry(QRect(0, 120, 81, 24))

        self.sourcePower2 = QCheckBox(self.page_2)
        self.sourcePower2.setObjectName("sourcePower2")
        self.sourcePower2.setGeometry(QRect(110, 120, 81, 24))

        # 기본 선택(안전하게 Power1만 기본 체크, 둘 다 가능)
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
        self.vacuumOnBtn.setText(QCoreApplication.translate("Form", "Vacuum\nON", None))
        self.vvBtn.setText(QCoreApplication.translate("Form", "V / V", None))
        self.doorBtn.setText(QCoreApplication.translate("Form", "Door", None))
        self.ms2shutterBtn.setText(QCoreApplication.translate("Form", "M.S 2\nShutter", None))
        self.ms1shutterBtn.setText(QCoreApplication.translate("Form", "M.S 1\nShutter", None))
        self.mainshutterBtn.setText(QCoreApplication.translate("Form", "Main\nShutter", None))
        # DAC 수동 입력
        self.dac1Edit.setPlaceholderText(QCoreApplication.translate("Form", "P1 DAC (0-4000)", None))
        self.dac2Edit.setPlaceholderText(QCoreApplication.translate("Form", "P2 DAC (0-4000)", None))
        self.dac1ApplyBtn.setText(QCoreApplication.translate("Form", "Set1", None))
        self.dac2ApplyBtn.setText(QCoreApplication.translate("Form", "Set2", None))
        self.ms1powerBtn.setText(QCoreApplication.translate("Form", "M.S 1\nPower", None))
        self.allstopBtn.setText(QCoreApplication.translate("Form", "ALL\nSTOP", None))
        self.label.setText(QCoreApplication.translate("Form", "G1", None))
        self.label_2.setText(QCoreApplication.translate("Form", "G2", None))
        self.label_3.setText(QCoreApplication.translate("Form", "Air", None))
        self.label_4.setText(QCoreApplication.translate("Form", "Water", None))
        self.rvBtn.setText(QCoreApplication.translate("Form", "R / V", None))

        # ✅ Chamber 타이틀
        self.chamberLabel.setText(QCoreApplication.translate("Form", "Chamber", None))

        # ✅ Pressure 표시(기본값)
        self.pressureCaption.setText(QCoreApplication.translate("Form", "Pressure", None))
        self.pressureValue.setText(QCoreApplication.translate("Form", "--- Torr", None))

        self.materialLabel.setText(QCoreApplication.translate("Form", "Material Name", None))
        self.stopProcess.setText(QCoreApplication.translate("Form", "Stop", None))
        self.startProcess.setText(QCoreApplication.translate("Form", "Start", None))
        self.thicknessLabel.setText(QCoreApplication.translate("Form", "Thickness", None))
        self.delayLabel.setText(QCoreApplication.translate("Form", "Delay", None))
        self.pwoerLabel.setText(QCoreApplication.translate("Form", "Power Ramp", None))
        self.deprateLabel.setText(QCoreApplication.translate("Form", "Dep.Rate", None))
        self.evaporatorLabel.setText(QCoreApplication.translate("Form", "Evaporator", None))
        self.hmiBtn.setText(QCoreApplication.translate("Form", "HMI", None))
        self.sourcePower1.setText(QCoreApplication.translate("Form", "Power 1", None))
        self.sourcePower2.setText(QCoreApplication.translate("Form", "Power 2", None))

        # ✅ Material 선택 버튼 기본 표시
        self.materialEdit.setText(QCoreApplication.translate("Form", "Select", None))
        self.materialEdit2.setText(QCoreApplication.translate("Form", "Select", None))

        # ✅ Density / Z-Factor 라벨 (위 라벨)
        self.materialRhoLabel1.setText(QCoreApplication.translate("Form", "Density", None))
        self.materialRhoLabel2.setText(QCoreApplication.translate("Form", "", None))  # 숨김 처리용
        self.materialZLabel1.setText(QCoreApplication.translate("Form", "Z-Factor", None))
        self.materialZLabel2.setText(QCoreApplication.translate("Form", "", None))    # 숨김 처리용

        # Cur Rate는 2칸이므로 각 칸 라벨 분리(재료/소스 1/2 의미)
        self.currentRateLabel.setText(QCoreApplication.translate("Form", "Cur Rate 1", None))
        self.currentRateLabel2.setText(QCoreApplication.translate("Form", "Cur Rate 2", None))

        # Thick는 전체 1칸
        self.currentThicknessLabel.setText(QCoreApplication.translate("Form", "Cur Thick", None))

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

        # Chamber 타이틀(조잡하지 않게 약간만 줄임)
        if hasattr(self, "chamberLabel"):
            ch_font = QFont()
            ch_font.setPointSize(20)
            ch_font.setBold(True)
            self.chamberLabel.setFont(ch_font)
            self.chamberLabel.setStyleSheet("color: rgb(90,90,90); background: transparent;")

        # Pressure 캡션(작게, 회색)
        if hasattr(self, "pressureCaption"):
            cap_font = QFont()
            cap_font.setPointSize(9)
            cap_font.setBold(False)
            self.pressureCaption.setFont(cap_font)
            self.pressureCaption.setStyleSheet("color: rgb(120,120,120); background: transparent;")

        # Pressure 값(크게, 진한색, 숫자 보기 좋게)
        if hasattr(self, "pressureValue"):
            val_font = QFont("Consolas")  # 없으면 자동 fallback
            val_font.setPointSize(18)
            val_font.setBold(True)
            self.pressureValue.setFont(val_font)
            self.pressureValue.setStyleSheet("color: rgb(40,40,40); background: transparent;")

        # ---- indicators ----
        self._style_indicator(self.g2_indicator_2, on=False)  # G1
        self._style_indicator(self.g2_indicator_3, on=False)  # G2
        self._style_indicator(self.g2_indicator_4, on=False)  # Air
        self._style_indicator(self.g2_indicator_5, on=False)  # Water

        for lab in (self.label, self.label_2, self.label_3, self.label_4):
            lab.setStyleSheet("color: black; background: transparent; font-weight: bold;")

        # ---- log windows (optional: 살짝 깔끔하게) ----
        self.hmiLogWindow.setStyleSheet("background: white; border: 1px solid #d0d0d0;")
        self.logWindow.setStyleSheet("background: white; border: 1px solid #d0d0d0;")
        self.processMonitor_HMI.setStyleSheet("background: white; border: 1px solid #d0d0d0;")

        # ✅ 상태 표시용 라인에딧: 입력 불가(표시 전용)
        for le in (self.processMonitor_HMI, self.processMonitor_Process):
            le.setStyleSheet("background: white; border: 1px solid #d0d0d0;")
            le.setReadOnly(True)
            le.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            le.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        # ✅ 표시용(LineEdit readOnly)도 "기존 입력칸(Dep.Rate 등)"과 동일한 기본 디자인 유지
        # - border를 강제하지 않는다(기본 테마 테두리 유지)
        # - readOnly 때문에 회색 배경이 되는 경우만 팔레트로 흰색 강제
        readonly_display = [
            self.materialDensityEdit1, self.materialDensityEdit2,
            self.materialZfactorEdit1, self.materialZfactorEdit2,
            self.currentRateEdit, self.currentThicknessEdit,
        ]
        if hasattr(self, "currentRateEdit2"):
            readonly_display.append(self.currentRateEdit2)

        for le in readonly_display:
            # 혹시 이전에 스타일을 먹인 적이 있으면 제거(기본 디자인으로 복귀)
            le.setStyleSheet("")

            pal = le.palette()
            pal.setColor(QPalette.ColorRole.Base, Qt.GlobalColor.white)
            le.setPalette(pal)

            # 표시 전용 UX
            le.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            le.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)


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
