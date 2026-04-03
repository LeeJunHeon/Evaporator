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

import concurrent.futures
import threading
import time
import uuid
import math
from dataclasses import dataclass
from typing import Any, Callable, Optional
from process.safety import (
    build_engine_safe_shutdown_steps,
    build_safety_steps
)
from process.evap_runtime import run_evap_deposition_control

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
        turbovac: Optional[Any] = None,
        callbacks: Optional[EngineCallbacks] = None,
        plc_cmd_timeout_s: float = 2.0,
        status_emit_interval_s: float = 0.2,
    ):
        self.plc = plc
        self.stm = stm
        self.acs = acs
        self.turbovac = turbovac
        self.log = log

        self.callbacks = callbacks or EngineCallbacks()

        self._plc_cmd_timeout_s = max(0.2, float(plc_cmd_timeout_s))
        self._status_emit_interval_s = max(0.05, float(status_emit_interval_s))

        # run control flags
        self._stop_event = threading.Event()
        self._stop_mode_value: Optional[StopMode] = None
        self._stop_mode_lock = threading.Lock()

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
        self._stm_log_ts: float = 0.0

        self._last_dac_power_1: Optional[int] = None
        self._last_dac_power_2: Optional[int] = None

        # ✅ ADC readback 캐시
        self._last_adc_power_1: Optional[float] = None
        self._last_adc_power_2: Optional[float] = None

        # ✅ UI 표시용: 마지막 메시지 캐시 (tick emit이 message=""로 덮는 문제 방지)
        self._ui_last_message: str = ""
        self._shutdown_already_executed: bool = False
        self._graph_frozen: bool = False
        self._last_sw_thickness_nm: Optional[float] = None  # 소프트웨어 두께 캐시

    # --------------------------------------------------------
    # External controls (thread-safe-ish: 단순 플래그)
    # --------------------------------------------------------
    def request_stop(self, mode: StopMode = StopMode.STOP) -> None:
        with self._stop_mode_lock:
            self._stop_mode_value = mode
        self._stop_event.set()

    # --------------------------------------------------------
    # Public entry
    # --------------------------------------------------------
    def run(self, recipe: ProcessRecipe, *, run_id: Optional[str] = None) -> EngineResult:
        """
        블로킹 실행. 보통 QThread에서 호출해야 UI가 멈추지 않음.
        """
        # 기본 검증(여기서 한번 더)
        recipe.validate(strict=True)

        with self._stop_mode_lock:
            self._stop_mode_value = None
        self._stop_event.clear()

        self._phase = ProcessPhase.RUNNING
        self._recipe_name = recipe.recipe_name
        self._run_id = run_id or self._make_run_id()
        self._started_ts = time.time()
        self._current_step_idx = -1
        self._current_step_name = ""
        self._last_status_emit_ts = 0.0
        self._last_telemetry_ts = 0.0
        self._stm_log_ts = 0.0

        # ✅ 이전 run의 잔상 제거
        self._last_dac_power_1 = None
        self._last_dac_power_2 = None
        self._last_adc_power_1 = None
        self._last_adc_power_2 = None
        self._ui_last_message = ""
        self._shutdown_already_executed = False

        # run 파일 open/close는 ProcessWindow가 담당한다.
        # engine은 현재 열려 있는 run에 내부 이벤트만 기록한다.
        self._run_line(
            f"RUN START | run_id={self._run_id} | recipe={self._recipe_name}"
        )

        self._emit_status(message="공정 시작")

        err_obj: Optional[ProcessError] = None
        ok = False

        try:
            # ✅ controller 외에 engine도 한 번 더 PLC ready 확인
            self._current_step_idx = -1
            self._current_step_name = "ENGINE_START_CHECK"
            self._ensure_plc_ready(where="ENGINE_START_CHECK")

            for idx, step in enumerate(recipe.steps):
                self._current_step_idx = idx
                self._current_step_name = step.name

                self._check_stop(recipe, step)

                self._emit_step(StepStatus(idx=idx, name=step.name, type=step.type, started_ts=time.time(), ok=None))
                self._emit_status(step_idx=idx, step_name=step.name, message=f"STEP START: {step.type.value}")

                # step 실행
                self._execute_step(recipe, step)

                self._emit_step(StepStatus(idx=idx, name=step.name, type=step.type, finished_ts=time.time(), ok=True))
                self._emit_status(step_idx=idx, step_name=step.name, message="STEP OK")

            ok = True
            self._phase = ProcessPhase.FINISHED
            self._run_line(
                f"RUN FINISHED OK | run_id={self._run_id} | recipe={self._recipe_name}"
            )
            self._emit_status(message="공정 완료")

        except EngineStopRequested as e:
            # STOP/ABORT 요청
            self._phase = ProcessPhase.STOPPING
            self._emit_status(message=f"정지 요청 처리중: {e.mode.value}")
            self._log_warn(f"Stop requested: {e.mode.value}", tag="ENGINE", also_ui=True)

            # runtime(EVAP)에서 이미 안전정지를 수행한 경우 중복 실행 금지
            if not self._shutdown_already_executed:
                self._safe_shutdown_sequence(tag=f"STOP_{e.mode.value}", mode=e.mode)

            self._phase = ProcessPhase.FINISHED
            self._run_line(
                f"RUN STOPPED | run_id={self._run_id} | recipe={self._recipe_name} | mode={e.mode.value}"
            )
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
            self._run_line(
                f"RUN ERROR | run_id={self._run_id} | recipe={self._recipe_name} | "
                f"where={err_obj.where} | error={err_obj.message}"
            )

            # runtime(EVAP)에서 이미 안전정지를 수행한 경우 중복 실행 금지
            if not self._shutdown_already_executed:
                self._safe_shutdown_sequence(tag="ERROR_ABORT", mode=StopMode.ABORT)

            ok = False

        finally:
            finished_ts = time.time()

            # run 파일 open/close는 ProcessWindow가 담당한다.
            # engine은 상태만 마무리한다.
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
        t = step.type

        self._tick_emit(recipe, step)

        if t in (StepType.LOG, StepType.MARK):
            self._execute_meta_step(recipe, step)
            return

        if t in (
            StepType.PLC_WRITE_COIL,
            StepType.PLC_PULSE_COIL,
            StepType.PLC_WRITE_REG,
            StepType.PLC_SET_DAC_MA,
        ):
            self._execute_plc_step(step)
            return

        if t in (
            StepType.WAIT_SECONDS,
            StepType.WAIT_PRESSURE_LEQ,
            StepType.WAIT_THICKNESS_GEQ,
            StepType.WAIT_RATE_IN_RANGE,
            StepType.WAIT_COIL_IS,
        ):
            self._execute_wait_step(recipe, step)
            return

        raise EngineFailed(step.name, f"unsupported step type: {t}")


    def _execute_meta_step(self, recipe: ProcessRecipe, step: ProcessStep) -> None:
        if step.type == StepType.LOG:
            self._log_info(step.message, tag="PROCESS", also_ui=True)
            return

        if step.type == StepType.MARK:
            # MARK 스텝 중 이름이 "EVAP_DEPOSITION_CONTROL"이면 증착 제어 루프로 진입
            if step.name == "EVAP_DEPOSITION_CONTROL":
                run_evap_deposition_control(self, recipe, step)
                return

            self._log_info(f"MARK: {step.name}", tag="PROCESS", also_ui=False)
            return

        raise EngineFailed(step.name, f"unsupported meta step type: {step.type}")


    def _execute_plc_step(self, step: ProcessStep) -> None:
        t = step.type

        if t == StepType.PLC_WRITE_COIL:
            coil = str(step.coil or "")
            on = bool(step.on)
            self._emit_status(
                step_idx=self._current_step_idx,
                step_name=self._current_step_name,
                message=self._ui_coil_text(coil, on),
                force=True,
            )
            self._plc_write_coil(coil, on, tag=step.name)
            return

        if t == StepType.PLC_PULSE_COIL:
            coil = str(step.coil or "")
            ms = int(step.pulse_ms or 200)
            self._emit_status(
                step_idx=self._current_step_idx,
                step_name=self._current_step_name,
                message=self._ui_pulse_text(coil, ms),
                force=True,
            )
            self._plc_pulse_coil(coil, ms, tag=step.name)
            return

        if t == StepType.PLC_WRITE_REG:
            reg = str(step.reg or "")
            v = int(step.value or 0)
            self._emit_status(
                step_idx=self._current_step_idx,
                step_name=self._current_step_name,
                message=self._ui_reg_text(reg, v),
                force=True,
            )
            self._plc_write_reg(reg, v, tag=step.name)
            return

        if t == StepType.PLC_SET_DAC_MA:
            self._plc_set_dac_ma(int(step.dac_ch or 1), float(step.dac_ma or 4.0), tag=step.name)
            return

        raise EngineFailed(step.name, f"unsupported plc step type: {t}")


    def _execute_wait_step(self, recipe: ProcessRecipe, step: ProcessStep) -> None:
        t = step.type

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
                sensor_value_fn=self._get_rate,
                sensor_label="STM dep.rate",
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

        raise EngineFailed(step.name, f"unsupported wait step type: {t}")

    def _safe_shutdown_sequence(
        self,
        *,
        tag: str,
        mode: Optional[StopMode] = None,
    ) -> None:
        """
        기존 engine.py의 하드코딩 안전정지 순서를 그대로 유지하되,
        실제 step 정의는 process/safety.py 에서 가져와 실행한다.

        순서(최신 safety plan 기준):
        1) MAIN_SHUTTER close
        2) SHUTTER_1_SW close
        3) SHUTTER_2_SW close
        4) DAC pair ramp-down marker 해석
        - 현재 DAC 값에서 시작
        - step_dac 만큼 감소
        - interval_s 간격으로 반복
        - 0까지 clamp
        5) POWER_1_SW off
        6) POWER_2_SW off
        7) FTM_SW off

        실패해도 예외는 삼키고 계속 진행(best-effort)한다.
        """
        if not self._is_plc_ready():
            self._log_warn(
                f"[SAFETY:{tag}] PLC 미연결 상태라 안전 출력 시퀀스를 실제로 전달할 수 없습니다.",
                tag="ENGINE",
                also_ui=True,
            )
            return

        try:
            if mode is None:
                steps = build_engine_safe_shutdown_steps()
                plan_name = "build_engine_safe_shutdown_steps"
            else:
                steps = build_safety_steps(mode)
                plan_name = f"build_safety_steps({mode.value})"
        except Exception as e:
            self._log_error(
                f"[SAFETY:{tag}] {plan_name} failed: {e!r}",
                tag="ENGINE",
                also_ui=True,
            )
            return

        for st in steps:
            try:
                meta = getattr(st, "meta", None)
                if not isinstance(meta, dict):
                    meta = {}

                action = str(
                    (getattr(st, "action", None) or meta.get("action") or "")
                ).strip().lower()

                step_dac_raw = getattr(st, "step_dac", None)
                if step_dac_raw is None:
                    step_dac_raw = meta.get("step_dac", 100)

                interval_s_raw = getattr(st, "interval_s", None)
                if interval_s_raw is None:
                    interval_s_raw = meta.get("interval_s", 1.0)

                if st.type == StepType.PLC_WRITE_COIL:
                    self._plc_write_coil(
                        st.coil,
                        bool(st.on),
                        tag=f"{tag}_{st.name}",
                    )

                elif st.type == StepType.PLC_WRITE_REG:
                    reg = str(st.reg or "")
                    value = int(st.value or 0)
                    self._plc_write_reg(
                        reg,
                        value,
                        tag=f"{tag}_{st.name}",
                    )

                elif st.type in (StepType.MARK, StepType.LOG) and action == "ramp_down_dac_pair":
                    self._run_shutdown_dac_pair_ramp_down(
                        step_dac=int(step_dac_raw or 100),
                        interval_s=float(interval_s_raw or 1.0),
                        tag=f"{tag}_{st.name}",
                    )

                elif st.type == StepType.LOG:
                    self._run_line(st.message or st.name)

                elif st.type == StepType.MARK:
                    self._log_warn(
                        f"[SAFETY:{tag}] unsupported safety MARK action: "
                        f"name={st.name}, action={action!r}",
                        tag="ENGINE",
                        also_ui=True,
                    )

                else:
                    self._log_warn(
                        f"[SAFETY:{tag}] unsupported safety step type: {st.type}",
                        tag="ENGINE",
                        also_ui=True,
                    )

            except Exception as e:
                self._log_warn(
                    f"[SAFETY:{tag}] step failed but continue: {st.name} / {e!r}",
                    tag="ENGINE",
                    also_ui=True,
                )

    def _get_reg_int_from_snapshot(self, reg_name: str) -> Optional[int]:
        """
        PLC snapshot.regs 에서 정수형 레지스터 값을 읽는다.
        - 없으면 None
        - 형변환 실패해도 None
        """
        snap = self._get_plc_snapshot()
        if snap is None:
            return None
        if not getattr(snap, "connected", False):
            return None

        regs = getattr(snap, "regs", None)
        if not isinstance(regs, dict):
            return None

        raw = regs.get(str(reg_name))
        if raw is None:
            return None

        try:
            return int(float(raw))
        except Exception:
            return None


    def _get_shutdown_dac_start_pair(self) -> tuple[int, int]:
        """
        종료 ramp-down 시작 DAC를 결정한다.

        우선순위:
        1) engine 내부 last cache (_last_dac_power_1/_2)
        2) PLC snapshot.regs["DAC_POWER_1"/"DAC_POWER_2"] fallback
        3) 둘 다 없으면 0
        """
        snap_dac1 = self._get_reg_int_from_snapshot("DAC_POWER_1")
        snap_dac2 = self._get_reg_int_from_snapshot("DAC_POWER_2")

        dac1 = self._last_dac_power_1 if self._last_dac_power_1 is not None else snap_dac1
        dac2 = self._last_dac_power_2 if self._last_dac_power_2 is not None else snap_dac2

        return max(0, int(dac1 or 0)), max(0, int(dac2 or 0))


    def _run_shutdown_dac_pair_ramp_down(
        self,
        *,
        step_dac: int = 100,
        interval_s: float = 1.0,
        tag: str,
    ) -> None:
        """
        종료 전용 DAC pair ramp-down.
        - 1초 간격(기본)
        - 100씩 감소(기본)
        - 0 clamp
        - DAC1/DAC2 각각 독립 처리
        - shutdown 경로이므로 _wait_seconds()를 쓰지 않고 직접 sleep 한다.
        """
        step_dac = max(1, int(step_dac or 100))
        interval_s = max(0.0, float(interval_s or 1.0))

        dac1, dac2 = self._get_shutdown_dac_start_pair()

        self._log_info(
            f"[SAFETY:{tag}] ramp_down_dac_pair start | "
            f"dac1={dac1}, dac2={dac2}, step_dac={step_dac}, interval_s={interval_s}",
            tag="ENGINE",
            also_ui=True,
        )

        # DAC가 이미 0이면 ramp-down 생략: 불필요한 PLC 명령 전송 방지
        if dac1 <= 0 and dac2 <= 0:
            self._last_dac_power_1 = 0
            self._last_dac_power_2 = 0
            self._emit_status(message="POWER RAMP DOWN (DAC1=0, DAC2=0)", force=True)
            return

        while True:
            next_dac1 = max(0, dac1 - step_dac) if dac1 > 0 else 0
            next_dac2 = max(0, dac2 - step_dac) if dac2 > 0 else 0

            # 채널별 독립 처리
            if next_dac1 != dac1:
                self._plc_write_reg("DAC_POWER_1", next_dac1, tag=f"{tag}_DAC1_RAMP")

            if next_dac2 != dac2:
                self._plc_write_reg("DAC_POWER_2", next_dac2, tag=f"{tag}_DAC2_RAMP")

            self._emit_status(
                message=f"POWER RAMP DOWN (DAC1={next_dac1}, DAC2={next_dac2})",
                force=True,
            )
            self._run_line(
                f"[SAFETY:{tag}] DAC ramp -> DAC_POWER_1={next_dac1}, DAC_POWER_2={next_dac2}"
            )

            dac1, dac2 = next_dac1, next_dac2

            if dac1 <= 0 and dac2 <= 0:
                break

            time.sleep(interval_s)

        # 최종 캐시 정리
        self._last_dac_power_1 = 0
        self._last_dac_power_2 = 0

    # --------------------------------------------------------
    # PLC wrappers
    # --------------------------------------------------------
    def _plc_write_coil(self, coil: Optional[str], on: bool, *, tag: str) -> None:
        if not coil:
            raise EngineFailed(tag, "coil is None")

        # ✅ 자동 공정에서 Power ON 전 TMP 상태 확인
        self._check_tmp_interlock_for_power_coil(str(coil), bool(on), tag=tag)

        # PLC 명령을 Future로 비동기 제출 후, 완료 대기(블로킹)
        fut = self.plc.submit_write_coil(
            coil_name=str(coil),
            on=bool(on),
            momentary=False,
            pulse_ms=None,
            tag=tag,
        )
        self._wait_future(
            fut,
            timeout_s=self._plc_cmd_timeout_s,
            where=f"PLC_WRITE_COIL {coil}={on}",
        )
        self._tele_event(
            event="WRITE_COIL",
            target=str(coil),
            value=int(bool(on)),
            detail=f"tag={tag}",
        )

    def _plc_pulse_coil(self, coil: Optional[str], pulse_ms: int, *, tag: str) -> None:
        if not coil:
            raise EngineFailed(tag, "coil is None")
        pulse_ms = int(pulse_ms)
        if pulse_ms <= 0:
            pulse_ms = 200
        fut = self.plc.submit_write_coil(coil_name=str(coil), on=True, momentary=True, pulse_ms=pulse_ms, tag=tag)
        self._wait_future(fut, timeout_s=max(self._plc_cmd_timeout_s, pulse_ms / 1000.0 + 0.5), where=f"PLC_PULSE_COIL {coil} ({pulse_ms}ms)")
        self._tele_event(event="PULSE_COIL", target=str(coil), value=int(pulse_ms), detail=f"tag={tag}")

    def _plc_write_reg(self, reg: Optional[str], value: int, *, tag: str, silent: bool = False) -> None:
        if not reg:
            raise EngineFailed(tag, "reg is None")

        reg_name = str(reg)
        v = int(value)

        # silent=True이면 PLC 서비스에도 silent 전달 → sig_cmd_trace 미발행
        fut = self.plc.submit_write_reg(reg_name=reg_name, value=v, tag=tag, silent=silent)
        self._wait_future(fut, timeout_s=self._plc_cmd_timeout_s, where=f"PLC_WRITE_REG {reg_name}={v}")

        # ✅ telemetry(dac1/dac2) 정확도 확보: DAC 레지스터면 마지막값 갱신
        rn = reg_name.upper()
        if rn == "DAC_POWER_1":
            self._last_dac_power_1 = v
        elif rn == "DAC_POWER_2":
            self._last_dac_power_2 = v

        # silent=True이면 _tele_event(CSV detail 행)도 생략
        if not silent:
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
        except concurrent.futures.TimeoutError as e:
            raise EngineTimeout(where, f"{msg + ': ' if msg else ''}future timeout: {e!r}")
        except Exception as e:
            raise EngineFailed(where, f"{msg + ': ' if msg else ''}future failed: {e!r}")

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
            self._check_stop(recipe, step)
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

            # ✅ 표시 제목: step.message가 있으면 그것을 우선 사용("텍스트 직접 지정" 지원)
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
            self._check_stop(recipe, step)
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
                    # 조건을 처음 만족한 시점 기록 → stable_s 동안 유지해야 통과
                    stable_start_m = now_m
                if step.stable_s is None or (now_m - stable_start_m) >= float(step.stable_s):
                    # 마지막 1회 표시(성공)
                    elapsed_s = now_m - start_m
                    prefix = (str(getattr(step, "message", "") or "").strip()) or f"대기: {cond_desc}"
                    self._emit_status(message=f"{prefix} | 조건 만족 (경과 {self._fmt_hms(elapsed_s)})", force=True)
                    return
            else:
                # 조건이 깨지면 stable 타이머 리셋 → 처음부터 다시 안정 구간 측정
                stable_start_m = None

            # UI 표시(조건/경과/남은)
            if now_m >= next_ui_m:
                elapsed_s = now_m - start_m
                # ✅ 표시 제목: step.message가 있으면 그것을 우선 사용("텍스트 직접 지정" 지원)
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
    # Stop check
    # --------------------------------------------------------
    def _check_stop(self, recipe: ProcessRecipe, step: ProcessStep) -> None:
        with self._stop_mode_lock:
            _mode = self._stop_mode_value
        if _mode is not None:
            raise EngineStopRequested(_mode)

    def _is_plc_ready(self) -> bool:
        try:
            if hasattr(self.plc, "is_running") and not self.plc.is_running():
                return False

            if hasattr(self.plc, "is_connected"):
                return bool(self.plc.is_connected())

            snap = self.plc.get_last_snapshot() if hasattr(self.plc, "get_last_snapshot") else None
            return bool(getattr(snap, "connected", False)) if snap is not None else False
        except Exception:
            return False

    def _ensure_plc_ready(self, *, where: str) -> None:
        if not self._is_plc_ready():
            raise EngineFailed(where, "PLC가 연결되지 않았거나 I/O 가능한 상태가 아닙니다.")

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
    
    def _get_power_read_pair(self) -> tuple[Optional[float], Optional[float]]:
        """
        PLC snapshot.regs 에서 ADC readback(POWER_READ_1/2)을 읽는다.
        """
        snap = self._get_plc_snapshot()
        if snap is None:
            return None, None
        if not getattr(snap, "connected", False):
            return None, None

        regs = getattr(snap, "regs", None)
        if not isinstance(regs, dict):
            return None, None

        def _to_float(v: Any) -> Optional[float]:
            if v is None:
                return None
            try:
                return float(v)
            except Exception:
                return None

        return _to_float(regs.get("POWER_READ_1")), _to_float(regs.get("POWER_READ_2"))

    def _get_power_read_raw_pair(self) -> tuple[Optional[float], Optional[float]]:
        """
        PLC snapshot.regs 에서 ADC 원본(POWER_READ_1_RAW/2_RAW)을 읽는다.
        CSV adc1_raw/adc2_raw 컬럼용.
        """
        snap = self._get_plc_snapshot()
        if snap is None:
            return None, None
        regs = getattr(snap, "regs", None)
        if not isinstance(regs, dict):
            return None, None

        def _to_float(v: Any) -> Optional[float]:
            if v is None:
                return None
            try:
                return float(v)
            except Exception:
                return None

        return _to_float(regs.get("POWER_READ_1_RAW")), _to_float(regs.get("POWER_READ_2_RAW"))

    def _get_power_read_pair_cached(self) -> tuple[Optional[float], Optional[float]]:
        """
        현재 snapshot 값이 있으면 캐시를 갱신하고,
        없으면 마지막 정상 ADC 값을 유지한다.
        """
        adc1, adc2 = self._get_power_read_pair()

        if adc1 is not None:
            self._last_adc_power_1 = float(adc1)
        if adc2 is not None:
            self._last_adc_power_2 = float(adc2)

        return self._last_adc_power_1, self._last_adc_power_2

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
    # TMP 인터락
    # --------------------------------------------------------
    def _check_tmp_interlock_for_power_coil(self, coil: str, on: bool, *, tag: str) -> None:
        """
        현재 PLC에서 Turbo를 제거하며 사라진 Power 관련 인터락만 자동 공정에서 복원한다.
        - POWER_1_SW / POWER_2_SW ON 전에 TMP ready for power 확인
        """
        if not bool(on):
            return

        coil_u = str(coil or "").upper()
        if coil_u not in ("POWER_1_SW", "POWER_2_SW"):
            return

        svc = getattr(self, "turbovac", None)
        if svc is None:
            raise EngineFailed(tag, f"TMP service is not set (coil={coil_u})")

        fn = getattr(svc, "check_tmp_ready_for_power", None)
        if not callable(fn):
            raise EngineFailed(tag, "TMP helper missing: check_tmp_ready_for_power")

        try:
            result = fn()
        except Exception as e:
            raise EngineFailed(tag, f"TMP helper failed: {e!r}")

        ok = False
        reason = ""
        if isinstance(result, tuple) and len(result) == 2:
            ok = bool(result[0])
            reason = str(result[1] or "")
        else:
            ok = bool(result)

        if not ok:
            if not reason:
                reason = f"TMP interlock blocked: coil={coil_u}"
            raise EngineFailed(tag, reason)

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
        self._last_status_emit_ts = now

        if message:
            self._ui_last_message = message
        else:
            # 메시지가 없는 tick emit이 들어와도 마지막 메시지를 유지해 UI가 빈 문자열로 덮이는 것 방지
            message = self._ui_last_message

        adc1, adc2 = self._get_power_read_raw_pair()

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
            dac1=(self._last_dac_power_1 if self._is_plc_ready() else None),
            dac2=(self._last_dac_power_2 if self._is_plc_ready() else None),
            adc1=adc1,
            adc2=adc2,
        )

        cb = self.callbacks.on_status
        if cb:
            try:
                cb(st)
            except Exception:
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
        try:
            adc1_raw, adc2_raw = self._get_power_read_raw_pair()
            rate = self._get_rate()
            thickness_a = self._get_thickness()
            thickness_nm = (thickness_a / 10.0) if thickness_a is not None else None

            self.log.telemetry({
                "step": (self._ui_last_message or self._current_step_name),
                "detail": "",
                "pressure_torr": self._get_pressure(),
                "dac1": (self._last_dac_power_1 if self._is_plc_ready() else None),
                "dac2": (self._last_dac_power_2 if self._is_plc_ready() else None),
                "adc1": adc1_raw,
                "adc2": adc2_raw,
                "dep.rate": rate,
                "thickness_nm": thickness_nm,
                "sw_thickness_nm": self._last_sw_thickness_nm,
            })

            now = time.monotonic()
            if self._graph_frozen:
                return
            if (now - self._stm_log_ts) >= 1.0:
                self._stm_log_ts = now
                pressure = self._get_pressure()
                adc1_v, adc2_v = self._get_power_read_raw_pair()
                rate_v = rate if rate is not None else 0.0
                thick_nm = (thickness_a / 10.0) if thickness_a is not None else None

                # step.meta에서 파워 선택 정보 읽기
                _meta = dict(step.meta or {}) if step is not None and hasattr(step, "meta") else {}
                _use_p1 = bool(_meta.get("use_power1", False))
                _use_p2 = bool(_meta.get("use_power2", False))

                # DAC 문자열 구성
                _dac_parts = []
                if _use_p1 and self._last_dac_power_1 is not None:
                    _dac_parts.append(f"DAC1={self._last_dac_power_1}")
                if _use_p2 and self._last_dac_power_2 is not None:
                    _dac_parts.append(f"DAC2={self._last_dac_power_2}")
                # meta 없는 step이면 nonzero DAC fallback
                if not _dac_parts and not (_use_p1 or _use_p2):
                    if self._last_dac_power_1 is not None:
                        _dac_parts.append(f"DAC1={self._last_dac_power_1}")
                    if self._last_dac_power_2 is not None:
                        _dac_parts.append(f"DAC2={self._last_dac_power_2}")

                # ADC 문자열 (현재 배선: 항상 ADC2 피드백)
                _adc_str = f"ADC2={adc2_v:.1f}" if adc2_v is not None else "ADC2=---"

                _plc_str = (", ".join(_dac_parts) + ", " + _adc_str) if _dac_parts else _adc_str

                pres_str = f"{pressure:.2e}" if pressure is not None else "---"
                sw_th = self._last_sw_thickness_nm
                stm_str = (
                    f"rate={rate_v:.3f} Å/s, STM_thick={thick_nm:.2f} nm"
                    + (f", SW_thick={sw_th:.2f} nm" if sw_th is not None else ", SW_thick=---")
                    if thick_nm is not None else "---"
                )
                self._log_info(
                    f"[POLL] PLC: {_plc_str} | ACS: {pres_str} Torr | STM: {stm_str}",
                    tag="POLL",
                    also_ui=True,
                )
        except Exception:
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

    def _run_line(self, text: str) -> None:
        """
        UI를 거치지 않고 현재 run의 ProcessWindowLog(.log)에 직접 기록.
        run이 아직 없으면 log_service 쪽에서 [PROCESS][NO-RUN] fallback 처리됨.
        """
        try:
            self.log.run_line(str(text))
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
        try:
            line = f"{str(event)} {str(target)}={value}"
            if detail:
                line += f" | {str(detail)}"

            adc1_raw, adc2_raw = self._get_power_read_raw_pair()
            _th_a = self._get_thickness()
            _th_nm = (_th_a / 10.0) if _th_a is not None else None

            self.log.telemetry({
                "step": (self._ui_last_message or self._current_step_name),
                "detail": line,
                "pressure_torr": self._get_pressure(),
                "dac1": (self._last_dac_power_1 if self._is_plc_ready() else None),
                "dac2": (self._last_dac_power_2 if self._is_plc_ready() else None),
                "adc1": adc1_raw,
                "adc2": adc2_raw,
                "dep.rate": self._get_rate(),
                "thickness_nm": _th_nm,
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

        # divmod로 H/M/S 분리 후 1시간 미만이면 MM:SS 형식 사용
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
