# -*- coding: utf-8 -*-
"""
process/engine.py

ProcessEngine
- ProcessRecipe(모델) 기반으로 공정을 순차 실행하는 상태 머신(엔진)
- UI/장비 객체를 직접 다루지 않고, services(PLC/STM/ACS/Log)를 통해서만 동작

의존:
- process/models.py  : Recipe/Step/Status/Enums
- services/plc_service.py : PLCService (submit_* 지원)
- services/stm_service.py : STMService (get_last_snapshot)
- services/acs_service.py : ACSService (get_last_snapshot)
- services/log_service.py : LogService (open_run/telemetry/log)

엔진의 실행 방식:
- 엔진은 "블로킹" 실행(run())을 제공
- UI 스레드를 막지 않기 위해, 보통 QThread에서 run()을 호출하는 래퍼(worker)를 둔다.
"""

from __future__ import annotations

import contextlib
import time
import uuid
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from process.models import (
    ProcessRecipe,
    ProcessStep,
    StepType,
    ProcessPhase,
    StopMode,
    OnTimeout,
    ProcessStatus,
    StepStatus,
    ProcessError,
)

from services.plc_service import PLCService, PLCSnapshot
from services.stm_service import STMService, STMSnapshot
from services.acs_service import ACSService, ACSSnapshot
from services.log_service import LogService


# ============================================================
# Exceptions (엔진 내부 전용)
# ============================================================

class EngineStopRequested(Exception):
    def __init__(self, mode: StopMode):
        super().__init__(f"stop requested: {mode}")
        self.mode = mode


class EngineTimeout(Exception):
    def __init__(self, step_name: str, detail: str):
        super().__init__(f"timeout at step={step_name}: {detail}")
        self.step_name = step_name
        self.detail = detail


class EngineFailed(Exception):
    def __init__(self, step_name: str, detail: str):
        super().__init__(f"failed at step={step_name}: {detail}")
        self.step_name = step_name
        self.detail = detail


# ============================================================
# Callbacks (UI/Controller가 구독할 훅)
# ============================================================

@dataclass
class EngineCallbacks:
    """
    엔진이 상태/스텝 진행 상황을 외부(UI/컨트롤러)로 전달하는 콜백.

    - 콜백은 반드시 예외를 삼켜야 하므로 엔진에서 try/except로 감싼다.
    - UI는 보통 process_worker(QThread)에서 signal로 브릿지하게 됨.
    """
    on_status: Optional[Callable[[ProcessStatus], None]] = None
    on_step: Optional[Callable[[StepStatus], None]] = None
    on_error: Optional[Callable[[ProcessError], None]] = None


# ============================================================
# Engine Result
# ============================================================

@dataclass
class EngineResult:
    ok: bool
    run_id: str
    recipe_name: str
    started_ts: float
    finished_ts: float
    error: Optional[ProcessError] = None


# ============================================================
# Engine
# ============================================================

