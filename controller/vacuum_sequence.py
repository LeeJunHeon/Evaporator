# -*- coding: utf-8 -*-
"""
controller/vacuum_sequence.py

Vacuum ON 자동 시퀀스 (QThread 기반)

동작 순서:
  1. M/V 확인 (이미 OFF 상태여야 함 — 진입 시 이미 인터락 통과했으므로 보험용)
  2. F/V 닫기 (F_V_SW = OFF)
  3. R/V 열기 (R_V_SW = ON)
  4. ACS 압력 <= 5e-2 Torr 될 때까지 대기 (타임아웃: 300초)
  5. R/V 닫기 (R_V_SW = OFF)
  6. F/V 열기 (F_V_SW = ON)
  7. M/V 열기 (M_V_SW = ON)

사전 조건 (시작 전 hmi_plc_binder에서 확인):
  - OFF: V_V_SW, DOOR_SW, MAIN_SHUTTER_SW, SHUTTER_1_SW, SHUTTER_2_SW,
         POWER_1_SW, POWER_2_SW, M_V_SW
  - ON : R_P_SW, F_V_SW, TMP_SW
  - TMP freq >= 900 Hz

사용법:
    seq = VacuumSequence(plc_binder=binder, acs_service=acs_svc,
                         get_tmp_freq_fn=lambda: binder.get_tmp_freq())
    seq.sig_log.connect(some_log_fn)
    seq.sig_done.connect(some_done_fn)    # (success: bool, reason: str)
    seq.start()
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from PySide6.QtCore import QThread, Signal


# ── 상수 ────────────────────────────────────────────────────────
PRESSURE_TARGET_TORR: float = 5e-2       # R/V 닫기 기준 압력 (Torr)
PRESSURE_TIMEOUT_S: float   = 300.0      # 압력 도달 대기 최대 시간 (초)
PRESSURE_POLL_S: float      = 0.5        # 압력 폴링 간격 (초)
STEP_SETTLE_S: float        = 3.0        # 밸브 명령 후 안정 대기 (초)
SETTLE_TICK_S: float        = 0.1        # _settle() 내부 체크 간격 (초)
PLC_WRITE_TIMEOUT_S: float  = 5.0        # PLCService submit 대기 타임아웃 (초)


class VacuumSequence(QThread):
    """
    Vacuum ON 자동 시퀀스를 별도 스레드에서 실행한다.

    Parameters
    ----------
    plc_binder : HmiPlcBinder
        PLC 쓰기/읽기에 사용. submit_write() / read_coil() 사용.
    acs_service : ACSService | None
        압력 폴링용. get_last_snapshot() 사용.
    get_tmp_freq_fn : Callable[[], float | None]
        현재 TMP freq(Hz)를 반환하는 콜백.
        binder에 get_tmp_freq()를 추가하거나 lambda로 전달.
    pressure_target : float
        R/V 닫기 기준 압력 (Torr, 기본 5e-2).
    pressure_timeout_s : float
        압력 대기 타임아웃 (초, 기본 300).
    """

    sig_log  = Signal(str)              # 로그 메시지
    sig_done = Signal(bool, str)        # (success, reason)

    def __init__(
        self,
        *,
        plc_binder: object,
        acs_service: Optional[object] = None,
        get_tmp_freq_fn: Optional[Callable[[], Optional[float]]] = None,
        pressure_target: float = PRESSURE_TARGET_TORR,
        pressure_timeout_s: float = PRESSURE_TIMEOUT_S,
        parent=None,
    ):
        super().__init__(parent)
        self._binder          = plc_binder
        self._acs             = acs_service
        self._get_tmp_freq    = get_tmp_freq_fn
        self._pressure_target = float(pressure_target)
        self._pressure_timeout_s = float(pressure_timeout_s)

        self._abort_requested: bool = False
        self._stop_reason: str = ""

    # ── 외부 중단 요청 ────────────────────────────────────────────
    def request_abort(self, reason: str = "사용자 중단") -> None:
        """시퀀스 중단 요청 (스레드 안전)."""
        self._abort_requested = True
        if not self._stop_reason:
            self._stop_reason = str(reason)

    # ── QThread entry point ───────────────────────────────────────
    def run(self) -> None:
        self._log("[VACUUM] === Vacuum ON 시퀀스 시작 ===")
        try:
            self._run_sequence()
        except Exception as e:
            self._log(f"[VACUUM][ERROR] 예외 발생: {e!r}")
            self.sig_done.emit(False, f"예외: {e!r}")

    # ── 메인 시퀀스 ──────────────────────────────────────────────
    def _run_sequence(self) -> None:
        # ── Step 0: 중단 체크 ──────────────────────────────────
        if self._abort_requested:
            self._log("[VACUUM] 시작 전 중단 요청")
            self.sig_done.emit(False, "시작 전 중단")
            return

        # ── Step 1: M/V 이미 OFF 확인 (보험) ──────────────────
        self._log("[VACUUM] Step 1: M/V 상태 확인")
        if self._read_coil("M_V_SW"):
            # 혹시라도 ON이면 먼저 닫음
            self._log("[VACUUM] M/V가 열려 있음 → 닫기")
            ok, err = self._write_and_verify("M_V_SW", False, "M/V 닫기")
            if not ok:
                self._fail(f"M/V 닫기 실패: {err}")
                return

        if self._check_abort():
            return

        # ── Step 2: F/V 닫기 ───────────────────────────────────
        self._log("[VACUUM] Step 2: F/V 닫기 (F_V_SW = OFF)")
        ok, err = self._write_and_verify("F_V_SW", False, "F/V 닫기")
        if not ok:
            self._restore_rough_phase()
            if not self._check_abort():
                self._fail(f"F/V 닫기 실패: {err}")
            return

        # ── Step 3: R/V 열기 ───────────────────────────────────
        self._log("[VACUUM] Step 3: R/V 열기 (R_V_SW = ON)")
        ok, err = self._write_and_verify("R_V_SW", True, "R/V 열기")
        if not ok:
            self._restore_rough_phase()
            if not self._check_abort():
                self._fail(f"R/V 열기 실패: {err}")
            return

        if self._check_abort():
            self._restore_rough_phase()
            return

        # ── Step 4: 압력 대기 (ACS <= 5e-2 Torr) ──────────────
        self._log(
            f"[VACUUM] Step 4: 압력 대기 (목표: {self._pressure_target:.1e} Torr, "
            f"타임아웃: {self._pressure_timeout_s:.0f}초)"
        )
        reached = self._wait_pressure(self._pressure_target, self._pressure_timeout_s)

        if self._check_abort():
            self._restore_rough_phase()
            return

        if not reached:
            # 타임아웃: R/V 닫고 복구 후 중단
            self._log("[VACUUM] 압력 타임아웃 → 복구 후 중단")
            self._restore_rough_phase()
            self._fail(
                f"압력 {self._pressure_target:.1e} Torr 도달 타임아웃 "
                f"({self._pressure_timeout_s:.0f}초)"
            )
            return

        # ── Step 5: R/V 닫기 ───────────────────────────────────
        self._log("[VACUUM] Step 5: R/V 닫기 (R_V_SW = OFF)")
        ok, err = self._write_and_verify("R_V_SW", False, "R/V 닫기")
        if not ok:
            self._restore_rough_phase()
            if not self._check_abort():
                self._fail(f"R/V 닫기 실패: {err}")
            return

        if self._check_abort():
            self._restore_rough_phase()
            return

        # ── Step 6: F/V 열기 ───────────────────────────────────
        self._log("[VACUUM] Step 6: F/V 열기 (F_V_SW = ON)")
        ok, err = self._write_and_verify("F_V_SW", True, "F/V 열기")
        if not ok:
            self._restore_fine_phase()
            if not self._check_abort():
                self._fail(f"F/V 열기 실패: {err}")
            return

        if self._check_abort():
            self._restore_fine_phase()
            return

        # ── Step 7: M/V 열기 ───────────────────────────────────
        self._log("[VACUUM] Step 7: M/V 열기 (M_V_SW = ON)")
        ok, err = self._write_and_verify("M_V_SW", True, "M/V 열기")
        if not ok:
            self._restore_fine_phase()
            if not self._check_abort():
                self._fail(f"M/V 열기 실패: {err}")
            return
        
        self._log("[VACUUM] === Vacuum ON 시퀀스 완료 ✅ ===")
        self.sig_done.emit(True, "진공 도달 완료")

    # ── 헬퍼: 압력 폴링 대기 ─────────────────────────────────────
    def _wait_pressure(self, target: float, timeout_s: float) -> bool:
        """
        ACS 압력이 target Torr 이하가 될 때까지 대기.
        Returns True if target reached, False on timeout.
        """
        deadline = time.monotonic() + timeout_s
        last_log = 0.0

        while time.monotonic() < deadline:
            if self._abort_requested:
                return False
            if not self._is_plc_ok():
                self._log("[VACUUM] 압력 대기 중 PLC 끊김 감지")
                self.request_abort("PLC 통신 끊김")
                return False

            pressure = self._current_pressure()
            now = time.monotonic()

            # 5초마다 진행상황 로그
            if now - last_log >= 5.0:
                p_text = f"{pressure:.3e} Torr" if pressure is not None else "--- (읽기 실패)"
                remaining = deadline - now
                self._log(
                    f"[VACUUM]   압력: {p_text} | 목표: {target:.1e} Torr | "
                    f"남은 시간: {remaining:.0f}초"
                )
                last_log = now

            if pressure is not None and pressure <= target:
                self._log(f"[VACUUM]   목표 압력 도달: {pressure:.3e} Torr ✅")
                return True

            time.sleep(PRESSURE_POLL_S)

        return False  # timeout

    # ── 헬퍼: 현재 압력 ──────────────────────────────────────────
    def _current_pressure(self) -> Optional[float]:
        if self._acs is None:
            return None
        try:
            snap = self._acs.get_last_snapshot()
            if snap is None:
                return None
            p = snap.pressure if hasattr(snap, "pressure") else snap.get("pressure", None)
            return float(p) if p is not None else None
        except Exception:
            return None

    # ── 헬퍼: PLC 코일 쓰기 ──────────────────────────────────────
    def _write_and_verify(
        self, coil: str, on: bool, label: str, *, ignore_abort: bool = False
    ) -> tuple[bool, str]:
        # 1. 명령 전송
        try:
            fut = self._binder.submit_write(coil, on, tag=f"VACUUM:{coil}")
            if fut is not None and hasattr(fut, "result"):
                fut.result(timeout=PLC_WRITE_TIMEOUT_S)
            self._log(f"[VACUUM]   {label} ({coil} = {'ON' if on else 'OFF'}) 전송 완료")
        except Exception as e:
            return False, repr(e)

        # 2. 3초 대기 (밸브 물리적 동작 시간)
        #    ignore_abort=True 면 abort 중에도 안정 대기를 끝까지 수행 (복구 경로 전용)
        deadline = time.monotonic() + STEP_SETTLE_S
        while time.monotonic() < deadline:
            if self._abort_requested and not ignore_abort:
                return False, "중단 요청"
            time.sleep(SETTLE_TICK_S)

        # 3. PLC 직접 읽어서 확인
        actual = self._read_coil(coil)
        if actual != on:
            return False, f"{coil} 상태 불일치 (기대: {'ON' if on else 'OFF'}, 실제: {'ON' if actual else 'OFF'})"

        self._log(f"[VACUUM]   {label} ({coil} = {'ON' if on else 'OFF'}) 확인 ✅")
        return True, ""

    # ── 헬퍼: PLC 코일 읽기 ──────────────────────────────────────
    def _read_coil(self, coil: str) -> bool:
        try:
            return bool(self._binder.read_coil(coil, False))
        except Exception:
            return False

    # ── 헬퍼: PLC 연결 확인 ───────────────────────────────────────
    def _is_plc_ok(self) -> bool:
        try:
            return bool(self._binder.is_connected())
        except Exception:
            return False

    # ── 헬퍼: 중단 체크 ──────────────────────────────────────────
    def _check_abort(self) -> bool:
        if self._abort_requested:
            reason = self._stop_reason or "사용자 중단"
            self._log(f"[VACUUM] 중단 감지: {reason} → 시퀀스 종료")
            self.sig_done.emit(False, reason)
            return True
        return False

    # ── 헬퍼: Step 2~5 실패 시 복구 (Rough 구간) ─────────────────
    def _restore_rough_phase(self) -> None:
        """PLC 실제 상태를 읽어서 안전 순서로 밸브 복구.
        목표: M/V=OFF, R/V=OFF, F/V=ON

        주의: abort 요청 직후에도 호출되므로, 내부 밸브 쓰기는
        ignore_abort=True 로 안정 대기/검증을 끝까지 수행한다.
        """
        try:
            # 1. M/V 먼저 닫기 (F/V 닫힌 상태에서 M/V ON 금지 규칙)
            if self._read_coil("M_V_SW"):
                self._log("[VACUUM] 복구: M/V 닫기")
                ok, err = self._write_and_verify(
                    "M_V_SW", False, "복구: M/V 닫기", ignore_abort=True
                )
                if not ok:
                    self._log(f"[VACUUM][WARN] 복구 M/V 닫기 검증 실패: {err}")

            # 2. R/V 닫기 (F/V 열기 전에 반드시 먼저)
            if self._read_coil("R_V_SW"):
                self._log("[VACUUM] 복구: R/V 닫기")
                ok, err = self._write_and_verify(
                    "R_V_SW", False, "복구: R/V 닫기", ignore_abort=True
                )
                if not ok:
                    self._log(f"[VACUUM][WARN] 복구 R/V 닫기 검증 실패: {err}")

            # 3. F/V 열기
            if not self._read_coil("F_V_SW"):
                self._log("[VACUUM] 복구: F/V 열기")
                ok, err = self._write_and_verify(
                    "F_V_SW", True, "복구: F/V 열기", ignore_abort=True
                )
                if not ok:
                    self._log(f"[VACUUM][WARN] 복구 F/V 열기 검증 실패: {err}")
        except Exception as e:
            self._log(f"[VACUUM][WARN] 복구 중 예외: {e!r}")

    # ── 헬퍼: Step 6~7 실패 시 복구 (Fine 구간) ──────────────────
    def _restore_fine_phase(self) -> None:
        """Fine 구간(F/V·M/V 열기) 실패/중단 시 복구.
        모두 정상 ON이면 그대로 유지, 아니면 Rough 구간 복구로 위임.
        """
        try:
            rp  = self._read_coil("R_P_SW")
            fv  = self._read_coil("F_V_SW")
            tmp = self._read_coil("TMP_SW")
            mv  = self._read_coil("M_V_SW")
            self._log(f"[VACUUM] 복구 확인: R/P={rp}, F/V={fv}, TMP={tmp}, M/V={mv}")
            if rp and fv and tmp and mv:
                self._log("[VACUUM] 복구: R/P·F/V·TMP·M/V 모두 ON → 그대로 유지")
                return
            self._restore_rough_phase()
        except Exception as e:
            self._log(f"[VACUUM][WARN] Fine 구간 복구 중 예외: {e!r}")

    # ── 헬퍼: 실패 처리 ──────────────────────────────────────────
    def _fail(self, reason: str) -> None:
        self._log(f"[VACUUM][FAIL] {reason}")
        self.sig_done.emit(False, reason)

    # ── 헬퍼: 로그 ───────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        try:
            self.sig_log.emit(str(msg))
        except Exception:
            pass