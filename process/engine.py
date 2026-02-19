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
                    "recipe": recipe.to_dict(),
                    "started_at": self._ts_str(self._started_ts),
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
            self._plc_write_coil(step.coil, bool(step.on), tag=step.name)
            return

        if t == StepType.PLC_PULSE_COIL:
            # momentary pulse(ON 펄스)로 통일
            self._plc_pulse_coil(step.coil, int(step.pulse_ms or 200), tag=step.name)
            return

        if t == StepType.PLC_WRITE_REG:
            self._plc_write_reg(step.reg, int(step.value or 0), tag=step.name)
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
                cond_desc=f"pressure <= {target}",
            )
            return

        if t == StepType.WAIT_THICKNESS_GEQ:
            target = float(step.thickness_target)
            self._wait_condition(
                recipe=recipe,
                step=step,
                cond_fn=lambda: self._get_thickness_ok_geq(target),
                cond_desc=f"thickness >= {target}",
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
                cond_desc=f"rate in [{mn}, {mx}] A/s",
                sensor_value_fn=self._get_rate,   # ✅ 추가
                sensor_label="STM rate",          # ✅ 추가
            )
            return

        if t == StepType.WAIT_COIL_IS:
            coil = str(step.coil)
            exp = bool(step.expected)
            self._wait_condition(
                recipe=recipe,
                step=step,
                cond_fn=lambda: self._get_coil_is(coil, exp),
                cond_desc=f"coil {coil} == {exp}",
            )
            return

        raise EngineFailed(step.name, f"unsupported step type: {t}")
    
    # --------------------------------------------------------
    # EVAP 증착 제어 루프
    # --------------------------------------------------------
    def _evap_deposition_control(self, recipe: ProcessRecipe, step: ProcessStep) -> None:
        """
        EVAP 증착 제어 루프 (요구사항 반영)

        - DAC 0~1000: 1초에 100씩 상승(FAST)
        - 1000 이후: 기본 100 단위로 조정 + 10초 동안 1Hz로 dep.rate 확인
          단, 목표 근접 시 step을 자동으로 줄여 미세조정
        - target_rate 도달 → delay_min(분, 소수 가능) 유지 → MAIN_SHUTTER OPEN
        - target_thickness 도달까지 rate 유지(동적 보정)
        - dep.rate이 기준 대비 50% 이하로 급락하면 즉시 안전정지 후 실패 처리
        - dep.rate/thickness가 None 상태가 10초 지속되면 실패 처리(재연결 기회 제공)
        """
        meta: dict[str, Any] = {}
        meta.update(getattr(recipe, "meta", None) or {})
        meta.update(getattr(step, "meta", None) or {})  # step.meta 우선

        use_p1 = bool(meta.get("use_power1", False))
        use_p2 = bool(meta.get("use_power2", False))
        if not (use_p1 or use_p2):
            raise EngineFailed(step.name, "use_power1/use_power2가 모두 False 입니다.")

        target_rate = float(meta.get("target_rate", 0.0) or 0.0)
        target_th = float(meta.get("target_thickness", 0.0) or 0.0)
        delay_min = float(meta.get("delay_min", 0.0) or 0.0)

        if target_rate <= 0:
            raise EngineFailed(step.name, f"target_rate invalid: {target_rate}")
        if target_th <= 0:
            raise EngineFailed(step.name, f"target_thickness invalid: {target_th}")
        if delay_min < 0:
            raise EngineFailed(step.name, f"delay_min invalid: {delay_min}")

        if self.stm is None:
            raise EngineFailed(step.name, "STMService가 None입니다(증착 rate/thickness 읽기 불가).")

        # --- 튜닝 파라미터 ---
        DAC_MAX = int(meta.get("dac_max", 4000) or 4000)
        DAC_FAST_LIMIT = int(meta.get("dac_fast_limit", 1000) or 1000)
        DAC_FAST_STEP = int(meta.get("dac_fast_step", 100) or 100)
        DAC_SLOW_STEP = int(meta.get("dac_slow_step", 100) or 100)

        RATE_TOL = float(meta.get("rate_tol_ratio", 0.02) or 0.02)   # ±2% 밴드
        DROP_RATIO = float(meta.get("rate_drop_ratio", 0.5) or 0.5)  # 50% 급락
        DROP_COUNT = int(meta.get("rate_drop_count", 3) or 3)        # 3회 연속이면 abort

        SENSOR_NONE_ABORT_S = float(meta.get("sensor_none_abort_s", 10.0) or 10.0)
        delay_s = max(0.0, delay_min * 60.0)

        # --- rate/thickness None 10초 감시 ---
        last_rate_valid_ts = time.time()
        last_th_valid_ts = time.time()

        def read_rate_or_abort() -> Optional[float]:
            nonlocal last_rate_valid_ts
            rt = self._get_rate()
            now = time.time()
            if rt is not None:
                last_rate_valid_ts = now
                return float(rt)
            if (now - last_rate_valid_ts) >= SENSOR_NONE_ABORT_S:
                raise EngineFailed(step.name, f"STM rate=None 지속 {SENSOR_NONE_ABORT_S:.0f}s 초과")
            return None

        def read_th_or_abort() -> Optional[float]:
            nonlocal last_th_valid_ts
            th = self._get_thickness()
            now = time.time()
            if th is not None:
                last_th_valid_ts = now
                return float(th)
            if (now - last_th_valid_ts) >= SENSOR_NONE_ABORT_S:
                raise EngineFailed(step.name, f"STM thickness=None 지속 {SENSOR_NONE_ABORT_S:.0f}s 초과")
            return None

        # 0) STM 연결 확인(짧게)
        t0 = time.time()
        while True:
            self._check_stop_pause(recipe, step)
            self._tick_emit(recipe, step)

            s = self._get_stm_snapshot()
            if s is not None and bool(getattr(s, "connected", False)):
                break
            if (time.time() - t0) > 5.0:
                raise EngineFailed(step.name, "STM 연결 실패/미연결(5s timeout).")
            time.sleep(0.2)

        # 1) 안전 초기화: 셔터 닫기 → DAC 0 → 선택 power ON
        self._safe_shutdown_sequence(tag="EVAP_INIT")
        self._plc_write_coil("POWER_1_SW", bool(use_p1), tag="EVAP_POWER1_ON" if use_p1 else "EVAP_POWER1_OFF")
        self._plc_write_coil("POWER_2_SW", bool(use_p2), tag="EVAP_POWER2_ON" if use_p2 else "EVAP_POWER2_OFF")

        # 2) 목표 rate 도달까지 DAC 램프 + 피드백
        dac = 0
        self._evap_apply_dac(use_p1, use_p2, dac, tag="EVAP_DAC_SET_0")

        def in_band(v: float) -> bool:
            tol = abs(target_rate) * RATE_TOL
            return abs(v - target_rate) <= tol

        # ---- 2-A) 0~1000: 1초마다 +100 (요구사항 그대로) ----
        while True:
            self._check_stop_pause(recipe, step)
            self._tick_emit(recipe, step)

            rt = read_rate_or_abort()
            if rt is not None and rt >= target_rate:
                break
            if dac >= DAC_FAST_LIMIT:
                break

            dac = min(DAC_FAST_LIMIT, dac + DAC_FAST_STEP)
            self._evap_apply_dac(use_p1, use_p2, dac, tag=f"EVAP_DAC_FAST_{dac}")

            # 1초 대기(0.1s x 10) - stop/pause 반응성 확보
            for _ in range(10):
                self._check_stop_pause(recipe, step)
                self._tick_emit(recipe, step)
                time.sleep(0.1)

            rt2 = read_rate_or_abort()
            self._emit_status(message=f"EVAP RAMP_FAST dac={dac} rate={rt2}")
            if rt2 is not None and rt2 >= target_rate:
                break

        # ---- 2-B) 1000 이후: 10초/1Hz 기반 + 근접 시 미세조정 + overshoot 시 내림 ----
        while True:
            self._check_stop_pause(recipe, step)
            self._tick_emit(recipe, step)

            rt = read_rate_or_abort()

            # 센서 잠깐 None이면(재연결 중) DAC는 건드리지 말고 기다림
            if rt is None:
                time.sleep(0.2)
                continue

            if in_band(rt) or rt >= target_rate:
                break

            err = target_rate - rt
            ratio = abs(err) / max(abs(target_rate), 1e-9)

            # 멀면 100/10초, 가까우면 50/5초, 20/3초, 10/2초
            if ratio >= 0.30:
                step_sz, dwell_s = 100, 10.0
            elif ratio >= 0.15:
                step_sz, dwell_s = 50, 5.0
            elif ratio >= 0.08:
                step_sz, dwell_s = 20, 3.0
            else:
                step_sz, dwell_s = 10, 2.0

            step_sz = int(min(int(DAC_SLOW_STEP), step_sz))  # 기본 slow_step(100) 이상은 못가게 제한

            if err > 0:
                dac = min(DAC_MAX, dac + step_sz)
            else:
                dac = max(0, dac - step_sz)

            self._evap_apply_dac(use_p1, use_p2, dac, tag=f"EVAP_DAC_SLOW_{dac}")

            n = max(1, int(round(dwell_s)))
            reached = False
            for i in range(n):
                self._check_stop_pause(recipe, step)
                self._tick_emit(recipe, step)

                rt2 = read_rate_or_abort()
                self._emit_status(message=f"EVAP RAMP_SLOW dac={dac} t={i+1}/{n} rate={rt2}")

                if rt2 is not None and (in_band(rt2) or rt2 >= target_rate):
                    reached = True
                    break
                time.sleep(1.0)

            if reached:
                break

            if dac >= DAC_MAX and rt < target_rate:
                raise EngineFailed(step.name, f"target_rate 미도달: dac={dac} rate={rt}")

        # 3) delay 구간(셔터 닫힌 상태): rate 유지(미세 보정)
        if delay_s > 0:
            t_end = time.time() + delay_s
            self._emit_status(message=f"EVAP delay start: {delay_s:.1f}s (keep rate)")

            while time.time() < t_end:
                self._check_stop_pause(recipe, step)
                self._tick_emit(recipe, step)

                rt = read_rate_or_abort()
                if rt is not None and rt < (target_rate * DROP_RATIO):
                    raise EngineFailed(step.name, f"rate drop(before shutter open): rate={rt} < {target_rate*DROP_RATIO}")

                if rt is not None:
                    new_dac = self._evap_adjust_dac(
                        dac, rt, target_rate,
                        tol_ratio=RATE_TOL,
                        step_up=50, step_dn=50,
                        dac_max=DAC_MAX
                    )
                    if new_dac != dac:
                        dac = new_dac
                        self._evap_apply_dac(use_p1, use_p2, dac, tag=f"EVAP_DAC_FINE_{dac}")

                time.sleep(1.0)

        # 4) MAIN SHUTTER OPEN
        self._plc_write_coil("MAIN_SHUTTER_SW", True, tag="EVAP_SHUTTER_OPEN")
        self._emit_status(message="EVAP shutter opened")

        # thickness baseline: 셔터 오픈 시점 thickness가 None이면 10초 내 확보
        th0 = read_th_or_abort()
        if th0 is None:
            t_th0 = time.time()
            while True:
                self._check_stop_pause(recipe, step)
                self._tick_emit(recipe, step)

                th0 = read_th_or_abort()
                if th0 is not None:
                    break
                if (time.time() - t_th0) >= SENSOR_NONE_ABORT_S:
                    raise EngineFailed(step.name, "thickness baseline 확보 실패")
                time.sleep(0.2)

        baseline_rate: Optional[float] = None
        drop_count = 0

        # 5) target_thickness 도달까지 rate 유지 + 급락 abort
        while True:
            self._check_stop_pause(recipe, step)
            self._tick_emit(recipe, step)

            rt = read_rate_or_abort()
            th = read_th_or_abort()

            if baseline_rate is None and rt is not None and rt >= (target_rate * 0.8):
                baseline_rate = float(rt)

            if th is not None:
                dep_th = float(th) - float(th0)
                self._emit_status(message=f"EVAP deposit: dac={dac} rate={rt} dth={dep_th:.1f}/{target_th:.1f}A")
                if dep_th >= target_th:
                    break
            else:
                self._emit_status(message=f"EVAP deposit: dac={dac} rate={rt} thickness=None")

            if rt is not None:
                ref = baseline_rate if baseline_rate is not None else target_rate
                if rt < (float(ref) * DROP_RATIO):
                    drop_count += 1
                else:
                    drop_count = 0
                if drop_count >= DROP_COUNT:
                    raise EngineFailed(step.name, f"rate drop detected: rate={rt} ref={ref} (x{DROP_COUNT})")

                new_dac = self._evap_adjust_dac(
                    dac, rt, target_rate,
                    tol_ratio=RATE_TOL,
                    step_up=50, step_dn=50,
                    dac_max=DAC_MAX
                )
                if new_dac != dac:
                    dac = new_dac
                    self._evap_apply_dac(use_p1, use_p2, dac, tag=f"EVAP_DAC_HOLD_{dac}")

            time.sleep(1.0)

        # 6) 완료: 안전정지(셔터 close → DAC 0 → power off)
        self._safe_shutdown_sequence(use_p1=use_p1, use_p2=use_p2, tag="EVAP_DONE")
        self._log_info("EVAP deposition done", tag="PROCESS", also_ui=True)


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
        요구사항 안전 시퀀스(통일):
        1) MAIN_SHUTTER close
        2) DAC 0
        3) POWER off
        (실패해도 예외 삼킴: best-effort)
        """
        # 1) shutter close
        try:
            self._plc_write_coil("MAIN_SHUTTER_SW", False, tag=f"{tag}_SHUTTER_CLOSE")
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

        # 3) power off
        try:
            self._plc_write_coil("POWER_1_SW", False, tag=f"{tag}_PWR1_OFF")
        except Exception:
            pass
        try:
            self._plc_write_coil("POWER_2_SW", False, tag=f"{tag}_PWR2_OFF")
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

    def _plc_pulse_coil(self, coil: Optional[str], pulse_ms: int, *, tag: str) -> None:
        if not coil:
            raise EngineFailed(tag, "coil is None")
        pulse_ms = int(pulse_ms)
        if pulse_ms <= 0:
            pulse_ms = 200
        fut = self.plc.submit_write_coil(coil_name=str(coil), on=True, momentary=True, pulse_ms=pulse_ms, tag=tag)
        self._wait_future(fut, timeout_s=max(self._plc_cmd_timeout_s, pulse_ms / 1000.0 + 0.5), where=f"PLC_PULSE_COIL {coil} ({pulse_ms}ms)")

    def _plc_write_reg(self, reg: Optional[str], value: int, *, tag: str) -> None:
        if not reg:
            raise EngineFailed(tag, "reg is None")
        fut = self.plc.submit_write_reg(reg_name=str(reg), value=int(value), tag=tag)
        self._wait_future(fut, timeout_s=self._plc_cmd_timeout_s, where=f"PLC_WRITE_REG {reg}={value}")

    def _plc_set_dac_ma(self, ch: int, ma: float, *, tag: str) -> None:
        fut = self.plc.submit_set_dac_current(ch=int(ch), ma=float(ma), tag=tag)
        # 결과는 dac code(int)일 수 있음
        _ = self._wait_future(fut, timeout_s=self._plc_cmd_timeout_s, where=f"PLC_SET_DAC_MA ch={ch} ma={ma}")

    @staticmethod
    def _wait_future(fut: Any, *, timeout_s: float, where: str) -> Any:
        """
        PLCService.submit_*()가 반환하는 Future를 기다림.
        """
        try:
            return fut.result(timeout=float(timeout_s))
        except Exception as e:
            raise EngineFailed(where, f"future failed/timeout: {e!r}")

    # --------------------------------------------------------
    # WAIT helpers
    # --------------------------------------------------------
    def _wait_seconds(self, recipe: ProcessRecipe, step: ProcessStep, sec: float) -> None:
        deadline = time.time() + max(0.0, float(sec))
        poll = self._get_poll_s(recipe, step, fallback=0.05)

        while True:
            self._check_stop_pause(recipe, step)
            now = time.time()
            self._tick_emit(recipe, step)
            if now >= deadline:
                return
            time.sleep(min(poll, max(0.0, deadline - now)))

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
        stable_start: Optional[float] = None
        deadline: Optional[float] = None
        if step.timeout_s is not None:
            deadline = time.time() + float(step.timeout_s)

        poll_s = max(0.05, float(step.poll_s or 0.2))

        # None 감시(기본 10초)
        missing_abort_s = sensor_missing_abort_s
        if sensor_value_fn is not None and missing_abort_s is None:
            missing_abort_s = float(getattr(self, "_sensor_missing_abort_s", 10.0) or 10.0)
        last_valid_ts = time.time()

        while True:
            self._check_stop_pause(recipe, step)
            self._tick_emit(recipe, step)

            now = time.time()

            # ✅ 센서 None 지속 감시
            if sensor_value_fn is not None and missing_abort_s is not None and missing_abort_s > 0:
                try:
                    v = sensor_value_fn()
                except Exception:
                    v = None

                if v is not None:
                    last_valid_ts = now
                elif (now - last_valid_ts) >= missing_abort_s:
                    lab = sensor_label or "sensor"
                    raise EngineFailed(step.name, f"{lab}=None 지속 {missing_abort_s:.0f}s 초과(연결 끊김/미보고)")

            if deadline is not None and now >= deadline:
                raise EngineTimeout(step.name, f"timeout waiting {cond_desc}")

            ok = bool(cond_fn())
            if ok:
                if stable_start is None:
                    stable_start = now
                if step.stable_s is None or (now - stable_start) >= float(step.stable_s):
                    return
            else:
                stable_start = None

            time.sleep(poll_s)

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
        now = time.time()

        if (now - self._last_status_emit_ts) >= self._status_emit_interval_s:
            self._emit_status(step_idx=self._current_step_idx, step_name=self._current_step_name)

        tele_int = float(recipe.telemetry_interval_s or 0.5)
        if tele_int <= 0:
            tele_int = 0.5

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
        now = time.time()
        if not force and (now - self._last_status_emit_ts) < self._status_emit_interval_s:
            return
        self._last_status_emit_ts = now  # ✅ 여기서 갱신

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
            self.log.telemetry({
                "step_idx": self._current_step_idx,
                "step_name": self._current_step_name,
                "step_type": step.type.value,
                "phase": self._phase.value,
                "pressure": self._get_pressure(),
                "thickness_A": self._get_thickness(),
                "rate_Aps": self._get_rate(),
                "dac1": getattr(self, "_last_dac_power_1", 0),
                "dac2": getattr(self, "_last_dac_power_2", 0),
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
