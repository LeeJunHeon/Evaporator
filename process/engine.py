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

            # 여기서 “안전정지 시퀀스”를 넣을 수도 있음(장비마다 다르므로 기본은 최소화)
            self._do_minimal_shutdown(e.mode)

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

            # 에러 시에도 최소 안전조치
            self._do_minimal_shutdown(StopMode.ABORT)
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
            # UI trace용 마커
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
            target = float(step.thickness_target_a)
            self._wait_condition(
                recipe=recipe,
                step=step,
                cond_fn=lambda: self._get_thickness_ok_geq(target),
                cond_desc=f"thickness >= {target}A",
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
    ) -> None:
        """
        cond_fn이 True가 될 때까지 대기.
        - timeout_s: 제한 시간
        - stable_s: stable 동안 연속 True면 성공
        - poll_s: 재확인 주기
        - on_timeout: 정책(ABORT/CONTINUE/ERROR)
        """
        poll = self._get_poll_s(recipe, step, fallback=recipe.default_poll_s)
        timeout_s = float(step.timeout_s) if step.timeout_s is not None else None
        stable_s = float(step.stable_s) if step.stable_s is not None else 0.0

        t_start = time.time()
        deadline = (t_start + timeout_s) if timeout_s is not None else None

        stable_start: Optional[float] = None

        while True:
            self._check_stop_pause(recipe, step)
            now = time.time()

            ok = False
            try:
                ok = bool(cond_fn())
            except Exception as e:
                # 조건 평가 중 예외는 센서/스냅샷 None 같은 경우가 많음 -> ok=False로 계속 대기
                self._log_warn(f"condition eval error ignored: {e!r}", tag="ENGINE", also_ui=False)
                ok = False

            if ok:
                if stable_s <= 0:
                    self._log_info(f"WAIT OK: {cond_desc}", tag="ENGINE", also_ui=False)
                    return
                if stable_start is None:
                    stable_start = now
                elif (now - stable_start) >= stable_s:
                    self._log_info(f"WAIT OK(stable {stable_s}s): {cond_desc}", tag="ENGINE", also_ui=False)
                    return
            else:
                stable_start = None

            # timeout 검사
            if deadline is not None and now >= deadline:
                detail = f"{cond_desc} (timeout={timeout_s}s, stable={stable_s}s)"
                self._handle_timeout(step, detail)
                return  # CONTINUE인 경우 여기 도달

            # 주기 상태/텔레메트리 emit
            self._tick_emit(recipe, step)

            time.sleep(poll)

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
            self._last_status_emit_ts = now
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
            # tick_emit가 아닌 외부 호출에서 스팸 방지
            pass

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
            })
        except Exception:
            # 텔레메트리 실패해도 공정은 계속
            pass

    # --------------------------------------------------------
    # Minimal shutdown (기본은 최소화)
    # --------------------------------------------------------
    def _do_minimal_shutdown(self, mode: StopMode) -> None:
        """
        장비마다 '진짜 안전정지'가 달라서 엔진에서 함부로 모든 밸브/유틸을 끄면 오히려 위험할 수 있음.
        그래서 기본은 최소 OFF만 수행.

        현재 구현(보수적):
        - POWER_1_SW, POWER_2_SW OFF 시도
        - GAS_1_SW, GAS_2_SW OFF 시도
        (실패해도 예외 삼킴)

        나중에 사용자가 원하는 안전 시퀀스가 정해지면:
        - 엔진에 shutdown recipe(steps)나 안전정지 함수 훅을 추가하는 방식으로 확장 권장
        """
        to_off = ["POWER_1_SW", "POWER_2_SW", "GAS_1_SW", "GAS_2_SW"]
        for coil in to_off:
            try:
                self.plc.enqueue_write_coil(coil, False, tag=f"SHUTDOWN_{mode.value}")
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
