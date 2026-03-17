# -*- coding: utf-8 -*-

"""
✅ Qt Designer로 잡아둔 "좌표/선 정렬"은 그대로 두고
- HMI 페이지 버튼 스타일
- 선(QFrame) 스타일
- 인디케이터(원형 LED) 스타일
만 적용한 "가벼운" 버전입니다.

※ Process 페이지의 기존 hmiBtn은 제거하고,
   공정 이름(Process Name) 입력칸(processNameEdit)을 추가합니다.
"""

from PySide6.QtCore import QCoreApplication, QMetaObject, QRect, Qt
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QStackedWidget, QWidget, QGroupBox, QSpinBox, QGridLayout,
    QAbstractSpinBox, QHBoxLayout, QVBoxLayout,
)


class Ui_Form(object):
    def setupUi(self, Form):
        if not Form.objectName():
            Form.setObjectName("Form")

        self._form = Form
        self._hmi_window_size = (1121, 860)
        self._normal_window_size = (1121, 700)

        Form.resize(*self._hmi_window_size)
        Form.setAutoFillBackground(True)

        self.stackedWidget = QStackedWidget(Form)
        self.stackedWidget.setObjectName("stackedWidget")
        self.stackedWidget.setGeometry(QRect(10, 0, 1101, 700))

        # =========================
        # HMI 전용 footer
        # - stackedWidget 바깥(Form 직속)
        # - HMI 페이지에서만 표시
        # =========================
        self.hmiFooter = QWidget(Form)
        self.hmiFooter.setObjectName("hmiFooter")
        self.hmiFooter.setGeometry(QRect(10, 705, 1101, 152))
        self.hmiFooter.setAutoFillBackground(False)

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
        # Turbo status
        # - TMP 상태 표시
        # - Start / Stop 버튼으로 실제 TMP 장비 명령 전송
        # - 상단 TMP 버튼(pushButton_13)은 PLC TMP coil만 제어
        # =========================
        self.tmpGroup = QGroupBox(self.hmiFooter)
        self.tmpGroup.setObjectName("tmpGroup")
        self.tmpGroup.setGeometry(QRect(0, 0, 540, 148))
        self.tmpGroup.setAutoFillBackground(True)

        self.tmpGroupLayout = QVBoxLayout(self.tmpGroup)
        self.tmpGroupLayout.setContentsMargins(10, 16, 10, 10)
        self.tmpGroupLayout.setSpacing(5)

        tmp_label_w = 48
        tmp_edit_h = 24

        # =========================
        # Row 0 : Conn / State
        # =========================
        self.tmpTopRow = QWidget(self.tmpGroup)
        self.tmpTopRow.setObjectName("tmpTopRow")
        self.tmpTopRowLayout = QHBoxLayout(self.tmpTopRow)
        self.tmpTopRowLayout.setContentsMargins(0, 0, 0, 0)
        self.tmpTopRowLayout.setSpacing(8)

        self.tmpConnBlock = QWidget(self.tmpTopRow)
        self.tmpConnBlock.setObjectName("tmpConnBlock")
        self.tmpConnBlockLayout = QHBoxLayout(self.tmpConnBlock)
        self.tmpConnBlockLayout.setContentsMargins(0, 0, 0, 0)
        self.tmpConnBlockLayout.setSpacing(6)

        self.tmpConnLabel = QLabel(self.tmpConnBlock)
        self.tmpConnLabel.setObjectName("tmpConnLabel")
        self.tmpConnLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tmpConnLabel.setFixedWidth(tmp_label_w)
        self.tmpConnBlockLayout.addWidget(self.tmpConnLabel)

        self.tmpConnEdit = QLineEdit(self.tmpConnBlock)
        self.tmpConnEdit.setObjectName("tmpConnEdit")
        self.tmpConnEdit.setReadOnly(True)
        self.tmpConnEdit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tmpConnEdit.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.tmpConnEdit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tmpConnEdit.setFixedHeight(tmp_edit_h)
        self.tmpConnBlockLayout.addWidget(self.tmpConnEdit)

        self.tmpStateBlock = QWidget(self.tmpTopRow)
        self.tmpStateBlock.setObjectName("tmpStateBlock")
        self.tmpStateBlockLayout = QHBoxLayout(self.tmpStateBlock)
        self.tmpStateBlockLayout.setContentsMargins(0, 0, 0, 0)
        self.tmpStateBlockLayout.setSpacing(6)

        self.tmpStateLabel = QLabel(self.tmpStateBlock)
        self.tmpStateLabel.setObjectName("tmpStateLabel")
        self.tmpStateLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tmpStateLabel.setFixedWidth(tmp_label_w)
        self.tmpStateBlockLayout.addWidget(self.tmpStateLabel)

        self.tmpStateEdit = QLineEdit(self.tmpStateBlock)
        self.tmpStateEdit.setObjectName("tmpStateEdit")
        self.tmpStateEdit.setReadOnly(True)
        self.tmpStateEdit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tmpStateEdit.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.tmpStateEdit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tmpStateEdit.setFixedHeight(tmp_edit_h)
        self.tmpStateBlockLayout.addWidget(self.tmpStateEdit)

        self.tmpTopRowLayout.addWidget(self.tmpConnBlock, 1)
        self.tmpTopRowLayout.addWidget(self.tmpStateBlock, 1)
        self.tmpGroupLayout.addWidget(self.tmpTopRow)

        # =========================
        # Row 1 : Freq / Current / Temp
        # =========================
        self.tmpMidRow = QWidget(self.tmpGroup)
        self.tmpMidRow.setObjectName("tmpMidRow")
        self.tmpMidRowLayout = QHBoxLayout(self.tmpMidRow)
        self.tmpMidRowLayout.setContentsMargins(0, 0, 0, 0)
        self.tmpMidRowLayout.setSpacing(8)

        self.tmpFreqBlock = QWidget(self.tmpMidRow)
        self.tmpFreqBlock.setObjectName("tmpFreqBlock")
        self.tmpFreqBlockLayout = QHBoxLayout(self.tmpFreqBlock)
        self.tmpFreqBlockLayout.setContentsMargins(0, 0, 0, 0)
        self.tmpFreqBlockLayout.setSpacing(6)

        self.tmpFreqLabel = QLabel(self.tmpFreqBlock)
        self.tmpFreqLabel.setObjectName("tmpFreqLabel")
        self.tmpFreqLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tmpFreqLabel.setFixedWidth(tmp_label_w)
        self.tmpFreqBlockLayout.addWidget(self.tmpFreqLabel)

        self.tmpFreqEdit = QLineEdit(self.tmpFreqBlock)
        self.tmpFreqEdit.setObjectName("tmpFreqEdit")
        self.tmpFreqEdit.setReadOnly(True)
        self.tmpFreqEdit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tmpFreqEdit.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.tmpFreqEdit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tmpFreqEdit.setFixedHeight(tmp_edit_h)
        self.tmpFreqBlockLayout.addWidget(self.tmpFreqEdit)

        self.tmpCurrentBlock = QWidget(self.tmpMidRow)
        self.tmpCurrentBlock.setObjectName("tmpCurrentBlock")
        self.tmpCurrentBlockLayout = QHBoxLayout(self.tmpCurrentBlock)
        self.tmpCurrentBlockLayout.setContentsMargins(0, 0, 0, 0)
        self.tmpCurrentBlockLayout.setSpacing(6)

        self.tmpCurrentLabel = QLabel(self.tmpCurrentBlock)
        self.tmpCurrentLabel.setObjectName("tmpCurrentLabel")
        self.tmpCurrentLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tmpCurrentLabel.setFixedWidth(tmp_label_w)
        self.tmpCurrentBlockLayout.addWidget(self.tmpCurrentLabel)

        self.tmpCurrentEdit = QLineEdit(self.tmpCurrentBlock)
        self.tmpCurrentEdit.setObjectName("tmpCurrentEdit")
        self.tmpCurrentEdit.setReadOnly(True)
        self.tmpCurrentEdit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tmpCurrentEdit.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.tmpCurrentEdit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tmpCurrentEdit.setFixedHeight(tmp_edit_h)
        self.tmpCurrentBlockLayout.addWidget(self.tmpCurrentEdit)

        self.tmpTempBlock = QWidget(self.tmpMidRow)
        self.tmpTempBlock.setObjectName("tmpTempBlock")
        self.tmpTempBlockLayout = QHBoxLayout(self.tmpTempBlock)
        self.tmpTempBlockLayout.setContentsMargins(0, 0, 0, 0)
        self.tmpTempBlockLayout.setSpacing(6)

        self.tmpTempLabel = QLabel(self.tmpTempBlock)
        self.tmpTempLabel.setObjectName("tmpTempLabel")
        self.tmpTempLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tmpTempLabel.setFixedWidth(tmp_label_w)
        self.tmpTempBlockLayout.addWidget(self.tmpTempLabel)

        self.tmpTempEdit = QLineEdit(self.tmpTempBlock)
        self.tmpTempEdit.setObjectName("tmpTempEdit")
        self.tmpTempEdit.setReadOnly(True)
        self.tmpTempEdit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tmpTempEdit.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.tmpTempEdit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tmpTempEdit.setFixedHeight(tmp_edit_h)
        self.tmpTempBlockLayout.addWidget(self.tmpTempEdit)

        self.tmpMidRowLayout.addWidget(self.tmpFreqBlock, 1)
        self.tmpMidRowLayout.addWidget(self.tmpCurrentBlock, 1)
        self.tmpMidRowLayout.addWidget(self.tmpTempBlock, 1)
        self.tmpGroupLayout.addWidget(self.tmpMidRow)

        # =========================
        # Row 2 : Detail / Alarm
        # =========================
        self.tmpInfoRow = QWidget(self.tmpGroup)
        self.tmpInfoRow.setObjectName("tmpInfoRow")
        self.tmpInfoRowLayout = QHBoxLayout(self.tmpInfoRow)
        self.tmpInfoRowLayout.setContentsMargins(0, 0, 0, 0)
        self.tmpInfoRowLayout.setSpacing(8)

        self.tmpDetailBlock = QWidget(self.tmpInfoRow)
        self.tmpDetailBlock.setObjectName("tmpDetailBlock")
        self.tmpDetailBlockLayout = QHBoxLayout(self.tmpDetailBlock)
        self.tmpDetailBlockLayout.setContentsMargins(0, 0, 0, 0)
        self.tmpDetailBlockLayout.setSpacing(6)

        self.tmpDetailLabel = QLabel(self.tmpDetailBlock)
        self.tmpDetailLabel.setObjectName("tmpDetailLabel")
        self.tmpDetailLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tmpDetailLabel.setFixedWidth(tmp_label_w)
        self.tmpDetailBlockLayout.addWidget(self.tmpDetailLabel)

        self.tmpDetailEdit = QLineEdit(self.tmpDetailBlock)
        self.tmpDetailEdit.setObjectName("tmpDetailEdit")
        self.tmpDetailEdit.setReadOnly(True)
        self.tmpDetailEdit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tmpDetailEdit.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.tmpDetailEdit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tmpDetailEdit.setFixedHeight(tmp_edit_h)
        self.tmpDetailBlockLayout.addWidget(self.tmpDetailEdit)

        self.tmpAlarmBlock = QWidget(self.tmpInfoRow)
        self.tmpAlarmBlock.setObjectName("tmpAlarmBlock")
        self.tmpAlarmBlockLayout = QHBoxLayout(self.tmpAlarmBlock)
        self.tmpAlarmBlockLayout.setContentsMargins(0, 0, 0, 0)
        self.tmpAlarmBlockLayout.setSpacing(6)

        self.tmpAlarmLabel = QLabel(self.tmpAlarmBlock)
        self.tmpAlarmLabel.setObjectName("tmpAlarmLabel")
        self.tmpAlarmLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tmpAlarmLabel.setFixedWidth(tmp_label_w)
        self.tmpAlarmBlockLayout.addWidget(self.tmpAlarmLabel)

        self.tmpAlarmEdit = QLineEdit(self.tmpAlarmBlock)
        self.tmpAlarmEdit.setObjectName("tmpAlarmEdit")
        self.tmpAlarmEdit.setReadOnly(True)
        self.tmpAlarmEdit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tmpAlarmEdit.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.tmpAlarmEdit.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.tmpAlarmEdit.setFixedHeight(tmp_edit_h)
        self.tmpAlarmBlockLayout.addWidget(self.tmpAlarmEdit)

        self.tmpInfoRowLayout.addWidget(self.tmpDetailBlock, 1)
        self.tmpInfoRowLayout.addWidget(self.tmpAlarmBlock, 2)
        self.tmpGroupLayout.addWidget(self.tmpInfoRow)

        # =========================
        # Row 3 : Start / Stop
        # =========================
        self.tmpButtonRow = QWidget(self.tmpGroup)
        self.tmpButtonRow.setObjectName("tmpButtonRow")
        self.tmpButtonRowLayout = QHBoxLayout(self.tmpButtonRow)
        self.tmpButtonRowLayout.setContentsMargins(0, 2, 0, 0)
        self.tmpButtonRowLayout.setSpacing(8)

        self.tmpStartBtn = QPushButton(self.tmpButtonRow)
        self.tmpStartBtn.setObjectName("tmpStartBtn")
        self.tmpStartBtn.setAutoDefault(False)
        self.tmpStartBtn.setFixedHeight(28)
        self.tmpButtonRowLayout.addWidget(self.tmpStartBtn, 1)

        self.tmpStopBtn = QPushButton(self.tmpButtonRow)
        self.tmpStopBtn.setObjectName("tmpStopBtn")
        self.tmpStopBtn.setAutoDefault(False)
        self.tmpStopBtn.setFixedHeight(28)
        self.tmpButtonRowLayout.addWidget(self.tmpStopBtn, 1)

        self.tmpGroupLayout.addWidget(self.tmpButtonRow)

        # =========================
        # DAC manual set (Power1/Power2)
        # - Turbo Status와 같은 "박스형" 느낌으로 맞추기 위해
        #   row 단위 QWidget + QHBoxLayout 으로 재구성
        # - QSpinBox의 표시 화살표는 숨기고 입력 박스처럼 사용
        #   (마우스 휠 / 키보드 ↑↓ 는 그대로 동작)
        # =========================
        self.dacGroup = QGroupBox(self.hmiFooter)
        self.dacGroup.setObjectName("dacGroup")
        self.dacGroup.setGeometry(QRect(551, 0, 540, 148))
        self.dacGroup.setAutoFillBackground(True)

        self.dacGroupLayout = QVBoxLayout(self.dacGroup)
        self.dacGroupLayout.setContentsMargins(16, 18, 16, 12)
        self.dacGroupLayout.setSpacing(8)

        control_h = 26
        ch_w = 62
        set_w = 84
        step_btn_w = 46
        act_btn_w = 56
        read_w = 92

        # =========================
        # Header row
        # =========================
        self.dacHeaderRow = QWidget(self.dacGroup)
        self.dacHeaderRow.setObjectName("dacHeaderRow")
        self.dacHeaderRowLayout = QHBoxLayout(self.dacHeaderRow)
        self.dacHeaderRowLayout.setContentsMargins(0, 0, 0, 0)
        self.dacHeaderRowLayout.setSpacing(8)

        self.dacChannelHeader = QLabel(self.dacHeaderRow)
        self.dacChannelHeader.setObjectName("dacChannelHeader")
        self.dacChannelHeader.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dacChannelHeader.setFixedWidth(ch_w)
        self.dacHeaderRowLayout.addWidget(self.dacChannelHeader)

        self.dacSetHeader = QLabel(self.dacHeaderRow)
        self.dacSetHeader.setObjectName("dacSetHeader")
        self.dacSetHeader.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dacSetHeader.setFixedWidth(set_w)
        self.dacHeaderRowLayout.addWidget(self.dacSetHeader)

        self.dacStepHeader = QLabel(self.dacHeaderRow)
        self.dacStepHeader.setObjectName("dacStepHeader")
        self.dacStepHeader.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dacStepHeader.setFixedWidth(step_btn_w * 2 + 4)
        self.dacHeaderRowLayout.addWidget(self.dacStepHeader)

        self.dacActionHeader = QLabel(self.dacHeaderRow)
        self.dacActionHeader.setObjectName("dacActionHeader")
        self.dacActionHeader.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dacActionHeader.setFixedWidth(act_btn_w * 2 + 4)
        self.dacHeaderRowLayout.addWidget(self.dacActionHeader)

        self.dacHeaderRowLayout.addStretch(1)

        self.dacReadHeader = QLabel(self.dacHeaderRow)
        self.dacReadHeader.setObjectName("dacReadHeader")
        self.dacReadHeader.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dacReadHeader.setFixedWidth(read_w)
        self.dacHeaderRowLayout.addWidget(self.dacReadHeader)

        self.dacGroupLayout.addWidget(self.dacHeaderRow)

        # =========================
        # Row 1 : Power 1
        # =========================
        self.dacRow1 = QWidget(self.dacGroup)
        self.dacRow1.setObjectName("dacRow1")
        self.dacRow1Layout = QHBoxLayout(self.dacRow1)
        self.dacRow1Layout.setContentsMargins(0, 0, 0, 0)
        self.dacRow1Layout.setSpacing(8)

        self.dac1Label = QLabel(self.dacRow1)
        self.dac1Label.setObjectName("dac1Label")
        self.dac1Label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dac1Label.setFixedWidth(ch_w)
        self.dacRow1Layout.addWidget(self.dac1Label)

        self.dac1Spin = QSpinBox(self.dacRow1)
        self.dac1Spin.setObjectName("dac1Spin")
        self.dac1Spin.setRange(0, 4000)
        self.dac1Spin.setSingleStep(1)
        self.dac1Spin.setAccelerated(True)
        self.dac1Spin.setKeyboardTracking(False)
        self.dac1Spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.dac1Spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dac1Spin.setFixedSize(set_w, control_h)
        self.dacRow1Layout.addWidget(self.dac1Spin)

        self.dac1StepBox = QWidget(self.dacRow1)
        self.dac1StepBox.setObjectName("dac1StepBox")
        self.dac1StepBoxLayout = QHBoxLayout(self.dac1StepBox)
        self.dac1StepBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.dac1StepBoxLayout.setSpacing(6)

        self.dac1Down100Btn = QPushButton(self.dac1StepBox)
        self.dac1Down100Btn.setObjectName("dac1Down100Btn")
        self.dac1Down100Btn.setFixedSize(step_btn_w, control_h)
        self.dac1StepBoxLayout.addWidget(self.dac1Down100Btn)

        self.dac1Up100Btn = QPushButton(self.dac1StepBox)
        self.dac1Up100Btn.setObjectName("dac1Up100Btn")
        self.dac1Up100Btn.setFixedSize(step_btn_w, control_h)
        self.dac1StepBoxLayout.addWidget(self.dac1Up100Btn)

        self.dacRow1Layout.addWidget(self.dac1StepBox)

        self.dac1ActionBox = QWidget(self.dacRow1)
        self.dac1ActionBox.setObjectName("dac1ActionBox")
        self.dac1ActionBoxLayout = QHBoxLayout(self.dac1ActionBox)
        self.dac1ActionBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.dac1ActionBoxLayout.setSpacing(6)

        self.dac1SetBtn = QPushButton(self.dac1ActionBox)
        self.dac1SetBtn.setObjectName("dac1SetBtn")
        self.dac1SetBtn.setFixedSize(act_btn_w, control_h)
        self.dac1ActionBoxLayout.addWidget(self.dac1SetBtn)

        self.dac1ResetBtn = QPushButton(self.dac1ActionBox)
        self.dac1ResetBtn.setObjectName("dac1ResetBtn")
        self.dac1ResetBtn.setFixedSize(act_btn_w, control_h)
        self.dac1ActionBoxLayout.addWidget(self.dac1ResetBtn)

        self.dacRow1Layout.addWidget(self.dac1ActionBox)
        self.dacRow1Layout.addStretch(1)

        self.dacActual1Edit = QLineEdit(self.dacRow1)
        self.dacActual1Edit.setObjectName("dacActual1Edit")
        self.dacActual1Edit.setReadOnly(True)
        self.dacActual1Edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.dacActual1Edit.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.dacActual1Edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dacActual1Edit.setFixedSize(read_w, control_h)
        self.dacRow1Layout.addWidget(self.dacActual1Edit)

        self.dacGroupLayout.addWidget(self.dacRow1)

        # =========================
        # Row 2 : Power 2
        # =========================
        self.dacRow2 = QWidget(self.dacGroup)
        self.dacRow2.setObjectName("dacRow2")
        self.dacRow2Layout = QHBoxLayout(self.dacRow2)
        self.dacRow2Layout.setContentsMargins(0, 0, 0, 0)
        self.dacRow2Layout.setSpacing(8)

        self.dac2Label = QLabel(self.dacRow2)
        self.dac2Label.setObjectName("dac2Label")
        self.dac2Label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dac2Label.setFixedWidth(ch_w)
        self.dacRow2Layout.addWidget(self.dac2Label)

        self.dac2Spin = QSpinBox(self.dacRow2)
        self.dac2Spin.setObjectName("dac2Spin")
        self.dac2Spin.setRange(0, 4000)
        self.dac2Spin.setSingleStep(1)
        self.dac2Spin.setAccelerated(True)
        self.dac2Spin.setKeyboardTracking(False)
        self.dac2Spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.dac2Spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dac2Spin.setFixedSize(set_w, control_h)
        self.dacRow2Layout.addWidget(self.dac2Spin)

        self.dac2StepBox = QWidget(self.dacRow2)
        self.dac2StepBox.setObjectName("dac2StepBox")
        self.dac2StepBoxLayout = QHBoxLayout(self.dac2StepBox)
        self.dac2StepBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.dac2StepBoxLayout.setSpacing(6)

        self.dac2Down100Btn = QPushButton(self.dac2StepBox)
        self.dac2Down100Btn.setObjectName("dac2Down100Btn")
        self.dac2Down100Btn.setFixedSize(step_btn_w, control_h)
        self.dac2StepBoxLayout.addWidget(self.dac2Down100Btn)

        self.dac2Up100Btn = QPushButton(self.dac2StepBox)
        self.dac2Up100Btn.setObjectName("dac2Up100Btn")
        self.dac2Up100Btn.setFixedSize(step_btn_w, control_h)
        self.dac2StepBoxLayout.addWidget(self.dac2Up100Btn)

        self.dacRow2Layout.addWidget(self.dac2StepBox)

        self.dac2ActionBox = QWidget(self.dacRow2)
        self.dac2ActionBox.setObjectName("dac2ActionBox")
        self.dac2ActionBoxLayout = QHBoxLayout(self.dac2ActionBox)
        self.dac2ActionBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.dac2ActionBoxLayout.setSpacing(6)

        self.dac2SetBtn = QPushButton(self.dac2ActionBox)
        self.dac2SetBtn.setObjectName("dac2SetBtn")
        self.dac2SetBtn.setFixedSize(act_btn_w, control_h)
        self.dac2ActionBoxLayout.addWidget(self.dac2SetBtn)

        self.dac2ResetBtn = QPushButton(self.dac2ActionBox)
        self.dac2ResetBtn.setObjectName("dac2ResetBtn")
        self.dac2ResetBtn.setFixedSize(act_btn_w, control_h)
        self.dac2ActionBoxLayout.addWidget(self.dac2ResetBtn)

        self.dacRow2Layout.addWidget(self.dac2ActionBox)
        self.dacRow2Layout.addStretch(1)

        self.dacActual2Edit = QLineEdit(self.dacRow2)
        self.dacActual2Edit.setObjectName("dacActual2Edit")
        self.dacActual2Edit.setReadOnly(True)
        self.dacActual2Edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.dacActual2Edit.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.dacActual2Edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dacActual2Edit.setFixedSize(read_w, control_h)
        self.dacRow2Layout.addWidget(self.dacActual2Edit)

        self.dacGroupLayout.addWidget(self.dacRow2)

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

        self.processMonitor_HMI = QLabel(self.page)
        self.processMonitor_HMI.setObjectName("processMonitor_HMI")
        self.processMonitor_HMI.setGeometry(QRect(230, 20, 365, 71))
        self.processMonitor_HMI.setWordWrap(True)
        self.processMonitor_HMI.setTextFormat(Qt.TextFormat.PlainText)
        self.processMonitor_HMI.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.processMonitor_HMI.setMargin(10)
        self.processMonitor_HMI.setAutoFillBackground(True)

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

        # ✅ Cur Rate: 라벨 1개(전체폭) + Edit 1개
        self.currentRateLabel = QLabel(self.page_2)
        self.currentRateLabel.setObjectName("currentRateLabel")
        self.currentRateLabel.setGeometry(QRect(0, 356, 191, 20))

        self.currentRateEdit = QLineEdit(self.page_2)
        self.currentRateEdit.setObjectName("currentRateEdit")
        self.currentRateEdit.setGeometry(QRect(0, 376, 191, 26))
        self.currentRateEdit.setReadOnly(True)

        # ✅ Cur Thick (Edit 끝 +4 = 다음 라벨)
        self.currentThicknessLabel = QLabel(self.page_2)
        self.currentThicknessLabel.setObjectName("currentThicknessLabel")
        self.currentThicknessLabel.setGeometry(QRect(0, 406, 191, 20))

        self.currentThicknessEdit = QLineEdit(self.page_2)
        self.currentThicknessEdit.setObjectName("currentThicknessEdit")
        self.currentThicknessEdit.setGeometry(QRect(0, 426, 191, 26))
        self.currentThicknessEdit.setReadOnly(True)

        self.actualPower1Label = QLabel(self.page_2)
        self.actualPower1Label.setObjectName("actualPower1Label")
        self.actualPower1Label.setGeometry(QRect(0, 456, 91, 20))
        self.actualPower1Label.setAlignment(
            Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        self.actualPower2Label = QLabel(self.page_2)
        self.actualPower2Label.setObjectName("actualPower2Label")
        self.actualPower2Label.setGeometry(QRect(100, 456, 91, 20))
        self.actualPower2Label.setAlignment(
            Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        self.actualPower1Edit = QLineEdit(self.page_2)
        self.actualPower1Edit.setObjectName("actualPower1Edit")
        self.actualPower1Edit.setGeometry(QRect(0, 476, 91, 26))
        self.actualPower1Edit.setReadOnly(True)
        self.actualPower1Edit.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.actualPower2Edit = QLineEdit(self.page_2)
        self.actualPower2Edit.setObjectName("actualPower2Edit")
        self.actualPower2Edit.setGeometry(QRect(100, 476, 91, 26))
        self.actualPower2Edit.setReadOnly(True)
        self.actualPower2Edit.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ✅ Recipe (Edit 끝 +4)
        self.recipeBtn = QPushButton(self.page_2)
        self.recipeBtn.setObjectName("recipeBtn")
        self.recipeBtn.setGeometry(QRect(0, 556, 191, 41))

        self.graphWidget = QWidget(self.page_2)
        self.graphWidget.setObjectName("graphWidget")
        self.graphWidget.setGeometry(QRect(209, 50, 891, 431))
        self.graphWidget.setAutoFillBackground(True)

        self.deprateEdit = QLineEdit(self.page_2)
        self.deprateEdit.setObjectName("deprateEdit")
        self.deprateEdit.setGeometry(QRect(0, 226, 91, 26))

        self.deprateEdit2 = QLineEdit(self.page_2)
        self.deprateEdit2.setObjectName("deprateEdit2")
        self.deprateEdit2.setGeometry(QRect(100, 226, 91, 26))

        self.thicknessEdit = QLineEdit(self.page_2)
        self.thicknessEdit.setObjectName("thicknessEdit")
        self.thicknessEdit.setGeometry(QRect(0, 276, 191, 26))

        self.delayEdit = QLineEdit(self.page_2)
        self.delayEdit.setObjectName("delayEdit")
        self.delayEdit.setGeometry(QRect(0, 326, 191, 26))

        self.materialLabel = QLabel(self.page_2)
        self.materialLabel.setObjectName("materialLabel")
        self.materialLabel.setGeometry(QRect(0, 150, 181, 20))
        self.materialLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # ✅ Start/Stop (Recipe 끝 +4)
        self.stopProcess = QPushButton(self.page_2)
        self.stopProcess.setObjectName("stopProcess")
        self.stopProcess.setGeometry(QRect(100, 601, 91, 71))

        self.startProcess = QPushButton(self.page_2)
        self.startProcess.setObjectName("startProcess")
        self.startProcess.setGeometry(QRect(0, 601, 91, 71))

        self.logWindow = QPlainTextEdit(self.page_2)
        self.logWindow.setObjectName("logWindow")
        self.logWindow.setGeometry(QRect(210, 490, 891, 191))

        self.logWindow.setReadOnly(True)
        self.logWindow.setUndoRedoEnabled(False)
        self.logWindow.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.logWindow.document().setMaximumBlockCount(2000)

        self.thicknessLabel = QLabel(self.page_2)
        self.thicknessLabel.setObjectName("thicknessLabel")
        self.thicknessLabel.setGeometry(QRect(0, 256, 181, 20))
        self.thicknessLabel.setAlignment(
            Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        self.delayLabel = QLabel(self.page_2)
        self.delayLabel.setObjectName("delayLabel")
        self.delayLabel.setGeometry(QRect(0, 306, 191, 20))
        self.delayLabel.setAlignment(
            Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        self.deprateLabel = QLabel(self.page_2)
        self.deprateLabel.setObjectName("deprateLabel")
        self.deprateLabel.setGeometry(QRect(0, 206, 181, 20))
        self.deprateLabel.setAlignment(
            Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )

        self.evaporatorLabel = QLabel(self.page_2)
        self.evaporatorLabel.setObjectName("evaporatorLabel")
        self.evaporatorLabel.setGeometry(QRect(0, 10, 191, 31))

        font2 = QFont()
        font2.setPointSize(19)
        self.evaporatorLabel.setFont(font2)
        self.evaporatorLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ✅ Process Name (HMI 버튼 자리 대체)
        self.processNameLabel = QLabel(self.page_2)
        self.processNameLabel.setObjectName("processNameLabel")
        self.processNameLabel.setGeometry(QRect(0, 60, 191, 20))
        self.processNameLabel.setAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.processNameEdit = QLineEdit(self.page_2)
        self.processNameEdit.setObjectName("processNameEdit")
        self.processNameEdit.setGeometry(QRect(0, 85, 191, 26))

        self.sourcePower1 = QCheckBox(self.page_2)
        self.sourcePower1.setObjectName("sourcePower1")
        self.sourcePower1.setGeometry(QRect(0, 120, 81, 24))

        self.sourcePower2 = QCheckBox(self.page_2)
        self.sourcePower2.setObjectName("sourcePower2")
        self.sourcePower2.setGeometry(QRect(110, 120, 81, 24))

        self.processMonitor_Process = QLineEdit(self.page_2)
        self.processMonitor_Process.setObjectName("processMonitor_Process")
        self.processMonitor_Process.setGeometry(QRect(210, 5, 891, 41))

        self.stackedWidget.addWidget(self.page_2)

        # ---- translation ----
        self.retranslateUi(Form)
        self.stackedWidget.setCurrentIndex(0)
        QMetaObject.connectSlotsByName(Form)

        # 페이지 전환 시 HMI footer 표시/숨김 + 창 높이 변경
        self.stackedWidget.currentChanged.connect(self._on_stacked_index_changed)
        self._on_stacked_index_changed(self.stackedWidget.currentIndex())

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
        self.vvBtn.setText(QCoreApplication.translate("Form", "Vent", None))
        self.doorBtn.setText(QCoreApplication.translate("Form", "Door", None))
        self.ms2shutterBtn.setText(QCoreApplication.translate("Form", "M.S 2\nShutter", None))
        self.ms1shutterBtn.setText(QCoreApplication.translate("Form", "M.S 1\nShutter", None))
        self.mainshutterBtn.setText(QCoreApplication.translate("Form", "Main\nShutter", None))

        # TMP 상태 표시
        self.tmpGroup.setTitle(QCoreApplication.translate("Form", "Turbo Status", None))
        self.tmpConnLabel.setText(QCoreApplication.translate("Form", "Conn", None))
        self.tmpStateLabel.setText(QCoreApplication.translate("Form", "State", None))
        self.tmpFreqLabel.setText(QCoreApplication.translate("Form", "Freq", None))
        self.tmpCurrentLabel.setText(QCoreApplication.translate("Form", "Current", None))
        self.tmpTempLabel.setText(QCoreApplication.translate("Form", "Temp", None))
        self.tmpAlarmLabel.setText(QCoreApplication.translate("Form", "Alarm", None))

        self.tmpConnEdit.setText(QCoreApplication.translate("Form", "Disconnected", None))
        self.tmpStateEdit.setText(QCoreApplication.translate("Form", "-", None))
        self.tmpFreqEdit.setText(QCoreApplication.translate("Form", "- Hz", None))
        self.tmpCurrentEdit.setText(QCoreApplication.translate("Form", "- A", None))
        self.tmpTempEdit.setText(QCoreApplication.translate("Form", "- °C", None))
        self.tmpAlarmEdit.setText(QCoreApplication.translate("Form", "-", None))
        self.tmpDetailLabel.setText(QCoreApplication.translate("Form", "Detail", None))
        self.tmpDetailEdit.setText(QCoreApplication.translate("Form", "-", None))

        self.tmpStartBtn.setText(QCoreApplication.translate("Form", "Start", None))
        self.tmpStopBtn.setText(QCoreApplication.translate("Form", "Stop", None))

        # DAC 수동 입력
        self.dacGroup.setTitle(QCoreApplication.translate("Form", "Power Manual", None))
        self.dac1Label.setText(QCoreApplication.translate("Form", "Power 1", None))
        self.dac2Label.setText(QCoreApplication.translate("Form", "Power 2", None))

        self.dacChannelHeader.setText(QCoreApplication.translate("Form", "CH", None))
        self.dacSetHeader.setText(QCoreApplication.translate("Form", "SET", None))
        self.dacStepHeader.setText(QCoreApplication.translate("Form", "STEP", None))
        self.dacActionHeader.setText(QCoreApplication.translate("Form", "ACTION", None))
        self.dacReadHeader.setText(QCoreApplication.translate("Form", "READ", None))

        self.dac1Down100Btn.setText(QCoreApplication.translate("Form", "-100", None))
        self.dac1Up100Btn.setText(QCoreApplication.translate("Form", "+100", None))
        self.dac2Down100Btn.setText(QCoreApplication.translate("Form", "-100", None))
        self.dac2Up100Btn.setText(QCoreApplication.translate("Form", "+100", None))

        self.dac1SetBtn.setText(QCoreApplication.translate("Form", "Apply", None))
        self.dac2SetBtn.setText(QCoreApplication.translate("Form", "Apply", None))
        self.dac1ResetBtn.setText(QCoreApplication.translate("Form", "Reset", None))
        self.dac2ResetBtn.setText(QCoreApplication.translate("Form", "Reset", None))

        self.dacActual1Edit.setText(QCoreApplication.translate("Form", "---", None))
        self.dacActual2Edit.setText(QCoreApplication.translate("Form", "---", None))

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
        self.thicknessLabel.setText(QCoreApplication.translate("Form", "Thickness (Å)", None))
        self.delayLabel.setText(QCoreApplication.translate("Form", "Shutter Delay (min)", None))
        #self.pwoerLabel.setText(QCoreApplication.translate("Form", "Power Ramp", None))
        self.deprateLabel.setText(QCoreApplication.translate("Form", "Dep.Rate (Å/s)", None))
        self.evaporatorLabel.setText(QCoreApplication.translate("Form", "Evaporator", None))

        # ✅ Process Name
        self.processNameLabel.setText(QCoreApplication.translate("Form", "Process Name", None))

        self.sourcePower1.setText(QCoreApplication.translate("Form", "Power 1", None))
        self.sourcePower2.setText(QCoreApplication.translate("Form", "Power 2", None))

        # ✅ Material 선택 버튼 기본 표시
        self.materialEdit.setText(QCoreApplication.translate("Form", "Select", None))
        self.materialEdit2.setText(QCoreApplication.translate("Form", "Select", None))

        self.actualPower1Label.setText(QCoreApplication.translate("Form", "Power 1 (A)", None))
        self.actualPower2Label.setText(QCoreApplication.translate("Form", "Power 2 (A)", None))
        self.actualPower1Edit.setText(QCoreApplication.translate("Form", "---", None))
        self.actualPower2Edit.setText(QCoreApplication.translate("Form", "---", None))

        # Cur Rate는 표시값이므로 1칸만 사용
        self.currentRateLabel.setText(QCoreApplication.translate("Form", "Cur Rate (Å/s)", None))

        # Thick는 전체 1칸
        self.currentThicknessLabel.setText(QCoreApplication.translate("Form", "Cur Thick (Å)", None))

        self.recipeBtn.setText("Recipe")
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

        # ✅ processBtn / configBtn / vacuumOnBtn : 기본 스타일 유지 + 글자만 키움
        top_font = self.processBtn.font()
        top_font.setPointSize(12)
        self.processBtn.setFont(top_font)
        self.configBtn.setFont(top_font)

        # "Vacuum\nON"은 2줄이라 너무 크게 하면 잘릴 수 있어서 살짝 작게(원하면 16으로 동일하게 올려도 됨)
        vac_font = self.vacuumOnBtn.font()
        vac_font.setPointSize(12)
        self.vacuumOnBtn.setFont(vac_font)

        # 상단 상태창(HMI) - QLabel로 바뀌었으므로 별도 박스 스타일 복구
        self.processMonitor_HMI.setStyleSheet("""
            QLabel {
                background: white;
                border: 1px solid #d0d0d0;
                border-radius: 2px;
                color: #111111;
                padding: 6px 10px;
            }
        """)

        status_font = QFont("Consolas")
        status_font.setPointSize(12)
        status_font.setBold(True)
        self.processMonitor_HMI.setFont(status_font)

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

        # ---- TMP status group ----
        # ---- footer info groups: Turbo / Power ----
        footer_group_qss = """
            QGroupBox {
                font-weight: bold;
                font-size: 10pt;
                border: 1px solid #cfd4da;
                border-radius: 8px;
                margin-top: 8px;
                background: #f8f9fb;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px 0 4px;
                color: #202428;
            }
            QLabel {
                background: transparent;
                color: #444444;
                font-size: 9pt;
                font-weight: bold;
            }
            QSpinBox {
                background: white;
                border: 1px solid #d4d8de;
                border-radius: 4px;
                padding: 0 6px 0 6px;
                font-size: 10pt;
                color: black;
            }
        """

        footer_step_btn_qss = """
            QPushButton {
                background: #f0f2f5;
                border: 1px solid #cfd4da;
                border-radius: 4px;
                color: #222222;
                font-size: 9pt;
                font-weight: bold;
            }
            QPushButton:pressed {
                background: #e3e7ec;
            }
        """

        footer_apply_btn_qss = """
            QPushButton {
                background: #e8f1ff;
                border: 1px solid #aac3ee;
                border-radius: 4px;
                color: #174a9c;
                font-size: 9pt;
                font-weight: bold;
            }
            QPushButton:pressed {
                background: #d8e7ff;
            }
        """

        footer_reset_btn_qss = """
            QPushButton {
                background: #f5f5f5;
                border: 1px solid #d0d0d0;
                border-radius: 4px;
                color: #444444;
                font-size: 9pt;
                font-weight: bold;
            }
            QPushButton:pressed {
                background: #e7e7e7;
            }
        """

        footer_start_btn_qss = """
            QPushButton {
                background: #eaf8ea;
                border: 1px solid #a8d5a8;
                border-radius: 5px;
                color: #1e6b1e;
                font-size: 10pt;
                font-weight: bold;
            }
            QPushButton:pressed {
                background: #dff1df;
            }
        """

        footer_stop_btn_qss = """
            QPushButton {
                background: #fff0f0;
                border: 1px solid #e0b4b4;
                border-radius: 5px;
                color: #a12626;
                font-size: 10pt;
                font-weight: bold;
            }
            QPushButton:pressed {
                background: #ffe3e3;
            }
        """

        footer_status_qss = """
            QLineEdit {
                background: white;
                border: 1px solid #d5dae0;
                border-radius: 4px;
                padding: 0 8px 0 8px;
                color: #1e1e1e;
            }
        """

        footer_metric_qss = """
            QLineEdit {
                background: #fbfcfe;
                border: 1px solid #cfd8e3;
                border-radius: 4px;
                padding: 0 8px 0 8px;
                color: #0f2940;
                font-weight: bold;
            }
        """

        footer_alarm_qss = """
            QLineEdit {
                background: #fff8f8;
                border: 1px solid #e1b6b6;
                border-radius: 4px;
                padding: 0 8px 0 8px;
                color: #8d2020;
                font-weight: bold;
            }
        """

        footer_readback_qss = """
            QLineEdit {
                background: #f4f7fb;
                border: 1px solid #c5d2e3;
                border-radius: 4px;
                padding: 0 8px 0 8px;
                color: #17324d;
                font-weight: bold;
            }
        """

        self.tmpGroup.setStyleSheet(footer_group_qss)
        self.dacGroup.setStyleSheet(footer_group_qss)

        # Turbo buttons
        self.tmpStartBtn.setStyleSheet(footer_start_btn_qss)
        self.tmpStopBtn.setStyleSheet(footer_stop_btn_qss)

        # Power buttons
        for b in (
            self.dac1Down100Btn, self.dac1Up100Btn,
            self.dac2Down100Btn, self.dac2Up100Btn,
        ):
            b.setStyleSheet(footer_step_btn_qss)

        for b in (self.dac1SetBtn, self.dac2SetBtn):
            b.setStyleSheet(footer_apply_btn_qss)

        for b in (self.dac1ResetBtn, self.dac2ResetBtn):
            b.setStyleSheet(footer_reset_btn_qss)

        # Turbo displays
        for le in (self.tmpConnEdit, self.tmpStateEdit, self.tmpDetailEdit):
            le.setStyleSheet(footer_status_qss)

        for le in (self.tmpFreqEdit, self.tmpCurrentEdit, self.tmpTempEdit):
            le.setStyleSheet(footer_metric_qss)

        self.tmpAlarmEdit.setStyleSheet(footer_alarm_qss)

        # Power readback
        self.dacActual1Edit.setStyleSheet(footer_readback_qss)
        self.dacActual2Edit.setStyleSheet(footer_readback_qss)

        # Footer fonts
        mono_font = QFont("Consolas")
        mono_font.setPointSize(10)
        mono_font.setBold(True)

        for w in (
            self.tmpConnEdit, self.tmpStateEdit,
            self.tmpFreqEdit, self.tmpCurrentEdit, self.tmpTempEdit,
            self.tmpDetailEdit, self.tmpAlarmEdit,
            self.dacActual1Edit, self.dacActual2Edit,
        ):
            w.setFont(mono_font)

        footer_font = self.tmpConnEdit.font()
        for w in (
            self.dacChannelHeader, self.dacSetHeader, self.dacStepHeader,
            self.dacActionHeader, self.dacReadHeader,
            self.dac1Label, self.dac2Label,
            self.dac1Spin, self.dac2Spin,
            self.dac1Down100Btn, self.dac1Up100Btn,
            self.dac1SetBtn, self.dac1ResetBtn,
            self.dac2Down100Btn, self.dac2Up100Btn,
            self.dac2SetBtn, self.dac2ResetBtn,
            self.tmpStartBtn, self.tmpStopBtn,
        ):
            w.setFont(footer_font)

        for hdr in (
            self.dacChannelHeader, self.dacSetHeader,
            self.dacStepHeader, self.dacActionHeader, self.dacReadHeader
        ):
            hdr.setStyleSheet("color: #6b7280; background: transparent; font-weight: bold;")

        # Process 페이지 표시 전용 edit만 기본 흰색 처리
        readonly_display = [
            self.currentRateEdit,
            self.currentThicknessEdit,
        ]

        for le in readonly_display:
            le.setStyleSheet("")
            pal = le.palette()
            pal.setColor(QPalette.ColorRole.Base, Qt.GlobalColor.white)
            le.setPalette(pal)
            le.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            le.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        # Footer 표시 전용 위젯은 커스텀 스타일 유지, UX만 잠금
        for le in (
            self.tmpConnEdit, self.tmpStateEdit,
            self.tmpFreqEdit, self.tmpCurrentEdit,
            self.tmpTempEdit, self.tmpDetailEdit,
            self.tmpAlarmEdit,
            self.dacActual1Edit, self.dacActual2Edit,
        ):
            le.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            le.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)


    def _style_indicator(self, w: QWidget, on: bool):
        bg = "#38d62f" if on else "#d82c2c"
        # 61x61 기준 radius 30
        w.setStyleSheet(f"background: {bg}; border-radius: 30px; border: 2px solid #333333;")

    def _on_stacked_index_changed(self, index: int):
        is_hmi = (index == 0)

        # HMI footer는 HMI 페이지에서만 보이게
        self.hmiFooter.setVisible(is_hmi)

        # 창 높이도 페이지에 따라 변경
        if is_hmi:
            self._form.resize(*self._hmi_window_size)
        else:
            self._form.resize(*self._normal_window_size)

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
