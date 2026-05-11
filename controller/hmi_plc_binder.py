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

import time
import threading
import contextlib
import urllib.request
import json as _json
from dataclasses import dataclass, asdict, is_dataclass
from typing import Dict, Optional, Tuple, Any
from services.log_service import LogService

try:
    from secrets import GCHAT_WEBHOOK_URL
except Exception:
    GCHAT_WEBHOOK_URL = ""

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

    def __init__(self, ui: object, settings: PLCSettings, parent: Optional[QObject] = None, log: Optional[LogService] = None):
        super().__init__(parent)
        self.ui = ui
        self.settings = settings
        self._log = log  # ✅ 공정 CSV(LogService) 주입
        self._process_controller: Optional[object] = None

        # thread-safe state (다른 스레드에서도 읽을 수 있음)
        self._state_lock = threading.Lock()
        self._last_states: Dict[str, bool] = {}
        self._connected: bool = False

        # ✅ UI 표시용: 최근 I/O 성공 여부(한 번이라도 실패하면 False)
        self._io_healthy: bool = False

        # ✅ 상단 상태라인에는 "연결 상태만" 표시하기 위한 외부 장비 연결 상태 저장소
        self._external_connected: Dict[str, bool] = {}

        # ✅ ACS 서비스 연결/상태
        self._acs_service: Optional[object] = None
        self._acs_connected: bool = False
        self._acs_last_snapshot: Dict[str, Any] = {}
        self._acs_last_pressure: Optional[float] = None

        # ✅ TMP(TURBOVAC) 서비스 연결/상태
        self._turbovac_service: Optional[object] = None
        self._tmp_connected: bool = False
        self._tmp_last_snapshot: Dict[str, Any] = {}
        # TMP service thread를 이미 시작했는지
        self._tmp_service_started: bool = False

        # TMP 최초 attach 사용자 확인 상태
        self._tmp_attach_prompt_open: bool = False
        self._tmp_attach_prompt_done: bool = False
        self._tmp_attach_prompt_suppressed: bool = False

        # TMP 온도 알림 상태
        self._tmp_temp_alert_last_ts: float = 0.0
        self._tmp_temp_alert_cooldown_s: float = 300.0  # 5분에 한 번만 재알림
        self._tmp_emergency_stop_triggered: bool = False  # 79°C 긴급 정지 중복 방지

        # ✅ 통신 끊김/복구 알림 상태 (변화 시점에만 구글챗 발송)
        # None = baseline 미설정 → 첫 호출은 알림 없이 baseline만 기록
        self._last_tmp_alert_state: Optional[bool] = None
        self._last_plc_alert_state: Optional[bool] = None

        # (선택) 초기 표시
        self._render_status_line()

        # Door 이동 중 인터락(시간 기반)
        self._door_busy_until: float = 0.0
        self._vacuum_sequence: Optional[object] = None
        self._door_busy_timer = QTimer(self)
        self._door_busy_timer.setSingleShot(True)
        self._door_busy_timer.timeout.connect(self._end_door_busy)

        # ✅ 단일 PLC 게이트웨이
        self._plc = PLCService(settings=settings, parent=self)
        self._plc.sig_connected.connect(self._on_connected)
        self._plc.sig_error.connect(self._on_error)
        self._plc.sig_coils.connect(self._apply_states)  # PLCService는 coils dict를 emit
        if hasattr(self._plc, "sig_regs"):
            self._plc.sig_regs.connect(self._apply_regs)  # register(readback) 상태

        # ✅ PLC 명령 실행 trace → 공정 CSV로 저장
        if hasattr(self._plc, "sig_cmd_trace"):
            self._plc.sig_cmd_trace.connect(self._on_plc_cmd_trace)

        self._wire_ui()

        # DAC 수동 입력 UI (있으면 자동으로 연결)
        self._wire_dac_controls()

        self._set_controls_enabled(self.is_ui_connected())

    def set_external_connected(self, name: str, ok: bool) -> None:
        """main.py 등 외부에서 ACS 같은 장비 연결상태를 상단 상태라인에 반영."""
        key = str(name).strip().upper()
        if not key:
            return
        with self._state_lock:
            self._external_connected[key] = bool(ok)
        self._render_status_line()

    def set_acs_service(self, svc: object | None) -> None:
        """
        main.py 또는 hmi_window 쪽에서 ACSService를 주입한다.
        ACS는 TMP처럼 start/stop attach 개념은 없고,
        연결 상태 + pressure/snapshot을 받아서 UI에 반영하는 역할만 맡는다.
        """
        self._disconnect_acs_service(getattr(self, "_acs_service", None))
        self._acs_service = svc
        self._acs_connected = False
        self._acs_last_snapshot = {}
        self._acs_last_pressure = None

        if svc is None:
            self.set_external_connected("ACS", False)
            self._render_acs_status()
            return

        if hasattr(svc, "sig_connected"):
            svc.sig_connected.connect(self._on_acs_connected)
        if hasattr(svc, "sig_snapshot"):
            svc.sig_snapshot.connect(self._on_acs_snapshot)
        if hasattr(svc, "sig_pressure"):
            svc.sig_pressure.connect(self._on_acs_pressure)
        if hasattr(svc, "sig_error"):
            svc.sig_error.connect(self._on_acs_error)

        # 현재 상태 즉시 반영
        try:
            v = getattr(svc, "is_connected", False)
            self._acs_connected = bool(v() if callable(v) else v)
        except Exception:
            self._acs_connected = False

        try:
            snap = svc.get_last_snapshot() if hasattr(svc, "get_last_snapshot") else None
            self._acs_last_snapshot = self._snapshot_to_dict(snap)
        except Exception:
            self._acs_last_snapshot = {}

        try:
            snap_pressure = self._acs_last_snapshot.get("pressure", None)
            self._acs_last_pressure = float(snap_pressure) if snap_pressure is not None else None
        except Exception:
            self._acs_last_pressure = None

        self.set_external_connected("ACS", self._acs_connected)
        self._render_acs_status()

    def set_turbovac_service(self, svc: object | None) -> None:
        """
        main.py 등에서 TurbovacService를 주입한다.
        이 메서드는 보통 1회만 호출하는 전제를 둔다.
        """
        self._turbovac_service = svc
        self._tmp_connected = False
        self._tmp_last_snapshot = {}
        self._tmp_service_started = False

        if svc is None:
            self.set_external_connected("TMP", False)
            self._render_tmp_status()
            self._set_controls_enabled(self.is_ui_connected())
            return

        if hasattr(svc, "sig_snapshot"):
            svc.sig_snapshot.connect(self._on_tmp_snapshot)
        if hasattr(svc, "sig_connected"):
            svc.sig_connected.connect(self._on_tmp_connected)
        if hasattr(svc, "sig_error"):
            svc.sig_error.connect(self._on_tmp_error)
        if hasattr(svc, "sig_log"):
            svc.sig_log.connect(self._on_tmp_log)

        # 현재 연결 상태/마지막 snapshot 즉시 반영
        try:
            v = getattr(svc, "is_connected", False)
            self._tmp_connected = bool(v() if callable(v) else v)
        except Exception:
            self._tmp_connected = False

        try:
            snap = svc.get_last_snapshot() if hasattr(svc, "get_last_snapshot") else None
            self._tmp_last_snapshot = dict(snap or {})
        except Exception:
            self._tmp_last_snapshot = {}

        self.set_external_connected("TMP", self._tmp_connected)
        self._render_tmp_status()
        self._set_controls_enabled(self.is_ui_connected())

        # PLC 상태를 이미 읽은 뒤 service가 주입된 경우,
        # 실제 TMP_SW가 ON이면 바로 attach하지 말고 사용자에게 먼저 묻는다.
        if self.is_ui_connected() and self.read_coil("TMP_SW", False):
            self._queue_tmp_attach_prompt(force=False)

    def set_process_controller(self, pc: object | None) -> None:
        self._process_controller = pc

    def _render_status_line(self) -> None:
        """상단 상태라인(processMonitor_HMI)에는 연결 상태만 표시."""
        parts = []
        parts.append("PLC CONNECTED" if self.is_ui_connected() else "PLC DISCONNECTED")

        with self._state_lock:
            ext = dict(self._external_connected)

        # 예: ACS CONNECTED / ACS DISCONNECTED
        for k in sorted(ext.keys()):
            parts.append(f"{k} {'CONNECTED' if ext[k] else 'DISCONNECTED'}")

        self._set_hmi_status(" | ".join(parts))

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

        with self._state_lock:
            self._connected = False
            self._io_healthy = False
            self._last_states = {}

        self._render_status_line()
        self._set_controls_enabled(False)

        # ACS/TMP는 외부 서비스이므로 객체는 유지하되, 상태표시는 즉시 최신화
        with contextlib.suppress(Exception):
            self._render_acs_status()
        with contextlib.suppress(Exception):
            self._render_tmp_status()

        self.settings = new_settings

        # ✅ (중요) DAC UI range도 settings에 맞춰 갱신
        #    - _wire_dac_controls() 재호출은 clicked 시그널 중복 연결 위험이 있으니
        #      "range만" 안전하게 업데이트
        try:
            fs = int(getattr(self.settings, "dac_full_scale_code", 4000))
            off = int(getattr(self.settings, "dac_offset_code", 0))
            lo, hi = off, off + fs

            for sp in (getattr(self, "_dac1_spin", None), getattr(self, "_dac2_spin", None)):
                if sp is not None and hasattr(sp, "setRange"):
                    sp.setRange(int(lo), int(hi))
        except Exception:
            pass

        # ✅ 새 서비스 생성/연결
        self._plc = PLCService(settings=new_settings, parent=self)
        self._plc.sig_connected.connect(self._on_connected)
        self._plc.sig_error.connect(self._on_error)
        self._plc.sig_coils.connect(self._apply_states)

        if hasattr(self._plc, "sig_regs"):
            self._plc.sig_regs.connect(self._apply_regs)

        if hasattr(self._plc, "sig_cmd_trace"):
            self._plc.sig_cmd_trace.connect(self._on_plc_cmd_trace)

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
        
    def is_ui_connected(self) -> bool:
        """UI 표시/버튼 enable 기준 연결 상태 (링크 연결 + 최근 I/O OK)."""
        # 링크만 연결되어도 I/O가 없으면 조작 차단 → coil 수신 후에야 True
        with self._state_lock:
            return bool(self._connected) and bool(self._io_healthy)

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

        tmp_start_btn = getattr(self.ui, "tmpStartBtn", None)
        if tmp_start_btn is None:
            raise AttributeError("UI 위젯 누락: tmpStartBtn")
        tmp_start_btn.clicked.connect(self._on_tmp_start_clicked)

        tmp_stop_btn = getattr(self.ui, "tmpStopBtn", None)
        if tmp_stop_btn is None:
            raise AttributeError("UI 위젯 누락: tmpStopBtn")
        tmp_stop_btn.clicked.connect(self._on_tmp_stop_clicked)

        all_stop = getattr(self.ui, "allstopBtn", None)
        if all_stop is not None:
            all_stop.clicked.connect(self._on_all_stop_clicked)

        vacuum_on_btn = getattr(self.ui, "vacuumOnBtn", None)
        if vacuum_on_btn is not None:
            vacuum_on_btn.setCheckable(True)
            vacuum_on_btn.toggled.connect(self._on_vacuum_on_toggled)

    def _get_state_locked(self, coil: str, default: bool = False) -> bool:
        with self._state_lock:
            return bool(self._last_states.get(coil, default))

    def _ensure_tmp_service_started(self, *, silent: bool = False) -> bool:
        svc = self._turbovac_service
        if svc is None:
            self._set_hmi_log("[BLOCK] TMP service not set")
            if not silent:
                self._popup_warn("TMP 미설정", "TMP 서비스가 연결되지 않았습니다.")
            return False

        if self._tmp_service_started:
            return True

        try:
            svc.start()
            self._tmp_service_started = True
            self._set_hmi_log("[TMP] service start requested")
            return True
        except Exception as e:
            self._set_hmi_log(f"[FAIL] TMP service start: {e!r}")
            if not silent:
                self._popup_warn("TMP 시작 실패", f"TMP service 시작 실패: {e!r}")
            return False

    def _set_tmp_field(self, widget_name: str, text: str) -> None:
        w = getattr(self.ui, widget_name, None)
        if w is None:
            return
        try:
            if hasattr(w, "setPlainText"):
                w.setPlainText(str(text))
            elif hasattr(w, "setText"):
                w.setText(str(text))
                if hasattr(w, "setCursorPosition"):
                    w.setCursorPosition(0)
        except Exception:
            pass

    def _set_acs_field(self, widget_name: str, text: str) -> None:
        w = getattr(self.ui, widget_name, None)
        if w is None:
            return
        try:
            if hasattr(w, "setPlainText"):
                w.setPlainText(str(text))
            elif hasattr(w, "setText"):
                w.setText(str(text))
                if hasattr(w, "setCursorPosition"):
                    w.setCursorPosition(0)
        except Exception:
            pass

    def _snapshot_to_dict(self, snap_obj: object) -> Dict[str, Any]:
        if snap_obj is None:
            return {}

        if isinstance(snap_obj, dict):
            return dict(snap_obj)

        try:
            if is_dataclass(snap_obj):
                return dict(asdict(snap_obj))
        except Exception:
            pass

        out: Dict[str, Any] = {}
        for name in (
            "ts", "connected", "pressure", "meta",
            "state_text", "freq_hz", "current_a",
            "motor_temp_c", "converter_temp_c", "bearing_temp_c", "dc_bus_v",
            "warning_bits", "last_error_code", "last_error_freq_hz", "last_error_hours",
            "alarm_text", "detail_text",
            "normal_operation", "accelerating",
            "decelerating", "pump_turning"
        ):
            try:
                if hasattr(snap_obj, name):
                    out[name] = getattr(snap_obj, name)
            except Exception:
                pass
        return out

    def _format_acs_pressure_text(self, value: Optional[float]) -> str:
        if value is None:
            return "---"
        try:
            v = float(value)
        except Exception:
            return "---"

        # 너무 긴 소수 문자열 방지
        if v == 0.0:
            return "0"
        if abs(v) >= 1e-3:
            return f"{v:.3e}"
        return f"{v:.3e}"
    
    def _format_tmp_detail_text(self, snap: Dict[str, Any]) -> str:
        detail_text = str(snap.get("detail_text", "") or "").strip()
        if detail_text not in ("", "-"):
            return detail_text

        parts: list[str] = []

        conv = snap.get("converter_temp_c", None)
        bear = snap.get("bearing_temp_c", None)
        dc = snap.get("dc_bus_v", None)

        try:
            if conv is not None:
                parts.append(f"CV {float(conv):.1f}C")
        except Exception:
            pass

        try:
            if bear is not None:
                parts.append(f"BR {float(bear):.1f}C")
        except Exception:
            pass

        try:
            if dc is not None:
                parts.append(f"DC {float(dc):.0f}V")
        except Exception:
            pass

        if parts:
            return " | ".join(parts)

        try:
            warn_bits = int(snap.get("warning_bits", 0) or 0)
        except Exception:
            warn_bits = 0

        if warn_bits:
            return f"WARN 0x{warn_bits:04X}"

        try:
            last_error_code = int(snap.get("last_error_code", 0) or 0)
        except Exception:
            last_error_code = 0

        if last_error_code:
            return f"LAST ERR {last_error_code}"

        return "---"
    
    def _render_acs_status(self) -> None:
        snap = dict(self._acs_last_snapshot or {})

        connected = bool(snap.get("connected", self._acs_connected))

        # ✅ 화면 표시는 live pressure 우선
        pressure = self._acs_last_pressure
        if pressure is None and "pressure" in snap:
            pressure = snap.get("pressure", None)

        self.set_external_connected("ACS", connected)

        if (not connected) or (pressure is None):
            pressure_text = "--- Torr"
        else:
            try:
                pressure_text = f"{float(pressure):.3e} Torr"
            except Exception:
                pressure_text = "--- Torr"

        self._set_acs_field("pressureValue", pressure_text)

    def _render_tmp_status(self) -> None:
        snap = dict(self._tmp_last_snapshot or {})

        connected = bool(snap.get("connected", self._tmp_connected))
        state_text = str(snap.get("state_text", "") or "").strip()
        freq_hz = snap.get("freq_hz", None)
        current_a = snap.get("current_a", None)
        motor_temp_c = snap.get("motor_temp_c", None)
        alarm_text = str(snap.get("alarm_text", "") or "").strip()
        detail_text = self._format_tmp_detail_text(snap)

        if not state_text:
            if not connected:
                state_text = "DISCONNECTED"
            elif snap.get("normal_operation", False):
                state_text = "NORMAL"
            elif snap.get("accelerating", False):
                state_text = "ACCEL"
            elif snap.get("decelerating", False):
                state_text = "DECEL"
            elif snap.get("pump_turning", False):
                state_text = "TURNING"
            else:
                state_text = "STOPPED"

        conn_text = "CONNECTED" if connected else "DISCONNECTED"
        freq_text = "---" if freq_hz is None else str(int(freq_hz))
        curr_text = "---" if current_a is None else f"{float(current_a):.2f}"
        temp_text = "---" if motor_temp_c is None else f"{float(motor_temp_c):.1f}"
        alarm_disp = "---" if alarm_text in ("", "-") else alarm_text

        self._set_tmp_field("tmpConnEdit", conn_text)
        self._set_tmp_field("tmpStateEdit", state_text)
        self._set_tmp_field("tmpFreqEdit", freq_text)
        self._set_tmp_field("tmpCurrentEdit", curr_text)
        self._set_tmp_field("tmpTempEdit", temp_text)
        self._set_tmp_field("tmpDetailEdit", detail_text)
        self._set_tmp_field("tmpAlarmEdit", alarm_disp)

        # ✅ Detail 툴팁: 마우스 오버 시 전체 내용 표시
        try:
            w = getattr(self.ui, "tmpDetailEdit", None)
            if w is not None:
                w.setToolTip(detail_text if detail_text not in ("", "---") else "")
        except Exception:
            pass

    def _call_tmp_helper(self, helper_name: str) -> tuple[bool, str]:
        svc = self._turbovac_service
        if svc is None:
            return False, "TMP 서비스가 설정되지 않았습니다."

        fn = getattr(svc, helper_name, None)
        if not callable(fn):
            return False, f"TMP helper 누락: {helper_name}"

        try:
            result = fn()
            if isinstance(result, tuple) and len(result) == 2:
                return bool(result[0]), str(result[1] or "")
            return bool(result), ""
        except Exception as e:
            return False, f"TMP 상태 확인 실패: {e!r}"
        
    def _set_tmp_connect_hint(self, running: bool) -> None:
        svc = self._turbovac_service
        if svc is None:
            return

        fn = getattr(svc, "set_connect_hint_running", None)
        if not callable(fn):
            return

        try:
            fn(bool(running))
        except Exception as e:
            self._set_hmi_log(f"[WARN] TMP connect hint set failed: {e!r}")

    def _reset_tmp_attach_prompt_state(self) -> None:
        self._tmp_attach_prompt_open = False
        self._tmp_attach_prompt_done = False
        self._tmp_attach_prompt_suppressed = False

    def _prompt_tmp_attach_choice(self) -> Optional[bool]:
        """
        반환:
        True  -> 현재 TMP가 이미 회전 중
        False -> 현재 TMP가 정지 상태
        None  -> 취소(attach 보류)
        """
        parent = None
        try:
            btn = getattr(self.ui, "tmpStartBtn", None)
            parent = btn.window() if btn is not None else None
        except Exception:
            parent = None

        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("TMP 연결 확인")
        box.setText("현재 터보펌프가 이미 회전 중입니까?")
        box.setInformativeText(
            "회전 중이면 [회전 중], 정지 상태면 [정지],\n"
            "지금 연결하지 않으려면 [취소]를 선택하세요."
        )
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        running_btn = box.addButton("회전 중", QMessageBox.YesRole)
        stopped_btn = box.addButton("정지", QMessageBox.NoRole)
        cancel_btn = box.addButton("취소", QMessageBox.RejectRole)

        box.exec()

        clicked = box.clickedButton()
        if clicked is running_btn:
            return True
        if clicked is stopped_btn:
            return False
        return None
    
    def _queue_tmp_attach_prompt(self, *, force: bool = False) -> None:
        """
        TMP auto attach 전에 사용자 선택을 받는다.

        force=False:
            PLC readback 기반 자동 attach 경로
        force=True:
            사용자가 TMP START를 눌렀을 때의 수동 경로
        """
        if not self.is_ui_connected():
            return
        if not self.read_coil("TMP_SW", False):
            return
        if self._tmp_service_started or self._tmp_connected:
            return
        if self._tmp_attach_prompt_open:
            return

        if not force:
            if self._tmp_attach_prompt_done or self._tmp_attach_prompt_suppressed:
                return

        self._tmp_attach_prompt_open = True

        def _run() -> None:
            try:
                # 다시 한 번 조건 확인
                if not self.is_ui_connected():
                    return
                if not self.read_coil("TMP_SW", False):
                    return
                if self._tmp_service_started or self._tmp_connected:
                    return

                choice = self._prompt_tmp_attach_choice()

                if choice is None:
                    self._tmp_attach_prompt_suppressed = True
                    self._set_hmi_log("[TMP] attach deferred by user")
                    return

                self._set_tmp_connect_hint(bool(choice))

                ok = self._ensure_tmp_service_started(silent=not force)
                if ok:
                    self._tmp_attach_prompt_done = True
                    self._tmp_attach_prompt_suppressed = False
                    self._set_hmi_log(
                        "[TMP] attach requested with hint="
                        + ("running" if choice else "idle")
                    )
            finally:
                self._tmp_attach_prompt_open = False

        # 현재 이벤트 루프 사이클 종료 후 실행: UI 스레드 블로킹 없이 QMessageBox 표시
        QTimer.singleShot(0, _run)

    def _on_acs_connected(self, ok: bool) -> None:
        prev = bool(self._acs_connected)
        self._acs_connected = bool(ok)

        self.set_external_connected("ACS", self._acs_connected)

        if not self._acs_connected:
            # 연결 끊기면 stale 값 즉시 제거
            self._acs_last_pressure = None
            self._acs_last_snapshot = {
                "connected": False,
                "pressure": None,
            }

        self._render_acs_status()

        if prev != self._acs_connected:
            self._set_hmi_log("ACS CONNECTED" if self._acs_connected else "ACS DISCONNECTED")

    def _disconnect_acs_service(self, svc: object | None) -> None:
        if svc is None:
            return
        for sig_name, slot in (
            ("sig_connected", self._on_acs_connected),
            ("sig_snapshot", self._on_acs_snapshot),
            ("sig_pressure", self._on_acs_pressure),
            ("sig_error", self._on_acs_error),
        ):
            try:
                sig = getattr(svc, sig_name, None)
                if sig is not None:
                    sig.disconnect(slot)
            except Exception:
                pass

    def _on_acs_snapshot(self, snap_obj: object) -> None:
        snap = self._snapshot_to_dict(snap_obj)
        self._acs_last_snapshot = snap

        if "connected" in snap:
            self._acs_connected = bool(snap.get("connected", False))
            self.set_external_connected("ACS", self._acs_connected)

        # snapshot 기준 pressure 반영
        if "pressure" in snap:
            try:
                p = snap.get("pressure", None)
                self._acs_last_pressure = float(p) if p is not None else None
            except Exception:
                self._acs_last_pressure = None

        self._render_acs_status()

    def _on_acs_pressure(self, value: object) -> None:
        try:
            self._acs_last_pressure = float(value) if value is not None else None
        except Exception:
            self._acs_last_pressure = None

        # pressure signal이 None이면 바로 숫자 지워야 stale 표시가 안 남음
        if value is None:
            self._acs_last_snapshot = {
                **dict(self._acs_last_snapshot or {}),
                "pressure": None,
            }

        self._render_acs_status()

    def _on_acs_error(self, msg: str) -> None:
        # error 자체는 로그만 남기고,
        # 실제 값 클리어는 sig_connected(False) / sig_pressure(None) / snapshot(None)에 맡긴다.
        self._set_hmi_log(str(msg))

    def _on_tmp_connected(self, ok: bool) -> None:
        prev = bool(self._tmp_connected)
        self._tmp_connected = bool(ok)

        self.set_external_connected("TMP", self._tmp_connected)
        self._render_tmp_status()
        self._set_controls_enabled(self.is_ui_connected())

        if prev != self._tmp_connected:
            self._set_hmi_log("TMP CONNECTED" if self._tmp_connected else "TMP DISCONNECTED")
            self._notify_connection_change("TMP", self._tmp_connected)

    def _on_tmp_snapshot(self, snap_obj: object) -> None:
        snap = dict(snap_obj or {})
        self._tmp_last_snapshot = snap

        if "connected" in snap:
            self._tmp_connected = bool(snap.get("connected", False))
            self.set_external_connected("TMP", self._tmp_connected)

        self._render_tmp_status()
        self._set_controls_enabled(self.is_ui_connected())
        self._check_tmp_temp_alert(snap)

    def _on_tmp_error(self, msg: str) -> None:
        self._render_tmp_status()

    def _on_tmp_log(self, msg: str) -> None:
        self._set_hmi_log(str(msg))

    def _check_tmp_temp_alert(self, snap: Dict[str, Any]) -> None:
        """TMP 모터 온도 감시:
        - 75°C 이상: Google Chat 알림 (5분 쿨다운)
        - 79°C 이상: M/V 닫기 + TMP stop 긴급 처리 (별도 스레드)
        """
        try:
            if not snap.get("connected", False):
                return
            motor_temp = snap.get("motor_temp_c", None)
            if motor_temp is None:
                return

            temp = float(motor_temp)
            now = time.time()

            # ── 79°C 긴급 정지 ──────────────────────────────────
            EMERGENCY_THRESHOLD = 79.0
            if temp >= EMERGENCY_THRESHOLD:
                if not self._tmp_emergency_stop_triggered:
                    self._tmp_emergency_stop_triggered = True
                    self._set_hmi_log(
                        f"[EMERGENCY] TMP 온도 {temp:.1f}°C → M/V 닫기 + TMP 정지 시작"
                    )
                    msg = (
                        f"🚨🚨 [Evaporator] TMP 긴급 정지\n"
                        f"모터 온도: {temp:.1f}°C (기준: {EMERGENCY_THRESHOLD:.0f}°C)\n"
                        f"시각: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"M/V 닫기 후 TMP 정지 명령 실행"
                    )
                    self._send_gchat_alert(msg)
                    threading.Thread(
                        target=self._emergency_tmp_stop,
                        daemon=True,
                        name="TmpEmergencyStop",
                    ).start()
                return

            # ── 75°C 알림 ────────────────────────────────────────
            ALERT_THRESHOLD = 75.0
            if temp >= ALERT_THRESHOLD:
                if now - self._tmp_temp_alert_last_ts >= self._tmp_temp_alert_cooldown_s:
                    self._tmp_temp_alert_last_ts = now
                    msg = (
                        f"🚨 [Evaporator] TMP 온도 경고\n"
                        f"모터 온도: {temp:.1f}°C  (기준: {ALERT_THRESHOLD:.0f}°C 이상)\n"
                        f"시각: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"즉시 확인 바랍니다."
                    )
                    self._set_hmi_log(
                        f"[ALERT] TMP 온도 경고: {temp:.1f}°C → Google Chat 알림 전송"
                    )
                    self._send_gchat_alert(msg)
            else:
                # 온도 정상 복귀 시 쿨다운 및 긴급 정지 플래그 리셋
                self._tmp_temp_alert_last_ts = 0.0
                self._tmp_emergency_stop_triggered = False
        except Exception:
            pass

    def _send_gchat_alert(self, message: str) -> None:
        """Google Chat Webhook으로 알림 전송 (별도 daemon 스레드)."""
        def _send() -> None:
            try:
                url = GCHAT_WEBHOOK_URL
                if not url:
                    return
                payload = _json.dumps({"text": message}).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5):
                    pass
            except Exception as e:
                try:
                    self._set_hmi_log(f"[WARN] Google Chat 알림 전송 실패: {e!r}")
                except Exception:
                    pass

        threading.Thread(target=_send, daemon=True).start()

    def _emergency_tmp_stop(self) -> None:
        """
        TMP 79°C 긴급 정지 처리 (별도 스레드에서 실행).
        1. M/V 닫기 명령 전송
        2. 3초 대기 후 PLC 직접 읽어서 상태 확인
        3. TMP stop_pump() 명령 전송
        """
        try:
            # 1. M/V 닫기
            self._set_hmi_log("[EMERGENCY] M/V 닫기 명령 전송")
            try:
                fut = self.submit_write("M_V_SW", False, tag="EMERGENCY:M_V")
                if fut is not None and hasattr(fut, "result"):
                    fut.result(timeout=5.0)
            except Exception as e:
                self._set_hmi_log(f"[EMERGENCY] M/V 닫기 명령 실패: {e!r}")

            # 2. M/V 닫힘 확인될 때까지 반복 시도 (최대 5회)
            MAX_RETRY = 5
            mv_closed = False
            for attempt in range(1, MAX_RETRY + 1):
                time.sleep(3.0)
                mv_state = self.read_coil("M_V_SW", True)
                if not mv_state:
                    self._set_hmi_log(f"[EMERGENCY] M/V 닫힘 확인 ✅ (시도 {attempt}/{MAX_RETRY})")
                    mv_closed = True
                    break
                else:
                    self._set_hmi_log(
                        f"[EMERGENCY] M/V 닫힘 확인 실패 ({attempt}/{MAX_RETRY}) → 재시도"
                    )
                    # 재시도: M/V 닫기 명령 재전송
                    try:
                        fut = self.submit_write("M_V_SW", False, tag="EMERGENCY:M_V_RETRY")
                        if fut is not None and hasattr(fut, "result"):
                            fut.result(timeout=5.0)
                    except Exception as e:
                        self._set_hmi_log(f"[EMERGENCY] M/V 닫기 재시도 실패: {e!r}")

            if not mv_closed:
                # 최대 재시도 초과 → TMP 정지 금지, Google Chat 알림
                self._set_hmi_log(
                    f"[EMERGENCY] M/V 닫힘 확인 {MAX_RETRY}회 모두 실패 → TMP 정지 중단 (역류 방지)"
                )
                msg = (
                    f"🚨🚨 [Evaporator] TMP 긴급 정지 실패\n"
                    f"M/V 닫힘 {MAX_RETRY}회 시도 모두 실패 → TMP 정지 취소 (역류 방지)\n"
                    f"시각: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"즉시 수동 확인 바랍니다."
                )
                self._send_gchat_alert(msg)
                return  # ← TMP stop 실행 안 함

            # 3. M/V 닫힘 확인된 경우에만 TMP stop 명령 전송
            svc = self._turbovac_service
            if svc is not None and hasattr(svc, "stop_pump"):
                try:
                    svc.stop_pump()
                    self._set_hmi_log("[EMERGENCY] TMP stop_pump() 명령 전송 ✅")
                except Exception as e:
                    self._set_hmi_log(f"[EMERGENCY] TMP stop_pump() 실패: {e!r}")
            else:
                self._set_hmi_log("[EMERGENCY] TMP 서비스 없음 → stop_pump 스킵")

        except Exception as e:
            self._set_hmi_log(f"[EMERGENCY] 긴급 정지 처리 중 예외: {e!r}")

    def _notify_connection_change(self, device: str, connected: bool) -> None:
        """
        TMP / PLC 연결 상태 변화 시점에만 구글챗 알림.
        - 첫 호출은 baseline만 기록하고 알림 안 보냄 (앱 시작 시점 spam 방지)
        - 이후 상태가 바뀐 시점에만 1회 알림

        Args:
            device: "TMP" 또는 "PLC"
            connected: 현재 연결 상태 (True=연결, False=끊김)
        """
        attr = f"_last_{device.lower()}_alert_state"
        last = getattr(self, attr, None)

        if last is None:
            # 첫 호출: baseline 기록만, 알림 없음
            setattr(self, attr, bool(connected))
            return

        if bool(last) == bool(connected):
            # 상태 변화 없음
            return

        # 상태 변화 → 알림
        setattr(self, attr, bool(connected))

        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if connected:
            msg = (
                f"✅ [Evaporator] {device} 통신 복구\n"
                f"시각: {ts}"
            )
        else:
            msg = (
                f"🚨 [Evaporator] {device} 통신 끊김\n"
                f"시각: {ts}\n"
                f"즉시 확인 바랍니다."
            )

        self._set_hmi_log(
            f"[ALERT] {device} {'CONNECTED' if connected else 'DISCONNECTED'} → Google Chat 알림 발송"
        )
        self._send_gchat_alert(msg)

    def _on_button_toggled(self, binding: ButtonBinding, on: bool) -> None:
        on_i = int(bool(on))

        if not self.is_ui_connected():
            self._set_hmi_log(f"[BLOCK] {binding.coil_name} <- {on_i} (PLC not connected)")
            self._popup_warn("PLC 미연결", "PLC가 연결되지 않아 명령을 전송할 수 없습니다.")
            self._revert_button_to_plc(binding, fallback=not bool(on))
            return

        if binding.coil_name == "TMP_SW":
            if not self.get_states():
                self._set_hmi_log(f"[BLOCK] TMP_SW <- {on_i} (PLC state not ready)")
                self._popup_warn("인터락", "PLC 상태를 아직 읽지 못했습니다.\n잠시 후 다시 시도하세요.")
                self._revert_button_to_plc(binding, fallback=not bool(on))
                return

            if (not bool(on)) and self._get_state_locked("M_V_SW", False):
                self._set_hmi_log("[BLOCK] TMP_SW <- 0 (M_V ON)")
                self._popup_warn("인터락", "Main Valve(MV)가 열려 있어 TMP 전원을 끌 수 없습니다.\nMV를 먼저 닫아주세요.")
                self._revert_button_to_plc(binding, fallback=True)
                return

            # TMP 전원이 실제로 OFF가 되면 다음 attach는 idle 기준이어야 한다.
            if not bool(on):
                self._set_tmp_connect_hint(False)

            self._plc.enqueue_write_coil(
                "TMP_SW",
                bool(on),
                momentary=False,
                pulse_ms=None,
                tag="HMI:TMP_SW",
            )

            if bool(on):
                self._set_hmi_log("[UI] TMP_SW <- 1 (service attach after PLC ON confirmed)")
            else:
                self._set_hmi_log("[UI] TMP_SW <- 0")

            return

        # Door 인터락(기존 유지) + 차단 사유도 하단 로그로
        if binding.coil_name == "DOOR_SW":
            if self._is_door_busy():
                self._set_hmi_log(f"[BLOCK] DOOR_SW <- {on_i} (door busy)")
                self._popup_warn("인터락", "Door가 열리거나 닫히는 중입니다.\n완료 후 다시 시도하세요.")
                self._revert_button_to_plc(binding, fallback=not bool(on))
                return

            if not self.get_states():
                self._set_hmi_log(f"[BLOCK] DOOR_SW <- {on_i} (PLC state not ready)")
                self._popup_warn("인터락", "PLC 상태를 아직 읽지 못했습니다.\n잠시 후 다시 시도하세요.")
                self._revert_button_to_plc(binding, fallback=not bool(on))
                return

            if not self._get_state_locked("MAIN_SHUTTER_SW", False):
                self._set_hmi_log(f"[BLOCK] DOOR_SW <- {on_i} (MAIN_SHUTTER closed)")
                self._popup_warn("인터락", "Main Shutter가 닫혀 있습니다.\nMain Shutter를 먼저 열어주세요.")
                self._revert_button_to_plc(binding, fallback=not bool(on))
                return
            
            # ✅ 추가: Door '열기'(on=True)일 때만 조건 적용
            if bool(on):
                # 1) Vent는 꺼져 있어야 함 (V_V_SW == 0)
                if self._get_state_locked("V_V_SW", False):
                    self._set_hmi_log(f"[BLOCK] DOOR_SW <- {on_i} (VENT ON: V_V_SW=1)")
                    self._popup_warn("인터락", "Vent(VV)가 켜져 있습니다.\nVent를 먼저 꺼주세요.")
                    self._revert_button_to_plc(binding, fallback=False)
                    return

                # 2) Main Valve는 꺼져 있어야 함 (M_V_SW == 0)
                if self._get_state_locked("M_V_SW", False):
                    self._set_hmi_log(f"[BLOCK] DOOR_SW <- {on_i} (MAIN VALVE ON: M_V_SW=1)")
                    self._popup_warn("인터락", "Main Valve(MV)가 켜져 있습니다.\nMain Valve를 먼저 꺼주세요.")
                    self._revert_button_to_plc(binding, fallback=False)
                    return

            # ✅ write는 PLCService로
            self._plc.enqueue_write_coil("DOOR_SW", bool(on), momentary=False, pulse_ms=None, tag="HMI:DOOR_SW")
            self._begin_door_busy()

            # ✅ 상단 상태라인은 건드리지 않음(연결 상태만 표시)
            self._set_hmi_log(f"[UI] DOOR_SW <- {on_i} (moving)")
            return

        # Main shutter 인터락(door 이동중 닫기 금지) - 기존 유지 + 로그
        if binding.coil_name == "MAIN_SHUTTER_SW":
            if self._is_door_busy() and (not bool(on)):
                self._set_hmi_log("[BLOCK] MAIN_SHUTTER_SW <- 0 (door moving)")
                self._popup_warn("인터락", "Door가 열리거나 닫히는 중에는\nMain Shutter를 닫을 수 없습니다.")
                self._revert_button_to_plc(binding, fallback=True)
                return
            
        # ✅ MV ON 시: 현재 PLC에서 빠진 Turbo 조건만 복원
        # if binding.coil_name == "M_V_SW" and bool(on):
        #     ok, reason = self._call_tmp_helper("check_tmp_ready_for_mv")
        #     if not ok:
        #         self._set_hmi_log(f"[BLOCK] M_V_SW <- {on_i} ({reason})")
        #         self._popup_warn("TMP 인터락", reason)
        #         self._revert_button_to_plc(binding, fallback=False)
        #         return
            
        # ✅ POWER ON 시: 현재 PLC에서 빠진 Turbo 조건만 복원
        # if binding.coil_name in ("POWER_1_SW", "POWER_2_SW") and bool(on):
        #     ok, reason = self._call_tmp_helper("check_tmp_ready_for_power")
        #     if not ok:
        #         self._set_hmi_log(f"[BLOCK] {binding.coil_name} <- {on_i} ({reason})")
        #         self._popup_warn("TMP 인터락", reason)
        #         self._revert_button_to_plc(binding, fallback=False)
        #         return

        # 일반 토글: 하단 로그만
        self._plc.enqueue_write_coil(
            binding.coil_name, bool(on),
            momentary=binding.momentary,
            pulse_ms=None,
            tag=f"HMI:{binding.coil_name}",
        )
        self._set_hmi_log(f"[UI] {binding.coil_name} <- {on_i}")

    def _on_all_stop_clicked(self) -> None:
        # ✅ 추가: Vacuum 시퀀스 실행 중이면 먼저 중단 요청
        vseq = getattr(self, "_vacuum_sequence", None)
        if vseq is not None and vseq.isRunning():
            try:
                vseq.request_abort("ALL STOP")
                self._set_hmi_log("[UI] ALL STOP → Vacuum 시퀀스 중단 요청")
            except Exception:
                pass

        if not self.is_ui_connected():
            self._set_hmi_log("[BLOCK] ALL STOP (PLC not connected)")
            self._popup_warn("PLC 미연결", "PLC가 연결되지 않아 ALL STOP을 전송할 수 없습니다.")
            return

        # 1) 공정 실행 중이면 controller/engine 종료 경로로 위임
        pc = getattr(self, "_process_controller", None)
        try:
            is_running = bool(pc.is_running()) if pc is not None and hasattr(pc, "is_running") else False
        except Exception:
            is_running = False

        if is_running and pc is not None:
            try:
                pc.stop()
                self._set_hmi_log("[UI] ALL STOP -> delegated to process_controller.stop()")
                return
            except Exception as e:
                self._set_hmi_log(f"[WARN] ALL STOP delegate failed: {e!r} -> fallback hard stop")

        # 2) 공정이 없거나 위임 실패 시: 셔터/DAC/파워 직접 OFF → 안전 최소 보장
        if self._turbovac_service is not None:
            try:
                self._set_tmp_connect_hint(False)
                self._turbovac_service.stop_pump()
                self._set_hmi_log("[UI] TMP STOP sent (ALL STOP fallback)")
            except Exception as e:
                self._set_hmi_log(f"[WARN] TMP STOP failed in ALL STOP fallback: {e!r}")

        self._plc.enqueue_write_coil("MAIN_SHUTTER_SW", False, tag="HMI:ALL_STOP_FALLBACK")
        self._plc.enqueue_write_reg("DAC_POWER_1", 0, tag="HMI:ALL_STOP_FALLBACK")
        self._plc.enqueue_write_reg("DAC_POWER_2", 0, tag="HMI:ALL_STOP_FALLBACK")
        self._plc.enqueue_write_coil("POWER_1_SW", False, tag="HMI:ALL_STOP_FALLBACK")
        self._plc.enqueue_write_coil("POWER_2_SW", False, tag="HMI:ALL_STOP_FALLBACK")

        for b in self.BUTTONS:
            if b.coil_name in ("MAIN_SHUTTER_SW", "POWER_1_SW", "POWER_2_SW"):
                continue
            self._plc.enqueue_write_coil(b.coil_name, False, tag="HMI:ALL_STOP_FALLBACK")

        self._set_hmi_log("[UI] ALL STOP fallback sent (direct PLC hard stop)")

    def _on_tmp_start_clicked(self) -> None:
        svc = self._turbovac_service
        if svc is None:
            self._popup_warn("TMP 미설정", "TMP 서비스가 설정되지 않았습니다.")
            return

        if not self.read_coil("TMP_SW", False):
            self._set_hmi_log("[BLOCK] TMP START (TMP_SW OFF)")
            self._popup_warn("인터락", "PLC TMP 전원이 OFF 상태입니다.\n먼저 T.M.P 버튼으로 전원을 켜주세요.")
            return

        if not self._tmp_service_started and not self._tmp_connected:
            self._queue_tmp_attach_prompt(force=True)
            return

        if not self._ensure_tmp_service_started():
            return

        if not self._tmp_connected:
            self._set_hmi_log("[BLOCK] TMP START (TMP not connected yet)")
            self._popup_warn("TMP 연결 대기", "TMP 연결 중입니다.\nConn이 CONNECTED로 바뀐 후 Start를 다시 눌러주세요.")
            return

        ok, reason = self._call_tmp_helper("check_tmp_ready_for_start")
        if not ok:
            self._set_hmi_log(f"[BLOCK] TMP START ({reason})")
            self._popup_warn("TMP 인터락", reason)
            return

        try:
            self._set_tmp_connect_hint(True)
            svc.start_pump()
            self._set_hmi_log("[UI] TMP START sent")
        except Exception as e:
            self._set_hmi_log(f"[FAIL] TMP START: {e!r}")
            self._popup_warn("TMP 전송 실패", f"TMP START 실패: {e!r}")

    def _on_tmp_stop_clicked(self) -> None:
        svc = self._turbovac_service
        if svc is None:
            self._popup_warn("TMP 미설정", "TMP 서비스가 설정되지 않았습니다.")
            return

        if self._get_state_locked("M_V_SW", False):
            self._set_hmi_log("[BLOCK] TMP STOP (M_V ON)")
            self._popup_warn("인터락", "Main Valve(MV)가 열려 있어 TMP를 정지할 수 없습니다.\nMV를 먼저 닫아주세요.")
            return

        if not self._tmp_connected:
            self._set_hmi_log("[BLOCK] TMP STOP (TMP not connected)")
            self._popup_warn("TMP 미연결", "TMP가 아직 연결되지 않았습니다.")
            return

        try:
            self._set_tmp_connect_hint(False)
            svc.stop_pump()
            self._set_hmi_log("[UI] TMP STOP sent")
        except Exception as e:
            self._set_hmi_log(f"[FAIL] TMP STOP: {e!r}")
            self._popup_warn("TMP 전송 실패", f"TMP STOP 실패: {e!r}")

    # ============================================================
    # PLCService signals → UI apply
    # ============================================================
    def _on_plc_cmd_trace(self, obj: object) -> None:
        """
        PLCServiceWorker에서 발생한 '실제 실행된 명령' trace.
        - HMI에서 보낸 명령(tag이 HMI:로 시작)만 공정 CSV에 기록
        """
        try:
            d = dict(obj or {})
        except Exception:
            return

        tag = str(d.get("tag", "") or "")
        ok = bool(d.get("ok", True))
        detail = str(d.get("detail", "") or "")

        # ✅ tag가 HMI가 아니어도, FAIL이면 UI는 끊김처럼 표시
        if not ok:
            self._mark_io_failed(f"[PLC CMD FAIL] {tag} | {detail}".strip())

        # 기존 정책 유지: HMI 태그만 하단로그/CSV 기록
        if not tag.startswith("HMI:"):
            return

        cmd = str(d.get("event", "") or "")
        target = str(d.get("target", "") or "")
        value = d.get("value", "")
        detail = str(d.get("detail", "") or "")
        ok = bool(d.get("ok", True))

        # ✅ 하단 로그창에는 항상 남김
        try:
            vdisp = int(value) if isinstance(value, bool) else value
            if ok:
                self._set_hmi_log(f"[CMD][OK] {target}={vdisp} ({cmd})")
            else:
                tail = f" | {detail}" if detail else ""
                self._set_hmi_log(f"[CMD][FAIL] {target}={vdisp} ({cmd}){tail}")
        except Exception:
            pass

        # ✅ 공정 CSV(LogService)는 주입된 경우에만 기존 로직 유지
        if self._log is None:
            return

        cmd = str(d.get("event", "") or "")
        target = str(d.get("target", "") or "")
        value = d.get("value", "")
        detail = str(d.get("detail", "") or "")
        ok = bool(d.get("ok", True))

        # cmd(type명) → 공정 CSV용 event로 매핑
        event = cmd
        if cmd == "CmdWriteCoil":
            event = "PULSE_COIL" if ("pulse_ms" in detail or "momentary=1" in detail) else "WRITE_COIL"
            if isinstance(value, bool):
                value = int(value)
        elif cmd == "CmdWriteReg":
            tu = target.upper()
            event = "SET_DAC" if tu in ("DAC_POWER_1", "DAC_POWER_2") else "WRITE_REG"
        elif cmd == "CmdSetDacCurrent":
            event = "SET_DAC_MA"

        # detail에 tag/성공여부를 같이 넣어 사람이 보기 좋게
        detail2 = f"{tag} {'OK' if ok else 'ERR'}"
        if detail:
            detail2 += f" | {detail}"

        try:
            detail_line = f"{event} {target}={value}"
            if detail2:
                detail_line += f" | {detail2}"

            self._log.telemetry({
                "step": "HMI",
                "detail": detail_line,
            })
        except Exception:
            pass

    def _apply_states(self, states_obj: object) -> None:
        states: Dict[str, bool] = dict(states_obj or {})

        # ✅ 이전값 보관 후 갱신
        with self._state_lock:
            prev = dict(self._last_states)
            prev_ui = bool(self._connected) and bool(self._io_healthy)

            self._last_states = states
            # ✅ 상태를 정상 수신했으면 health 회복
            if self._connected:
                self._io_healthy = True

            now_ui = bool(self._connected) and bool(self._io_healthy)

        if prev_ui != now_ui:
            self._render_status_line()
            self._set_controls_enabled(self.is_ui_connected())
            if now_ui:
                self._set_hmi_log("PLC CONNECTED (I/O recovered)")

        # ✅ 상태 변화 로그(너무 많아지는 것을 방지하기 위해 "UI에 매핑된 coil"만)
        #    - 필요하면 이 필터를 제거하면 states 전체 변화도 찍을 수 있음.
        if prev:
            watch = {b.coil_name for b in self.BUTTONS} | set(self.INDICATORS.values())
            for k in watch:
                if k in prev and k in states and bool(prev[k]) != bool(states[k]):
                    self._set_hmi_log(f"[STATE] {k} -> {int(bool(states[k]))}")

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
                # QSignalBlocker로 setChecked 중 toggled 시그널 재발생 방지
                with QSignalBlocker(w):
                    w.setChecked(target)
            except Exception:
                pass

        prev_tmp_sw = bool(prev.get("TMP_SW", False)) if prev else None
        now_tmp_sw = bool(states.get("TMP_SW", False))

        if now_tmp_sw:
            # TMP_SW가 실제로 ON이면, 바로 attach하지 말고 사용자에게 먼저 묻는다.
            self._queue_tmp_attach_prompt(force=False)
        elif prev_tmp_sw is None or prev_tmp_sw != now_tmp_sw:
            # TMP 전원이 실제로 OFF가 되면 다음 ON 사이클에서는 다시 물어봐야 한다.
            self._set_tmp_connect_hint(False)
            self._reset_tmp_attach_prompt_state()

    def _on_connected(self, ok: bool) -> None:
        with self._state_lock:
            prev_ui = bool(self._connected) and bool(self._io_healthy)
            self._connected = bool(ok)
            if not self._connected:
                self._io_healthy = False
            else:
                # 연결 직후는 일단 healthy로 시작(실패하면 _mark_io_failed가 다시 내림)
                self._io_healthy = True
            now_ui = bool(self._connected) and bool(self._io_healthy)

        self._render_status_line()
        self._set_controls_enabled(self.is_ui_connected())

        if not now_ui:
            self._set_dac_actual_text(1, None)
            self._set_dac_actual_text(2, None)
            vseq = getattr(self, "_vacuum_sequence", None)
            if vseq is not None and vseq.isRunning():
                try:
                    vseq.request_abort("PLC 통신 끊김")
                    self._set_hmi_log("[VACUUM] PLC 끊김 → 시퀀스 중단 요청")
                except Exception:
                    pass

        if prev_ui != now_ui:
            self._set_hmi_log("PLC CONNECTED" if now_ui else "PLC DISCONNECTED")
            self._notify_connection_change("PLC", now_ui)

    def _on_error(self, msg: str) -> None:
        # ✅ 한 번이라도 I/O 에러면 UI는 끊김으로
        self._mark_io_failed(msg)

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
            
    def _mark_io_failed(self, reason: str = "") -> None:
        with self._state_lock:
            prev_ui = bool(self._connected) and bool(self._io_healthy)
            self._io_healthy = False
            now_ui = bool(self._connected) and bool(self._io_healthy)

        if prev_ui != now_ui:
            self._render_status_line()
            self._set_controls_enabled(False)
            self._set_dac_actual_text(1, None)
            self._set_dac_actual_text(2, None)
            self._set_hmi_log("PLC DISCONNECTED (I/O failed)")

        if reason:
            self._set_hmi_log(reason)

    def _set_controls_enabled(self, enabled: bool) -> None:
        # coil 버튼들
        for b in self.BUTTONS:
            w = getattr(self.ui, b.widget_name, None)
            if w is not None and hasattr(w, "setEnabled"):
                try:
                    w.setEnabled(bool(enabled))
                except Exception:
                    pass

        # ALL STOP
        all_stop = getattr(self.ui, "allstopBtn", None)
        if all_stop is not None and hasattr(all_stop, "setEnabled"):
            try:
                all_stop.setEnabled(bool(enabled))
            except Exception:
                pass

        # DAC 수동 컨트롤
        for w in (
            getattr(self, "_dac1_spin", None),
            getattr(self, "_dac1_down100_btn", None),
            getattr(self, "_dac1_up100_btn", None),
            getattr(self, "_dac1_set_btn", None),
            getattr(self, "_dac1_reset_btn", None),

            getattr(self, "_dac2_spin", None),
            getattr(self, "_dac2_down100_btn", None),
            getattr(self, "_dac2_up100_btn", None),
            getattr(self, "_dac2_set_btn", None),
            getattr(self, "_dac2_reset_btn", None),

            getattr(self.ui, "tmpStartBtn", None),
            getattr(self.ui, "tmpStopBtn", None),
        ):
            if w is not None and hasattr(w, "setEnabled"):
                try:
                    w.setEnabled(bool(enabled))
                except Exception:
                    pass

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
        self._set_hmi_log("DOOR: move done")

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

        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}"

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

    def _set_dac_actual_text(self, ch: int, value: Optional[float]) -> None:
        w = self._dac1_actual if int(ch) == 1 else self._dac2_actual
        if w is None:
            return

        try:
            if value is None:
                text = "---"
            else:
                text = f"{float(value):.1f}"

            if hasattr(w, "setPlainText"):
                w.setPlainText(text)
            elif hasattr(w, "setText"):
                w.setText(text)
        except Exception:
            pass

    def _apply_regs(self, regs_obj: object) -> None:
        regs = dict(regs_obj or {})

        raw1 = regs.get("POWER_READ_1", None)
        raw2 = regs.get("POWER_READ_2", None)

        try:
            v1 = None if raw1 is None else float(raw1)
        except Exception:
            v1 = None

        try:
            v2 = None if raw2 is None else float(raw2)
        except Exception:
            v2 = None

        self._set_dac_actual_text(1, v1)
        self._set_dac_actual_text(2, v2)

    # ============================================================
    # DAC 수동 입력 (PLCService로 write_reg만)
    # ============================================================
    def _wire_dac_controls(self) -> None:
        fs = int(getattr(self.settings, "dac_full_scale_code", 4000))
        off = int(getattr(self.settings, "dac_offset_code", 0))
        lo, hi = off, off + fs

        self._dac1_spin = getattr(self.ui, "dac1Spin", None)
        self._dac1_down100_btn = getattr(self.ui, "dac1Down100Btn", None)
        self._dac1_up100_btn = getattr(self.ui, "dac1Up100Btn", None)
        self._dac1_set_btn = getattr(self.ui, "dac1SetBtn", None)
        self._dac1_reset_btn = getattr(self.ui, "dac1ResetBtn", None)
        self._dac1_actual = getattr(self.ui, "dacActual1Edit", None)

        self._dac2_spin = getattr(self.ui, "dac2Spin", None)
        self._dac2_down100_btn = getattr(self.ui, "dac2Down100Btn", None)
        self._dac2_up100_btn = getattr(self.ui, "dac2Up100Btn", None)
        self._dac2_set_btn = getattr(self.ui, "dac2SetBtn", None)
        self._dac2_reset_btn = getattr(self.ui, "dac2ResetBtn", None)
        self._dac2_actual = getattr(self.ui, "dacActual2Edit", None)

        for sp in (self._dac1_spin, self._dac2_spin):
            if sp is not None and hasattr(sp, "setRange"):
                try:
                    sp.setRange(int(lo), int(hi))
                    sp.setSingleStep(1)
                except Exception:
                    pass

        if self._dac1_down100_btn is not None:
            self._dac1_down100_btn.clicked.connect(lambda: self._on_step_dac_code(1, -100))
        if self._dac1_up100_btn is not None:
            self._dac1_up100_btn.clicked.connect(lambda: self._on_step_dac_code(1, +100))
        if self._dac1_set_btn is not None:
            self._dac1_set_btn.clicked.connect(lambda: self._on_apply_dac_code(1))
        if self._dac1_reset_btn is not None:
            self._dac1_reset_btn.clicked.connect(lambda: self._on_reset_dac(1))

        if self._dac2_down100_btn is not None:
            self._dac2_down100_btn.clicked.connect(lambda: self._on_step_dac_code(2, -100))
        if self._dac2_up100_btn is not None:
            self._dac2_up100_btn.clicked.connect(lambda: self._on_step_dac_code(2, +100))
        if self._dac2_set_btn is not None:
            self._dac2_set_btn.clicked.connect(lambda: self._on_apply_dac_code(2))
        if self._dac2_reset_btn is not None:
            self._dac2_reset_btn.clicked.connect(lambda: self._on_reset_dac(2))

        self._set_dac_actual_text(1, None)
        self._set_dac_actual_text(2, None)

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

    def _on_step_dac_code(self, ch: int, delta: int) -> None:
        sp = self._dac1_spin if int(ch) == 1 else self._dac2_spin
        if sp is None:
            self._popup_warn("UI 오류", "DAC 입력 위젯(QSpinBox)을 찾지 못했습니다.")
            return

        try:
            cur = int(sp.value())
        except Exception:
            self._popup_warn("입력 오류", "현재 DAC 값 읽기 실패")
            return

        new_v = int(cur) + int(delta)

        # 버튼 누른 즉시 입력칸(QSpinBox)에 증가/감소된 값이 보이게 함
        try:
            sp.setValue(int(new_v))
        except Exception:
            pass

        # 실제 전송은 기존 공용 apply 경로 재사용
        self._apply_dac_code(ch, new_v)

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
        if not self.is_ui_connected():
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
            self._plc.enqueue_write_reg(key, int(v_clamped), tag=f"HMI:DAC{ch}")
            # ✅ 상단 상태라인은 건드리지 않음(연결 상태만)
            self._set_hmi_log(f"[UI] {key} <- {int(v_clamped)} (DAC{ch} set)")
        except Exception as e:
            self._popup_warn("전송 실패", f"DAC 값 전송 실패: {e!r}")


    # ============================================================
    # Vacuum ON 시퀀스
    # ============================================================
 
    def get_tmp_freq(self) -> Optional[float]:
        """현재 TMP 주파수(Hz) 반환. 없으면 None."""
        try:
            snap = dict(self._tmp_last_snapshot or {})
            v = snap.get("freq_hz", None)
            return float(v) if v is not None else None
        except Exception:
            return None
        
    def _cancel_vacuum_btn(self) -> None:
        """인터락 실패 시 vacuumOnBtn을 조용히 해제."""
        btn = getattr(self.ui, "vacuumOnBtn", None)
        if btn is not None:
            try:
                with QSignalBlocker(btn):
                    btn.setChecked(False)
            except Exception:
                pass
 
    def _on_vacuum_on_toggled(self, on: bool) -> None:
        """
        Vacuum ON 버튼 클릭 핸들러.
 
        사전 조건 확인 후 VacuumSequence 스레드를 시작한다.
        """
        # ── 버튼 OFF: 시퀀스 중단 요청 ──────────────────────────
        if not on:
            seq = getattr(self, "_vacuum_sequence", None)
            if seq is not None and seq.isRunning():
                seq.request_abort("사용자 중단")
                self._set_hmi_log("[VACUUM] 사용자가 버튼으로 시퀀스 중단 요청")
            return
        
        # ── 0. PLC 연결 확인 ─────────────────────────────────────
        if not self.is_ui_connected():
            self._set_hmi_log("[BLOCK] VACUUM ON (PLC not connected)")
            self._popup_warn("PLC 미연결", "PLC가 연결되지 않아 Vacuum ON을 시작할 수 없습니다.")
            self._cancel_vacuum_btn()
            return
 
        # ── 1. 시퀀스 중복 실행 방지 ─────────────────────────────
        seq = getattr(self, "_vacuum_sequence", None)
        if seq is not None and seq.isRunning():
            self._set_hmi_log("[BLOCK] VACUUM ON (already running)")
            self._popup_warn("진행 중", "Vacuum ON 시퀀스가 이미 실행 중입니다.")
            self._cancel_vacuum_btn()
            return
 
        # ── 2. 사전 조건: 꺼져 있어야 하는 코일 ─────────────────
        MUST_OFF = {
            "V_V_SW":          "Vent (V/V)",
            "DOOR_SW":         "Door",
            "MAIN_SHUTTER_SW": "Main Shutter",
            "SHUTTER_1_SW":    "MS1 Shutter",
            "POWER_1_SW":      "MS1 Power",
            "SHUTTER_2_SW":    "MS2 Shutter",
            "POWER_2_SW":      "MS2 Power",
            "M_V_SW":          "Main Valve (M/V)",
            "R_V_SW":          "Rough Valve (R/V)",
        }
        for coil, label in MUST_OFF.items():
            if self._get_state_locked(coil, False):
                msg = f"{label}이(가) 켜져 있습니다.\nVacuum ON 시작 전에 꺼주세요."
                self._set_hmi_log(f"[BLOCK] VACUUM ON ({coil} ON)")
                self._popup_warn("인터락", msg)
                self._cancel_vacuum_btn()
                return
 
        # ── 3. 사전 조건: 켜져 있어야 하는 코일 ─────────────────
        MUST_ON = {
            "R_P_SW": "R/P (Rotary Pump)",
            "F_V_SW": "F/V (Fine Valve)",
            "TMP_SW": "TMP",
        }
        for coil, label in MUST_ON.items():
            if not self._get_state_locked(coil, False):
                msg = f"{label}이(가) 꺼져 있습니다.\nVacuum ON 시작 전에 켜주세요."
                self._set_hmi_log(f"[BLOCK] VACUUM ON ({coil} OFF)")
                self._popup_warn("인터락", msg)
                self._cancel_vacuum_btn()
                return
 
        # ── 4. 사전 조건: TMP 주파수 >= 900 Hz ───────────────────
        tmp_freq = self.get_tmp_freq()
        if tmp_freq is None:
            self._set_hmi_log("[BLOCK] VACUUM ON (TMP freq 읽기 실패)")
            self._popup_warn(
                "인터락",
                "TMP 주파수를 읽을 수 없습니다.\nTMP가 연결되어 있는지 확인해주세요.",
            )
            self._cancel_vacuum_btn()
            return
 
        TMP_FREQ_MIN = 900.0
        if tmp_freq < TMP_FREQ_MIN:
            self._set_hmi_log(
                f"[BLOCK] VACUUM ON (TMP freq {tmp_freq:.0f} Hz < {TMP_FREQ_MIN:.0f} Hz)"
            )
            self._popup_warn(
                "인터락",
                f"TMP 주파수가 부족합니다.\n"
                f"현재: {tmp_freq:.0f} Hz  (필요: {TMP_FREQ_MIN:.0f} Hz 이상)\n"
                f"TMP가 정상 속도에 도달할 때까지 기다려 주세요.",
            )
            self._cancel_vacuum_btn()
            return
 
        # ── 5. ACS 연결 확인 ─────────────────────────────────────
        acs = getattr(self, "_acs_service", None)
        if acs is None or not acs.is_connected():
            self._set_hmi_log("[BLOCK] VACUUM ON (ACS not connected)")
            self._popup_warn("ACS 미연결", "ACS 압력 센서가 연결되지 않아 Vacuum ON을 시작할 수 없습니다.")
            self._cancel_vacuum_btn()
            return
 
        # ── 6. 이미 목표 압력 이하인지 확인 ─────────────────────────
        snap = acs.get_last_snapshot()
        current_pressure = snap.pressure if snap is not None else None
        if current_pressure is not None and current_pressure <= 5e-2:
            self._set_hmi_log(
                f"[VACUUM] 이미 목표 압력 이하 ({current_pressure:.3e} Torr) → 시퀀스 생략"
            )
            self._popup_warn(
                "Vacuum ON",
                f"이미 진공이 잡혀 있습니다.\n"
                f"현재 압력: {current_pressure:.3e} Torr\n"
                f"(목표: 5e-2 Torr 이하)",
            )
            self._cancel_vacuum_btn()
            return

        # ── 7. 시퀀스 시작 ───────────────────────────────────────────
        from controller.vacuum_sequence import VacuumSequence
 
        self._set_hmi_log(
            f"[VACUUM] 사전 조건 통과 (TMP freq={tmp_freq:.0f} Hz) → 시퀀스 시작"
        )
 
        seq = VacuumSequence(
            plc_binder=self,
            acs_service=acs,
            get_tmp_freq_fn=self.get_tmp_freq,
            parent=self,
        )
        seq.sig_log.connect(self._set_hmi_log)
        seq.sig_done.connect(self._on_vacuum_sequence_done)
 
        self._vacuum_sequence = seq
        seq.start()
 
    def _on_vacuum_sequence_done(self, success: bool, reason: str) -> None:
        """VacuumSequence 완료 콜백 (UI 스레드에서 호출됨)."""
        # 버튼 토글 해제 (시그널 재발생 방지)
        btn = getattr(self.ui, "vacuumOnBtn", None)
        if btn is not None:
            try:
                with QSignalBlocker(btn):
                    btn.setChecked(False)
            except Exception:
                pass
 
        if success:
            self._set_hmi_log(f"[VACUUM] 완료: {reason}")
        else:
            self._set_hmi_log(f"[VACUUM] 실패/중단: {reason}")
            self._popup_warn("Vacuum ON 실패", f"Vacuum ON 시퀀스 실패:\n{reason}")
 
        self._vacuum_sequence = None