class ProcessEngine:
    """
    공정 엔진.

    사용 예(개념):
        engine = ProcessEngine(plc, stm, acs, log, callbacks=EngineCallbacks(...))
        result = engine.run(recipe)
    """

    def __init__(
        self,
        plc: PLCService,
        stm: Optional[STMService],
        acs: Optional[ACSService],
        log: LogService,
        *,
        callbacks: Optional[EngineCallbacks] = None,
        plc_cmd_timeout_s: float = 2.0,
        status_emit_interval_s: float = 0.2,
    ):
        self.plc = plc
        self.stm = stm
        self.acs = acs
        self.log = log

        self.callbacks = callbacks or EngineCallbacks()

        self._plc_cmd_timeout_s = max(0.2, float(plc_cmd_timeout_s))
        self._status_emit_interval_s = max(0.05, float(status_emit_interval_s))

        # run control flags
        self._stop_mode: Optional[StopMode] = None
        self._paused: bool = False

        # 내부 상태
        self._phase: ProcessPhase = ProcessPhase.IDLE
        self._run_id: str = ""
        self._recipe_name: str = ""
        self._started_ts: float = 0.0
        self._current_step_idx: int = -1
        self._current_step_name: str = ""

        # last status emit timestamps
        self._last_status_emit_ts: float = 0.0
        self._last_telemetry_ts: float = 0.0

        self._last_dac_power_1: int = 0
        self._last_dac_power_2: int = 0

        # ✅ UI 표시용: 마지막 메시지 캐시 (tick emit이 message=""로 덮는 문제 방지)
        self._ui_last_message: str = ""

    # --------------------------------------------------------
    # External controls (thread-safe-ish: 단순 플래그)
    # --------------------------------------------------------
    def request_stop(self, mode: StopMode = StopMode.STOP) -> None:
        self._stop_mode = mode

    def request_pause(self) -> None:
        self._paused = True

    def request_resume(self) -> None:
        self._paused = False

    # --------------------------------------------------------
    # Public entry
    # --------------------------------------------------------
    def run(self, recipe: ProcessRecipe, *, run_id: Optional[str] = None) -> EngineResult:
        """
        블로킹 실행. 보통 QThread에서 호출해야 UI가 멈추지 않음.
        """
        # 기본 검증(여기서 한번 더)
        recipe.validate(strict=True)

        self._stop_mode = None
        self._paused = False

        self._phase = ProcessPhase.RUNNING
        self._recipe_name = recipe.recipe_name
        self._run_id = run_id or self._make_run_id()
        self._started_ts = time.time()
        self._current_step_idx = -1
        self._current_step_name = ""
        self._last_status_emit_ts = 0.0
        self._last_telemetry_ts = 0.0

        # run open
        try:
            self.log.open_run(
                run_id=self._run_id,
                recipe_name=self._recipe_name,
                meta={
                    "started_at": self._ts_str(self._started_ts),
                    "telemetry_interval_s": float(getattr(recipe, "telemetry_interval_s", 1.0) or 1.0),
                    # recipe.meta는 너가 ProcessController에서 작게 넣어두는 값만 있으니 부담 적음
                    "recipe_meta": dict(getattr(recipe, "meta", {}) or {}),
                },
            )
        except Exception as e:
            # 로그가 죽어도 공정 자체는 진행할 수 있게(단, 운영에서는 로그가 매우 중요하니 에러 출력)
            self._log_error(f"open_run failed: {e!r}", tag="ENGINE", also_ui=True)

        self._emit_status(message="공정 시작")

        err_obj: Optional[ProcessError] = None
        ok = False

        try:
            for idx, step in enumerate(recipe.steps):
                self._current_step_idx = idx
                self._current_step_name = step.name

                self._check_stop_pause(recipe, step)

                self._emit_step(StepStatus(idx=idx, name=step.name, type=step.type, started_ts=time.time(), ok=None))
                self._emit_status(step_idx=idx, step_name=step.name, message=f"STEP START: {step.type.value}")

                # step 실행
                self._execute_step(recipe, step)

                self._emit_step(StepStatus(idx=idx, name=step.name, type=step.type, finished_ts=time.time(), ok=True))
                self._emit_status(step_idx=idx, step_name=step.name, message="STEP OK")

            ok = True
            self._phase = ProcessPhase.FINISHED
            self._emit_status(message="공정 완료")

        except EngineStopRequested as e:
            # STOP/ABORT 요청
            self._phase = ProcessPhase.STOPPING
            self._emit_status(message=f"정지 요청 처리중: {e.mode.value}")
            self._log_warn(f"Stop requested: {e.mode.value}", tag="ENGINE", also_ui=True)

            # ✅ EV 안전정지 시퀀스 통일 (셔터닫기 → DAC0 → PowerOff)
            self._safe_shutdown_sequence(tag=f"STOP_{e.mode.value}")

            self._phase = ProcessPhase.FINISHED
            self._emit_status(message="정지 종료")
            ok = False

        except Exception as e:
            self._phase = ProcessPhase.ERROR
            err_obj = ProcessError(
                where=f"step[{self._current_step_idx}] {self._current_step_name}",
                message=str(e),
                exception_repr=repr(e),
            )
            self._emit_error(err_obj)
            self._emit_status(message=f"에러: {e!r}")
            self._log_error(f"ENGINE ERROR: {e!r}", tag="ENGINE", also_ui=True)

            # ✅ 에러 시에도 동일한 안전정지 시퀀스
            self._safe_shutdown_sequence(tag="ERROR_ABORT")
            ok = False

        finally:
            finished_ts = time.time()
            try:
                self.log.close_run()
            except Exception:
                pass

            # 엔진 상태 마무리
            if self._phase not in (ProcessPhase.FINISHED, ProcessPhase.ERROR):
                self._phase = ProcessPhase.FINISHED if ok else ProcessPhase.ERROR

            return EngineResult(
                ok=ok,
                run_id=self._run_id,
                recipe_name=self._recipe_name,
                started_ts=self._started_ts,
                finished_ts=finished_ts,
                error=err_obj,
            )

    # --------------------------------------------------------
    # Step execution
    # --------------------------------------------------------
    def _execute_step(self, recipe: ProcessRecipe, step: ProcessStep) -> None:
        """
        step.type에 따라 동작 수행.
        """
        t = step.type

        # 항상 주기적으로 status/telemetry emit
        self._tick_emit(recipe, step)

        if t == StepType.LOG:
            self._log_info(step.message, tag="PROCESS", also_ui=True)
            return

        if t == StepType.MARK:
            # ✅ EVAP 증착 제어 루프(특수 MARK)
            # - process_controller에서 step.name="EVAP_DEPOSITION_CONTROL" 로 넣어주면 여기로 들어온다.
            if step.name == "EVAP_DEPOSITION_CONTROL":
                self._evap_deposition_control(recipe, step)  # ✅ 함수명 일치
                return

            # UI trace용 마커(기본)
            self._log_info(f"MARK: {step.name}", tag="PROCESS", also_ui=False)
            return

        # ---------- PLC actions ----------
        if t == StepType.PLC_WRITE_COIL:
            coil = str(step.coil or "")
            on = bool(step.on)
            # ✅ 스텝별 짧은 표시
            self._emit_status(step_idx=self._current_step_idx, step_name=self._current_step_name,
                            message=self._ui_coil_text(coil, on), force=True)
            self._plc_write_coil(coil, on, tag=step.name)
            return

        if t == StepType.PLC_PULSE_COIL:
            coil = str(step.coil or "")
            ms = int(step.pulse_ms or 200)

            # ✅ 펄스 스텝도 상단에 짧게 표시
            self._emit_status(
                step_idx=self._current_step_idx,
                step_name=self._current_step_name,
                message=self._ui_pulse_text(coil, ms),
                force=True,
            )

            # momentary pulse(ON 펄스)로 통일
            self._plc_pulse_coil(coil, ms, tag=step.name)
            return

        if t == StepType.PLC_WRITE_REG:
            reg = str(step.reg or "")
            v = int(step.value or 0)
            # ✅ 스텝별 짧은 표시
            self._emit_status(step_idx=self._current_step_idx, step_name=self._current_step_name,
                            message=self._ui_reg_text(reg, v), force=True)
            self._plc_write_reg(reg, v, tag=step.name)
            return

        if t == StepType.PLC_SET_DAC_MA:
            self._plc_set_dac_ma(int(step.dac_ch or 1), float(step.dac_ma or 4.0), tag=step.name)
            return

        # ---------- WAIT actions ----------
        if t == StepType.WAIT_SECONDS:
            sec = float(step.seconds or 0.0)
            self._wait_seconds(recipe, step, sec)
            return

        if t == StepType.WAIT_PRESSURE_LEQ:
            target = float(step.pressure_target)
            self._wait_condition(
                recipe=recipe,
                step=step,
                cond_fn=lambda: self._get_pressure_ok_leq(target),
                cond_desc=f"압력 ≤ {target:g}",
                # ✅ 압력도 표시하고 싶으면 sensor_value_fn을 넣되,
                #    압력계가 순간 None이어도 바로 실패하지 않게 abort는 꺼둠(0.0)
                sensor_value_fn=self._get_pressure,
                sensor_label="압력",
                sensor_missing_abort_s=0.0,
            )
            return

        if t == StepType.WAIT_THICKNESS_GEQ:
            raw = getattr(step, "thickness_target_a", None)
            if raw is None:
                raw = getattr(step, "thickness_target", None)

            if raw is None:
                raise EngineFailed(step.name, "WAIT_THICKNESS_GEQ requires thickness_target_a(thickness_target)")

            target = float(raw)

            self._wait_condition(
                recipe=recipe,
                step=step,
                cond_fn=lambda: self._get_thickness_ok_geq(target),
                cond_desc=f"thickness >= {target:g} Å",
                sensor_value_fn=self._get_thickness,
                sensor_label="STM thickness",
            )
            return

        if t == StepType.WAIT_RATE_IN_RANGE:
            mn = float(step.rate_min_a_s)
            mx = float(step.rate_max_a_s)
            self._wait_condition(
                recipe=recipe,
                step=step,
                cond_fn=lambda: self._get_rate_ok_in_range(mn, mx),
                cond_desc=f"dep.rate [{mn}, {mx}] Å/s",
                sensor_value_fn=self._get_rate,   # ✅ 추가
                sensor_label="STM dep.rate",          # ✅ 추가
            )
            return

        if t == StepType.WAIT_COIL_IS:
            coil = str(step.coil)
            exp = bool(step.expected)
            self._wait_condition(
                recipe=recipe,
                step=step,
                cond_fn=lambda: self._get_coil_is(coil, exp),
                cond_desc=f"coil {coil} == {'ON' if exp else 'OFF'}",
            )
            return

        raise EngineFailed(step.name, f"unsupported step type: {t}")
    
    # ------------------------------------------------------------
    # STM helper (material params / zero)
    # ------------------------------------------------------------
    def _stm_wait_connected(self, recipe: ProcessRecipe, step: ProcessStep, *, timeout_s: float = 5.0) -> None:
        """STMService가 connected=True가 될 때까지 대기(카운트다운 표시)."""
        if self.stm is None:
            raise EngineFailed(step.name, "STM 서비스가 주입되지 않았습니다(stm=None).")

        start_m = time.monotonic()
        deadline_m = start_m + float(timeout_s)
        next_ui_m = start_m

        while True:
            self._check_stop_pause(recipe, step)
            self._tick_emit(recipe, step)

            now_m = time.monotonic()
            remain_s = deadline_m - now_m
            if remain_s <= 0:
                break

            snap = self._get_stm_snapshot()
            ok = bool(snap is not None and bool(getattr(snap, "connected", False)))

            # ✅ 1초마다 카운트다운 표시
            if now_m >= next_ui_m:
                self._emit_status(
                    message=f"[남은 {self._fmt_hms(remain_s, ceil=True)}] STM 연결 대기",
                    force=True,
                )
                next_ui_m = now_m + 1.0

            if ok:
                self._emit_status(message="[남은 00:00] STM 연결 완료", force=True)
                return

            time.sleep(0.1)

        raise EngineFailed(step.name, f"STM 연결 대기 timeout: {timeout_s:.1f}s")

    def _stm_apply_material_params(
        self,
        recipe: ProcessRecipe,
        step: ProcessStep,
        *,
        density_g_cm3: float,
        z_factor: float,
    ) -> None:
        """STM-100에 density/z-factor 적용 (필요 시)."""
        if self.stm is None:
            return

        self._stm_wait_connected(recipe, step, timeout_s=5.0)

        fut = self.stm.submit_apply_material_params(
            density_g_cm3=float(density_g_cm3),
            z_factor=float(z_factor),
            film_no=None,
            do_zero_thickness=False,
        )
        self._wait_future(fut, timeout_s=5.0, where=f"{step.name}/STM_APPLY", msg="STM film params 적용 실패")
        self._tele_event(event="STM_APPLY", target="FILM_PARAM", value="", detail=f"density={density_g_cm3}, z={z_factor}")


    def _stm_zero_thickness(self, recipe: ProcessRecipe, step: ProcessStep, *, mode: str = "B") -> None:
        """STM-100 thickness/time zero. mode='B' 권장(두께+타이머)."""
        if self.stm is None:
            return

        self._stm_wait_connected(recipe, step, timeout_s=5.0)

        fut = self.stm.submit_zero_thickness(mode=str(mode))
        self._wait_future(fut, timeout_s=5.0, where=f"{step.name}/STM_ZERO", msg="STM zero 실패")
        self._tele_event(event="STM_ZERO", target="THICKNESS", value=str(mode), detail="zero thickness/time")


    # --------------------------------------------------------
    # EVAP deposition control
    # --------------------------------------------------------
    def _evap_deposition_control(self, recipe: ProcessRecipe, step: ProcessStep) -> None:
        """
        EVAP 증착 제어(사용자 요구 공정 순서 반영)

        전제(레시피/상위 단계에서 완료되어야 함)
        - POWER_1_SW / POWER_2_SW는 이미 ON/OFF 상태가 결정되어 있음
        - MAIN_SHUTTER_SW는 닫혀 있는 상태에서 시작(필요 시 여기서 한 번 더 close)
        - FTM_SW, SOURCE_SHUTTER 등은 상위 레시피 단계에서 켜는 것을 권장

        순서(요청 사양)
        1) (옵션) STM film params(density/z) 적용
        2) DAC를 1초마다 +100씩 ramp-up 하며 dep.rate가 pre_rate(기본 0.4 Å/s) 도달
        3) pre_rate에서 pre_hold_s(기본 120s) 유지
        4) target_rate까지 ramp-up 후 ±5% 이내 도달하면 delay(=delay_min) 시작
        5) delay 종료 시 STM thickness zero + MAIN_SHUTTER open
        6) 증착 중 target_rate 유지(미세 DAC 조정), dep.rate가 70% 이상 급감하면 중단
        7) target_thickness 도달 시 MAIN_SHUTTER close + POWER off
        """

        meta = dict(step.meta or {})

        use_p1 = bool(meta.get("use_power1", False))
        use_p2 = bool(meta.get("use_power2", False))

        # ✅ power는 1개 또는 2개 허용(둘 다 False만 금지)
        if not (use_p1 or use_p2):
            raise EngineFailed(step.name, "EVAP: use_power1/use_power2 중 최소 1개는 True여야 합니다.")

        target_rate = float(meta.get("target_rate", 0.0) or 0.0)        # Å/s
        target_th = float(meta.get("target_thickness", 0.0) or 0.0)     # Å
        delay_min = float(meta.get("delay_min", 0.0) or 0.0)            # min

        if target_rate <= 0:
            raise EngineFailed(step.name, "EVAP: target_rate must be > 0")
        if target_th <= 0:
            raise EngineFailed(step.name, "EVAP: target_thickness must be > 0")
        if delay_min < 0:
            raise EngineFailed(step.name, "EVAP: delay_min must be >= 0")

        # --- 공정 파라미터(기본값은 요구사항 기준) ---
        pre_rate = float(meta.get("pre_rate", 0.4) or 0.4)                  # Å/s
        pre_hold_s = float(meta.get("pre_hold_s", 120.0) or 120.0)          # s

        # ✅ pre_rate 도달 후 대기(hold) 동작
        # - fixed  : dep.rate가 pre_rate 도달하면 '그 DAC 그대로' 1~2분 대기(파워 흔들림 방지)
        # - control: pre_rate 근처로 유지 제어를 하되, DAC 변경 텀을 둠
        pre_hold_mode = str(meta.get("pre_hold_mode", "fixed") or "fixed").strip().lower()
        if pre_hold_mode not in ("fixed", "control"):
            pre_hold_mode = "fixed"

        # ✅ dep.rate 도달 이후 파워 변화가 너무 빠르지 않도록 DAC 변경 최소 간격(초)
        pre_hold_adjust_interval_s = max(0.1, float(meta.get("pre_hold_adjust_interval_s", 10.0) or 10.0))
        dac_adjust_interval_s = max(0.1, float(meta.get("dac_adjust_interval_s", 10.0) or 10.0))

        rate_tol_ratio = float(meta.get("rate_tol_ratio", 0.05) or 0.05)    # ±5%
        pre_tol_ratio = float(meta.get("pre_tol_ratio", 0.10) or 0.10)      # pre_rate는 조금 넓게

        ramp_step_dac = int(meta.get("ramp_step_dac", 100) or 100)

        # ✅ 사용자 요구: DAC 구간별 램프 템포
        ramp_seg1_max_dac = int(meta.get("ramp_seg1_max_dac", 700) or 700)
        ramp_seg2_max_dac = int(meta.get("ramp_seg2_max_dac", 1500) or 1500)
        ramp_interval_seg1_s = float(meta.get("ramp_interval_seg1_s", 10.0) or 10.0)
        ramp_interval_seg2_s = float(meta.get("ramp_interval_seg2_s", 30.0) or 30.0)
        ramp_interval_after_seg2_s = float(meta.get("ramp_interval_after_seg2_s", ramp_interval_seg2_s) or ramp_interval_seg2_s)

        # ✅ DAC=1500인데 dep.rate==0이면, dep.rate>=0.1 될 때까지 대기(최대 5분)
        ignite_dac = int(meta.get("ignite_dac", ramp_seg2_max_dac) or ramp_seg2_max_dac)
        ignite_trigger_rate_max = float(meta.get("ignite_trigger_rate_max", 0.0) or 0.0)
        ignite_rate_min = float(meta.get("ignite_rate_min", 0.1) or 0.1)
        ignite_timeout_s = float(meta.get("ignite_timeout_s", 300.0) or 300.0)

        # ✅ 미세 조정(목표 근접 시): 기본 10 step 이내에서 동적 보정
        fine_step_dac = int(meta.get("fine_step_dac", 10) or 10)

        # ✅ ignite 안정화 판정(연속 N회, 기본 3회)
        ignite_stable_hits = int(meta.get("ignite_stable_hits", 3) or 3)
        ignite_stable_interval_s = max(0.1, float(meta.get("ignite_stable_interval_s", 1.0) or 1.0))

        # ✅ pre_rate 도달 판정(연속 N회, 기본 3회) — 스파이크 1번으로 ramp 종료되는 것 방지
        pre_stable_hits = int(meta.get("pre_stable_hits", 3) or 3)
        pre_stable_interval_s = max(0.1, float(meta.get("pre_stable_interval_s", 1.0) or 1.0))

        # ✅ target_ramp 종료 판정(연속 N회, 기본 3회) — 스파이크 1번으로 target_ramp 탈출 방지
        target_ramp_stable_hits = int(meta.get("target_ramp_stable_hits", 3) or 3)
        target_ramp_stable_interval_s = max(0.1, float(meta.get("target_ramp_stable_interval_s", 1.0) or 1.0))

        # ✅ target_rate 도달 판정: tol 이내 N회 연속 (기본 3회로 변경)
        target_stable_hits = int(meta.get("target_stable_hits", 3) or 3)
        target_stable_interval_s = max(0.1, float(meta.get("target_stable_interval_s", 1.0) or 1.0))

        rate_drop_ratio = float(meta.get("rate_drop_ratio", 0.30) or 0.30)  # 70% 급감 => 30% 이하
        rate_drop_count = int(meta.get("rate_drop_count", 3) or 3)

        dac_max = int(meta.get("dac_max", 4000) or 4000)
        sensor_none_abort_s = float(meta.get("sensor_none_abort_s", 5.0) or 5.0)

        # ✅ stuck guard(“rate가 안 오르는데 DAC만 계속 올리는 것” 방지)
        stuck_dac_guard = int(meta.get("stuck_dac_guard", 1500) or 1500)  # 이 이상 DAC인데도
        stuck_rate_abs = float(meta.get("stuck_rate_abs", 0.05) or 0.05)  # rate가 이 값 미만이면
        stuck_time_s = float(meta.get("stuck_time_s", 60.0) or 60.0)      # 이 시간 지속 시 중단

        # ✅ material shortage guard (요구사항)
        # - DAC가 2000 이상인데도 dep.rate가 0 이하이면 "물질 부족"으로 중단
        material_shortage_dac = int(meta.get("material_shortage_dac", 2000) or 2000)
        material_shortage_rate_max = float(meta.get("material_shortage_rate_max", 0.0) or 0.0)  # <=
        # "계속"을 엄밀히 하려면 지속시간을 쓰면 됨(초). 10이면 10초 대기 후 중단.
        material_shortage_time_s = float(meta.get("material_shortage_time_s", 10.0) or 10.0)

        # delay는 분 단위 입력(기존 UI) → 초로 변환
        delay_s = delay_min * 60.0

        # 0) MAIN_SHUTTER는 닫힌 상태에서 시작(한 번 더 안전 close)
        self._plc_write_coil("MAIN_SHUTTER_SW", False, tag="EVAP_INIT")

        # 1) STM 연결 확인
        self._stm_wait_connected(recipe, step, timeout_s=5.0)

        # 1-1) (옵션) film params 적용
        density = float(meta.get("density", 0.0) or 0.0)
        z_factor = float(meta.get("z_factor", 0.0) or 0.0)
        if density > 0 and z_factor > 0:
            self._emit_status(message=f"STM film params 적용: density={density}, z={z_factor}")
            self._stm_apply_material_params(recipe, step, density_g_cm3=density, z_factor=z_factor)

        # 2) 초기 DAC: 항상 0부터 시작
        dac = 0

        # ✅ material shortage 타이머(전 구간 공통)
        shortage_start_ts: Optional[float] = None

        # --- 내부 유틸 ---
        def _sleep_with_checks(total_s: float, *, where: str = "sleep") -> None:
            nonlocal shortage_start_ts
            total_s = float(total_s)
            end_t = time.monotonic() + total_s

            # ✅ 1초마다 카운트다운 표시(짧은 sleep은 생략 가능)
            next_ui_m = time.monotonic()
            show_ui = total_s >= 1.0

            while True:
                self._check_stop_pause(recipe, step)
                self._tick_emit(recipe, step)

                # ✅ sleep 중에도 rate를 읽어 material shortage 감시(10초 카운트가 정확해짐)
                rt = self._get_rate()
                if rt is not None:
                    shortage_start_ts = _material_shortage_guard(float(rt), shortage_start_ts, where=where)

                now = time.monotonic()
                remain = end_t - now
                if remain <= 0:
                    return

                if show_ui and now >= next_ui_m:
                    self._emit_status(
                        message=f"[남은 {self._fmt_hms(remain, ceil=True)}] 대기중 ({where})",
                        force=True,
                    )
                    next_ui_m = now + 1.0

                time.sleep(min(0.1, remain))

        ignite_wait_active: bool = False
        ignite_wait_start_m: Optional[float] = None
        ignite_ok_hits: int = 0  # ✅ rate>=ignite_rate_min 연속 카운트
        ignite_next_ui_m: float = 0.0  # ✅ ignite 대기 카운트다운 UI 갱신 타이밍

        def _handle_ignite_wait(rt: float, *, where: str) -> bool:
            nonlocal ignite_wait_active, ignite_wait_start_m, ignite_ok_hits, ignite_next_ui_m

            if int(dac) != int(ignite_dac):
                ignite_wait_active = False
                ignite_wait_start_m = None
                ignite_ok_hits = 0
                ignite_next_ui_m = 0.0
                return False

            rtf = float(rt)

            # ✅ 이미 대기 상태면: rate>=ignite_rate_min 을 N회 연속 확인할 때까지 계속 유지
            if ignite_wait_active:
                if rtf >= float(ignite_rate_min):
                    ignite_ok_hits += 1
                else:
                    ignite_ok_hits = 0

                now_m = time.monotonic()
                if ignite_wait_start_m is not None and now_m >= ignite_next_ui_m:
                    remain_s = max(0.0, float(ignite_timeout_s) - (now_m - float(ignite_wait_start_m)))
                    self._emit_status(
                        message=(
                            f"[남은 {self._fmt_hms(remain_s, ceil=True)}] IGNITE WAIT | "
                            f"DAC={dac} | rate={rtf:.3f} | ok {ignite_ok_hits}/{ignite_stable_hits}"
                        ),
                        force=True,
                    )
                    ignite_next_ui_m = now_m + 1.0

                if ignite_ok_hits >= max(1, int(ignite_stable_hits)):
                    ignite_wait_active = False
                    ignite_wait_start_m = None
                    ignite_ok_hits = 0
                    ignite_next_ui_m = 0.0
                    self._emit_status(message=f"IGNITE OK: rate={rtf:.3f} (hits={ignite_stable_hits})", force=True)
                    return False

                if ignite_wait_start_m is not None and (now_m - float(ignite_wait_start_m)) >= float(ignite_timeout_s):
                    raise EngineFailed(step.name, f"IGNITE WAIT TIMEOUT: DAC={dac}, rate={rtf:.3f} < {ignite_rate_min:.3f}")

                return True

            # ✅ 대기 시작 조건(요구사항: rate==0일 때)
            if rtf <= float(ignite_trigger_rate_max):
                ignite_wait_active = True
                ignite_wait_start_m = time.monotonic()
                ignite_ok_hits = 0
                ignite_next_ui_m = ignite_wait_start_m  # 시작 즉시 표시되게
                self._emit_status(
                    message=(
                        f"[남은 {self._fmt_hms(float(ignite_timeout_s), ceil=True)}] IGNITE WAIT START | "
                        f"DAC={dac} rate={rtf:.3f} → rate>={ignite_rate_min:.3f} hits={ignite_stable_hits}"
                    ),
                    force=True,
                )
                return True

            return False

        def _ramp_interval_by_dac(cur_dac: int) -> float:
            d = int(cur_dac)
            if d < int(ramp_seg1_max_dac):
                return float(ramp_interval_seg1_s)          # 0~700 : 10s
            if d < int(ramp_seg2_max_dac):
                return float(ramp_interval_seg2_s)          # 700~1500 : 30s
            return float(ramp_interval_after_seg2_s)        # 1500 이후 : 30s(기본)
        
        def _dac_min_interval_by_dac(cur_dac: int) -> float:
            # 기존 dac_adjust_interval_s도 존중하되,
            # 구간 규칙보다 더 빠르게는 못 움직이게
            return max(float(dac_adjust_interval_s), _ramp_interval_by_dac(cur_dac))

        def _ramp_sleep_by_dac(cur_dac: int, *, where: str) -> None:
            _sleep_with_checks(_ramp_interval_by_dac(cur_dac), where=where)

        def _in_band(rt: float, tgt: float, *, tol_ratio: float) -> bool:
            tol = max(1e-9, abs(float(tgt)) * float(tol_ratio))
            return abs(float(rt) - float(tgt)) <= tol

        def read_rate_or_abort(*, where: str) -> float:
            nonlocal shortage_start_ts
            t0 = time.monotonic()
            while True:
                self._check_stop_pause(recipe, step)
                self._tick_emit(recipe, step)

                rt = self._get_rate()
                if rt is not None:
                    rt_f = float(rt)

                    # ✅ 모든 공정 구간에서 material shortage 감시
                    shortage_start_ts = _material_shortage_guard(rt_f, shortage_start_ts, where=where)

                    return rt_f

                if (time.monotonic() - t0) >= sensor_none_abort_s:
                    raise EngineFailed(step.name, f"EVAP: rate 센서 None 지속 {sensor_none_abort_s}s")

                time.sleep(0.1)

        def read_th_or_abort() -> float:
            t0 = time.monotonic()
            while True:
                self._check_stop_pause(recipe, step)
                self._tick_emit(recipe, step)

                th = self._get_thickness()
                if th is not None:
                    return float(th)

                if (time.monotonic() - t0) >= sensor_none_abort_s:
                    raise EngineFailed(step.name, f"EVAP: thickness 센서 None 지속 {sensor_none_abort_s}s")

                time.sleep(0.1)

        # ✅ material shortage guard helper
        def _material_shortage_guard(rt: float, start_ts: Optional[float], *, where: str) -> Optional[float]:
            """
            DAC가 충분히 올라갔는데(dep.rate <= 0)면 물질 부족으로 중단.
            - material_shortage_time_s == 0 이면 즉시 중단
            - > 0 이면 그 시간만큼 조건이 '연속' 유지될 때 중단
            """
            if int(dac) < int(material_shortage_dac):
                return None

            if float(rt) <= float(material_shortage_rate_max):
                now = time.monotonic()

                if float(material_shortage_time_s) <= 0:
                    raise EngineFailed(
                        step.name,
                        f"EVAP: 물질 부족 의심({where}) - DAC>={material_shortage_dac} 인데 dep.rate<={material_shortage_rate_max:.3f} "
                        f"(rt={rt:.3f}, dac={dac})"
                    )

                if start_ts is None:
                    return now

                if (now - float(start_ts)) >= float(material_shortage_time_s):
                    raise EngineFailed(
                        step.name,
                        f"EVAP: 물질 부족 의심({where}) - DAC>={material_shortage_dac} 인데 dep.rate<={material_shortage_rate_max:.3f} "
                        f"지속 {material_shortage_time_s:.0f}s (rt={rt:.3f}, dac={dac})"
                    )
                return start_ts

            return None

        last_dac_apply_m: float = 0.0
        pending_dac: Optional[int] = None

        def apply_dac() -> None:
            """즉시 DAC 적용(램프업 등에서 사용) + 마지막 적용 시각 갱신."""
            nonlocal last_dac_apply_m, pending_dac
            self._evap_apply_dac(use_p1, use_p2, dac, tag="EVAP_DAC")
            last_dac_apply_m = time.monotonic()
            pending_dac = None

        def maybe_apply_dac(new_dac: int, *, min_interval_s: float) -> bool:
            """DAC 변경은 너무 잦지 않게(min_interval_s) 제한."""
            nonlocal dac, pending_dac, last_dac_apply_m
            new_dac = int(new_dac)
            if new_dac == int(dac):
                pending_dac = None
                return False

            now_m = time.monotonic()
            if (now_m - float(last_dac_apply_m)) >= float(min_interval_s):
                dac = new_dac
                apply_dac()
                return True

            # 아직 텀이 안 찼으면 "대기중 목표값"만 저장
            pending_dac = new_dac
            return False

        # 3) pre_rate까지 ramp-up (구간별 템포)
        #   - 0~700 : 10초에 +100
        #   - 700~1500 : 30초에 +100
        #   - 1500에서 rate=0이면 rate>=0.1 될 때까지 대기(최대 5분)
        self._emit_status(message=f"EVAP ramp-up 시작: pre_rate={pre_rate} Å/s")
        apply_dac()

        # ramp timeout(무한 루프 방지)
        ramp_timeout_s = float(meta.get("ramp_timeout_s", 600.0) or 600.0)
        t_ramp0 = time.monotonic()

        stuck_start_ts_pre: Optional[float] = None

        # ✅ pre_rate는 "연속 N회" 확인 후 통과(기본 3회)
        pre_ok_hits: int = 0

        while True:
            rt = read_rate_or_abort(where="pre_ramp")

            if rt >= pre_rate:
                pre_ok_hits += 1
                self._emit_status(
                    message=f"PRE RATE CHECK {pre_ok_hits}/{pre_stable_hits} | DAC={dac} | rate={rt:.3f}/{pre_rate:.3f} Å/s",
                    force=True,
                )
                if pre_ok_hits >= max(1, int(pre_stable_hits)):
                    break

                # DAC는 올리지 말고, 다음 샘플(폴링)까지 기다렸다가 재확인
                _sleep_with_checks(pre_stable_interval_s, where="pre_rate_confirm")
                continue
            else:
                pre_ok_hits = 0

            # ✅ [추가] ignite_dac(1500)에서 rate=0이면 0.1 될 때까지 대기(최대 5분)
            if _handle_ignite_wait(rt, where="pre_ramp"):
                # ignite 대기 중에는 stuck 타이머도 리셋(요구사항 충돌 방지)
                stuck_start_ts_pre = None
                _sleep_with_checks(ignite_stable_interval_s, where="ignite_wait/pre_ramp")
                continue

            # ✅ stuck guard(ignite 대기 아닐 때만)
            now = time.monotonic()
            if dac >= stuck_dac_guard and rt < stuck_rate_abs:
                if stuck_start_ts_pre is None:
                    stuck_start_ts_pre = now
                elif (now - stuck_start_ts_pre) >= stuck_time_s:
                    raise EngineFailed(
                        step.name,
                        f"EVAP: rate 상승 없음(stuck/pre) rt={rt:.3f} < {stuck_rate_abs:.3f} "
                        f"@ dac={dac} (>= {stuck_dac_guard}) for {stuck_time_s:.0f}s"
                    )
            else:
                stuck_start_ts_pre = None

            if (now - t_ramp0) > ramp_timeout_s:
                raise EngineFailed(step.name, f"EVAP: pre_rate ramp timeout {ramp_timeout_s}s (rt={rt:.3f})")

            if dac >= dac_max:
                raise EngineFailed(step.name, f"EVAP: DAC_MAX({dac_max}) 도달했지만 pre_rate 미도달 (rt={rt:.3f})")

            # ✅ 램프업(+100) 후 구간별 템포 sleep
            dac = min(dac_max, dac + ramp_step_dac)
            apply_dac()
            self._emit_status(
                message=f"POWER RAMP UP (PRE) / DAC={dac} / rate={rt:.3f}/{pre_rate:.3f} Å/s",
                force=True,
            )
            _ramp_sleep_by_dac(dac, where="pre_ramp_sleep")

        # 4) pre_rate 유지(1~2분)
        # - 요청사항: pre_rate(0.4) 도달 후 1~2분 "대기" → 그 다음 target로 ramp
        if pre_hold_s > 0:
            self._emit_status(message=f"EVAP pre_rate 유지: {pre_hold_s:.0f}s (mode={pre_hold_mode})")
            end_m = time.monotonic() + float(pre_hold_s)
            next_ui_m = time.monotonic()
            while True:
                now_m = time.monotonic()
                remain_s = end_m - now_m
                if remain_s <= 0:
                    break

                rt = read_rate_or_abort(where="pre_hold")

                # control 모드만 pre_rate 근처 유지 제어 (단, DAC 변경 텀 적용)
                if pre_hold_mode == "control":
                    new_dac = self._evap_adjust_dac(
                        dac=dac,
                        rate=rt,
                        target_rate=pre_rate,
                        tol_ratio=pre_tol_ratio,
                        step_up=max(1, fine_step_dac // 2),
                        step_dn=max(1, fine_step_dac // 2),
                        dac_max=dac_max,
                    )
                    maybe_apply_dac(
                        new_dac,
                        min_interval_s=max(float(pre_hold_adjust_interval_s), _ramp_interval_by_dac(max(int(dac), int(new_dac))))
                    )

                # 1초마다 표시
                if now_m >= next_ui_m:
                    self._emit_status(
                        message=(
                            f"EVAP PRE 안정화 대기({pre_hold_mode}) | 남은시간 {self._fmt_hms(remain_s, ceil=True)} | "
                            f"DAC={dac} | rate {rt:.3f}/{pre_rate:.3f} Å/s"
                        ),
                        force=True,  # ✅ interval 때문에 스킵되지 않게 강제 갱신
                    )
                    next_ui_m = now_m + 1.0

                _sleep_with_checks(min(0.5, max(0.0, remain_s)))

        # 5) target_rate까지 ramp-up (일단은 계속 +100)
        self._emit_status(message=f"EVAP target_rate ramp-up: target_rate={target_rate} Å/s")
        t_ramp1 = time.monotonic()

        stuck_start_ts_target: Optional[float] = None

        # ✅ target_ramp 탈출도 "연속 N회" 확인 후 통과(기본 3회)
        target_ramp_ok_hits: int = 0
        target_ramp_threshold = float(target_rate) * (1.0 - float(rate_tol_ratio))

        while True:
            rt = read_rate_or_abort(where="target_ramp")

            if rt >= target_ramp_threshold:
                target_ramp_ok_hits += 1
                self._emit_status(
                    message=(
                        f"TARGET RAMP CHECK {target_ramp_ok_hits}/{target_ramp_stable_hits} | "
                        f"DAC={dac} | rate={rt:.3f}/{target_rate:.3f} Å/s"
                    ),
                    force=True,
                )
                if target_ramp_ok_hits >= max(1, int(target_ramp_stable_hits)):
                    break

                _sleep_with_checks(target_ramp_stable_interval_s, where="target_ramp_confirm")
                continue
            else:
                target_ramp_ok_hits = 0

            # ✅ ignite 대기 적용(1500에서 rate=0이면 0.1까지)
            if _handle_ignite_wait(rt, where="target_ramp"):
                stuck_start_ts_target = None
                _sleep_with_checks(ignite_stable_interval_s, where="ignite_wait/target_ramp")
                continue

            # ✅ stuck guard(ignite 대기 아닐 때만)
            now = time.monotonic()
            if dac >= stuck_dac_guard and rt < stuck_rate_abs:
                if stuck_start_ts_target is None:
                    stuck_start_ts_target = now
                elif (now - stuck_start_ts_target) >= stuck_time_s:
                    raise EngineFailed(
                        step.name,
                        f"EVAP: rate 상승 없음(stuck/target) rt={rt:.3f} < {stuck_rate_abs:.3f} "
                        f"@ dac={dac} (>= {stuck_dac_guard}) for {stuck_time_s:.0f}s"
                    )
            else:
                stuck_start_ts_target = None

            if (now - t_ramp1) > ramp_timeout_s:
                raise EngineFailed(step.name, f"EVAP: target_rate ramp timeout {ramp_timeout_s}s (rt={rt:.3f})")

            if dac >= dac_max:
                raise EngineFailed(step.name, f"EVAP: DAC_MAX({dac_max}) 도달했지만 target_rate 미도달 (rt={rt:.3f})")

            dac = min(dac_max, dac + ramp_step_dac)
            apply_dac()
            self._emit_status(
                message=f"POWER RAMP UP (TARGET) / DAC={dac} / rate={rt:.3f}/{target_rate:.3f} Å/s",
                force=True,
            )
            _ramp_sleep_by_dac(dac, where="target_ramp_sleep")

        # 6) target_rate ±5% band 안으로 fine tune
        self._emit_status(message=f"EVAP fine tune: tol=±{rate_tol_ratio*100:.1f}% / stable_hits={target_stable_hits}")
        t_tune0 = time.monotonic()
        tune_timeout_s = float(meta.get("tune_timeout_s", 120.0) or 120.0)

        stable_hits = 0

        while True:
            rt = read_rate_or_abort(where="fine_tune")

            if _in_band(rt, target_rate, tol_ratio=rate_tol_ratio):
                stable_hits += 1
            else:
                stable_hits = 0

            if stable_hits >= max(1, int(target_stable_hits)):
                break

            if (time.monotonic() - t_tune0) > tune_timeout_s:
                raise EngineFailed(step.name, f"EVAP: target_rate fine tune timeout {tune_timeout_s}s (rt={rt:.3f})")

            new_dac = self._evap_adjust_dac(
                dac=dac,
                rate=rt,
                target_rate=target_rate,
                tol_ratio=rate_tol_ratio,
                step_up=fine_step_dac,
                step_dn=fine_step_dac,
                dac_max=dac_max,
            )

            # ✅ 미세조정도 구간 템포(10s/30s)보다 빠르게 못 움직이게
            maybe_apply_dac(new_dac, min_interval_s=_dac_min_interval_by_dac(max(int(dac), int(new_dac))))

            # ✅ 연속 판정을 위해 샘플링 간격을 meta에서 사용
            _sleep_with_checks(target_stable_interval_s, where="fine_tune_sleep")

        # 7) delay (shutter delay)
        if delay_s > 0:
            total_min = float(delay_min)
            t0m = time.monotonic()
            tendm = t0m + float(delay_s)
            next_ui = t0m  # 1초마다 UI 업데이트

            # 시작 표시(0/total)
            self._emit_status(message=f"SHUTTER DELAY / 0.00/{total_min:.2f} min", force=True)

            while True:
                rt = read_rate_or_abort(where="shutter_delay")

                # rate 급감 감지(셔터 열기 전)
                if rt < target_rate * rate_drop_ratio:
                    raise EngineFailed(step.name, f"EVAP: dep.rate 급감(셔터 전) rt={rt:.3f} < {target_rate*rate_drop_ratio:.3f}")

                new_dac = self._evap_adjust_dac(
                    dac=dac,
                    rate=rt,
                    target_rate=target_rate,
                    tol_ratio=rate_tol_ratio,
                    step_up=fine_step_dac,
                    step_dn=fine_step_dac,
                    dac_max=dac_max,
                )
                maybe_apply_dac(new_dac, min_interval_s=_dac_min_interval_by_dac(max(int(dac), int(new_dac))))

                nowm = time.monotonic()
                remain_s = tendm - nowm
                if remain_s <= 0:
                    break

                # ✅ 1초마다 "경과/남은(min)" 표시
                if nowm >= next_ui:
                    elapsed_min = (nowm - t0m) / 60.0
                    remain_min = max(0.0, remain_s) / 60.0
                    self._emit_status(
                        message=f"SHUTTER DELAY / {elapsed_min:.2f}/{total_min:.2f} min (remain {remain_min:.2f}) / DAC={dac}",
                        force=True,
                    )
                    next_ui = nowm + 1.0

                # ✅ 남은 시간 기준 sleep(딜레이 누적 오차 최소화)
                _sleep_with_checks(min(0.1, max(0.0, remain_s)))

        # 8) delay 종료 → STM zero
        self._emit_status(message="MAIN SHUTTER OPEN", force=True)
        zero_mode = str(meta.get("zero_mode", "B") or "B")
        self._stm_zero_thickness(recipe, step, mode=zero_mode)
        
        # zero 직후 thickness baseline 확보(소프트웨어 기준)
        th0 = read_th_or_abort()

        #  MAIN_SHUTTER open
        self._plc_write_coil("MAIN_SHUTTER_SW", True, tag="EVAP_MAIN_SHUTTER_OPEN")

        self._emit_status(message=f"EVAP: thickness baseline th0={th0:.1f} Å")

        # 9) 증착 루프
        drop_hits = 0
        baseline_rate: float | None = None

        while True:
            self._check_stop_pause(recipe, step)
            self._tick_emit(recipe, step)

            rt = read_rate_or_abort(where="main_process")

            th = read_th_or_abort()

            dep_th = th - th0
            if dep_th < 0:
                dep_th = 0.0

            # 기준 rate는 셔터 열린 후 첫 유효값으로 고정
            if baseline_rate is None:
                baseline_rate = rt

            # 급감 감지: baseline 대비 70% 이상 감소(= 30% 이하)
            if baseline_rate > 0 and rt < baseline_rate * rate_drop_ratio:
                drop_hits += 1
                if drop_hits >= rate_drop_count:
                    raise EngineFailed(step.name, f"EVAP: dep.rate 급감 감지 → 중단 (rt={rt:.3f}, base={baseline_rate:.3f})")
            else:
                drop_hits = 0

            # rate 유지
            new_dac = self._evap_adjust_dac(
                dac=dac,
                rate=rt,
                target_rate=target_rate,  # ✅ 여기
                tol_ratio=rate_tol_ratio,
                step_up=fine_step_dac,
                step_dn=fine_step_dac,
                dac_max=dac_max,
            )
            maybe_apply_dac(new_dac, min_interval_s=_dac_min_interval_by_dac(max(int(dac), int(new_dac))))

            remain_th = max(0.0, float(target_th) - float(dep_th))
            self._emit_status(
                message=f"MAIN PROCESSING / remain {remain_th:.1f}Å ( {dep_th:.1f}/{target_th:.1f}Å ) / DAC={dac}",
            )

            if dep_th >= target_th:
                break

            _sleep_with_checks(0.5)

        # 10) 종료
        self._emit_status(message="EVAP 완료: shutter close + power off")
        self._safe_shutdown_sequence(tag="EVAP_DONE")
        self._emit_status(message="EVAP 완료")

    def _evap_apply_dac(self, use_p1: bool, use_p2: bool, dac: int, *, tag: str) -> None:
        """선택된 채널에만 DAC 값을 적용(동기 submit + wait)."""
        dac = int(max(0, dac))
        if use_p1:
            self._plc_write_reg("DAC_POWER_1", dac, tag=f"{tag}_CH1")
            self._last_dac_power_1 = dac
        if use_p2:
            self._plc_write_reg("DAC_POWER_2", dac, tag=f"{tag}_CH2")
            self._last_dac_power_2 = dac


    @staticmethod
    def _evap_adjust_dac(
        dac: int,
        rate: Optional[float],
        target_rate: float,
        *,
        tol_ratio: float,
        step_up: int,
        step_dn: int,
        dac_max: int,
    ) -> int:
        """
        ✅ 목표 근접 시 미세 조정용 DAC 보정기
        - rate None이면 DAC 변경하지 않음(센서 끊김 동안 power 흔들지 않기)
        - 목표보다 낮으면 올림 / 목표보다 높으면 내림
        - 목표에 가까울수록 step을 자동으로 줄임
        """
        dac = int(dac)
        if rate is None:
            return dac

        tr = float(target_rate)
        if tr <= 0:
            return dac

        r = float(rate)
        tol = abs(tr) * float(tol_ratio)
        err = tr - r

        # ✅ 허용밴드면 유지
        if abs(err) <= tol:
            return dac

        ratio = abs(err) / max(abs(tr), 1e-9)  # 목표 대비 오차 비율

        # ✅ 멀면 크게 / 가까우면 작게
        if ratio >= 0.50:
            base = 100
        elif ratio >= 0.25:
            base = 50
        elif ratio >= 0.12:
            base = 20
        elif ratio >= 0.06:
            base = 10
        else:
            base = 5

        if err > 0:
            step = min(int(step_up), int(base))
            return int(min(int(dac_max), dac + step))
        else:
            step = min(int(step_dn), int(base))
            return int(max(0, dac - step))

    def _safe_shutdown_sequence(self, *, tag: str) -> None:
        """
        안전 시퀀스(통일):
        1) MAIN_SHUTTER close
        2) DAC 0
        3) SOURCE SHUTTER close (1/2)
        4) POWER off
        5) FTM off
        (실패해도 예외 삼킴: best-effort)
        """
        # 1) main shutter close
        try:
            self._plc_write_coil("MAIN_SHUTTER_SW", False, tag=f"{tag}_MAIN_SHUTTER_CLOSE")
        except Exception:
            pass

        # 2) dac 0
        try:
            self._plc_write_reg("DAC_POWER_1", 0, tag=f"{tag}_DAC1_0")
            self._last_dac_power_1 = 0
        except Exception:
            pass
        try:
            self._plc_write_reg("DAC_POWER_2", 0, tag=f"{tag}_DAC2_0")
            self._last_dac_power_2 = 0
        except Exception:
            pass

        # 3) source shutters close (에러/정지시에도 원복 보장)
        for coil in ("SHUTTER_1_SW", "SHUTTER_2_SW"):
            try:
                self._plc_write_coil(coil, False, tag=f"{tag}_{coil}_CLOSE")
            except Exception:
                pass

        # 4) power off
        try:
            self._plc_write_coil("POWER_1_SW", False, tag=f"{tag}_PWR1_OFF")
        except Exception:
            pass
        try:
            self._plc_write_coil("POWER_2_SW", False, tag=f"{tag}_PWR2_OFF")
        except Exception:
            pass

        # 5) ftm off
        try:
            self._plc_write_coil("FTM_SW", False, tag=f"{tag}_FTM_OFF")
        except Exception:
            pass

    # --------------------------------------------------------
    # PLC wrappers
    # --------------------------------------------------------
    def _plc_write_coil(self, coil: Optional[str], on: bool, *, tag: str) -> None:
        if not coil:
            raise EngineFailed(tag, "coil is None")
        fut = self.plc.submit_write_coil(coil_name=str(coil), on=bool(on), momentary=False, pulse_ms=None, tag=tag)
        self._wait_future(fut, timeout_s=self._plc_cmd_timeout_s, where=f"PLC_WRITE_COIL {coil}={on}")
        self._tele_event(event="WRITE_COIL", target=str(coil), value=int(bool(on)), detail=f"tag={tag}")

    def _plc_pulse_coil(self, coil: Optional[str], pulse_ms: int, *, tag: str) -> None:
        if not coil:
            raise EngineFailed(tag, "coil is None")
        pulse_ms = int(pulse_ms)
        if pulse_ms <= 0:
            pulse_ms = 200
        fut = self.plc.submit_write_coil(coil_name=str(coil), on=True, momentary=True, pulse_ms=pulse_ms, tag=tag)
        self._wait_future(fut, timeout_s=max(self._plc_cmd_timeout_s, pulse_ms / 1000.0 + 0.5), where=f"PLC_PULSE_COIL {coil} ({pulse_ms}ms)")
        self._tele_event(event="PULSE_COIL", target=str(coil), value=int(pulse_ms), detail=f"tag={tag}")

    def _plc_write_reg(self, reg: Optional[str], value: int, *, tag: str) -> None:
        if not reg:
            raise EngineFailed(tag, "reg is None")

        reg_name = str(reg)
        v = int(value)

        fut = self.plc.submit_write_reg(reg_name=reg_name, value=v, tag=tag)
        self._wait_future(fut, timeout_s=self._plc_cmd_timeout_s, where=f"PLC_WRITE_REG {reg_name}={v}")

        # ✅ telemetry(dac1/dac2) 정확도 확보: DAC 레지스터면 마지막값 갱신
        rn = reg_name.upper()
        if rn == "DAC_POWER_1":
            self._last_dac_power_1 = v
        elif rn == "DAC_POWER_2":
            self._last_dac_power_2 = v

        ev = "SET_DAC" if rn in ("DAC_POWER_1", "DAC_POWER_2") else "WRITE_REG"
        self._tele_event(event=ev, target=rn, value=v, detail=f"tag={tag}")

    def _plc_set_dac_ma(self, ch: int, ma: float, *, tag: str) -> None:
        ch_i = int(ch)
        fut = self.plc.submit_set_dac_current(ch=ch_i, ma=float(ma), tag=tag)

        # 결과가 "dac code(int)"일 수 있으니 받아서 telemetry에 반영
        res = self._wait_future(fut, timeout_s=self._plc_cmd_timeout_s, where=f"PLC_SET_DAC_MA ch={ch_i} ma={ma}")

        code: Optional[int] = None
        try:
            if isinstance(res, (int, float)):
                code = int(res)
                if ch_i == 1:
                    self._last_dac_power_1 = code
                elif ch_i == 2:
                    self._last_dac_power_2 = code
        except Exception:
            pass

        # ✅ 이벤트 1줄 추가
        self._tele_event(
            event="SET_DAC_MA",
            target=f"DAC_CH{ch_i}",
            value=float(ma),
            detail=f"code={code}, tag={tag}",
        )

    @staticmethod
    def _wait_future(fut: Any, *, timeout_s: float, where: str, msg: str = "") -> Any:
        try:
            return fut.result(timeout=float(timeout_s))
        except Exception as e:
            raise EngineFailed(where, f"{msg + ': ' if msg else ''}future failed/timeout: {e!r}")

    # --------------------------------------------------------
    # WAIT helpers
    # --------------------------------------------------------
    def _wait_seconds(self, recipe: ProcessRecipe, step: ProcessStep, sec: float) -> None:
        total_s = max(0.0, float(sec))
        start_m = time.monotonic()
        deadline_m = start_m + total_s
        poll = self._get_poll_s(recipe, step, fallback=0.05)

        # ✅ WAIT 표시(경과/남은) : 1초마다 UI 갱신
        next_ui_m = start_m

        while True:
            self._check_stop_pause(recipe, step)
            self._tick_emit(recipe, step)

            now_m = time.monotonic()
            remain_s = deadline_m - now_m
            if remain_s <= 0:
                # 마지막 1회 표시(완료 직전 상태)
                if total_s > 0:
                    prefix = (str(getattr(step, "message", "") or "").strip()) or (step.name or "WAIT")
                    self._emit_status(
                        message=f"[남은 00:00] {prefix} | 완료",
                        force=True,
                    )
                return

            # ✅ 표시 제목: step.message가 있으면 그것을 우선 사용(“텍스트 직접 지정” 지원)
            prefix = (str(getattr(step, "message", "") or "").strip()) or (step.name or "WAIT")

            # 1초마다 표시
            if now_m >= next_ui_m:
                elapsed_s = now_m - start_m
                self._emit_status(
                    message=(
                        f"[남은 {self._fmt_hms(remain_s, ceil=True)}] {prefix} | "
                        f"경과 {self._fmt_hms(elapsed_s)} / 총 {self._fmt_hms(total_s)}"
                    ),
                    force=True,
                )
                next_ui_m = now_m + 1.0

            time.sleep(min(poll, max(0.0, remain_s)))

    def _wait_condition(
        self,
        *,
        recipe: ProcessRecipe,
        step: ProcessStep,
        cond_fn: Callable[[], bool],
        cond_desc: str,
        sensor_value_fn: Optional[Callable[[], Optional[float]]] = None,
        sensor_label: str = "",
        sensor_missing_abort_s: Optional[float] = None,
    ) -> None:
        # ✅ WAIT 표시(조건/경과/남은): monotonic 기반(시스템 시간 변경 영향 최소화)
        start_m = time.monotonic()

        # ✅ timeout: step.timeout_s 우선, 없으면 recipe.default_wait_timeout_s(없으면 3600s) 적용
        timeout_s = getattr(step, "timeout_s", None)
        if timeout_s is None:
            timeout_s = float(getattr(recipe, "default_wait_timeout_s", 3600.0) or 3600.0)
            # 사용자가 "진짜 무한대"를 원하면 recipe.default_wait_timeout_s=0(또는 음수)로 두면 됨
            if timeout_s <= 0:
                timeout_s = None

        deadline_m: Optional[float] = None
        if timeout_s is not None:
            deadline_m = start_m + float(timeout_s)

        poll_s = max(0.05, float(step.poll_s or 0.2))

        # ✅ 센서 None 지속 감시(기본 10초) — sensor_value_fn 제공 시에만 동작
        missing_abort_s = sensor_missing_abort_s
        if sensor_value_fn is not None and missing_abort_s is None:
            missing_abort_s = float(getattr(self, "_sensor_missing_abort_s", 10.0) or 10.0)
        last_valid_m = time.monotonic()

        stable_start_m: Optional[float] = None

        # 1초마다 UI 갱신
        next_ui_m = start_m

        while True:
            self._check_stop_pause(recipe, step)
            self._tick_emit(recipe, step)

            now_m = time.monotonic()

            # 센서 값 읽기(표시/None 감시 용)
            v: Optional[float] = None
            if sensor_value_fn is not None:
                try:
                    v = sensor_value_fn()
                except Exception:
                    v = None

                if missing_abort_s is not None and missing_abort_s > 0:
                    if v is not None:
                        last_valid_m = now_m
                    elif (now_m - last_valid_m) >= float(missing_abort_s):
                        lab = sensor_label or "sensor"
                        raise EngineFailed(step.name, f"{lab}=None 지속 {float(missing_abort_s):.0f}s 초과(연결 끊김/미보고)")

            # timeout
            if deadline_m is not None and now_m >= deadline_m:
                raise EngineTimeout(step.name, f"timeout waiting {cond_desc}")

            # condition + stable
            ok = bool(cond_fn())
            if ok:
                if stable_start_m is None:
                    stable_start_m = now_m
                if step.stable_s is None or (now_m - stable_start_m) >= float(step.stable_s):
                    # 마지막 1회 표시(성공)
                    elapsed_s = now_m - start_m
                    prefix = (str(getattr(step, "message", "") or "").strip()) or f"대기: {cond_desc}"
                    self._emit_status(message=f"{prefix} | 조건 만족 (경과 {self._fmt_hms(elapsed_s)})", force=True)
                    return
            else:
                stable_start_m = None

            # UI 표시(조건/경과/남은)
            if now_m >= next_ui_m:
                elapsed_s = now_m - start_m
                # ✅ 표시 제목: step.message가 있으면 그것을 우선 사용(“텍스트 직접 지정” 지원)
                prefix = (str(getattr(step, "message", "") or "").strip())
                if not prefix:
                    # 기본값(기존 cond_desc는 유지하되, 사용자 지정 메시지가 있으면 그게 우선)
                    prefix = f"대기: {cond_desc}"

            # deadline_m은 (위에서) 기본값이 들어가므로 보통 None이 아님
            if deadline_m is None:
                remain_txt = self._fmt_hms(0.0, ceil=True)
            else:
                remain_s = max(0.0, deadline_m - now_m)
                remain_txt = self._fmt_hms(remain_s, ceil=True)

            elapsed_txt = self._fmt_hms(elapsed_s)

            extra_parts = []
            if step.stable_s is not None and float(step.stable_s) > 0:
                stable_s = float(step.stable_s)
                stable_elapsed = 0.0 if stable_start_m is None else max(0.0, now_m - stable_start_m)
                stable_elapsed = min(stable_elapsed, stable_s)
                extra_parts.append(f"stable {stable_elapsed:.1f}/{stable_s:.1f}s")

            if sensor_value_fn is not None:
                lab = sensor_label or "sensor"
                if v is None:
                    extra_parts.append(f"{lab}=None")
                else:
                    extra_parts.append(f"{lab}={float(v):.3f}")

            extra = (" / " + " / ".join(extra_parts)) if extra_parts else ""

            # ✅ 표시 포맷 통일: [남은 ..] prefix | 경과 .. / stable .. / sensor ..
            self._emit_status(
                message=f"[남은 {remain_txt}] {prefix} | 경과 {elapsed_txt}{extra}",
                force=True,
            )
            next_ui_m = now_m + 1.0

            # sleep(남은 시간이 짧으면 그만큼만)
            if deadline_m is None:
                time.sleep(poll_s)
            else:
                remain_s = max(0.0, deadline_m - time.monotonic())
                time.sleep(min(poll_s, remain_s))

    def _handle_timeout(self, step: ProcessStep, detail: str) -> None:
        policy = step.on_timeout
        if policy == OnTimeout.CONTINUE:
            self._log_warn(f"TIMEOUT -> CONTINUE: {step.name} | {detail}", tag="ENGINE", also_ui=True)
            return
        if policy == OnTimeout.ERROR:
            raise EngineFailed(step.name, f"TIMEOUT(ERROR): {detail}")
        # default ABORT
        raise EngineTimeout(step.name, detail)

    def _get_poll_s(self, recipe: ProcessRecipe, step: ProcessStep, *, fallback: float) -> float:
        if step.poll_s is not None:
            return max(0.01, float(step.poll_s))
        if recipe.default_poll_s is not None:
            return max(0.01, float(recipe.default_poll_s))
        return max(0.01, float(fallback))

    # --------------------------------------------------------
    # Stop/Pause checks
    # --------------------------------------------------------
    def _check_stop_pause(self, recipe: ProcessRecipe, step: ProcessStep) -> None:
        # stop 우선
        if self._stop_mode is not None:
            raise EngineStopRequested(self._stop_mode)

        # pause 처리: paused일 때는 stop 요청만 감시하면서 대기
        while self._paused:
            # pause 중에도 stop이면 즉시 빠져나감
            if self._stop_mode is not None:
                raise EngineStopRequested(self._stop_mode)

            self._phase = ProcessPhase.PAUSED
            self._emit_status(message="일시정지", force=True)
            time.sleep(0.1)

        if self._phase == ProcessPhase.PAUSED:
            self._phase = ProcessPhase.RUNNING
            self._emit_status(message="재개", force=True)

    # --------------------------------------------------------
    # Snapshot getters (센서/PLC 상태 읽기)
    # --------------------------------------------------------
    def _get_plc_snapshot(self) -> Optional[PLCSnapshot]:
        try:
            return self.plc.get_last_snapshot()
        except Exception:
            return None

    def _get_stm_snapshot(self) -> Optional[STMSnapshot]:
        if self.stm is None:
            return None
        try:
            return self.stm.get_last_snapshot()
        except Exception:
            return None

    def _get_acs_snapshot(self) -> Optional[ACSSnapshot]:
        if self.acs is None:
            return None
        try:
            return self.acs.get_last_snapshot()
        except Exception:
            return None

    def _get_pressure(self) -> Optional[float]:
        s = self._get_acs_snapshot()
        if s is None:
            return None
        if not getattr(s, "connected", False):
            return None
        return getattr(s, "pressure", None)

    def _get_thickness(self) -> Optional[float]:
        s = self._get_stm_snapshot()
        if s is None:
            return None
        if not getattr(s, "connected", False):
            return None
        return getattr(s, "thickness_angstrom", None)

    def _get_rate(self) -> Optional[float]:
        s = self._get_stm_snapshot()
        if s is None:
            return None
        if not getattr(s, "connected", False):
            return None
        return getattr(s, "rate_angstrom_per_s", None)

    def _get_coil(self, coil: str) -> Optional[bool]:
        snap = self._get_plc_snapshot()
        if snap is None:
            return None
        if not getattr(snap, "connected", False):
            return None
        coils = getattr(snap, "coils", None)
        if not isinstance(coils, dict):
            return None
        v = coils.get(coil)
        return bool(v) if v is not None else None

    # condition helpers
    def _get_pressure_ok_leq(self, target: float) -> bool:
        p = self._get_pressure()
        if p is None:
            return False
        return float(p) <= float(target)

    def _get_thickness_ok_geq(self, target_a: float) -> bool:
        th = self._get_thickness()
        if th is None:
            return False
        return float(th) >= float(target_a)

    def _get_rate_ok_in_range(self, mn: float, mx: float) -> bool:
        rt = self._get_rate()
        if rt is None:
            return False
        r = float(rt)
        return float(mn) <= r <= float(mx)

    def _get_coil_is(self, coil: str, expected: bool) -> bool:
        v = self._get_coil(coil)
        if v is None:
            return False
        return bool(v) == bool(expected)

    # --------------------------------------------------------
    # Status / Telemetry emit
    # --------------------------------------------------------
    def _tick_emit(self, recipe: ProcessRecipe, step: ProcessStep) -> None:
        """
        너무 자주 내보내면 UI가 버벅일 수 있으므로 interval로 제한.
        - status emit: _status_emit_interval_s
        - telemetry: recipe.telemetry_interval_s
        """
        now = time.monotonic()

        if (now - self._last_status_emit_ts) >= self._status_emit_interval_s:
            self._emit_status(step_idx=self._current_step_idx, step_name=self._current_step_name)

        tele_int = float(recipe.telemetry_interval_s or 1.0)
        if tele_int <= 0:
            tele_int = 1.0

        if (now - self._last_telemetry_ts) >= tele_int:
            self._last_telemetry_ts = now
            self._emit_telemetry(recipe, step)

    def _emit_status(
        self,
        *,
        step_idx: Optional[int] = None,
        step_name: Optional[str] = None,
        message: str = "",
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and (now - self._last_status_emit_ts) < self._status_emit_interval_s:
            return
        self._last_status_emit_ts = now  # ✅ 여기서 갱신

        # ✅ message가 비어있으면 마지막 메시지 유지
        if message:
            self._ui_last_message = message
        else:
            message = self._ui_last_message

        st = ProcessStatus(
            phase=self._phase,
            recipe_name=self._recipe_name,
            run_id=self._run_id,
            step_idx=self._current_step_idx if step_idx is None else int(step_idx),
            step_name=self._current_step_name if step_name is None else str(step_name),
            started_ts=self._started_ts,
            message=message,
            pressure=self._get_pressure(),
            thickness_a=self._get_thickness(),
            rate_a_s=self._get_rate(),

            # ✅ models.py에 dac1/dac2를 추가했다면 여기서 값도 채워줘야 UI/그래프에서 사용 가능
            dac1=int(getattr(self, "_last_dac_power_1", 0) or 0),
            dac2=int(getattr(self, "_last_dac_power_2", 0) or 0),
        )

        cb = self.callbacks.on_status
        if cb:
            try:
                cb(st)
            except Exception:
                # 콜백 예외는 엔진에 영향 주지 않음
                pass

    def _emit_step(self, st: StepStatus) -> None:
        cb = self.callbacks.on_step
        if cb:
            try:
                cb(st)
            except Exception:
                pass

    def _emit_error(self, err: ProcessError) -> None:
        cb = self.callbacks.on_error
        if cb:
            try:
                cb(err)
            except Exception:
                pass

    def _emit_telemetry(self, recipe: ProcessRecipe, step: ProcessStep) -> None:
        """
        LogService.telemetry()로 run별 CSV에 저장.
        """
        try:
            acs = self._get_acs_snapshot()
            acs_meta = getattr(acs, "meta", None) if acs is not None else None
            pressure_status = None
            pressure_status_text = None
            pressure_ok = None
            pressure_raw = None
            if isinstance(acs_meta, dict):
                pressure_status = acs_meta.get("status")
                pressure_status_text = acs_meta.get("status_text")
                pressure_ok = acs_meta.get("ok")
                pressure_raw = acs_meta.get("raw")

            self.log.telemetry({
                "step": (self._ui_last_message or self._current_step_name),
                "detail": "",

                "pressure_torr": self._get_pressure(),
                "dac1": int(getattr(self, "_last_dac_power_1", 0) or 0),
                "dac2": int(getattr(self, "_last_dac_power_2", 0) or 0),
                "dep.rate": self._get_rate(),
                "thickness_A": self._get_thickness(),
            })
        except Exception:
            # 텔레메트리 실패해도 공정은 계속
            pass

    # --------------------------------------------------------
    # Logging wrappers
    # --------------------------------------------------------
    def _log_info(self, msg: str, *, tag: str = "ENGINE", also_ui: bool = True) -> None:
        try:
            self.log.info(msg, tag=tag, also_ui=also_ui)
        except Exception:
            pass

    def _log_warn(self, msg: str, *, tag: str = "ENGINE", also_ui: bool = True) -> None:
        try:
            self.log.warn(msg, tag=tag, also_ui=also_ui)
        except Exception:
            pass

    def _log_error(self, msg: str, *, tag: str = "ENGINE", also_ui: bool = True) -> None:
        try:
            self.log.error(msg, tag=tag, also_ui=also_ui)
        except Exception:
            pass

    def _ui_coil_text(self, coil: str, on: bool) -> str:
        c = (coil or "").upper()
        # ✅ 너가 원하는 대표 표시들
        if c == "FTM_SW":
            return f"FTM {'ON' if on else 'OFF'}"
        if c == "MAIN_SHUTTER_SW":
            return f"MAIN SHUTTER {'OPEN' if on else 'CLOSE'}"
        if c == "SHUTTER_1_SW":
            return f"SOURCE SHUTTER 1 {'OPEN' if on else 'CLOSE'}"
        if c == "SHUTTER_2_SW":
            return f"SOURCE SHUTTER 2 {'OPEN' if on else 'CLOSE'}"
        if c == "POWER_1_SW":
            return f"POWER 1 {'ON' if on else 'OFF'}"
        if c == "POWER_2_SW":
            return f"POWER 2 {'ON' if on else 'OFF'}"
        # fallback
        return f"{c}={'ON' if on else 'OFF'}"

    def _ui_reg_text(self, reg: str, value: int) -> str:
        r = (reg or "").upper()
        if r == "DAC_POWER_1":
            return f"POWER RAMP (DAC1={int(value)})"
        if r == "DAC_POWER_2":
            return f"POWER RAMP (DAC2={int(value)})"
        return f"{r}={int(value)}"
    
    def _ui_pulse_text(self, coil: str, pulse_ms: int) -> str:
        c = (coil or "").upper()
        # 필요하면 여기서 coil 이름별로 더 예쁘게 매핑 가능
        return f"{c} PULSE ({int(pulse_ms)}ms)" if c else f"PULSE ({int(pulse_ms)}ms)"
    
    def _tele_event(self, *, event: str, target: str, value: Any = "", detail: str = "") -> None:
        """공정 CSV에 이벤트 1줄 추가(event/target/value/detail)."""
        try:
            line = f"{str(event)} {str(target)}={value}"
            if detail:
                line += f" | {str(detail)}"

            self.log.telemetry({
                "step": (self._ui_last_message or self._current_step_name),
                "detail": line,

                "pressure_torr": self._get_pressure(),
                "dac1": int(getattr(self, "_last_dac_power_1", 0) or 0),
                "dac2": int(getattr(self, "_last_dac_power_2", 0) or 0),
                "dep.rate": self._get_rate(),
                "thickness_A": self._get_thickness(),
            })
        except Exception:
            pass

    @staticmethod
    def _fmt_hms(sec: float, *, ceil: bool = False) -> str:
        """
        초(sec)를 'MM:SS' 또는 'H:MM:SS'로 변환.
        - remain(남은시간)은 ceil=True 권장(0이 너무 빨리 뜨는 것 방지)
        """
        x = float(sec)
        if ceil:
            total = int(max(0, math.ceil(x)))
        else:
            total = int(max(0, x))

        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

    # --------------------------------------------------------
    # Utilities
    # --------------------------------------------------------
    @staticmethod
    def _make_run_id() -> str:
        # 예: 20260204_153012_ab12cd
        t = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        u = uuid.uuid4().hex[:6]
        return f"{t}_{u}"

    @staticmethod
    def _ts_str(ts: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
