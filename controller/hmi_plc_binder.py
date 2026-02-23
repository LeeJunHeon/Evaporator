# -*- coding: utf-8 -*-
"""
controller/hmi_plc_binder.py

HMI(UI) ↔ PLC 연결 바인더 (깔끔 버전)

원칙
- PLC I/O는 services/plc_service.PLCService 가 "단일 워커/단일 연결"로 독점 처리
- 이 바인더는 UI 갱신/버튼 이벤트만 담당
- UI 스레드 블로킹 금지: 모든 write는 PLCService.enqueue_* 로 비동기 큐잉만 수행
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QObject, QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import QMessageBox

from config.plc_config import PLCSettings
from services.plc_service import PLCService


# ------------------------------------------------------------
# UI 위젯 <-> PLC 코일 매핑
# ------------------------------------------------------------
@dataclass(frozen=True)
class ButtonBinding:
    widget_name: str
    coil_name: str
    momentary: bool = False  # 기본: 래치(유지). 펄스가 필요하면 True


class HmiPlcBinder(QObject):
    """UI(HMI) ↔ PLCService 연결을 담당."""

    # ✅ 여기 코일명이 plc.py의 PLC_COIL_MAP key와 1:1로 맞아야 함
    BUTTONS: Tuple[ButtonBinding, ...] = (
        ButtonBinding("rpBtn", "R_P_SW"),
        ButtonBinding("rvBtn", "R_V_SW"),
        ButtonBinding("fvBtn", "F_V_SW"),
        ButtonBinding("mvBtn", "M_V_SW"),
        ButtonBinding("vvBtn", "V_V_SW"),
        ButtonBinding("pushButton_13", "TMP_SW"),
        ButtonBinding("doorBtn", "DOOR_SW"),
        ButtonBinding("ftmBtn", "FTM_SW"),
        ButtonBinding("mainshutterBtn", "MAIN_SHUTTER_SW"),
        ButtonBinding("ms1shutterBtn", "SHUTTER_1_SW"),
        ButtonBinding("ms2shutterBtn", "SHUTTER_2_SW"),
        ButtonBinding("ms1powerBtn", "POWER_1_SW"),
        ButtonBinding("ms2powerBtn", "POWER_2_SW"),
    )

    # ✅ AIR/WATER/G1/G2
    INDICATORS: Dict[str, str] = {
        "g1": "GAUGE_1_SW",
        "g2": "GAUGE_2_SW",
        "air": "AIR_SW",
        "water": "WATER_SW",
    }

    def __init__(self, ui: object, settings: PLCSettings, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.ui = ui
        self.settings = settings

        # thread-safe state (다른 스레드에서도 읽을 수 있음)
        self._state_lock = threading.Lock()
        self._last_states: Dict[str, bool] = {}
        self._connected: bool = False

        # Door 이동 중 인터락(시간 기반)
        self._door_busy_until: float = 0.0
        self._door_busy_timer = QTimer(self)
        self._door_busy_timer.setSingleShot(True)
        self._door_busy_timer.timeout.connect(self._end_door_busy)

        # ✅ 단일 PLC 게이트웨이
        self._plc = PLCService(settings=settings, parent=self)
        self._plc.sig_connected.connect(self._on_connected)
        self._plc.sig_error.connect(self._on_error)
        self._plc.sig_coils.connect(self._apply_states)  # PLCService는 coils dict를 emit

        self._wire_ui()

        # DAC 수동 입력 UI (있으면 자동으로 연결)
        self._wire_dac_controls()

    # ============================================================
    # Public API (Process/Engine에서 사용)
    # ============================================================
    def get_plc_service(self) -> PLCService:
        """Engine/Process에서 PLCService 객체가 필요할 때 사용(내부 _plc 직접 접근 금지용)."""
        return self._plc

    def submit_write(self, coil_name: str, on: bool, *, momentary: bool = False, pulse_ms: int | None = None, tag: str = ""):
        """공정(QThread)에서 완료를 기다릴 때 사용: Future 반환"""
        if not self.is_connected():
            raise RuntimeError("PLC not connected")
        return self._plc.submit_write_coil(
            coil_name=str(coil_name),
            on=bool(on),
            momentary=bool(momentary),
            pulse_ms=pulse_ms,
            tag=str(tag),
        )

    def submit_write_reg(self, reg_name: str, value: int, *, tag: str = ""):
        """공정(QThread)에서 완료를 기다릴 때 사용: Future 반환"""
        if not self.is_connected():
            raise RuntimeError("PLC not connected")
        return self._plc.submit_write_reg(
            reg_name=str(reg_name),
            value=int(value),
            tag=str(tag),
        )

    # ============================================================
    # Public API
    # ============================================================
    def start(self) -> None:
        self._plc.start()

    def stop(self) -> None:
        # PLCService 내부 워커 종료
        self._plc.stop()

    def reload_settings(self, new_settings: PLCSettings) -> None:
        # ✅ 기존 서비스 정리
        old = getattr(self, "_plc", None)
        try:
            if old is not None:
                old.stop()
        except Exception:
            pass

        self.settings = new_settings

        # ✅ 새 서비스 생성/연결
        self._plc = PLCService(settings=new_settings, parent=self)
        self._plc.sig_connected.connect(self._on_connected)
        self._plc.sig_error.connect(self._on_error)
        self._plc.sig_coils.connect(self._apply_states)

        # ✅ old 객체는 Qt 이벤트루프에서 제거 예약
        try:
            if old is not None:
                old.deleteLater()
        except Exception:
            pass

        self.start()

    def is_connected(self) -> bool:
        with self._state_lock:
            return bool(self._connected)

    def get_states(self) -> Dict[str, bool]:
        with self._state_lock:
            return dict(self._last_states)

    def read_coil(self, coil_name: str, default: bool = False) -> bool:
        with self._state_lock:
            return bool(self._last_states.get(str(coil_name), bool(default)))

    # 공정/외부 코드에서 사용 가능(팝업 없음)
    def enqueue_write(self, coil_name: str, on: bool, momentary: bool = False) -> None:
        if not self.is_connected():
            raise RuntimeError("PLC not connected")
        self._plc.enqueue_write_coil(str(coil_name), bool(on), momentary=bool(momentary), pulse_ms=None)

    def pulse_coil(self, coil_name: str) -> None:
        self.enqueue_write(str(coil_name), True, momentary=True)

    def enqueue_write_reg(self, reg_name: str, value: int) -> None:
        if not self.is_connected():
            raise RuntimeError("PLC not connected")
        self._plc.enqueue_write_reg(str(reg_name), int(value))

    # ============================================================
    # UI wiring
    # ============================================================
    def _wire_ui(self) -> None:
        for b in self.BUTTONS:
            w = getattr(self.ui, b.widget_name, None)
            if w is None:
                raise AttributeError(f"UI 위젯 누락: {b.widget_name} (coil={b.coil_name})")

            try:
                w.setCheckable(True)
            except Exception:
                pass

            w.toggled.connect(lambda on, bb=b: self._on_button_toggled(bb, on))

        all_stop = getattr(self.ui, "allstopBtn", None)
        if all_stop is not None:
            all_stop.clicked.connect(self._on_all_stop_clicked)

    def _get_state_locked(self, coil: str, default: bool = False) -> bool:
        with self._state_lock:
            return bool(self._last_states.get(coil, default))

    def _on_button_toggled(self, binding: ButtonBinding, on: bool) -> None:
        # 미연결이면 팝업 + 버튼 원복
        if not self.is_connected():
            self._popup_warn("PLC 미연결", "PLC가 연결되지 않아 명령을 전송할 수 없습니다.")
            self._revert_button_to_plc(binding, fallback=not bool(on))
            return

        # Door 인터락(기존 유지)
        if binding.coil_name == "DOOR_SW":
            if self._is_door_busy():
                self._popup_warn("인터락", "Door가 열리거나 닫히는 중입니다.\n완료 후 다시 시도하세요.")
                self._revert_button_to_plc(binding, fallback=not bool(on))
                return

            # PLC 상태가 아직 없으면 차단(기존 유지)
            if not self.get_states():
                self._popup_warn("인터락", "PLC 상태를 아직 읽지 못했습니다.\n잠시 후 다시 시도하세요.")
                self._revert_button_to_plc(binding, fallback=not bool(on))
                return

            # Main shutter가 닫혀있으면 door 금지(기존 유지)
            if not self._get_state_locked("MAIN_SHUTTER_SW", False):
                self._popup_warn("인터락", "Main Shutter가 닫혀 있습니다.\nMain Shutter를 먼저 열어주세요.")
                self._revert_button_to_plc(binding, fallback=not bool(on))
                return

            # ✅ write는 PLCService로
            self._plc.enqueue_write_coil("DOOR_SW", bool(on), momentary=False, pulse_ms=None)
            self._begin_door_busy()
            self._set_hmi_status(f"DOOR_SW <- {int(bool(on))} (moving)")
            return

        # Main shutter 인터락(door 이동중 닫기 금지) - 기존 유지
        if binding.coil_name == "MAIN_SHUTTER_SW":
            if self._is_door_busy() and (not bool(on)):
                self._popup_warn("인터락", "Door가 열리거나 닫히는 중에는\nMain Shutter를 닫을 수 없습니다.")
                self._revert_button_to_plc(binding, fallback=True)
                return

        # 일반 토글은 바로 write
        self._plc.enqueue_write_coil(binding.coil_name, bool(on), momentary=binding.momentary, pulse_ms=None)
        self._set_hmi_status(f"{binding.coil_name} <- {int(bool(on))}")

    def _on_all_stop_clicked(self) -> None:
        if not self.is_connected():
            self._popup_warn("PLC 미연결", "PLC가 연결되지 않아 ALL STOP을 전송할 수 없습니다.")
            return

        # ✅ 안전 순서(권장): MAIN_SHUTTER close → DAC=0 → POWER off → 나머지 off
        self._plc.enqueue_write_coil("MAIN_SHUTTER_SW", False, tag="ALL_STOP")

        self._plc.enqueue_write_reg("DAC_POWER_1", 0, tag="ALL_STOP")
        self._plc.enqueue_write_reg("DAC_POWER_2", 0, tag="ALL_STOP")

        self._plc.enqueue_write_coil("POWER_1_SW", False, tag="ALL_STOP")
        self._plc.enqueue_write_coil("POWER_2_SW", False, tag="ALL_STOP")

        for b in self.BUTTONS:
            if b.coil_name in ("MAIN_SHUTTER_SW", "POWER_1_SW", "POWER_2_SW"):
                continue
            self._plc.enqueue_write_coil(b.coil_name, False, tag="ALL_STOP")

        self._set_hmi_status("ALL STOP sent (MAIN_SHUTTER close + DAC=0 + POWER OFF + others OFF)")
        self._set_hmi_log("ALL STOP (safe order)")

    # ============================================================
    # PLCService signals → UI apply
    # ============================================================
    def _apply_states(self, states_obj: object) -> None:
        # PLCService.sig_coils -> Dict[str,bool]
        states: Dict[str, bool] = dict(states_obj or {})

        with self._state_lock:
            self._last_states = states

        # indicators
        try:
            if hasattr(self.ui, "set_indicator_state"):
                for name, coil in self.INDICATORS.items():
                    self.ui.set_indicator_state(name, bool(states.get(coil, False)))
        except Exception:
            pass

        # buttons (PLC 상태로 UI를 "정답"으로 동기화)
        for b in self.BUTTONS:
            w = getattr(self.ui, b.widget_name, None)
            if w is None:
                continue
            target = bool(states.get(b.coil_name, False))
            try:
                with QSignalBlocker(w):
                    w.setChecked(target)
            except Exception:
                pass

    def _on_connected(self, ok: bool) -> None:
        with self._state_lock:
            prev = self._connected
            self._connected = bool(ok)

        status = "PLC CONNECTED" if self.is_connected() else "PLC DISCONNECTED"
        self._set_hmi_status(status)

        if prev != self.is_connected():
            self._set_hmi_log(status)

    def _on_error(self, msg: str) -> None:
        self._set_hmi_log(msg)

    # ============================================================
    # UI helpers
    # ============================================================
    def _popup_warn(self, title: str, message: str) -> None:
        parent = None
        try:
            btn = getattr(self.ui, "processBtn", None)
            parent = btn.window() if btn is not None else None
        except Exception:
            parent = None

        box = QMessageBox(QMessageBox.Warning, title, message, QMessageBox.Ok, parent)
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        box.exec()

    def _revert_button_to_plc(self, binding: ButtonBinding, fallback: Optional[bool] = None) -> None:
        w = getattr(self.ui, binding.widget_name, None)
        if w is None:
            return

        with self._state_lock:
            if binding.coil_name in self._last_states:
                target = bool(self._last_states.get(binding.coil_name, False))
            else:
                target = bool(fallback) if fallback is not None else False

        try:
            with QSignalBlocker(w):
                w.setChecked(target)
        except Exception:
            pass

    # ============================================================
    # Door busy
    # ============================================================
    def _is_door_busy(self) -> bool:
        return time.monotonic() < float(self._door_busy_until or 0.0)

    def _begin_door_busy(self) -> None:
        move_s = float(getattr(self.settings, "door_move_time_s", 10.0) or 10.0)
        move_s = max(0.1, move_s)
        self._door_busy_until = time.monotonic() + move_s
        try:
            self._door_busy_timer.stop()
            self._door_busy_timer.start(int(move_s * 1000))
        except Exception:
            pass

    def _end_door_busy(self) -> None:
        self._door_busy_until = 0.0
        self._set_hmi_status("DOOR: move done")

    # ============================================================
    # HMI log/status
    # ============================================================
    def _set_hmi_status(self, text: str) -> None:
        w = getattr(self.ui, "processMonitor_HMI", None)
        if w is not None:
            try:
                w.setText(text)
            except Exception:
                pass

    def _set_hmi_log(self, text: str) -> None:
        w = getattr(self.ui, "hmiLogWindow", None)
        if w is None:
            return

        line = f"[{time.strftime('%H:%M:%S')}] {text}"

        try:
            stick_to_bottom = True
            try:
                sb = w.verticalScrollBar()
                stick_to_bottom = (sb.value() >= (sb.maximum() - 2))
            except Exception:
                stick_to_bottom = True

            if hasattr(w, "appendPlainText"):
                w.appendPlainText(line)
            elif hasattr(w, "append"):
                w.append(line)
            else:
                prev = w.text() if hasattr(w, "text") else ""
                joined = (prev + "\n" + line).strip()
                if hasattr(w, "setText"):
                    w.setText(joined)

            if stick_to_bottom:
                try:
                    sb = w.verticalScrollBar()
                    sb.setValue(sb.maximum())
                except Exception:
                    pass
        except Exception:
            pass

    # ============================================================
    # DAC 수동 입력 (PLCService로 write_reg만)
    # ============================================================
    def _wire_dac_controls(self) -> None:
        fs = int(getattr(self.settings, "dac_full_scale_code", 4000))
        off = int(getattr(self.settings, "dac_offset_code", 0))
        lo, hi = off, off + fs

        self._dac1_spin = getattr(self.ui, "dac1Spin", None)
        self._dac1_set_btn = getattr(self.ui, "dac1SetBtn", None)
        self._dac1_reset_btn = getattr(self.ui, "dac1ResetBtn", None)

        self._dac2_spin = getattr(self.ui, "dac2Spin", None)
        self._dac2_set_btn = getattr(self.ui, "dac2SetBtn", None)
        self._dac2_reset_btn = getattr(self.ui, "dac2ResetBtn", None)

        for sp in (self._dac1_spin, self._dac2_spin):
            if sp is not None and hasattr(sp, "setRange"):
                try:
                    sp.setRange(int(lo), int(hi))
                    sp.setSingleStep(1)
                except Exception:
                    pass

        if self._dac1_set_btn is not None:
            self._dac1_set_btn.clicked.connect(lambda: self._on_apply_dac_code(1))
        if self._dac1_reset_btn is not None:
            self._dac1_reset_btn.clicked.connect(lambda: self._on_reset_dac(1))

        if self._dac2_set_btn is not None:
            self._dac2_set_btn.clicked.connect(lambda: self._on_apply_dac_code(2))
        if self._dac2_reset_btn is not None:
            self._dac2_reset_btn.clicked.connect(lambda: self._on_reset_dac(2))

    def _on_reset_dac(self, ch: int) -> None:
        sp = self._dac1_spin if int(ch) == 1 else self._dac2_spin
        if sp is None:
            self._popup_warn("UI 오류", "DAC 입력 위젯(QSpinBox)을 찾지 못했습니다.")
            return

        try:
            sp.setValue(0)
        except Exception:
            pass

        self._apply_dac_code(ch, 0)

    def _on_apply_dac_code(self, ch: int) -> None:
        sp = self._dac1_spin if int(ch) == 1 else self._dac2_spin
        if sp is None:
            self._popup_warn("UI 오류", "DAC 입력 위젯(QSpinBox)을 찾지 못했습니다.")
            return

        try:
            v = int(sp.value())
        except Exception:
            self._popup_warn("입력 오류", "DAC 값 읽기 실패")
            return

        self._apply_dac_code(ch, v)

    def _apply_dac_code(self, ch: int, v: int) -> None:
        if not self.is_connected():
            self._popup_warn("PLC 미연결", "PLC가 연결되지 않아 DAC 값을 전송할 수 없습니다.")
            return

        fs = int(getattr(self.settings, "dac_full_scale_code", 4000))
        off = int(getattr(self.settings, "dac_offset_code", 0))
        lo, hi = off, off + fs

        v0 = int(v)
        v_clamped = max(lo, min(v0, hi))

        sp = self._dac1_spin if int(ch) == 1 else self._dac2_spin
        try:
            if sp is not None and v_clamped != int(sp.value()):
                sp.setValue(int(v_clamped))
        except Exception:
            pass

        if v_clamped != v0:
            self._set_hmi_log(f"[WARN] DAC{ch}: 입력 {v0} -> clamp {v_clamped} (range {lo}..{hi})")

        key = "DAC_POWER_1" if int(ch) == 1 else "DAC_POWER_2"
        try:
            self._plc.enqueue_write_reg(key, int(v_clamped), tag=f"DAC{ch}")
            self._set_hmi_status(f"{key} <- {int(v_clamped)}")
            self._set_hmi_log(f"DAC{ch} set: {int(v_clamped)}")
        except Exception as e:
            self._popup_warn("전송 실패", f"DAC 값 전송 실패: {e!r}")
