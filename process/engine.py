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
from process.safety import build_engine_safe_shutdown_steps
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

        self._run_line(
            f"RUN START | run_id={self._run_id} | recipe={self._recipe_name}"
        )
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
            self._run_line(
                f"RUN FINISHED OK | run_id={self._run_id} | recipe={self._recipe_name}"
            )
            self._emit_status(message="공정 완료")

        except EngineStopRequested as e:
            # STOP/ABORT 요청
            self._phase = ProcessPhase.STOPPING
            self._emit_status(message=f"정지 요청 처리중: {e.mode.value}")
            self._log_warn(f"Stop requested: {e.mode.value}", tag="ENGINE", also_ui=True)

            # ✅ EV 안전정지 시퀀스 통일 (셔터닫기 → DAC0 → PowerOff)
            self._safe_shutdown_sequence(tag=f"STOP_{e.mode.value}")

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
                run_evap_deposition_control(self, recipe, step)
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

    def _safe_shutdown_sequence(self, *, tag: str) -> None:
        """
        기존 engine.py의 하드코딩 안전정지 순서를 그대로 유지하되,
        실제 step 정의는 process/safety.py 에서 가져와 실행한다.

        순서(기존과 동일):
        1) MAIN_SHUTTER close
        2) DAC_POWER_1 = 0
        3) DAC_POWER_2 = 0
        4) SHUTTER_1_SW close
        5) SHUTTER_2_SW close
        6) POWER_1_SW off
        7) POWER_2_SW off
        8) FTM_SW off

        실패해도 예외는 삼키고 계속 진행(best-effort)한다.
        """
        try:
            steps = build_engine_safe_shutdown_steps()
        except Exception as e:
            self._log_error(
                f"[SAFETY:{tag}] build_engine_safe_shutdown_steps failed: {e!r}",
                tag="ENGINE",
                also_ui=True,
            )
            return

        for st in steps:
            try:
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
                    # _plc_write_reg 안에서 DAC last 값도 이미 동기화됨

                elif st.type == StepType.LOG:
                    # 현재 legacy sequence에는 LOG step을 안 쓰지만,
                    # 혹시 plan에 추가되더라도 안전하게 기록 가능하도록 둠
                    self._run_line(st.message or st.name)

                else:
                    self._log_warn(
                        f"[SAFETY:{tag}] unsupported safety step type: {st.type}",
                        tag="ENGINE",
                        also_ui=True,
                    )

            except Exception as e:
                # 기존 엔진과 동일하게 best-effort
                self._log_warn(
                    f"[SAFETY:{tag}] step failed but continue: {st.name} / {e!r}",
                    tag="ENGINE",
                    also_ui=True,
                )

    # --------------------------------------------------------
    # PLC wrappers
    # --------------------------------------------------------
    def _plc_write_coil(self, coil: Optional[str], on: bool, *, tag: str) -> None:
        if not coil:
            raise EngineFailed(tag, "coil is None")

        # ✅ 자동 공정에서 Power ON 전 TMP 상태 확인
        self._check_tmp_interlock_for_power_coil(str(coil), bool(on), tag=tag)

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
