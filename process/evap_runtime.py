# -*- coding: utf-8 -*-
"""
process/evap_runtime.py

EVAP 전용 증착 runtime 로직.

현재 역할:
- engine 인스턴스를 받아 engine의 공용 helper를 사용
- EVAP 공정의 램프업 / fine tune / shutter delay / main processing 수행
- 기존 단순 DAC ramp 방식이 아니라,
  process_config 기반 ADC step ramp-up(1~10 step)을 지원

포함 함수:
- run_evap_deposition_control(engine, recipe, step)
- _stm_wait_connected(engine, ...)
- _stm_apply_material_params(engine, ...)
- _stm_zero_thickness(engine, ...)
- _evap_apply_dac(engine, ...)
- _evap_adjust_dac(...)
- ADC/DAC readback helper
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, List

from process.models import ProcessRecipe, ProcessStep


# ============================================================
# EvapStep 데이터클래스 (ADC 기준 최대 5스텝)
# ============================================================

@dataclass
class EvapStep:
    """
    ADC 기준 증착 ramp-up 단계 하나를 정의하는 데이터클래스.

    필드:
        target_adc       : 이 ADC 값에 도달할 때까지 DAC을 올림
        dac_step         : 한 번에 올리는 DAC 증분 (int)
        dac_interval_sec : DAC 증분 주기 (초)
        rate_wait_sec    : target ADC 도달 후 dep.rate 관찰 대기 시간 (초)
        min_dep_rate     : 정상 판정 최소 dep.rate (Å/s). 이상이면 다음 step
        rate_low_action  : dep.rate 낮을 때 동작
                           "next_step" — 그냥 다음 step으로 진행
                           "boost_dac" — boost_dac_step씩 올리며 rate_wait_sec 대기 (boost_max_count회)
                                         모두 소진 시 소진 판정 후 안전 정지
                           "stop"      — 즉시 소진 판정 후 안전 정지
        boost_dac_step   : boost_dac 선택 시 추가 DAC 증분
        boost_max_count  : boost 최대 시도 횟수

    레거시 호환:
        기존 ramp_steps[{target_adc, delay_s}] 포맷에서 자동 변환됨.
        delay_s → rate_wait_sec 으로 매핑, 나머지는 기본값 사용.

    마이그레이션 방법 (기존 레시피 JSON):
        기존:
            "ramp_steps": [{"target_adc": 100.0, "delay_s": 5.0}]
        신규:
            "ramp_steps": [
                {
                    "target_adc": 100.0,
                    "dac_step": 10,
                    "dac_interval_sec": 30.0,
                    "rate_wait_sec": 5.0,
                    "min_dep_rate": 0.1,
                    "rate_low_action": "next_step"
                }
            ]
        delay_s가 있으면 rate_wait_sec으로 자동 변환됨. 새 필드가 없으면 기본값 사용.
    """

    target_adc: float
    dac_step: int = 10
    dac_interval_sec: float = 30.0
    rate_wait_sec: float = 0.0
    min_dep_rate: float = 0.1
    rate_low_action: str = "next_step"   # "next_step" | "boost_dac" | "stop"
    boost_dac_step: int = 0
    boost_max_count: int = 0

# MODIFIED: Dep.rate 급증 스파이크 판정 임계값 (단일 샘플에서 이전 값 대비 이 값 이상 양의 급증 시 무시)
RATE_SPIKE_UP_THRESHOLD: float = 30.0

# MODIFIED: IIR 필터 기본값 (meta로 override 가능)
_DEFAULT_IIR_ALPHA = 0.3        # 0 < alpha <= 1.0  (1.0 = 필터 없음), thermal evap 권장

# MODIFIED: Slew Rate 기본값 (meta로 override 가능)
_DEFAULT_MAX_SLEW_DAC_PER_SEC = 200   # DAC units/s, 초당 200 DAC = rate 10 Å/s 이하에서 안전 수준


def _raise_engine_failed(step_name: str, detail: str) -> None:
    """
    EngineFailed는 engine.py 안에 있으므로 런타임 지연 import로 raise.
    순환 import 방지용.
    """
    from process.engine import EngineFailed
    raise EngineFailed(step_name, detail)


# ------------------------------------------------------------
# STM helper (material params / zero)
# ------------------------------------------------------------
def _stm_wait_connected(engine, recipe: ProcessRecipe, step: ProcessStep, *, timeout_s: float = 5.0) -> None:
    """STMService가 connected=True가 될 때까지 대기(카운트다운 표시)."""
    if engine.stm is None:
        _raise_engine_failed(step.name, "STM 서비스가 주입되지 않았습니다(stm=None).")

    start_m = time.monotonic()
    deadline_m = start_m + float(timeout_s)
    next_ui_m = start_m

    while True:
        engine._check_stop_pause(recipe, step)
        engine._tick_emit(recipe, step)

        now_m = time.monotonic()
        remain_s = deadline_m - now_m
        if remain_s <= 0:
            break

        snap = engine._get_stm_snapshot()
        ok = bool(snap is not None and bool(getattr(snap, "connected", False)))

        # ✅ 1초마다 카운트다운 표시
        if now_m >= next_ui_m:
            engine._emit_status(
                message=f"[남은 {engine._fmt_hms(remain_s, ceil=True)}] STM 연결 대기",
                force=True,
            )
            next_ui_m = now_m + 1.0

        if ok:
            engine._emit_status(message="[남은 00:00] STM 연결 완료", force=True)
            return

        time.sleep(0.1)

    _raise_engine_failed(step.name, f"STM 연결 대기 timeout: {timeout_s:.1f}s")


def _stm_apply_material_params(
    engine,
    recipe: ProcessRecipe,
    step: ProcessStep,
    *,
    density_g_cm3: float,
    z_factor: float,
) -> None:
    """STM-100에 density/z-factor 적용 (필요 시)."""
    if engine.stm is None:
        return

    _stm_wait_connected(engine, recipe, step, timeout_s=5.0)

    fut = engine.stm.submit_apply_material_params(
        density_g_cm3=float(density_g_cm3),
        z_factor=float(z_factor),
        film_no=None,
        do_zero_thickness=False,
    )
    engine._wait_future(fut, timeout_s=5.0, where=f"{step.name}/STM_APPLY", msg="STM film params 적용 실패")
    engine._tele_event(
        event="STM_APPLY",
        target="FILM_PARAM",
        value="",
        detail=f"density={density_g_cm3}, z={z_factor}",
    )


def _stm_zero_thickness(engine, recipe: ProcessRecipe, step: ProcessStep, *, mode: str = "B") -> None:
    """STM-100 thickness/time zero. mode='B' 권장(두께+타이머)."""
    if engine.stm is None:
        return

    _stm_wait_connected(engine, recipe, step, timeout_s=5.0)

    fut = engine.stm.submit_zero_thickness(mode=str(mode))
    engine._wait_future(fut, timeout_s=5.0, where=f"{step.name}/STM_ZERO", msg="STM zero 실패")
    engine._tele_event(event="STM_ZERO", target="THICKNESS", value=str(mode), detail="zero thickness/time")


# --------------------------------------------------------
# EVAP helpers
# --------------------------------------------------------
def _evap_apply_dac(engine, use_p1: bool, use_p2: bool, dac: int, *, tag: str) -> None:
    """선택된 채널에만 DAC 값을 적용(동기 submit + wait)."""
    dac = int(max(0, dac))
    if use_p1:
        engine._plc_write_reg("DAC_POWER_1", dac, tag=f"{tag}_CH1")
        engine._last_dac_power_1 = dac
    if use_p2:
        engine._plc_write_reg("DAC_POWER_2", dac, tag=f"{tag}_CH2")
        engine._last_dac_power_2 = dac


class EvapPI:
    """
    MODIFIED: 증착 rate PI 제어기
    - Ramp-up 구간에서는 사용 안 함 (기존 +step 방식 유지)
    - fine_tune / shutter_delay / main_loop 에서만 사용
    - Anti-windup: DAC 포화 시 적분 누적 차단
    """

    def __init__(self, kp: float, ki: float, dac_min: int = 0, dac_max: int = 4000):
        self.kp = kp
        self.ki = ki
        self.dac_min = dac_min
        self.dac_max = dac_max
        self._integral: float = 0.0
        self._last_t: Optional[float] = None

    def reset(self) -> None:
        self._integral = 0.0
        self._last_t = None

    def compute(self, current_dac: int, target_rate: float, actual_rate: float) -> int:
        now = time.monotonic()
        dt = (now - self._last_t) if self._last_t is not None else 1.0
        dt = max(0.01, min(dt, 5.0))   # 이상치 방지
        self._last_t = now

        err = target_rate - actual_rate

        # Anti-windup: DAC가 포화 중이면 같은 방향의 적분 차단
        at_max = current_dac >= self.dac_max
        at_min = current_dac <= self.dac_min
        if not (at_max and err > 0) and not (at_min and err < 0):
            self._integral += err * dt

        # 적분 클램프 (과도한 windup 방지)
        integral_limit = (self.dac_max - self.dac_min) / max(self.ki, 1e-9) * 0.5
        self._integral = max(-integral_limit, min(integral_limit, self._integral))

        delta = self.kp * err + self.ki * self._integral
        new_dac = int(current_dac + delta)
        return int(max(self.dac_min, min(self.dac_max, new_dac)))


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


def _sum_selected_values(
    use_p1: bool,
    use_p2: bool,
    v1: Optional[float],
    v2: Optional[float],
) -> Optional[float]:
    vals: list[float] = []
    if use_p1 and v1 is not None:
        vals.append(float(v1))
    if use_p2 and v2 is not None:
        vals.append(float(v2))
    if not vals:
        return None
    return sum(vals)


def _read_plc_reg_pair(engine, reg1: str, reg2: str) -> tuple[Optional[float], Optional[float]]:
    try:
        plc = getattr(engine, "plc", None)
        snap = plc.get_last_snapshot() if plc is not None else None
        if snap is None:
            return None, None

        regs = getattr(snap, "regs", None) or {}

        def _to_float(v):
            if v is None:
                return None
            return float(v)

        return _to_float(regs.get(reg1)), _to_float(regs.get(reg2))
    except Exception:
        return None, None


def _read_selected_adc_total(
    engine,
    use_p1: bool,
    use_p2: bool,
    *,
    power1_feedback_adc2: bool = False,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    a1, a2 = _read_plc_reg_pair(engine, "POWER_READ_1", "POWER_READ_2")

    # ✅ 임시 운전 모드:
    # Power1 공정이지만 실제 feedback은 ADC2를 사용
    if power1_feedback_adc2 and use_p1 and (not use_p2):
        return a2, a1, a2

    total = _sum_selected_values(use_p1, use_p2, a1, a2)
    return total, a1, a2


def _dynamic_dac_step_from_adc_gap(
    current_adc: float,
    target_adc: float,
    *,
    step_cap: float,
) -> int:
    """
    ADC gap 기준으로 DAC 증가폭을 동적으로 결정.
    단, 증가폭은 항상 100 이하.
    """
    cap = int(min(100.0, max(1.0, float(step_cap))))
    gap = max(0.0, float(target_adc) - float(current_adc))

    if gap >= 100.0:
        base = cap
    elif gap >= 50.0:
        base = min(cap, 50)
    elif gap >= 20.0:
        base = min(cap, 20)
    elif gap >= 10.0:
        base = min(cap, 10)
    else:
        base = min(cap, 5)

    return max(1, int(base))


# --------------------------------------------------------
# EVAP deposition control
# --------------------------------------------------------
def _load_runtime_process_config(meta: dict, *, step_name: str) -> dict:
    """
    controller가 만든 runtime meta에서 process 관련 설정을 읽는다.

    현재는 backward compatibility를 위해
    - meta["process_config"]
    - top-level alias(meta["ramp_steps"], meta["after_last_step_policy"] ...)
    둘 다 허용한다.
    단, 값 검증은 strict 하게 한다.
    """
    process_config = dict(meta.get("process_config") or {})

    raw_ramp_steps = meta.get("ramp_steps", None)
    if raw_ramp_steps is None:
        raw_ramp_steps = process_config.get("ramp_steps")

    if not isinstance(raw_ramp_steps, list) or not raw_ramp_steps:
        _raise_engine_failed(step_name, "EVAP: process_config.ramp_steps is empty")

    if len(raw_ramp_steps) > 10:
        _raise_engine_failed(
            step_name,
            f"EVAP: process_config.ramp_steps supports up to 10 steps only (got={len(raw_ramp_steps)})"
        )

    ramp_steps: list[dict] = []
    last_adc = -1.0

    for idx, item in enumerate(raw_ramp_steps, start=1):
        if not isinstance(item, dict):
            _raise_engine_failed(step_name, f"EVAP: ramp_steps[{idx}] must be dict")

        try:
            target_adc = float(item.get("target_adc", 0.0) or 0.0)
            # 레거시: delay_s → rate_wait_sec 자동 변환
            legacy_delay_s = float(item.get("delay_s", 0.0) or 0.0)
            rate_wait_sec = float(item.get("rate_wait_sec", legacy_delay_s) or 0.0)
        except Exception:
            _raise_engine_failed(step_name, f"EVAP: ramp_steps[{idx}] value conversion failed")

        if target_adc <= 0:
            _raise_engine_failed(step_name, f"EVAP: ramp_steps[{idx}].target_adc must be > 0")
        if rate_wait_sec < 0:
            _raise_engine_failed(step_name, f"EVAP: ramp_steps[{idx}].rate_wait_sec must be >= 0")

        if last_adc >= 0 and target_adc < last_adc:
            _raise_engine_failed(
                step_name,
                f"EVAP: ramp_steps[{idx}].target_adc must be non-decreasing "
                f"(prev={last_adc}, current={target_adc})"
            )

        # 신규 EvapStep 필드 파싱 (없으면 기본값)
        try:
            dac_step = max(1, int(item.get("dac_step", 10) or 10))
        except Exception:
            dac_step = 10
        try:
            dac_interval_sec = max(0.1, float(item.get("dac_interval_sec", 30.0) or 30.0))
        except Exception:
            dac_interval_sec = 30.0
        try:
            min_dep_rate = max(0.0, float(item.get("min_dep_rate", 0.1) or 0.0))
        except Exception:
            min_dep_rate = 0.1

        rate_low_action = str(item.get("rate_low_action", "next_step") or "next_step").strip().lower()
        if rate_low_action not in {"next_step", "boost_dac", "stop"}:
            rate_low_action = "next_step"

        try:
            boost_dac_step = max(0, int(item.get("boost_dac_step", 0) or 0))
        except Exception:
            boost_dac_step = 0
        try:
            boost_max_count = max(0, int(item.get("boost_max_count", 0) or 0))
        except Exception:
            boost_max_count = 0

        ramp_steps.append(
            {
                "step_no": idx,
                "target_adc": target_adc,
                # 레거시 호환
                "delay_s": rate_wait_sec,
                # EvapStep 신규 필드
                "dac_step": dac_step,
                "dac_interval_sec": dac_interval_sec,
                "rate_wait_sec": rate_wait_sec,
                "min_dep_rate": min_dep_rate,
                "rate_low_action": rate_low_action,
                "boost_dac_step": boost_dac_step,
                "boost_max_count": boost_max_count,
            }
        )
        last_adc = target_adc

    reach_main_on_rate = bool(
        meta.get("reach_main_on_rate", process_config.get("reach_main_on_rate", True))
    )

    policy_raw = meta.get(
        "after_last_step_policy",
        process_config.get("after_last_step_policy", "extra_ramp"),
    )
    after_last_step_policy = str(policy_raw or "extra_ramp").strip().lower()
    if after_last_step_policy not in {"extra_ramp", "stop"}:
        _raise_engine_failed(
            step_name,
            f"EVAP: invalid after_last_step_policy={after_last_step_policy!r}"
        )

    extra_ramp_raw = meta.get("extra_ramp", process_config.get("extra_ramp", {}))
    if extra_ramp_raw is None:
        extra_ramp_raw = {}
    if not isinstance(extra_ramp_raw, dict):
        _raise_engine_failed(step_name, "EVAP: extra_ramp config must be dict")

    try:
        extra_ramp_enabled = bool(extra_ramp_raw.get("enabled", True))
        extra_ramp_max_adc = float(
            extra_ramp_raw.get("max_adc", meta.get("last_step_target_adc", last_adc)) or last_adc
        )
        extra_ramp_step_cap = float(
            extra_ramp_raw.get("step_max", meta.get("adc_dynamic_step_cap", 50.0)) or 50.0
        )
        extra_ramp_interval_s = float(
            extra_ramp_raw.get("interval_s", 5.0) or 5.0
        )
    except Exception:
        _raise_engine_failed(step_name, "EVAP: extra_ramp config value conversion failed")

    if extra_ramp_max_adc < last_adc:
        _raise_engine_failed(
            step_name,
            f"EVAP: extra_ramp.max_adc must be >= last_step_target_adc "
            f"(last={last_adc}, max_adc={extra_ramp_max_adc})"
        )

    if not (1.0 <= extra_ramp_step_cap <= 100.0):
        _raise_engine_failed(
            step_name,
            f"EVAP: extra_ramp.step_max must be in [1, 100] (got={extra_ramp_step_cap})"
        )

    if extra_ramp_interval_s < 0.1:
        _raise_engine_failed(
            step_name,
            f"EVAP: extra_ramp.interval_s must be >= 0.1 (got={extra_ramp_interval_s})"
        )

    return {
        "process_config": process_config,
        "ramp_steps": ramp_steps,
        "reach_main_on_rate": reach_main_on_rate,
        "after_last_step_policy": after_last_step_policy,
        "extra_ramp_enabled": extra_ramp_enabled,
        "extra_ramp_max_adc": extra_ramp_max_adc,
        "extra_ramp_step_cap": extra_ramp_step_cap,
        "extra_ramp_interval_s": extra_ramp_interval_s,
    }

def run_evap_deposition_control(engine, recipe: ProcessRecipe, step: ProcessStep) -> None:
    """
    EVAP 증착 제어(ADC step 기반)

    전제
    - POWER_1_SW / POWER_2_SW는 상위 레시피 단계에서 이미 결정되어 있음
    - MAIN_SHUTTER_SW는 닫힌 상태에서 시작
    - FTM_SW / SOURCE_SHUTTER는 상위 단계에서 ON 되는 구조를 권장

    실제 순서
    1) STM 연결 확인 및 material params(density/z) 적용
    2) process_config.ramp_steps(1~10)를 순차 수행하며 ADC 기준 ramp-up
    3) 각 step의 target_adc 도달 후 delay_s 동안 DAC/ADC/dep.rate 계속 감시
    4) step 중 dep.rate가 target_rate 근처에 도달하면 나머지 step을 건너뛰고 fine tune 진입
    5) 마지막 step까지 dep.rate 미도달 시 extra_ramp 또는 stop
    6) fine tune 후 shutter delay 수행
    7) STM zero 후 MAIN_SHUTTER open
    8) main processing 중 target thickness 도달 시 종료
    """

    meta = dict(step.meta or {})

    def _meta_f(key: str, default: float) -> float:
        v = meta.get(key, None)
        return float(default) if v is None else float(v)

    use_p1 = bool(meta.get("use_power1", False))
    use_p2 = bool(meta.get("use_power2", False))

    # ✅ 임시 우회: Power1 공정 시 ADC2를 feedback으로 사용
    power1_feedback_adc2 = bool(meta.get("power1_feedback_adc2", False))

    # ✅ power는 1개 또는 2개 허용(둘 다 False만 금지)
    if not (use_p1 or use_p2):
        _raise_engine_failed(step.name, "EVAP: use_power1/use_power2 중 최소 1개는 True여야 합니다.")

    target_rate = float(meta.get("target_rate", 0.0) or 0.0)        # Å/s
    target_th = float(meta.get("target_thickness", 0.0) or 0.0)     # Å
    delay_min = float(meta.get("delay_min", 0.0) or 0.0)            # min

    if target_rate <= 0:
        _raise_engine_failed(step.name, "EVAP: target_rate must be > 0")
    if target_th <= 0:
        _raise_engine_failed(step.name, "EVAP: target_thickness must be > 0")
    if delay_min < 0:
        _raise_engine_failed(step.name, "EVAP: delay_min must be >= 0")

    # -------------------------------------------------
    # 신규 process config (ADC step 기반)
    # -------------------------------------------------
    adc_control_mode = str(meta.get("adc_control_mode", "adc") or "adc").strip().lower()

    proc_cfg = _load_runtime_process_config(meta, step_name=step.name)

    ramp_steps = list(proc_cfg["ramp_steps"])
    reach_main_on_rate = bool(proc_cfg["reach_main_on_rate"])
    after_last_step_policy = str(proc_cfg["after_last_step_policy"])
    extra_ramp_enabled = bool(proc_cfg["extra_ramp_enabled"])
    extra_ramp_max_adc = float(proc_cfg["extra_ramp_max_adc"])
    extra_ramp_step_cap = float(proc_cfg["extra_ramp_step_cap"])
    extra_ramp_interval_s = float(proc_cfg["extra_ramp_interval_s"])

    adc_none_abort_s = max(
        0.1,
        float(meta.get("adc_none_abort_s", meta.get("sensor_none_abort_s", 5.0)) or 5.0),
    )
    
    if adc_control_mode != "adc":
        _raise_engine_failed(step.name, f"EVAP: unsupported adc_control_mode={adc_control_mode}")

    # MODIFIED: IIR 필터 파라미터 (meta로 override)
    # iir_alpha = 0.3: thermal evap 권장. 1.0이면 필터 없음
    iir_alpha = float(meta.get("iir_alpha", _DEFAULT_IIR_ALPHA))
    iir_alpha = max(0.01, min(1.0, iir_alpha))

    # MODIFIED: PI 제어 파라미터 (meta로 override)
    # pi_kp = 5.0: 낮게 시작해서 올림 (SQC-310 방식)
    # pi_ki = 0.5: thermal evap I값 4~10s 기준에서 시작
    pi_kp = float(meta.get("pi_kp", 5.0))
    pi_ki = float(meta.get("pi_ki", 0.5))

    # MODIFIED: Slew Rate (meta로 override)
    # max_slew_dac_per_sec = 200: 초당 200 DAC = rate 10 Å/s 이하에서 안전 수준
    max_slew = float(meta.get("max_slew_dac_per_sec", _DEFAULT_MAX_SLEW_DAC_PER_SEC))

    # --- 공정 파라미터(기본값은 요구사항 기준) ---
    pre_rate = float(meta.get("pre_rate", 0.4) or 0.4)    # Å/s

    # ✅ dep.rate 도달 이후 파워 변화가 너무 빠르지 않도록 DAC 변경 최소 간격(초)
    dac_adjust_interval_s = max(0.1, float(meta.get("dac_adjust_interval_s", 10.0) or 10.0))

    rate_tol_ratio = float(meta.get("rate_tol_ratio", 0.05) or 0.05)    # ±5%



    # ✅ 사용자 요구: DAC 구간별 램프 템포
    ramp_seg1_max_dac = int(meta.get("ramp_seg1_max_dac", 700) or 700)
    ramp_seg2_max_dac = int(meta.get("ramp_seg2_max_dac", 2000) or 2000)
    ramp_interval_seg1_s = float(meta.get("ramp_interval_seg1_s", 10.0) or 10.0)
    ramp_interval_seg2_s = float(meta.get("ramp_interval_seg2_s", 30.0) or 30.0)
    ramp_interval_after_seg2_s = float(meta.get("ramp_interval_after_seg2_s", ramp_interval_seg2_s) or ramp_interval_seg2_s)

    # ✅ 미세 조정(목표 근접 시): 기본 10 step 이내에서 동적 보정
    fine_step_dac = int(meta.get("fine_step_dac", 10) or 10)

    # ✅ target_ramp 종료 판정(연속 N회, 기본 3회) — 스파이크 1번으로 target_ramp 탈출 방지
    target_ramp_stable_hits = int(meta.get("target_ramp_stable_hits", 3) or 3)

    # ✅ target_rate 도달 판정: tol 이내 N회 연속 (기본 3회로 변경)
    target_stable_hits = int(meta.get("target_stable_hits", 3) or 3)
    target_stable_interval_s = max(0.1, float(meta.get("target_stable_interval_s", 1.0) or 1.0))

    rate_filter_window = max(1, int(meta.get("rate_filter_window", 5) or 5))
    rate_stable_sec = max(0.0, float(meta.get("rate_stable_sec", 3.0) or 3.0))
    rate_drop_ratio = float(meta.get("rate_drop_ratio", 0.30) or 0.30)  # 70% 급감 => 30% 이하
    rate_drop_count = int(meta.get("rate_drop_count", 3) or 3)

    dac_max = int(meta.get("dac_max", 4000) or 4000)
    sensor_none_abort_s = float(meta.get("sensor_none_abort_s", 5.0) or 5.0)


    # ✅ material shortage guard (요구사항)
    # - DAC가 2000 이상인데도 dep.rate가 0 이하이면 "물질 부족"으로 중단
    material_shortage_dac = int(meta.get("material_shortage_dac", 2000) or 2000)
    material_shortage_rate_max = float(meta.get("material_shortage_rate_max", 0.0) or 0.0)  # <=
    # "계속"을 엄밀히 하려면 지속시간을 쓰면 됨(초). 10이면 10초 대기 후 중단.
    material_shortage_time_s = _meta_f("material_shortage_time_s", 10.0)  # ✅ 0이면 즉시 중단 로직이 살아남

    # delay는 분 단위 입력(기존 UI) → 초로 변환
    delay_s = delay_min * 60.0

    if ramp_seg2_max_dac < ramp_seg1_max_dac:
        _raise_engine_failed(step.name, "EVAP: ramp_seg2_max_dac must be >= ramp_seg1_max_dac")
    if material_shortage_time_s < 0:
        _raise_engine_failed(step.name, "EVAP: material_shortage_time_s must be >= 0")

    # 0) MAIN_SHUTTER는 닫힌 상태에서 시작(한 번 더 안전 close)
    engine._plc_write_coil("MAIN_SHUTTER_SW", False, tag="EVAP_INIT")

    # 1) STM 연결 확인
    _stm_wait_connected(engine, recipe, step, timeout_s=5.0)

    # 1-1) (옵션) film params 적용
    density = float(meta.get("density", 0.0) or 0.0)
    z_factor = float(meta.get("z_factor", 0.0) or 0.0)
    if density > 0 and z_factor > 0:
        engine._emit_status(message=f"STM film params 적용: density={density}, z={z_factor}")
        _stm_apply_material_params(engine, recipe, step, density_g_cm3=density, z_factor=z_factor)

    # 2) 초기 DAC: 항상 0부터 시작
    dac = 0

    # ✅ material shortage 타이머(전 구간 공통)
    shortage_start_ts: Optional[float] = None
    rate_filter_samples: list[float] = []

    # MODIFIED: 스파이크 필터용 직전 유효 rate 추적 (mutable holder)
    _last_valid_rate: list[Optional[float]] = [None]

    # MODIFIED: IIR 필터 상태 (mutable container for closure)
    _iir_prev: list[float] = [0.0]

    def _apply_iir(raw: float) -> float:
        """MODIFIED: IIR 저역통과 필터. filtered = alpha * raw + (1-alpha) * prev"""
        filtered = iir_alpha * float(raw) + (1.0 - iir_alpha) * _iir_prev[0]
        _iir_prev[0] = filtered
        return filtered

    def _update_filtered_rate(rt_raw: float) -> float:
        # MODIFIED: 급증 스파이크 필터 — 이전 유효값 대비 RATE_SPIKE_UP_THRESHOLD 이상
        # 양의 방향으로 순간 급증하면 해당 샘플을 무시하고 이전 값 유지
        rt_value = float(rt_raw)
        prev = _last_valid_rate[0]
        if prev is not None and (rt_value - prev) > RATE_SPIKE_UP_THRESHOLD:
            # 스파이크 감지: 이전 유효값으로 대체 (필터 샘플에도 스파이크 대신 prev 입력)
            rt_value = prev
        else:
            _last_valid_rate[0] = rt_value

        rate_filter_samples.append(rt_value)
        over = len(rate_filter_samples) - int(rate_filter_window)
        if over > 0:
            del rate_filter_samples[:over]
        return sum(rate_filter_samples) / float(len(rate_filter_samples))

    # --- 내부 유틸 ---
    def _sleep_with_checks(total_s: float, *, where: str = "sleep") -> None:
        nonlocal shortage_start_ts
        total_s = float(total_s)
        end_t = time.monotonic() + total_s

        # ✅ 1초마다 카운트다운 표시(짧은 sleep은 생략 가능)
        next_ui_m = time.monotonic()
        show_ui = total_s >= 1.0

        while True:
            engine._check_stop_pause(recipe, step)
            engine._tick_emit(recipe, step)
            flush_pending_dac_if_due()

            # ✅ sleep 중에도 rate를 읽어 material shortage 감시(10초 카운트가 정확해짐)
            rt = engine._get_rate()
            if rt is not None:
                rt_raw = float(rt)
                rt_iir = _apply_iir(rt_raw)   # MODIFIED: IIR 필터 적용
                _update_filtered_rate(rt_iir)
                shortage_start_ts = _material_shortage_guard(rt_iir, shortage_start_ts, where=where)

            now = time.monotonic()
            remain = end_t - now
            if remain <= 0:
                return

            if show_ui and now >= next_ui_m:
                engine._emit_status(
                    message=f"[남은 {engine._fmt_hms(remain, ceil=True)}] 대기중 ({where})",
                    force=True,
                )
                next_ui_m = now + 1.0

            time.sleep(min(0.1, remain))
    def _ramp_interval_by_dac(cur_dac: int) -> float:
        d = int(cur_dac)
        if d < int(ramp_seg1_max_dac):
            return float(ramp_interval_seg1_s)          # 0~700 : 10s
        if d < int(ramp_seg2_max_dac):
            return float(ramp_interval_seg2_s)          # 700~2000 : 30s
        return float(ramp_interval_after_seg2_s)        # 2000 이후 : 30s(기본)

    def _dac_min_interval_by_dac(cur_dac: int) -> float:
        # 기존 dac_adjust_interval_s도 존중하되,
        # 구간 규칙보다 더 빠르게는 못 움직이게
        return max(float(dac_adjust_interval_s), _ramp_interval_by_dac(cur_dac))

    def _ramp_sleep_by_dac(cur_dac: int, *, where: str) -> None:
        _sleep_with_checks(_ramp_interval_by_dac(cur_dac), where=where)

    def _in_band(rt: float, tgt: float, *, tol_ratio: float) -> bool:
        tol = max(1e-9, abs(float(tgt)) * float(tol_ratio))
        return abs(float(rt) - float(tgt)) <= tol

    def read_rate_or_abort(*, where: str) -> tuple[float, float]:
        nonlocal shortage_start_ts
        t0 = time.monotonic()
        while True:
            engine._check_stop_pause(recipe, step)
            engine._tick_emit(recipe, step)

            rt = engine._get_rate()
            if rt is not None:
                rt_raw = float(rt)
                rt_iir = _apply_iir(rt_raw)                    # MODIFIED: IIR 필터 적용
                rt_filtered = _update_filtered_rate(rt_iir)    # 이동평균은 IIR 출력에 적용

                # ✅ 모든 공정 구간에서 material shortage 감시 (IIR 필터 적용 값 기준)
                shortage_start_ts = _material_shortage_guard(rt_iir, shortage_start_ts, where=where)

                return rt_raw, rt_filtered

            if (time.monotonic() - t0) >= sensor_none_abort_s:
                _raise_engine_failed(step.name, f"EVAP: rate 센서 None 지속 {sensor_none_abort_s}s")

            time.sleep(0.1)

    def read_th_or_abort() -> float:
        t0 = time.monotonic()
        while True:
            engine._check_stop_pause(recipe, step)
            engine._tick_emit(recipe, step)

            th = engine._get_thickness()
            if th is not None:
                return float(th)

            if (time.monotonic() - t0) >= sensor_none_abort_s:
                _raise_engine_failed(step.name, f"EVAP: thickness 센서 None 지속 {sensor_none_abort_s}s")

            time.sleep(0.1)

    def read_adc_total_or_abort(*, where: str) -> tuple[float, Optional[float], Optional[float]]:
        t0 = time.monotonic()
        while True:
            engine._check_stop_pause(recipe, step)
            engine._tick_emit(recipe, step)

            adc_total, adc1, adc2 = _read_selected_adc_total(
                engine,
                use_p1,
                use_p2,
                power1_feedback_adc2=power1_feedback_adc2,
            )
            if adc_total is not None:
                return float(adc_total), adc1, adc2

            if (time.monotonic() - t0) >= adc_none_abort_s:
                _raise_engine_failed(step.name, f"EVAP: ADC readback None 지속 {adc_none_abort_s}s ({where})")

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
                _raise_engine_failed(
                    step.name,
                    f"EVAP: 물질 부족 의심({where}) - DAC>={material_shortage_dac} 인데 dep.rate<={material_shortage_rate_max:.3f} "
                    f"(rt={rt:.3f}, dac={dac})"
                )

            if start_ts is None:
                return now

            if (now - float(start_ts)) >= float(material_shortage_time_s):
                _raise_engine_failed(
                    step.name,
                    f"EVAP: 물질 부족 의심({where}) - DAC>={material_shortage_dac} 인데 dep.rate<={material_shortage_rate_max:.3f} "
                    f"지속 {material_shortage_time_s:.0f}s (rt={rt:.3f}, dac={dac})"
                )
            return start_ts

        return None

    def _is_below_threshold_sustained(
        rt_filtered: float,
        threshold: float,
        start_ts: Optional[float],
    ) -> tuple[bool, Optional[float]]:
        if float(rt_filtered) < float(threshold):
            now_m = time.monotonic()
            if start_ts is None:
                start_ts = now_m

            if float(rate_stable_sec) <= 0.0:
                return True, start_ts

            return ((now_m - float(start_ts)) >= float(rate_stable_sec)), start_ts

        return False, None

    last_dac_apply_m: float = 0.0
    pending_dac: Optional[int] = None
    pending_dac_min_interval_s: float = 0.0

    def apply_dac() -> None:
        """즉시 DAC 적용(램프업 등에서 사용) + 마지막 적용 시각 갱신."""
        nonlocal last_dac_apply_m, pending_dac, pending_dac_min_interval_s
        _evap_apply_dac(engine, use_p1, use_p2, dac, tag="EVAP_DAC")
        last_dac_apply_m = time.monotonic()
        pending_dac = None
        pending_dac_min_interval_s = 0.0

    def maybe_apply_dac(new_dac: int, *, min_interval_s: float) -> bool:
        """DAC 변경은 너무 잦지 않게(min_interval_s) 제한. PI 경로에서 slew rate 적용."""
        nonlocal dac, pending_dac, pending_dac_min_interval_s, last_dac_apply_m
        new_dac = int(new_dac)
        if new_dac == int(dac):
            pending_dac = None
            pending_dac_min_interval_s = 0.0
            return False

        wait_s = float(min_interval_s)
        now_m = time.monotonic()
        if (now_m - float(last_dac_apply_m)) >= wait_s:
            # MODIFIED: Slew Rate 제한 (PI 경로에만 적용)
            elapsed = now_m - float(last_dac_apply_m)
            max_delta = int(max_slew * elapsed)
            delta = new_dac - int(dac)
            if max_delta > 0 and abs(delta) > max_delta:
                new_dac = int(dac) + int(max_delta * (1 if delta > 0 else -1))
            dac = new_dac
            apply_dac()
            return True

        # 아직 텀이 안 찼으면 "대기중 목표값"만 저장
        pending_dac = new_dac
        pending_dac_min_interval_s = wait_s
        return False

    def flush_pending_dac_if_due() -> bool:
        nonlocal dac, pending_dac, pending_dac_min_interval_s, last_dac_apply_m
        if pending_dac is None:
            return False

        now_m = time.monotonic()
        if (now_m - float(last_dac_apply_m)) < float(pending_dac_min_interval_s):
            return False

        # MODIFIED: Slew Rate 제한 (pending 적용 시에도 PI 경로 slew 적용)
        elapsed = now_m - float(last_dac_apply_m)
        max_delta = int(max_slew * elapsed)
        new_dac = int(pending_dac)
        delta = new_dac - int(dac)
        if max_delta > 0 and abs(delta) > max_delta:
            new_dac = int(dac) + int(max_delta * (1 if delta > 0 else -1))
        pending_dac = new_dac  # slew clamp 후 적용
        dac = new_dac
        apply_dac()
        return True

    # 3) 사용자 step 기반 ADC ramp-up
    engine._emit_status(message=f"EVAP ADC step ramp 시작: steps={len(ramp_steps)}", force=True)
    apply_dac()

    entered_main_from_step = False
    # MODIFIED: rate drop 감지는 main shutter open 이후에만 활성화
    rate_drop_enabled: bool = False

    def _check_step_rate_state(rt_raw: float, rt_filtered: float, *, where: str) -> bool:
        """ramp/step/shutter_delay 구간에서 호출. rate drop 감지는 여기서 수행하지 않음."""
        nonlocal step_rate_peak, step_drop_hits, step_reach_hits, step_drop_low_start_ts

        # MODIFIED: peak 갱신 조건 강화 — target_rate * 0.3 이상일 때만 peak로 인정
        if float(rt_filtered) >= float(target_rate) * 0.3 and float(rt_filtered) > step_rate_peak:
            step_rate_peak = float(rt_filtered)

        # MODIFIED: rate drop 감지 제거 — ramp/step 구간에서는 false trigger 방지
        # (main shutter open 이후 main_loop 에서만 rate_drop_enabled 플래그로 검사)

        reach_threshold = float(target_rate) * (1.0 - float(rate_tol_ratio))
        if reach_main_on_rate and float(rt_filtered) >= reach_threshold:
            step_reach_hits += 1
        else:
            step_reach_hits = 0

        return step_reach_hits >= max(1, int(target_ramp_stable_hits))
    for step_cfg in ramp_steps:
        step_rate_peak = 0.0
        step_drop_hits = 0
        step_reach_hits = 0
        step_drop_low_start_ts = None

        step_no           = int(step_cfg["step_no"])
        step_target_adc   = float(step_cfg["target_adc"])
        # EvapStep 필드 (없으면 레거시 기본값)
        step_dac_step       = int(step_cfg.get("dac_step", 10))
        step_dac_interval   = float(step_cfg.get("dac_interval_sec", 30.0))
        step_rate_wait      = float(step_cfg.get("rate_wait_sec", step_cfg.get("delay_s", 0.0)))
        step_min_dep_rate   = float(step_cfg.get("min_dep_rate", 0.1))
        step_low_action     = str(step_cfg.get("rate_low_action", "next_step"))
        step_boost_dac_step = int(step_cfg.get("boost_dac_step", 0))
        step_boost_max      = int(step_cfg.get("boost_max_count", 0))

        engine._emit_status(
            message=(
                f"STEP {step_no}/{len(ramp_steps)} 시작 | "
                f"target_adc={step_target_adc:.1f} | "
                f"dac_step={step_dac_step} | interval={step_dac_interval:.1f}s | "
                f"rate_wait={step_rate_wait:.1f}s | min_rate={step_min_dep_rate:.2f}"
            ),
            force=True,
        )

        # (A) current_adc(EMA 필터값)가 step.target_adc 미만인 동안 DAC 증분
        #     dac_interval_sec마다 dac_step씩 올림 (ADC 도달 판정은 EMA값 기준)
        _last_dac_ramp_m = time.monotonic()

        while True:
            rt_raw, rt_filtered = read_rate_or_abort(where=f"step{step_no}_ramp")

            if _check_step_rate_state(rt_raw, rt_filtered, where=f"step{step_no}_ramp"):
                entered_main_from_step = True
                break

            adc_total, _, _ = read_adc_total_or_abort(where=f"step{step_no}_ramp")
            if adc_total >= step_target_adc:
                break

            if dac >= dac_max:
                _raise_engine_failed(
                    step.name,
                    f"EVAP: DAC_MAX({dac_max}) 도달했지만 STEP {step_no} target_adc 미도달 "
                    f"(adc={adc_total:.1f}/{step_target_adc:.1f}, rate={rt_raw:.3f})"
                )

            # dac_interval_sec마다 dac_step씩 증분 (EvapStep 방식)
            now_m = time.monotonic()
            if (now_m - _last_dac_ramp_m) >= step_dac_interval:
                dac = min(dac_max, dac + step_dac_step)
                apply_dac()
                _last_dac_ramp_m = now_m

            engine._emit_status(
                message=(
                    f"STEP {step_no}/{len(ramp_steps)} RAMP | "
                    f"ADC={adc_total:.1f}/{step_target_adc:.1f} | "
                    f"DAC={dac} | rate={rt_raw:.3f} Å/s"
                ),
                force=True,
            )

            _sleep_with_checks(min(1.0, step_dac_interval * 0.5), where=f"step{step_no}_ramp_sleep")

        if entered_main_from_step:
            break

        # (B) target_adc 도달 후 rate_wait_sec 대기 + dep.rate 판정
        if step_rate_wait > 0:
            end_m = time.monotonic() + float(step_rate_wait)
            next_ui_m = time.monotonic()

            while True:
                rt_raw, rt_filtered = read_rate_or_abort(where=f"step{step_no}_wait")

                if _check_step_rate_state(rt_raw, rt_filtered, where=f"step{step_no}_wait"):
                    entered_main_from_step = True
                    break

                adc_total, _, _ = read_adc_total_or_abort(where=f"step{step_no}_wait")
                remain_s = end_m - time.monotonic()
                if remain_s <= 0:
                    break

                now_m = time.monotonic()
                if now_m >= next_ui_m:
                    engine._emit_status(
                        message=(
                            f"STEP {step_no}/{len(ramp_steps)} WAIT | "
                            f"ADC={adc_total:.1f}/{step_target_adc:.1f} | "
                            f"DAC={dac} | rate={rt_raw:.3f} Å/s | "
                            f"남은 {engine._fmt_hms(remain_s, ceil=True)}"
                        ),
                        force=True,
                    )
                    next_ui_m = now_m + 1.0

                _sleep_with_checks(min(0.1, max(0.0, remain_s)), where=f"step{step_no}_wait_sleep")

        if entered_main_from_step:
            break

        # (C) rate_wait 완료 후 dep.rate 판정 → rate_low_action 처리
        rt_raw_now, rt_f_now = read_rate_or_abort(where=f"step{step_no}_rate_check")
        rate_ok = (rt_f_now >= step_min_dep_rate)

        if not rate_ok:
            if step_low_action == "stop":
                _raise_engine_failed(
                    step.name,
                    f"EVAP: STEP {step_no} rate 부족 → 소진 판정 정지 "
                    f"(rt_f={rt_f_now:.3f} < min={step_min_dep_rate:.3f})"
                )

            elif step_low_action == "boost_dac" and step_boost_dac_step > 0 and step_boost_max > 0:
                engine._emit_status(
                    message=f"STEP {step_no} rate 부족 → boost_dac 시작 (최대 {step_boost_max}회)",
                    force=True,
                )
                _boost_ok = False
                for _bi in range(step_boost_max):
                    dac = min(dac_max, dac + step_boost_dac_step)
                    apply_dac()
                    engine._emit_status(
                        message=f"STEP {step_no} BOOST {_bi+1}/{step_boost_max} | DAC={dac}",
                        force=True,
                    )
                    # boost 후 rate_wait_sec 대기
                    if step_rate_wait > 0:
                        _sleep_with_checks(step_rate_wait, where=f"step{step_no}_boost_wait")
                    else:
                        _sleep_with_checks(3.0, where=f"step{step_no}_boost_wait")

                    _rt_r, _rt_f = read_rate_or_abort(where=f"step{step_no}_boost_check")
                    if _rt_f >= step_min_dep_rate:
                        engine._emit_status(
                            message=f"STEP {step_no} BOOST 성공 (rate={_rt_f:.3f})",
                            force=True,
                        )
                        _boost_ok = True
                        break

                if not _boost_ok:
                    _raise_engine_failed(
                        step.name,
                        f"EVAP: STEP {step_no} boost {step_boost_max}회 소진 후 rate 미달 → 소진 판정 정지 "
                        f"(rt_f={_rt_f:.3f} < min={step_min_dep_rate:.3f})"
                    )

            # rate_low_action == "next_step": 그냥 다음 step으로 진행 (아무것도 안 함)
            else:
                engine._emit_status(
                    message=(
                        f"STEP {step_no} rate 부족이지만 next_step 정책 → 다음 step으로 "
                        f"(rt_f={rt_f_now:.3f} < min={step_min_dep_rate:.3f})"
                    ),
                    force=True,
                )

        if entered_main_from_step:
            break

    # 4) 모든 step 완료 후 dep.rate 미도달 시 정책 처리
    if not entered_main_from_step:
        # ✅ extra ramp는 별도 판정 구간으로 다시 시작
        step_rate_peak = 0.0
        step_drop_hits = 0
        step_reach_hits = 0
        step_drop_low_start_ts = None

        if after_last_step_policy == "extra_ramp" and extra_ramp_enabled:
            engine._emit_status(
                message=(
                    f"STEP 완료 후 extra ramp 진입 | "
                    f"max_adc={extra_ramp_max_adc:.1f} | "
                    f"step_cap={extra_ramp_step_cap:.1f} | "
                    f"interval={extra_ramp_interval_s:.1f}s"
                ),
                force=True,
            )

            while True:
                rt_raw, rt_filtered = read_rate_or_abort(where="extra_ramp")

                if _check_step_rate_state(rt_raw, rt_filtered, where="extra_ramp"):
                    entered_main_from_step = True
                    break

                adc_total, _, _ = read_adc_total_or_abort(where="extra_ramp")
                if adc_total >= extra_ramp_max_adc:
                    _raise_engine_failed(
                        step.name,
                        f"EVAP: extra ramp max_adc({extra_ramp_max_adc:.1f}) 도달했지만 "
                        f"target_rate 미도달 (rate={rt_raw:.3f})"
                    )

                if dac >= dac_max:
                    _raise_engine_failed(
                        step.name,
                        f"EVAP: DAC_MAX({dac_max}) 도달했지만 extra ramp 중 target_rate 미도달"
                    )

                dac_inc = _dynamic_dac_step_from_adc_gap(
                    adc_total,
                    extra_ramp_max_adc,
                    step_cap=extra_ramp_step_cap,
                )

                dac = min(dac_max, dac + int(dac_inc))
                apply_dac()

                engine._emit_status(
                    message=(
                        f"EXTRA RAMP | ADC={adc_total:.1f}/{extra_ramp_max_adc:.1f} | "
                        f"DAC={dac} | rate={rt_raw:.3f} Å/s | +{int(dac_inc)}"
                    ),
                    force=True,
                )

                _sleep_with_checks(
                    max(float(extra_ramp_interval_s), _ramp_interval_by_dac(dac)),
                    where="extra_ramp_sleep",
                )
        else:
            _raise_engine_failed(
                step.name,
                "EVAP: 모든 step 완료 후에도 target_rate 미도달 (after_last_step_policy=stop)"
            )

    engine._emit_status(message="STEP 기반 ADC ramp 완료 → fine tune 진입", force=True)

    # MODIFIED: PI 제어기 초기화 (fine_tune 진입 전 리셋)
    _pi = EvapPI(kp=pi_kp, ki=pi_ki, dac_min=0, dac_max=dac_max)
    _pi.reset()

    # 6) target_rate ±5% band 안으로 fine tune
    engine._emit_status(message=f"EVAP fine tune: tol=±{rate_tol_ratio*100:.1f}% / stable_hits={target_stable_hits} / PI(kp={pi_kp},ki={pi_ki})")
    t_tune0 = time.monotonic()
    tune_timeout_s = float(meta.get("tune_timeout_s", 120.0) or 120.0)

    stable_hits = 0

    while True:
        rt_raw, rt_filtered = read_rate_or_abort(where="fine_tune")

        if _in_band(rt_filtered, target_rate, tol_ratio=rate_tol_ratio):
            stable_hits += 1
        else:
            stable_hits = 0

        if stable_hits >= max(1, int(target_stable_hits)):
            break

        if (time.monotonic() - t_tune0) > tune_timeout_s:
            _raise_engine_failed(step.name, f"EVAP: target_rate fine tune timeout {tune_timeout_s}s (rt={rt_raw:.3f})")

        # MODIFIED: PI 제어기로 DAC 조정 (fine_tune)
        new_dac = _pi.compute(dac, target_rate, rt_filtered)

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
        engine._emit_status(message=f"SHUTTER DELAY / 0.00/{total_min:.2f} min", force=True)
        shutter_drop_low_start_ts: Optional[float] = None

        while True:
            rt_raw, rt_filtered = read_rate_or_abort(where="shutter_delay")

            # rate 급감 감지(셔터 열기 전)
            shutter_drop_met, shutter_drop_low_start_ts = _is_below_threshold_sustained(
                rt_filtered,
                target_rate * rate_drop_ratio,
                shutter_drop_low_start_ts,
            )
            if shutter_drop_met:
                _raise_engine_failed(
                    step.name,
                    f"EVAP: dep.rate 급감(셔터 전) rt_raw={rt_raw:.3f}, rt_f={rt_filtered:.3f}, "
                    f"th={target_rate*rate_drop_ratio:.3f}"
                )

            # MODIFIED: PI 제어기로 DAC 조정 (shutter_delay)
            new_dac = _pi.compute(dac, target_rate, rt_filtered)
            maybe_apply_dac(new_dac, min_interval_s=_dac_min_interval_by_dac(max(int(dac), int(new_dac))))

            nowm = time.monotonic()
            remain_s = tendm - nowm
            if remain_s <= 0:
                break

            # ✅ 1초마다 "경과/남은(min)" 표시
            if nowm >= next_ui:
                elapsed_min = (nowm - t0m) / 60.0
                remain_min = max(0.0, remain_s) / 60.0
                adc_total, _, _ = read_adc_total_or_abort(where="shutter_delay")
                engine._emit_status(
                    message=(
                        f"SHUTTER DELAY / {elapsed_min:.2f}/{total_min:.2f} min "
                        f"(remain {remain_min:.2f}) / ADC={adc_total:.1f} / DAC={dac}"
                    ),
                    force=True,
                )
                next_ui = nowm + 1.0

            # ✅ 남은 시간 기준 sleep(딜레이 누적 오차 최소화)
            _sleep_with_checks(min(0.1, max(0.0, remain_s)))

    # 8) delay 종료 → STM zero
    engine._emit_status(message="STM ZERO (thickness reset)", force=True)
    zero_mode = str(meta.get("zero_mode", "B") or "B")
    _stm_zero_thickness(engine, recipe, step, mode=zero_mode)

    # zero 직후 thickness baseline 확보(소프트웨어 기준)
    th0 = read_th_or_abort()

    # MAIN_SHUTTER open
    engine._emit_status(message="MAIN SHUTTER OPEN", force=True)
    engine._plc_write_coil("MAIN_SHUTTER_SW", True, tag="EVAP_MAIN_SHUTTER_OPEN")

    engine._emit_status(message=f"EVAP: thickness baseline th0={th0:.1f} Å")

    # MODIFIED: main shutter open 직후부터 rate drop 감지 활성화
    rate_drop_enabled = True
    engine._emit_status(message="EVAP: rate drop 감지 활성화 (main shutter open)", force=True)

    # 9) 증착 루프
    drop_hits = 0
    main_drop_threshold = float(target_rate) * float(rate_drop_ratio)
    main_drop_low_start_ts: Optional[float] = None

    while True:
        engine._check_stop_pause(recipe, step)
        engine._tick_emit(recipe, step)

        rt_raw, rt_filtered = read_rate_or_abort(where="main_process")
        th = read_th_or_abort()

        dep_th = th - th0
        if dep_th < 0:
            dep_th = 0.0

        # MODIFIED: rate drop 감지 — main shutter open 이후에만 활성화
        if rate_drop_enabled:
            main_drop_met, main_drop_low_start_ts = _is_below_threshold_sustained(
                rt_filtered,
                main_drop_threshold,
                main_drop_low_start_ts,
            )
            if main_drop_met:
                drop_hits += 1
                if drop_hits >= max(1, int(rate_drop_count)):
                    _raise_engine_failed(
                        step.name,
                        f"EVAP: dep.rate 급감 감지 → 중단 "
                        f"(rt_raw={rt_raw:.3f}, rt_f={rt_filtered:.3f}, "
                        f"th={main_drop_threshold:.3f}, hits={drop_hits}/{rate_drop_count})"
                    )
            else:
                drop_hits = 0
        else:
            main_drop_low_start_ts = None

        # MODIFIED: PI 제어기로 DAC 조정 (main_loop)
        new_dac = _pi.compute(dac, target_rate, rt_filtered)
        maybe_apply_dac(new_dac, min_interval_s=_dac_min_interval_by_dac(max(int(dac), int(new_dac))))

        remain_th = max(0.0, float(target_th) - float(dep_th))
        adc_total, _, _ = read_adc_total_or_abort(where="main_process_adc")
        engine._emit_status(
            message=(
                f"MAIN PROCESSING / remain {remain_th:.1f}Å "
                f"( {dep_th:.1f}/{target_th:.1f}Å ) / ADC={adc_total:.1f} / DAC={dac}"
            ),
        )

        if dep_th >= target_th:
            break

        _sleep_with_checks(0.5)

    # 10) 종료
    engine._emit_status(message="EVAP 완료: shutter close + power off")
    engine._safe_shutdown_sequence(tag="EVAP_DONE")
    engine._emit_status(message="EVAP 완료")
