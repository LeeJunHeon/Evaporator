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
from typing import Dict, Optional, Tuple, Any
from services.log_service import LogService

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

        # thread-safe state (다른 스레드에서도 읽을 수 있음)
        self._state_lock = threading.Lock()
        self._last_states: Dict[str, bool] = {}
        self._connected: bool = False

        # ✅ UI 표시용: 최근 I/O 성공 여부(한 번이라도 실패하면 False)
        self._io_healthy: bool = False

        # ✅ 상단 상태라인에는 "연결 상태만" 표시하기 위한 외부 장비 연결 상태 저장소
        self._external_connected: Dict[str, bool] = {}

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

        # (선택) 초기 표시
        self._render_status_line()

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

    def _render_tmp_status(self) -> None:
        snap = dict(self._tmp_last_snapshot or {})

        connected = bool(snap.get("connected", self._tmp_connected))
        state_text = str(snap.get("state_text", "") or "").strip()
        freq_hz = snap.get("freq_hz", None)
        current_a = snap.get("current_a", None)
        motor_temp_c = snap.get("motor_temp_c", None)
        alarm_text = str(snap.get("alarm_text", "") or "").strip()

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
        alarm_disp = alarm_text if alarm_text else "-"

        self._set_tmp_field("tmpConnEdit", conn_text)
        self._set_tmp_field("tmpStateEdit", state_text)
        self._set_tmp_field("tmpFreqEdit", freq_text)
        self._set_tmp_field("tmpCurrentEdit", curr_text)
        self._set_tmp_field("tmpTempEdit", temp_text)
        self._set_tmp_field("tmpAlarmEdit", alarm_disp)

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

        QTimer.singleShot(0, _run)

    def _on_tmp_connected(self, ok: bool) -> None:
        prev = bool(self._tmp_connected)
        self._tmp_connected = bool(ok)

        if self._tmp_connected:
            self._tmp_attach_prompt_done = True

    def _on_tmp_connected(self, ok: bool) -> None:
        prev = bool(self._tmp_connected)
        self._tmp_connected = bool(ok)

        self.set_external_connected("TMP", self._tmp_connected)
        self._render_tmp_status()
        self._set_controls_enabled(self.is_ui_connected())

        if prev != self._tmp_connected:
            self._set_hmi_log("TMP CONNECTED" if self._tmp_connected else "TMP DISCONNECTED")

    def _on_tmp_snapshot(self, snap_obj: object) -> None:
        snap = dict(snap_obj or {})
        self._tmp_last_snapshot = snap

        if "connected" in snap:
            self._tmp_connected = bool(snap.get("connected", False))
            self.set_external_connected("TMP", self._tmp_connected)

        self._render_tmp_status()
        self._set_controls_enabled(self.is_ui_connected())

    def _on_tmp_error(self, msg: str) -> None:
        self._render_tmp_status()

    def _on_tmp_log(self, msg: str) -> None:
        self._set_hmi_log(str(msg))

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
        if not self.is_ui_connected():
            self._set_hmi_log("[BLOCK] ALL STOP (PLC not connected)")
            self._popup_warn("PLC 미연결", "PLC가 연결되지 않아 ALL STOP을 전송할 수 없습니다.")
            return
        
        # ✅ ALL STOP에서는 TMP 장비 정지 + PLC TMP OFF 둘 다 수행
        if self._turbovac_service is not None:
            try:
                self._set_tmp_connect_hint(False)
                self._turbovac_service.stop_pump()
                self._set_hmi_log("[UI] TMP STOP sent (ALL STOP)")
            except Exception as e:
                self._set_hmi_log(f"[WARN] TMP STOP failed in ALL STOP: {e!r}")

        # ✅ 안전 순서(권장): MAIN_SHUTTER close → DAC=0 → POWER off → 나머지 off
        self._plc.enqueue_write_coil("MAIN_SHUTTER_SW", False, tag="HMI:ALL_STOP")

        self._plc.enqueue_write_reg("DAC_POWER_1", 0, tag="HMI:ALL_STOP")
        self._plc.enqueue_write_reg("DAC_POWER_2", 0, tag="HMI:ALL_STOP")

        self._plc.enqueue_write_coil("POWER_1_SW", False, tag="HMI:ALL_STOP")
        self._plc.enqueue_write_coil("POWER_2_SW", False, tag="HMI:ALL_STOP")
        for b in self.BUTTONS:
            if b.coil_name in ("MAIN_SHUTTER_SW", "POWER_1_SW", "POWER_2_SW"):
                continue
            self._plc.enqueue_write_coil(b.coil_name, False, tag="HMI:ALL_STOP")

        # ✅ 상단 상태라인은 건드리지 않고, 하단 로그만 남김
        self._set_hmi_log("[UI] ALL STOP sent (MAIN_SHUTTER close + DAC=0 + POWER OFF + others OFF)")

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

        if prev_ui != now_ui:
            self._set_hmi_log("PLC CONNECTED" if now_ui else "PLC DISCONNECTED")

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
            getattr(self, "_dac1_spin", None), getattr(self, "_dac1_set_btn", None), getattr(self, "_dac1_reset_btn", None),
            getattr(self, "_dac2_spin", None), getattr(self, "_dac2_set_btn", None), getattr(self, "_dac2_reset_btn", None),
            getattr(self.ui, "tmpStartBtn", None), getattr(self.ui, "tmpStopBtn", None),
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

    def _set_dac_actual_text(self, ch: int, value: Optional[int]) -> None:
        w = self._dac1_actual if int(ch) == 1 else self._dac2_actual
        if w is None:
            return

        try:
            if value is None:
                text = "---"
            else:
                text = str(int(value))

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
            self._set_dac_actual_text(1, None if raw1 is None else int(raw1))
        except Exception:
            self._set_dac_actual_text(1, None)

        try:
            self._set_dac_actual_text(2, None if raw2 is None else int(raw2))
        except Exception:
            self._set_dac_actual_text(2, None)

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
        self._dac1_actual = getattr(self.ui, "dacActual1Edit", None)

        self._dac2_spin = getattr(self.ui, "dac2Spin", None)
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

        if self._dac1_set_btn is not None:
            self._dac1_set_btn.clicked.connect(lambda: self._on_apply_dac_code(1))
        if self._dac1_reset_btn is not None:
            self._dac1_reset_btn.clicked.connect(lambda: self._on_reset_dac(1))

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
