# -*- coding: utf-8 -*-
# ui/config_dialog.py
"""
ConfigDialog: config/devices.ini 를 GUI로 편집하는 팝업

- 3개 장비(PLC / STM-100 / ACS-2000) 시리얼(RS-232) 연결 파라미터를 수정/저장
- 저장은 devices.ini 원본의 주석/정렬을 최대한 유지하도록 '라인 기반 업데이트' 방식 사용
- 저장 후 콜백(on_saved)이 있으면 호출(예: PLC 바인더 재로딩)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QTabWidget, QWidget, QFormLayout,
    QLineEdit, QComboBox, QCheckBox, QSpinBox, QDoubleSpinBox,
    QHBoxLayout, QPushButton, QMessageBox
)

from configparser import ConfigParser


# -----------------------------
# ini 편집(주석 보존) 유틸
# -----------------------------
class IniLineEditor:
    """
    configparser.write()를 쓰면 주석이 날아가므로,
    원본 텍스트를 '섹션 범위' 안에서 key=value 라인만 교체/삽입한다.
    """
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.exists():
            self.lines = self.path.read_text(encoding="utf-8", errors="ignore").splitlines(True)
        else:
            self.lines = []

    def _find_section_range(self, section: str) -> tuple[int, int] | None:
        sec_re = re.compile(rf"^\s*\[\s*{re.escape(section)}\s*\]\s*$", re.I)
        header_idx = None
        for i, line in enumerate(self.lines):
            if sec_re.match(line):
                header_idx = i
                break
        if header_idx is None:
            return None
        for j in range(header_idx + 1, len(self.lines)):
            if re.match(r"^\s*\[.*\]\s*$", self.lines[j]):
                return header_idx, j
        return header_idx, len(self.lines)

    def ensure_section(self, section: str) -> None:
        if self._find_section_range(section) is not None:
            return
        if self.lines and not self.lines[-1].endswith("\n"):
            self.lines[-1] += "\n"
        if self.lines and self.lines[-1].strip() != "":
            self.lines.append("\n")
        self.lines.append(f"[{section}]\n")

    def set(self, section: str, key: str, value: str) -> None:
        section = section.strip()
        key = key.strip()
        self.ensure_section(section)

        rng = self._find_section_range(section)
        assert rng is not None
        s, e = rng

        key_re = re.compile(rf"^(\s*){re.escape(key)}(\s*)=(.*)$", re.I)
        for i in range(s + 1, e):
            m = key_re.match(self.lines[i])
            if not m:
                continue
            indent, sp, _old = m.group(1), m.group(2), m.group(3)
            self.lines[i] = f"{indent}{key}{sp}= {value}\n"
            return

        insert_at = e
        while insert_at > s + 1 and self.lines[insert_at - 1].strip() == "":
            insert_at -= 1
        self.lines.insert(insert_at, f"{key} = {value}\n")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join(self.lines), encoding="utf-8")


# -----------------------------
# Dialog
# -----------------------------
@dataclass
class _SerialForm:
    port: QLineEdit
    baudrate: QSpinBox
    bytesize: QSpinBox
    parity: QComboBox
    stopbits: QSpinBox
    timeout_s: QDoubleSpinBox
    write_timeout_s: QDoubleSpinBox
    rtscts: QCheckBox
    dsrdtr: QCheckBox


class ConfigDialog(QDialog):
    def __init__(self, ini_path: str | Path, parent=None, on_saved: Optional[Callable[[], None]] = None):
        super().__init__(parent)
        self.setWindowTitle("Config")
        self.setModal(True)

        self.ini_path = Path(ini_path)
        self.on_saved = on_saved

        self._cfg = ConfigParser()
        if self.ini_path.exists():
            self._cfg.read(self.ini_path, encoding="utf-8")

        self._build_ui()
        self._load_into_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        # 1) PLC
        self.tab_plc = QWidget()
        self.tabs.addTab(self.tab_plc, "PLC (RS-232)")

        plc_form = QFormLayout(self.tab_plc)

        self.plc_port = QLineEdit()
        self.plc_baud = self._spin(300, 115200)
        self.plc_bytesize = self._spin(5, 8)
        self.plc_parity = self._parity_combo()
        self.plc_stopbits = self._spin(1, 2)
        self.plc_unit = self._spin(1, 247)

        self.plc_timeout = self._dspin(0.05, 30.0, decimals=2)
        self.plc_poll = self._dspin(0.05, 5.0, decimals=2)
        self.plc_reconn = self._dspin(0.1, 30.0, decimals=1)
        self.plc_pulse = self._spin(10, 2000)

        self.plc_method = QLineEdit("rtu")
        self.plc_method.setEnabled(False)

        plc_form.addRow("Port", self.plc_port)
        plc_form.addRow("Baudrate", self.plc_baud)
        plc_form.addRow("Bytesize", self.plc_bytesize)
        plc_form.addRow("Parity", self.plc_parity)
        plc_form.addRow("Stopbits", self.plc_stopbits)
        plc_form.addRow("Unit", self.plc_unit)
        plc_form.addRow("Method", self.plc_method)
        plc_form.addRow("Timeout (s)", self.plc_timeout)
        plc_form.addRow("Poll interval (s)", self.plc_poll)
        plc_form.addRow("Reconnect interval (s)", self.plc_reconn)
        plc_form.addRow("Pulse width (ms)", self.plc_pulse)

        # 2) STM100
        self.tab_stm = QWidget()
        self.tabs.addTab(self.tab_stm, "STM-100")
        self.stm = self._build_serial_tab(self.tab_stm, with_eom=False)

        # 3) ACS2000
        self.tab_acs = QWidget()
        self.tabs.addTab(self.tab_acs, "ACS-2000")
        self.acs = self._build_serial_tab(self.tab_acs, with_eom=True)

        btn_row = QHBoxLayout()
        root.addLayout(btn_row)
        btn_row.addStretch(1)

        self.btn_save = QPushButton("Save")
        self.btn_cancel = QPushButton("Cancel")
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_cancel)

        self.btn_save.clicked.connect(self._save)
        self.btn_cancel.clicked.connect(self.reject)

    def _build_serial_tab(self, tab: QWidget, *, with_eom: bool) -> _SerialForm:
        form = QFormLayout(tab)

        port = QLineEdit()
        baud = self._spin(300, 115200)
        bytesize = self._spin(5, 8)
        parity = self._parity_combo()
        stopbits = self._spin(1, 2)
        timeout_s = self._dspin(0.05, 30.0, decimals=2)
        write_timeout_s = self._dspin(0.05, 30.0, decimals=2)
        rtscts = QCheckBox()
        dsrdtr = QCheckBox()

        form.addRow("Port", port)
        form.addRow("Baudrate", baud)
        form.addRow("Bytesize", bytesize)
        form.addRow("Parity", parity)
        form.addRow("Stopbits", stopbits)
        form.addRow("Timeout (s)", timeout_s)
        form.addRow("Write timeout (s)", write_timeout_s)
        form.addRow("RTS/CTS", rtscts)
        form.addRow("DSR/DTR", dsrdtr)

        if with_eom:
            self.acs_eom = QComboBox()
            self.acs_eom.addItems(["CR", "CRLF"])
            form.addRow("EOM", self.acs_eom)

        return _SerialForm(
            port=port,
            baudrate=baud,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout_s=timeout_s,
            write_timeout_s=write_timeout_s,
            rtscts=rtscts,
            dsrdtr=dsrdtr,
        )

    def _spin(self, mn: int, mx: int) -> QSpinBox:
        w = QSpinBox()
        w.setRange(mn, mx)
        w.setAlignment(Qt.AlignRight)
        return w

    def _dspin(self, mn: float, mx: float, *, decimals: int) -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setRange(mn, mx)
        w.setDecimals(decimals)
        w.setSingleStep(0.1)
        w.setAlignment(Qt.AlignRight)
        return w

    def _parity_combo(self) -> QComboBox:
        w = QComboBox()
        w.addItems(["N", "E", "O", "M", "S"])
        return w

    def _load_into_ui(self) -> None:
        plc = self._cfg["plc"] if self._cfg.has_section("plc") else {}

        self.plc_port.setText(plc.get("port", "COM5"))
        self.plc_method.setText(plc.get("method", "rtu"))
        self.plc_baud.setValue(int(plc.get("baudrate", 9600)))
        self.plc_bytesize.setValue(int(plc.get("bytesize", 8)))
        self._set_combo(self.plc_parity, plc.get("parity", "N"))
        self.plc_stopbits.setValue(int(plc.get("stopbits", 1)))
        self.plc_unit.setValue(int(plc.get("unit", 1)))
        self.plc_timeout.setValue(float(plc.get("timeout_s", 2.0)))
        self.plc_poll.setValue(float(plc.get("poll_interval_s", 0.25)))
        self.plc_reconn.setValue(float(plc.get("reconnect_interval_s", 1.0)))
        self.plc_pulse.setValue(int(plc.get("pulse_ms", 180)))

        self._load_serial_section("stm100", self.stm, defaults=dict(timeout_s=0.3, write_timeout_s=0.5))
        self._load_serial_section("acs2000", self.acs, defaults=dict(timeout_s=0.5, write_timeout_s=0.5))

        acs = self._cfg["acs2000"] if self._cfg.has_section("acs2000") else {}
        if hasattr(self, "acs_eom"):
            self._set_combo(self.acs_eom, acs.get("eom", "CR"))

    def _load_serial_section(self, section: str, form: _SerialForm, *, defaults: dict) -> None:
        sec = self._cfg[section] if self._cfg.has_section(section) else {}

        form.port.setText(sec.get("port", "COM3"))
        form.baudrate.setValue(int(sec.get("baudrate", 9600)))
        form.bytesize.setValue(int(sec.get("bytesize", 8)))
        self._set_combo(form.parity, sec.get("parity", "N"))
        form.stopbits.setValue(int(sec.get("stopbits", 1)))

        form.timeout_s.setValue(float(sec.get("timeout_s", defaults.get("timeout_s", 0.5))))
        form.write_timeout_s.setValue(float(sec.get("write_timeout_s", defaults.get("write_timeout_s", 0.5))))

        form.rtscts.setChecked(sec.get("rtscts", "false").strip().lower() in ("1", "true", "yes", "on"))
        form.dsrdtr.setChecked(sec.get("dsrdtr", "false").strip().lower() in ("1", "true", "yes", "on"))

    def _set_combo(self, combo: QComboBox, value: str) -> None:
        v = (value or "").strip().upper()
        for i in range(combo.count()):
            if combo.itemText(i).upper() == v:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def _save(self) -> None:
        try:
            ed = IniLineEditor(self.ini_path)

            ed.set("plc", "port", self.plc_port.text().strip() or "COM5")
            ed.set("plc", "method", "rtu")
            ed.set("plc", "baudrate", str(self.plc_baud.value()))
            ed.set("plc", "bytesize", str(self.plc_bytesize.value()))
            ed.set("plc", "parity", self.plc_parity.currentText())
            ed.set("plc", "stopbits", str(self.plc_stopbits.value()))
            ed.set("plc", "unit", str(self.plc_unit.value()))
            ed.set("plc", "timeout_s", f"{self.plc_timeout.value():.2f}".rstrip("0").rstrip("."))
            ed.set("plc", "poll_interval_s", f"{self.plc_poll.value():.2f}".rstrip("0").rstrip("."))
            ed.set("plc", "reconnect_interval_s", f"{self.plc_reconn.value():.1f}".rstrip("0").rstrip("."))
            ed.set("plc", "pulse_ms", str(self.plc_pulse.value()))

            self._save_serial(ed, "stm100", self.stm)

            self._save_serial(ed, "acs2000", self.acs)
            if hasattr(self, "acs_eom"):
                ed.set("acs2000", "eom", self.acs_eom.currentText())

            ed.save()

            QMessageBox.information(self, "Saved", "devices.ini 저장 완료.\n(PLC는 저장 즉시 재적용 가능)")
            if self.on_saved:
                try:
                    self.on_saved()
                except Exception:
                    pass

            self.accept()

        except Exception as e:
            QMessageBox.critical(self, "Save failed", f"저장 실패:\n{e}")

    def _save_serial(self, ed: IniLineEditor, section: str, form: _SerialForm) -> None:
        ed.set(section, "port", form.port.text().strip())
        ed.set(section, "baudrate", str(form.baudrate.value()))
        ed.set(section, "bytesize", str(form.bytesize.value()))
        ed.set(section, "parity", form.parity.currentText())
        ed.set(section, "stopbits", str(form.stopbits.value()))
        ed.set(section, "timeout_s", f"{form.timeout_s.value():.2f}".rstrip("0").rstrip("."))
        ed.set(section, "write_timeout_s", f"{form.write_timeout_s.value():.2f}".rstrip("0").rstrip("."))
        ed.set(section, "rtscts", "true" if form.rtscts.isChecked() else "false")
        ed.set(section, "dsrdtr", "true" if form.dsrdtr.isChecked() else "false")
