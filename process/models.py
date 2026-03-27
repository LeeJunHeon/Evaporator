# -*- coding: utf-8 -*-
"""
process/models.py

공정(Recipe/Step/State) 모델 정의 파일.

목표
- UI에서 입력/로드한 레시피를 "정규화된 구조"로 만들기
- 공정 엔진(engine.py)이 이 모델만 보고 순차 실행할 수 있도록 하기
- 값 검증/타입 체크를 모델에서 확실히 처리(운영 안정성)

주의
- PLC/STM/ACS 같은 장비 객체는 여기서 다루지 않음
- 여기서는 "무엇을 할지"만 정의 (how는 engine/worker에서 수행)
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
import time


# ============================================================
# Enums
# ============================================================

class ProcessPhase(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FINISHED = "FINISHED"
    ERROR = "ERROR"


class StopMode(str, Enum):
    """
    공정 중지 모드(엔진에서 안전정지/즉시정지 분기할 때 사용)
    """
    STOP = "STOP"      # 정상적인 안전 시퀀스(권장)
    ABORT = "ABORT"    # 가능한 빨리 안전 상태로
    ESTOP = "ESTOP"    # UI/하드웨어 비상정지 개념(필요 시)


class StepType(str, Enum):
    """
    Step 종류.
    엔진은 step.type을 보고 동작을 분기한다.
    """
    # --- PLC actions ---
    PLC_WRITE_COIL = "PLC_WRITE_COIL"     # coil ON/OFF
    PLC_PULSE_COIL = "PLC_PULSE_COIL"     # coil momentary pulse
    PLC_WRITE_REG = "PLC_WRITE_REG"       # register write
    PLC_SET_DAC_MA = "PLC_SET_DAC_MA"     # set DAC output in mA

    # --- Wait / Conditions ---
    WAIT_SECONDS = "WAIT_SECONDS"         # simply sleep
    WAIT_PRESSURE_LEQ = "WAIT_PRESSURE_LEQ"   # ACS pressure <= target
    WAIT_THICKNESS_GEQ = "WAIT_THICKNESS_GEQ" # STM thickness >= target
    WAIT_RATE_IN_RANGE = "WAIT_RATE_IN_RANGE" # STM rate within [min,max]
    WAIT_COIL_IS = "WAIT_COIL_IS"         # PLC coil == expected

    # --- Utility ---
    LOG = "LOG"                           # log line only
    MARK = "MARK"                         # step marker for UI/trace


class OnTimeout(str, Enum):
    """
    WAIT 계열 스텝에서 timeout 발생 시 엔진이 어떻게 처리할지 정책.
    """
    ABORT = "ABORT"        # 공정 실패 처리(권장)
    CONTINUE = "CONTINUE"  # 경고만 남기고 다음 단계로
    ERROR = "ERROR"        # 즉시 에러 상태 전환(ABORT와 유사)


# ============================================================
# Known PLC names (typo 방지용)
# - 실제 PLC.py 내부 매핑과 동일한 이름을 쓰는 게 핵심
# - 엔진에서 PLCService.enqueue_write_coil("AIR_SW", True) 같은 식으로 호출
# ============================================================

KNOWN_COILS = {
    "R_P_SW", "R_V_SW", "F_V_SW", "M_V_SW", "V_V_SW", "TMP_SW",
    "SHUTTER_1_SW", "SHUTTER_2_SW", "MAIN_SHUTTER_SW",
    "POWER_1_SW", "POWER_2_SW",
    "FTM_SW", "DOOR_SW",
    "AIR_SW", "WATER_SW", "GAUGE_1_SW", "GAUGE_2_SW",
}

# ✅ UI/PLC 폴링으로 읽기만 하는 인디케이터 코일 (레시피에서 write 금지 권장)
READ_ONLY_COILS = {"AIR_SW", "WATER_SW", "GAUGE_1_SW", "GAUGE_2_SW"}

KNOWN_REGS = {
    "DAC_POWER_1",
    "DAC_POWER_2",
}


# ============================================================
# Utility Validators
# ============================================================

def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)

def _req(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)

def _opt_float(x: Any, field_name: str) -> Optional[float]:
    if x is None:
        return None
    _req(_is_num(x), f"{field_name} must be a number (got {type(x).__name__})")
    return float(x)

def _opt_int(x: Any, field_name: str) -> Optional[int]:
    if x is None:
        return None
    _req(isinstance(x, int) and not isinstance(x, bool), f"{field_name} must be int (got {type(x).__name__})")
    return int(x)

def _safe_str(x: Any) -> str:
    return "" if x is None else str(x)

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ============================================================
# Core Models
# ============================================================

@dataclass
class ProcessStep:
    """
    공정 스텝 정의.

    type별로 사용하는 필드가 다름(아래 validate() 참고)
    - PLC_WRITE_COIL:
        coil, on
    - PLC_PULSE_COIL:
        coil, pulse_ms
    - PLC_WRITE_REG:
        reg, value
    - PLC_SET_DAC_MA:
        dac_ch, dac_ma
    - WAIT_SECONDS:
        seconds
    - WAIT_PRESSURE_LEQ:
        pressure_target, timeout_s, stable_s(optional), poll_s(optional)
    - WAIT_THICKNESS_GEQ:
        thickness_target_a, timeout_s, stable_s(optional), poll_s(optional)
    - WAIT_RATE_IN_RANGE:
        rate_min_a_s, rate_max_a_s, timeout_s ...
    - WAIT_COIL_IS:
        coil, expected, timeout_s ...
    - LOG:
        message
    """

    name: str
    type: StepType

    # --- PLC action fields ---
    coil: Optional[str] = None
    on: Optional[bool] = None
    pulse_ms: Optional[int] = None

    reg: Optional[str] = None
    value: Optional[int] = None

    dac_ch: Optional[int] = None        # 1 or 2 (현재 프로젝트 기준)
    dac_ma: Optional[float] = None      # 4~20mA

    # --- Wait fields ---
    seconds: Optional[float] = None

    pressure_target: Optional[float] = None          # 단위: device 그대로(보통 Torr 또는 mbar, 너의 driver 기준)
    thickness_target_a: Optional[float] = None       # Angstrom
    rate_min_a_s: Optional[float] = None             # Angstrom/s
    rate_max_a_s: Optional[float] = None             # Angstrom/s

    expected: Optional[bool] = None                  # WAIT_COIL_IS 에서 기대값

    # --- Common timing control for waits ---
    timeout_s: Optional[float] = None                # None이면 무한대(권장 X)
    stable_s: Optional[float] = None                 # 조건을 stable_s 동안 연속 만족해야 통과
    poll_s: Optional[float] = None                   # 센서/coil 재확인 주기

    on_timeout: OnTimeout = OnTimeout.ABORT

    # --- Utility ---
    message: str = ""                                # LOG step
    meta: Dict[str, Any] = field(default_factory=dict)

    def validate(self, idx: int = -1, strict: bool = True) -> None:
        prefix = f"[Step {idx}] " if idx >= 0 else ""

        _req(isinstance(self.name, str) and self.name.strip() != "", prefix + "name is required")
        _req(isinstance(self.type, StepType), prefix + f"type must be StepType (got {self.type!r})")

        # timing options normalization
        if self.poll_s is not None:
            _req(_is_num(self.poll_s) and float(self.poll_s) > 0, prefix + "poll_s must be > 0")
            self.poll_s = float(self.poll_s)

        if self.timeout_s is not None:
            _req(_is_num(self.timeout_s) and float(self.timeout_s) > 0, prefix + "timeout_s must be > 0")
            self.timeout_s = float(self.timeout_s)

        if self.stable_s is not None:
            _req(_is_num(self.stable_s) and float(self.stable_s) >= 0, prefix + "stable_s must be >= 0")
            self.stable_s = float(self.stable_s)

        _req(isinstance(self.on_timeout, OnTimeout), prefix + "on_timeout must be OnTimeout")

        # type-specific rules
        t = self.type

        if t == StepType.PLC_WRITE_COIL:
            _req(self.coil is not None and str(self.coil).strip() != "", prefix + "coil is required")
            _req(isinstance(self.on, bool), prefix + "on(bool) is required")
            if strict:
                _req(str(self.coil) in KNOWN_COILS, prefix + f"unknown coil: {self.coil!r}")
                _req(str(self.coil) not in READ_ONLY_COILS, prefix + f"read-only indicator coil cannot be written: {self.coil!r}")
            self.coil = str(self.coil)

        elif t == StepType.PLC_PULSE_COIL:
            _req(self.coil is not None and str(self.coil).strip() != "", prefix + "coil is required")
            if strict:
                _req(str(self.coil) in KNOWN_COILS, prefix + f"unknown coil: {self.coil!r}")
                _req(str(self.coil) not in READ_ONLY_COILS, prefix + f"read-only indicator coil cannot be pulsed: {self.coil!r}")
            self.coil = str(self.coil)

            _req(self.pulse_ms is not None, prefix + "pulse_ms is required")
            _req(isinstance(self.pulse_ms, int) and self.pulse_ms > 0, prefix + "pulse_ms must be int > 0")

        elif t == StepType.PLC_WRITE_REG:
            _req(self.reg is not None and str(self.reg).strip() != "", prefix + "reg is required")
            _req(self.value is not None, prefix + "value is required")
            _req(isinstance(self.value, int) and not isinstance(self.value, bool), prefix + "value must be int")
            if strict:
                _req(str(self.reg) in KNOWN_REGS, prefix + f"unknown reg: {self.reg!r}")
            self.reg = str(self.reg)

        elif t == StepType.PLC_SET_DAC_MA:
            _req(self.dac_ch is not None, prefix + "dac_ch is required")
            _req(isinstance(self.dac_ch, int) and self.dac_ch in (1, 2), prefix + "dac_ch must be 1 or 2")

            _req(self.dac_ma is not None and _is_num(self.dac_ma), prefix + "dac_ma(number) is required")
            ma = float(self.dac_ma)
            # 4~20mA 범위 내 사용(프로젝트 설정과 동일)
            _req(4.0 <= ma <= 20.0, prefix + "dac_ma must be within 4.0~20.0 mA")
            self.dac_ma = ma

        elif t == StepType.WAIT_SECONDS:
            _req(self.seconds is not None and _is_num(self.seconds), prefix + "seconds(number) is required")
            sec = float(self.seconds)
            _req(sec >= 0, prefix + "seconds must be >= 0")
            self.seconds = sec

        elif t == StepType.WAIT_PRESSURE_LEQ:
            _req(self.pressure_target is not None and _is_num(self.pressure_target), prefix + "pressure_target(number) is required")
            _req(float(self.pressure_target) >= 0, prefix + "pressure_target must be >= 0")
            _req(self.timeout_s is not None, prefix + "timeout_s is required for WAIT_PRESSURE_LEQ")
            _req(float(self.timeout_s) > 0, prefix + "timeout_s must be > 0")

        elif t == StepType.WAIT_THICKNESS_GEQ:
            _req(self.thickness_target_a is not None and _is_num(self.thickness_target_a), prefix + "thickness_target_a(number) is required")
            _req(float(self.thickness_target_a) >= 0, prefix + "thickness_target_a must be >= 0")
            _req(self.timeout_s is not None, prefix + "timeout_s is required for WAIT_THICKNESS_GEQ")
            _req(float(self.timeout_s) > 0, prefix + "timeout_s must be > 0")

        elif t == StepType.WAIT_RATE_IN_RANGE:
            _req(self.rate_min_a_s is not None and _is_num(self.rate_min_a_s), prefix + "rate_min_a_s(number) is required")
            _req(self.rate_max_a_s is not None and _is_num(self.rate_max_a_s), prefix + "rate_max_a_s(number) is required")
            mn = float(self.rate_min_a_s)
            mx = float(self.rate_max_a_s)
            _req(mn <= mx, prefix + "rate_min_a_s must be <= rate_max_a_s")
            _req(self.timeout_s is not None, prefix + "timeout_s is required for WAIT_RATE_IN_RANGE")

        elif t == StepType.WAIT_COIL_IS:
            _req(self.coil is not None and str(self.coil).strip() != "", prefix + "coil is required")
            _req(isinstance(self.expected, bool), prefix + "expected(bool) is required")
            if strict:
                _req(str(self.coil) in KNOWN_COILS, prefix + f"unknown coil: {self.coil!r}")
            _req(self.timeout_s is not None, prefix + "timeout_s is required for WAIT_COIL_IS")

        elif t == StepType.LOG:
            _req(isinstance(self.message, str) and self.message.strip() != "", prefix + "message is required for LOG")

        elif t == StepType.MARK:
            # MARK는 name만 있으면 OK
            pass

        else:
            raise ValueError(prefix + f"Unsupported StepType: {t}")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Enum -> value
        d["type"] = self.type.value
        d["on_timeout"] = self.on_timeout.value
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ProcessStep":
        _req(isinstance(d, dict), "step must be dict")
        name = _safe_str(d.get("name"))
        t_raw = d.get("type")
        try:
            t = StepType(str(t_raw))
        except Exception:
            raise ValueError(f"invalid step.type: {t_raw!r}")

        ot_raw = d.get("on_timeout", OnTimeout.ABORT.value)
        try:
            ot = OnTimeout(str(ot_raw))
        except Exception:
            ot = OnTimeout.ABORT

        step = ProcessStep(
            name=name,
            type=t,
            coil=d.get("coil"),
            on=d.get("on"),
            pulse_ms=d.get("pulse_ms"),
            reg=d.get("reg"),
            value=d.get("value"),
            dac_ch=d.get("dac_ch"),
            dac_ma=d.get("dac_ma"),
            seconds=d.get("seconds"),
            pressure_target=d.get("pressure_target"),
            thickness_target_a=d.get("thickness_target_a"),
            rate_min_a_s=d.get("rate_min_a_s"),
            rate_max_a_s=d.get("rate_max_a_s"),
            expected=d.get("expected"),
            timeout_s=d.get("timeout_s"),
            stable_s=d.get("stable_s"),
            poll_s=d.get("poll_s"),
            on_timeout=ot,
            message=_safe_str(d.get("message", "")),
            meta=dict(d.get("meta") or {}),
        )
        return step


@dataclass
class ProcessRecipe:
    """
    공정 레시피.

    - steps: 수행할 스텝 리스트
    - defaults: 엔진에서 사용할 기본 poll/timeout 등을 레시피에 같이 넣어두면 운영이 편함
    """

    recipe_name: str
    steps: List[ProcessStep]

    # 기본 폴링 주기(엔진에서 wait 조건 체크할 때 사용)
    default_poll_s: float = 0.2

    # 텔레메트리 기록 주기(초) - engine이 log_service.telemetry() 호출할 주기
    telemetry_interval_s: float = 1.0

    # 레시피 메타(옵션)
    operator: str = ""
    note: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def validate(self, strict: bool = True) -> None:
        _req(isinstance(self.recipe_name, str) and self.recipe_name.strip() != "", "recipe_name is required")
        _req(isinstance(self.steps, list) and len(self.steps) > 0, "steps must be a non-empty list")

        _req(_is_num(self.default_poll_s) and float(self.default_poll_s) > 0, "default_poll_s must be > 0")
        self.default_poll_s = float(self.default_poll_s)

        _req(_is_num(self.telemetry_interval_s) and float(self.telemetry_interval_s) > 0, "telemetry_interval_s must be > 0")
        self.telemetry_interval_s = float(self.telemetry_interval_s)

        for i, st in enumerate(self.steps):
            _req(isinstance(st, ProcessStep), f"steps[{i}] must be ProcessStep")
            st.validate(i, strict=strict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recipe_name": self.recipe_name,
            "default_poll_s": self.default_poll_s,
            "telemetry_interval_s": self.telemetry_interval_s,
            "operator": self.operator,
            "note": self.note,
            "meta": dict(self.meta or {}),
            "steps": [s.to_dict() for s in self.steps],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ProcessRecipe":
        _req(isinstance(d, dict), "recipe must be dict")
        steps_raw = d.get("steps")
        _req(isinstance(steps_raw, list) and len(steps_raw) > 0, "recipe.steps must be non-empty list")

        steps = [ProcessStep.from_dict(x) for x in steps_raw]
        r = ProcessRecipe(
            recipe_name=_safe_str(d.get("recipe_name")),
            steps=steps,
            default_poll_s=float(d.get("default_poll_s", 0.2)),
            telemetry_interval_s=float(d.get("telemetry_interval_s", 1.0)),
            operator=_safe_str(d.get("operator", "")),
            note=_safe_str(d.get("note", "")),
            meta=dict(d.get("meta") or {}),
        )
        return r


# ============================================================
# Runtime status models (엔진/워커 -> UI 전달용)
# ============================================================

@dataclass(frozen=True)
class StepStatus:
    idx: int
    name: str
    type: StepType
    started_ts: Optional[float] = None
    finished_ts: Optional[float] = None
    ok: Optional[bool] = None
    message: str = ""


@dataclass(frozen=True)
class ProcessStatus:
    """
    공정 진행 상황(엔진이 UI에 전달하기 위한 표준 상태)
    """
    phase: ProcessPhase
    recipe_name: str = ""
    run_id: str = ""
    step_idx: int = -1
    step_name: str = ""
    started_ts: Optional[float] = None
    message: str = ""

    # 최신 센서값(옵션)
    pressure: Optional[float] = None
    thickness_a: Optional[float] = None
    rate_a_s: Optional[float] = None

    # 최신 DAC 출력값(옵션)
    dac1: Optional[int] = None
    dac2: Optional[int] = None

    # 최신 ADC 읽기값(옵션)
    adc1: Optional[float] = None
    adc2: Optional[float] = None


@dataclass(frozen=True)
class ProcessError:
    """
    엔진에서 발생한 에러를 UI/로그로 표준 전달하기 위한 모델
    """
    where: str
    message: str
    exception_repr: str = ""
    ts: float = field(default_factory=lambda: time.time())
