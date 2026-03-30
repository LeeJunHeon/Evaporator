# -*- coding: utf-8 -*-
"""
process/evap_runtime.py

EVAP 전용 증착 runtime 로직.

현재 역할:
- engine 인스턴스를 받아 engine의 공용 helper를 사용
- EVAP 공정의 상승 / 유지 / 하강 제어를 수행
- process_config exact schema 기반 step ramp-up(최대 10 step)을 수행
- 상승 중 dep.rate 안정 도달 시 즉시 유지 단계로 전환
- 유지 단계에서 target dep.rate를 맞추며 thickness 도달까지 진행
- 종료 시 shutter close 후 DAC ramp down을 수행하고 safety 시퀀스를 재사용

포함 함수:
- run_evap_deposition_control(engine, recipe, step)
- _load_runtime_process_config(meta, ...)
- _stm_wait_connected(engine, ...)
- _stm_apply_material_params(engine, ...)
- _stm_zero_thickness(engine, ...)
- _evap_apply_dac(engine, ...)
- _evap_adjust_dac(...)
- ADC/DAC readback helper
"""

from __future__ import annotations

import time
from typing import Optional

from process.models import ProcessRecipe, ProcessStep


def _raise_engine_failed(step_name: str, detail: str) -> None:
    """
    EngineFailed는 engine.py 안에 있으므로 런타임 지연 import로 raise.
    순환 import 방지용.
    """
    # 모듈 최상위 import 대신 호출 시점에 import해서 evap_runtime ↔ engine 순환 참조 방지
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
        engine._check_stop(recipe, step)
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


def _evap_adjust_dac(
    dac: int,
    rate: Optional[float],
    target_rate: float,
    *,
    tol_ratio: float,
    fine_step_dac: int,
    dac_max: int,
) -> int:
    dac = int(dac)

    if rate is None:
        return dac

    tr = float(target_rate)
    if tr <= 0:
        return dac

    tol = abs(tr) * float(tol_ratio)
    err = tr - float(rate)

    # 오차가 허용 범위 이내면 DAC를 변경하지 않음
    if abs(err) <= tol:
        return dac

    step = max(1, int(fine_step_dac))
    if err > 0:
        return min(int(dac_max), dac + step)
    return max(0, dac - step)


def _filter_hold_rate(
    raw_rate: Optional[float],
    prev_filtered: Optional[float],
    *,
    alpha: float,
    max_jump_abs: float,
) -> tuple[Optional[float], bool, bool]:
    if raw_rate is None:
        return prev_filtered, False, False

    sample = float(raw_rate)
    if sample < 0.0:
        return prev_filtered, False, False

    filtered = sample
    jump_clamped = False

    if prev_filtered is not None:
        prev = float(prev_filtered)
        # 순간 스파이크 억제: 이전 값 대비 max_jump_abs 초과 시 clamp 후 EMA 적용
        if max_jump_abs > 0.0 and abs(sample - prev) > max_jump_abs:
            sample = prev + max_jump_abs if sample > prev else prev - max_jump_abs
            jump_clamped = True
        # EMA(지수 이동 평균): filtered = prev + alpha * (sample - prev)
        a = min(1.0, max(0.01, float(alpha)))
        filtered = prev + a * (sample - prev)

    return filtered, True, jump_clamped


def _compute_hold_pi_delta(
    *,
    current_dac: int,
    target_rate: float,
    filtered_rate: Optional[float],
    interval_s: float,
    tol_ratio: float,
    kp: float,
    ki: float,
    integral_state: float,
    integral_limit: float,
    max_delta: int,
    dac_max: int,
) -> tuple[int, float, Optional[float], bool]:
    if filtered_rate is None or target_rate <= 0.0:
        return 0, integral_state, None, False

    error = float(target_rate) - float(filtered_rate)
    tolerance = abs(float(target_rate)) * float(tol_ratio)

    if abs(error) <= tolerance:
        # 허용 범위 내면 적분 항을 천천히 감쇠시켜 offset 누적 방지
        return 0, integral_state * 0.85, error, True

    dt = max(0.1, float(interval_s))
    # 오차를 목표값 대비 비율로 정규화 → Kp/Ki 게인 튜닝을 rate 절대값과 무관하게 유지
    error_ratio = error / max(abs(float(target_rate)), 1e-6)
    next_integral = integral_state + (error_ratio * dt)
    # 적분 windup 방지: 상/하한 clamp
    next_integral = max(-abs(float(integral_limit)), min(abs(float(integral_limit)), next_integral))

    max_step = max(1, int(max_delta))
    p_term = float(kp) * error_ratio
    i_term = float(ki) * next_integral
    delta_f = p_term + i_term
    delta_f = max(-float(max_step), min(float(max_step), delta_f))

    at_low = int(current_dac) <= 0
    at_high = int(current_dac) >= int(dac_max)
    if (at_low and delta_f < 0.0) or (at_high and delta_f > 0.0):
        next_integral = integral_state
        i_term = float(ki) * next_integral
        delta_f = p_term + i_term
        delta_f = max(-float(max_step), min(float(max_step), delta_f))

    delta = 0
    if abs(delta_f) >= 0.5:
        delta = int(round(delta_f))
    elif delta_f > 0.0:
        delta = 1
    elif delta_f < 0.0:
        delta = -1

    next_dac = max(0, min(int(dac_max), int(current_dac) + int(delta)))
    if next_dac == int(current_dac):
        return 0, next_integral, error, True

    return int(next_dac - int(current_dac)), next_integral, error, True


def _compute_hold_pid_delta(
    *,
    current_dac: int,
    target_rate: float,
    filtered_rate: Optional[float],
    interval_s: float,
    tol_ratio: float,
    kp: float,
    ki: float,
    kd: float,
    integral_state: float,
    prev_error_ratio: float,
    integral_limit: float,
    max_delta: int,
    dac_max: int,
) -> tuple[int, float, float, Optional[float], bool]:
    """
    반환: (delta, next_integral, next_prev_error_ratio, error, in_tolerance)
    """
    if filtered_rate is None or target_rate <= 0.0:
        return 0, integral_state, prev_error_ratio, None, False

    error = float(target_rate) - float(filtered_rate)
    tolerance = abs(float(target_rate)) * float(tol_ratio)

    if abs(error) <= tolerance:
        return 0, integral_state * 0.85, prev_error_ratio, error, True

    dt = max(0.1, float(interval_s))
    error_ratio = error / max(abs(float(target_rate)), 1e-6)

    # P항
    p_term = float(kp) * error_ratio

    # I항 (anti-windup)
    next_integral = integral_state + (error_ratio * dt)
    next_integral = max(-abs(float(integral_limit)), min(abs(float(integral_limit)), next_integral))
    i_term = float(ki) * next_integral

    # D항 (filtered_rate 기반 — 노이즈 억제)
    d_term = float(kd) * (error_ratio - prev_error_ratio) / dt

    delta_f = p_term + i_term + d_term

    max_step = max(1, int(max_delta))
    delta_f = max(-float(max_step), min(float(max_step), delta_f))

    # anti-windup: DAC 상/하한에서 integral 롤백
    at_low = int(current_dac) <= 0
    at_high = int(current_dac) >= int(dac_max)
    if (at_low and delta_f < 0.0) or (at_high and delta_f > 0.0):
        next_integral = integral_state
        i_term = float(ki) * next_integral
        delta_f = p_term + i_term + d_term
        delta_f = max(-float(max_step), min(float(max_step), delta_f))

    delta = 0
    if abs(delta_f) >= 0.5:
        delta = int(round(delta_f))
        delta = max(-max_step, min(max_step, delta))

    return delta, next_integral, error_ratio, error, False


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

    # 임시 하드웨어 매핑:
    # Power1 only 운전에서는 실제 Power1 feedback을 ADC2로 본다.
    # 반환값도 logical 기준으로 맞춰 downstream 표시/UI가 헷갈리지 않게 한다.
    # (배선 이슈로 ADC 채널이 물리적으로 반전된 경우에 대한 보정)
    if power1_feedback_adc2 and use_p1 and (not use_p2):
        logical_p1 = a2
        logical_p2 = a1
        total = logical_p1
        return total, logical_p1, logical_p2

    total = _sum_selected_values(use_p1, use_p2, a1, a2)
    return total, a1, a2


# --------------------------------------------------------
# EVAP deposition control
# --------------------------------------------------------
def _load_runtime_process_config(meta: dict, *, step_name: str) -> dict:
    process_config = dict(meta.get("process_config") or {})
    if not process_config:
        _raise_engine_failed(step_name, "EVAP: process_config is missing")

    def _require_int(name: str) -> int:
        if name not in process_config:
            _raise_engine_failed(step_name, f"EVAP: process_config.{name} is missing")
        try:
            return int(process_config[name])
        except Exception:
            _raise_engine_failed(step_name, f"EVAP: process_config.{name} conversion failed")

    def _require_float(name: str) -> float:
        if name not in process_config:
            _raise_engine_failed(step_name, f"EVAP: process_config.{name} is missing")
        try:
            return float(process_config[name])
        except Exception:
            _raise_engine_failed(step_name, f"EVAP: process_config.{name} conversion failed")

    raw_ramp_steps = process_config.get("ramp_steps")
    if not isinstance(raw_ramp_steps, list) or not raw_ramp_steps:
        _raise_engine_failed(step_name, "EVAP: process_config.ramp_steps is empty")

    if len(raw_ramp_steps) > 10:
        _raise_engine_failed(step_name, "EVAP: process_config.ramp_steps supports up to 10 steps only")

    step_count = _require_int("step_count")
    if step_count != len(raw_ramp_steps):
        _raise_engine_failed(
            step_name,
            f"EVAP: process_config.step_count mismatch "
            f"(step_count={step_count}, ramp_steps={len(raw_ramp_steps)})"
        )

    ramp_steps: list[dict] = []
    last_adc = -1.0

    for idx, item in enumerate(raw_ramp_steps, start=1):
        if not isinstance(item, dict):
            _raise_engine_failed(step_name, f"EVAP: ramp_steps[{idx}] must be dict")

        try:
            target_adc = float(item["target_adc"])
            dac_step = int(item["dac_step"])
            dac_interval_sec = float(item["dac_interval_sec"])
            hold_sec = float(item["hold_sec"])
        except KeyError as ex:
            _raise_engine_failed(step_name, f"EVAP: ramp_steps[{idx}] missing key: {ex}")
        except Exception:
            _raise_engine_failed(step_name, f"EVAP: ramp_steps[{idx}] conversion failed")

        if target_adc <= 0:
            _raise_engine_failed(step_name, f"EVAP: ramp_steps[{idx}].target_adc must be > 0")
        if dac_step <= 0:
            _raise_engine_failed(step_name, f"EVAP: ramp_steps[{idx}].dac_step must be > 0")
        if dac_interval_sec <= 0:
            _raise_engine_failed(step_name, f"EVAP: ramp_steps[{idx}].dac_interval_sec must be > 0")
        if hold_sec < 0:
            _raise_engine_failed(step_name, f"EVAP: ramp_steps[{idx}].hold_sec must be >= 0")

        # ramp_steps는 target_adc가 단조 증가해야 함: 이전 step보다 낮은 target 설정 금지
        if last_adc >= 0 and target_adc < last_adc:
            _raise_engine_failed(
                step_name,
                f"EVAP: ramp_steps[{idx}].target_adc must be non-decreasing "
                f"(prev={last_adc}, current={target_adc})"
            )

        ramp_steps.append(
            {
                "step_no": idx,
                "target_adc": target_adc,
                "dac_step": dac_step,
                "dac_interval_sec": dac_interval_sec,
                "hold_sec": hold_sec,
            }
        )
        last_adc = target_adc

    dac_max = _require_int("dac_max")
    rate_tol_ratio = _require_float("rate_tol_ratio")
    rate_stable_sec = _require_float("rate_stable_sec")
    hold_control_interval_s = _require_float("hold_control_interval_s")
    fine_step_dac = _require_int("fine_step_dac")
    rate_abort_ratio = _require_float("rate_abort_ratio")
    rate_abort_sec = _require_float("rate_abort_sec")
    sensor_none_abort_s = _require_float("sensor_none_abort_s")
    adc_none_abort_s = _require_float("adc_none_abort_s")

    spike_abort_ratio = float(process_config.get("spike_abort_ratio", 3.0) or 3.0)
    spike_grace_s = float(process_config.get("spike_grace_s", 5.0) or 5.0)
    if spike_abort_ratio < 1.0:
        spike_abort_ratio = 3.0
    if spike_grace_s < 0.0:
        spike_grace_s = 0.0

    hold_max_dac_delta = int(process_config.get("hold_max_dac_delta", fine_step_dac) or fine_step_dac)
    if hold_max_dac_delta <= 0:
        hold_max_dac_delta = max(1, fine_step_dac)

    hold_control_mode = str(process_config.get("hold_control_mode", "PI") or "").strip().upper() or "PI"
    if hold_control_mode not in {"PI", "PID", "STEP"}:
        hold_control_mode = "PI"

    hold_pi_kp = float(process_config.get("hold_pi_kp", max(1.0, hold_max_dac_delta * 5.0)) or max(1.0, hold_max_dac_delta * 5.0))
    hold_pi_ki = float(process_config.get("hold_pi_ki", max(0.0, hold_max_dac_delta * 0.8)) or max(0.0, hold_max_dac_delta * 0.8))
    hold_pi_kd = float(process_config.get("hold_pi_kd", 0.0) or 0.0)
    hold_pi_kd = max(0.0, hold_pi_kd)
    hold_integral_limit = float(
        process_config.get(
            "hold_integral_limit",
            max(1.0, (2.0 * hold_max_dac_delta) / max(hold_pi_ki, 1e-6)),
        ) or max(1.0, (2.0 * hold_max_dac_delta) / max(hold_pi_ki, 1e-6))
    )
    rate_filter_alpha = float(process_config.get("rate_filter_alpha", 0.35) or 0.35)
    rate_jump_guard_ratio = float(process_config.get("rate_jump_guard_ratio", 0.50) or 0.50)
    rate_jump_guard_abs = float(process_config.get("rate_jump_guard_abs", 0.15) or 0.15)

    if dac_max <= 0:
        _raise_engine_failed(step_name, "EVAP: process_config.dac_max must be > 0")
    if not (0.0 < rate_tol_ratio < 1.0):
        _raise_engine_failed(step_name, "EVAP: process_config.rate_tol_ratio must be in (0, 1)")
    if rate_stable_sec < 0:
        _raise_engine_failed(step_name, "EVAP: process_config.rate_stable_sec must be >= 0")
    if hold_control_interval_s <= 0:
        _raise_engine_failed(step_name, "EVAP: process_config.hold_control_interval_s must be > 0")
    if fine_step_dac <= 0:
        _raise_engine_failed(step_name, "EVAP: process_config.fine_step_dac must be > 0")
    if not (0.0 < rate_abort_ratio < 1.0):
        _raise_engine_failed(step_name, "EVAP: process_config.rate_abort_ratio must be in (0, 1)")
    if rate_abort_sec < 0:
        _raise_engine_failed(step_name, "EVAP: process_config.rate_abort_sec must be >= 0")
    if sensor_none_abort_s <= 0:
        _raise_engine_failed(step_name, "EVAP: process_config.sensor_none_abort_s must be > 0")
    if adc_none_abort_s <= 0:
        _raise_engine_failed(step_name, "EVAP: process_config.adc_none_abort_s must be > 0")

    hold_pi_kp = max(0.0, hold_pi_kp)
    hold_pi_ki = max(0.0, hold_pi_ki)
    hold_integral_limit = max(0.1, hold_integral_limit)
    rate_filter_alpha = min(1.0, max(0.01, rate_filter_alpha))
    rate_jump_guard_ratio = max(0.0, rate_jump_guard_ratio)
    rate_jump_guard_abs = max(0.0, rate_jump_guard_abs)

    return {
        "step_count": step_count,
        "ramp_steps": ramp_steps,
        "dac_max": dac_max,
        "rate_tol_ratio": rate_tol_ratio,
        "rate_stable_sec": rate_stable_sec,
        "hold_control_interval_s": hold_control_interval_s,
        "fine_step_dac": fine_step_dac,
        "rate_abort_ratio": rate_abort_ratio,
        "rate_abort_sec": rate_abort_sec,
        "spike_abort_ratio": spike_abort_ratio,
        "spike_grace_s": spike_grace_s,
        "sensor_none_abort_s": sensor_none_abort_s,
        "adc_none_abort_s": adc_none_abort_s,
        "hold_control_mode": hold_control_mode,
        "hold_pi_kp": hold_pi_kp,
        "hold_pi_ki": hold_pi_ki,
        "hold_pi_kd": hold_pi_kd,
        "hold_integral_limit": hold_integral_limit,
        "rate_filter_alpha": rate_filter_alpha,
        "rate_jump_guard_ratio": rate_jump_guard_ratio,
        "rate_jump_guard_abs": rate_jump_guard_abs,
        "hold_max_dac_delta": hold_max_dac_delta,
    }

def run_evap_deposition_control(engine, recipe: ProcessRecipe, step: ProcessStep) -> None:
    """
    EVAP 증착 제어

    목표 구조
    1) 파워 상승:
       - process_config.ramp_steps 순차 수행
       - 각 step:
         target_adc / dac_step / dac_interval_sec / hold_sec
       - step 진행 중에도 dep.rate 계속 확인
       - dep.rate가 target_rate에 안정 도달하면 남은 step 무시하고 즉시 hold 단계 진입

    2) 파워 유지:
       - target dep.rate 유지
       - thickness 도달까지 진행
       - fine_step_dac + hold_control_interval_s 기반 DAC 미세조정
       - rate_abort_ratio/sec 기반 abort

    3) 파워 하강:
       - shutter close
       - DAC 1초에 100씩 ramp down
       - 이후 safety 재사용
    """
    meta = dict(step.meta or {})

    use_p1 = bool(meta.get("use_power1", False))
    use_p2 = bool(meta.get("use_power2", False))
    power1_feedback_adc2 = bool(meta.get("power1_feedback_adc2", False))

    if not (use_p1 or use_p2):
        _raise_engine_failed(step.name, "EVAP: use_power1/use_power2 중 최소 1개는 True여야 합니다.")

    target_rate = float(meta.get("target_rate", 0.0) or 0.0)
    target_th = float(meta.get("target_thickness", 0.0) or 0.0)
    delay_min = float(meta.get("delay_min", 0.0) or 0.0)
    delay_s = max(0.0, delay_min * 60.0)

    if target_rate <= 0:
        _raise_engine_failed(step.name, "EVAP: target_rate must be > 0")
    if target_th <= 0:
        _raise_engine_failed(step.name, "EVAP: target_thickness must be > 0")

    proc_cfg = _load_runtime_process_config(meta, step_name=step.name)

    ramp_steps = list(proc_cfg["ramp_steps"])
    dac_max = int(proc_cfg["dac_max"])
    adc_max = int(proc_cfg.get("adc_max", 200) or 200)
    if adc_max <= 0:
        adc_max = 200

    engine._run_line(
        f"[CFG] target_rate={target_rate} Å/s | target_thickness={target_th} Å"
        f" | delay={delay_min:.1f}min | adc_max={adc_max}"
    )
    rate_tol_ratio = float(proc_cfg["rate_tol_ratio"])
    rate_stable_sec = float(proc_cfg["rate_stable_sec"])
    hold_control_interval_s = float(proc_cfg["hold_control_interval_s"])
    fine_step_dac = int(proc_cfg["fine_step_dac"])
    hold_control_mode = str(proc_cfg.get("hold_control_mode", "PI") or "PI").strip().upper() or "PI"
    hold_pi_kp = float(proc_cfg.get("hold_pi_kp", 0.0) or 0.0)
    hold_pi_ki = float(proc_cfg.get("hold_pi_ki", 0.0) or 0.0)
    hold_pi_kd = float(proc_cfg.get("hold_pi_kd", 0.0) or 0.0)
    hold_integral_limit = float(proc_cfg.get("hold_integral_limit", 1.0) or 1.0)
    rate_filter_alpha = float(proc_cfg.get("rate_filter_alpha", 0.35) or 0.35)
    rate_jump_guard_ratio = float(proc_cfg.get("rate_jump_guard_ratio", 0.50) or 0.50)
    rate_jump_guard_abs = float(proc_cfg.get("rate_jump_guard_abs", 0.15) or 0.15)
    hold_max_dac_delta = int(proc_cfg.get("hold_max_dac_delta", fine_step_dac) or fine_step_dac)
    rate_abort_ratio = float(proc_cfg["rate_abort_ratio"])
    rate_abort_sec = float(proc_cfg["rate_abort_sec"])
    spike_abort_ratio = float(proc_cfg.get("spike_abort_ratio", 3.0) or 3.0)
    spike_grace_s = float(proc_cfg.get("spike_grace_s", 5.0) or 5.0)
    sensor_none_abort_s = float(proc_cfg["sensor_none_abort_s"])
    adc_none_abort_s = float(proc_cfg["adc_none_abort_s"])

    dac = 0
    shutter_open = False
    shutdown_done = False

    def _wait_with_checks(wait_s: float, *, label: str) -> None:
        wait_s = float(wait_s)
        if wait_s <= 0:
            return

        start_m = time.monotonic()
        deadline_m = start_m + wait_s
        next_ui_m = start_m

        while True:
            engine._check_stop(recipe, step)
            engine._tick_emit(recipe, step)

            now_m = time.monotonic()
            remain_s = deadline_m - now_m
            if remain_s <= 0:
                break

            if now_m >= next_ui_m:
                engine._emit_status(
                    message=f"[남은 {engine._fmt_hms(remain_s, ceil=True)}] {label}",
                    force=True,
                )
                next_ui_m = now_m + 1.0

            time.sleep(0.1)

        engine._emit_status(message=f"[남은 00:00] {label} 완료", force=True)

    def _shutdown_wait(wait_s: float, *, label: str) -> None:
        """stop 요청을 무시하고 지정 시간을 끝까지 대기하는 함수.
        _ramp_down_then_shutdown() 내부에서만 사용.
        stop 플래그가 이미 set된 상태에서도 60초 대기를 스킵하지 않기 위해
        engine._check_stop() 호출을 제거한 버전."""
        wait_s = float(wait_s)
        if wait_s <= 0:
            return

        start_m = time.monotonic()
        deadline_m = start_m + wait_s
        next_ui_m = start_m

        while True:
            engine._tick_emit(recipe, step)

            now_m = time.monotonic()
            remain_s = deadline_m - now_m
            if remain_s <= 0:
                break

            if now_m >= next_ui_m:
                engine._emit_status(
                    message=f"[남은 {engine._fmt_hms(remain_s, ceil=True)}] {label}",
                    force=True,
                )
                next_ui_m = now_m + 1.0

            time.sleep(0.1)

        engine._emit_status(message=f"[남은 00:00] {label} 완료", force=True)

    def _read_rate_or_abort(*, where: str) -> float:
        t0 = time.monotonic()
        while True:
            engine._check_stop(recipe, step)
            engine._tick_emit(recipe, step)

            rt = engine._get_rate()
            if rt is not None:
                return float(rt)

            if (time.monotonic() - t0) >= sensor_none_abort_s:
                _raise_engine_failed(step.name, f"EVAP: dep.rate None 지속 {sensor_none_abort_s}s ({where})")

            time.sleep(0.1)

    def _read_thickness_or_abort(*, where: str) -> float:
        t0 = time.monotonic()
        while True:
            engine._check_stop(recipe, step)
            engine._tick_emit(recipe, step)

            th = engine._get_thickness()
            if th is not None:
                return float(th)

            if (time.monotonic() - t0) >= sensor_none_abort_s:
                _raise_engine_failed(step.name, f"EVAP: thickness None 지속 {sensor_none_abort_s}s ({where})")

            time.sleep(0.1)

    def _read_adc_or_abort(*, where: str) -> float:
        t0 = time.monotonic()
        while True:
            engine._check_stop(recipe, step)
            engine._tick_emit(recipe, step)

            adc_total, _, _ = _read_selected_adc_total(
                engine,
                use_p1,
                use_p2,
                power1_feedback_adc2=power1_feedback_adc2,
            )
            if adc_total is not None:
                return float(adc_total)

            if (time.monotonic() - t0) >= adc_none_abort_s:
                _raise_engine_failed(step.name, f"EVAP: ADC None 지속 {adc_none_abort_s}s ({where})")

            time.sleep(0.1)

    def _update_rate_stable_state(
        rt: float,
        stable_start_ts: Optional[float],
    ) -> tuple[bool, Optional[float]]:
        tol = abs(target_rate) * rate_tol_ratio
        if rt >= target_rate - tol:
            now_m = time.monotonic()
            if stable_start_ts is None:
                stable_start_ts = now_m

            if rate_stable_sec <= 0.0:
                return True, stable_start_ts

            return ((now_m - stable_start_ts) >= rate_stable_sec), stable_start_ts

        return False, None
    
    def _update_adc_reached_state(
        adc_total: float,
        target_adc_value: float,
        stable_start_ts: Optional[float],
        stable_sec: float,
    ) -> tuple[bool, Optional[float]]:
        if adc_total >= target_adc_value:
            now_m = time.monotonic()
            if stable_start_ts is None:
                stable_start_ts = now_m

            if stable_sec <= 0.0:
                return True, stable_start_ts

            return ((now_m - stable_start_ts) >= stable_sec), stable_start_ts

        return False, None

    def _ramp_down_then_shutdown(*, tag: str) -> None:
        nonlocal dac, shutter_open, shutdown_done

        if shutdown_done:
            return

        if shutter_open:
            engine._emit_status(message="EVAP 종료: MAIN SHUTTER CLOSE", force=True)
            engine._plc_write_coil("MAIN_SHUTTER_SW", False, tag=f"{tag}_SHUTTER_CLOSE")
            shutter_open = False

        engine._emit_status(message="EVAP 종료: DAC ramp down 시작 (100/sec)", force=True)
        while dac > 0:
            engine._tick_emit(recipe, step)
            dac = max(0, dac - 100)
            _evap_apply_dac(engine, use_p1, use_p2, dac, tag=f"{tag}_RAMPDOWN")
            time.sleep(1.0)

        engine._emit_status(message="EVAP 종료: DAC 0 도달 → 1분 후 전원 차단", force=True)
        _shutdown_wait(60.0, label="전원 차단 전 대기 (60초)")

        engine._safe_shutdown_sequence(tag=tag)

        shutdown_done = True
        # engine 레벨 플래그 설정: EngineStopRequested 처리 시 안전정지 중복 실행 방지
        setattr(engine, "_shutdown_already_executed", True)

    def _run_preopen_guard(prev_filtered: Optional[float]) -> Optional[float]:
        """
        완화형 pre-open overshoot guard.

        목적:
        - STM ZERO 직후 stale/overshoot dep.rate 상태에서 곧바로 MAIN SHUTTER를 열지 않도록
          짧은 재확인 + 제한적 DAC 감산으로 overshoot를 완화한다.

        정책:
        - hard-abort gate가 아니라 deadlock 방지 우선의 soft guard다.
        - guard_max_checks 횟수만큼만 재확인해서 셔터 open 직전 무한 대기에 빠지지 않게 한다.
        - 마지막 재확인까지 filtered_rate가 상한보다 높더라도, 그 사실을
          OPEN_WITH_GUARD_LIMIT로 telemetry에 남기고 공정 흐름은 계속 진행한다.
        - hold 진입 후 raw-rate 기반 abort/safety 의미는 기존 로직이 그대로 담당한다.
        """
        nonlocal dac

        if target_rate <= 0.0:
            return prev_filtered

        guard_wait_s = min(max(0.5, hold_control_interval_s), 2.0)
        # ZERO 직후 짧은 overshoot 완화만 노리고, 셔터 open을 무한정 지연시키지 않기 위해
        # 고정 횟수만 재확인한다.
        guard_max_checks = 3
        guard_upper = float(target_rate) * (1.0 + max(0.0, rate_tol_ratio))
        jump_guard_abs = max(rate_jump_guard_abs, abs(target_rate) * rate_jump_guard_ratio)
        filtered = prev_filtered

        for attempt in range(1, guard_max_checks + 1):
            raw_rate = _read_rate_or_abort(where=f"preopen_guard{attempt}")
            filtered, control_rate_valid, jump_clamped = _filter_hold_rate(
                raw_rate,
                filtered,
                alpha=rate_filter_alpha,
                max_jump_abs=jump_guard_abs,
            )

            current_filtered = filtered if control_rate_valid else None
            if current_filtered is not None and current_filtered <= guard_upper:
                engine._tele_event(
                    event="PREOPEN_GUARD",
                    target="RATE",
                    value=current_filtered,
                    detail=(
                        f"attempt={attempt}, "
                        f"raw_rate={raw_rate:.6f}, "
                        f"filtered_rate={current_filtered:.6f}, "
                        f"upper={guard_upper:.6f}, "
                        f"dac={dac}, "
                        f"jump_clamped={1 if jump_clamped else 0}, "
                        f"action=OPEN_OK, "
                        f"policy=SOFT_LIMIT, "
                        f"tag=EVAP_PREOPEN_GUARD"
                    ),
                )
                return filtered

            adjusted = False
            if current_filtered is not None:
                next_dac = dac
                adjust_limit = min(max(1, fine_step_dac), max(1, hold_max_dac_delta))
                if adjust_limit > 0 and dac > 0:
                    next_dac = max(0, dac - adjust_limit)

                if next_dac != dac:
                    delta = int(next_dac - dac)
                    dac = next_dac
                    adjusted = True
                    _evap_apply_dac(engine, use_p1, use_p2, dac, tag="EVAP_PREOPEN_GUARD")
                    engine._tele_event(
                        event="PREOPEN_GUARD_ADJUST",
                        target="DAC",
                        value=dac,
                        detail=(
                            f"attempt={attempt}, "
                            f"delta={delta}, "
                            f"raw_rate={raw_rate:.6f}, "
                            f"filtered_rate={current_filtered:.6f}, "
                            f"upper={guard_upper:.6f}, "
                            f"action=ADJUST_AND_RECHECK, "
                            f"policy=SOFT_LIMIT, "
                            f"tag=EVAP_PREOPEN_GUARD"
                        ),
                    )

            if attempt < guard_max_checks:
                engine._tele_event(
                    event="PREOPEN_GUARD_WAIT",
                    target="TIME",
                    value=guard_wait_s,
                    detail=(
                        f"attempt={attempt}, "
                        f"raw_rate={raw_rate:.6f}, "
                        f"filtered_rate={'' if current_filtered is None else f'{current_filtered:.6f}'}, "
                        f"upper={guard_upper:.6f}, "
                        f"dac={dac}, "
                        f"adjusted={1 if adjusted else 0}, "
                        f"action={'ADJUST_AND_RECHECK' if adjusted else 'WAIT_AND_RECHECK'}, "
                        f"policy=SOFT_LIMIT, "
                        f"tag=EVAP_PREOPEN_GUARD"
                    ),
                )
                _wait_with_checks(guard_wait_s, label="MAIN SHUTTER OPEN 전 rate 재확인 대기")
                continue

            engine._tele_event(
                event="PREOPEN_GUARD",
                target="RATE",
                value=(current_filtered if current_filtered is not None else raw_rate),
                detail=(
                    f"attempt={attempt}, "
                    f"raw_rate={raw_rate:.6f}, "
                    f"filtered_rate={'' if current_filtered is None else f'{current_filtered:.6f}'}, "
                    f"upper={guard_upper:.6f}, "
                    f"dac={dac}, "
                    f"action=OPEN_WITH_GUARD_LIMIT, "
                    f"policy=SOFT_LIMIT, "
                    f"tag=EVAP_PREOPEN_GUARD"
                ),
            )

        return filtered

    try:
        # 초기 안전 상태
        engine._plc_write_coil("MAIN_SHUTTER_SW", False, tag="EVAP_INIT_SHUTTER_CLOSE")

        # STM 연결 / material params
        _stm_wait_connected(engine, recipe, step, timeout_s=5.0)

        density = float(meta.get("density", 0.0) or 0.0)
        z_factor = float(meta.get("z_factor", 0.0) or 0.0)
        if density > 0 and z_factor > 0:
            engine._emit_status(message=f"STM film params 적용: density={density}, z={z_factor}", force=True)
            _stm_apply_material_params(
                engine,
                recipe,
                step,
                density_g_cm3=density,
                z_factor=z_factor,
            )

        # DAC 0 적용
        _evap_apply_dac(engine, use_p1, use_p2, dac, tag="EVAP_INIT_DAC0")

        # ramp-up 진입 전 고정 대기
        # 전원 ON 직후 ADC가 순간적으로 튀는 현상이 있어,
        # step target_adc 비교 전에 10초간 안정화 시간을 둔다.
        pre_ramp_wait_s = 10.0
        _wait_with_checks(pre_ramp_wait_s, label="EVAP ramp-up 전 ADC 안정화 대기")

        # -------------------------
        # 1) 파워 상승
        # -------------------------
        engine._emit_status(message=f"EVAP 상승 시작: ramp_steps={len(ramp_steps)}", force=True)

        reached_stable = False
        stable_start_ts: Optional[float] = None

        for step_cfg in ramp_steps:
            step_no = int(step_cfg["step_no"])
            step_target_adc = float(step_cfg["target_adc"])
            step_dac_step = int(step_cfg["dac_step"])
            step_dac_interval_sec = float(step_cfg["dac_interval_sec"])
            step_hold_sec = float(step_cfg["hold_sec"])

            engine._emit_status(
                message=(
                    f"RAMP STEP {step_no}/{len(ramp_steps)} 시작 | "
                    f"ADC 목표={step_target_adc:.1f} | "
                    f"DAC +{step_dac_step} / {step_dac_interval_sec:.1f}s | "
                    f"ADC 확인시간={step_hold_sec:.1f}s"
                ),
                force=True,
            )

            last_dac_apply_m = time.monotonic() - step_dac_interval_sec
            adc_reached_start_ts: Optional[float] = None

            while True:
                engine._check_stop(recipe, step)
                engine._tick_emit(recipe, step)

                rt = _read_rate_or_abort(where=f"ramp_step{step_no}")
                adc_total = _read_adc_or_abort(where=f"ramp_step{step_no}")

                stable_ok, stable_start_ts = _update_rate_stable_state(rt, stable_start_ts)
                if stable_ok:
                    reached_stable = True
                    engine._emit_status(
                        message=(
                            f"RAMP STEP {step_no}/{len(ramp_steps)} | "
                            f"dep.rate 안정 도달 -> HOLD 진입"
                        ),
                        force=True,
                    )
                    break

                adc_ok, adc_reached_start_ts = _update_adc_reached_state(
                    adc_total,
                    step_target_adc,
                    adc_reached_start_ts,
                    step_hold_sec,
                )
                if adc_ok:
                    engine._emit_status(
                        message=(
                            f"RAMP STEP {step_no}/{len(ramp_steps)} 완료 | "
                            f"ADC={adc_total:.1f}/{step_target_adc:.1f} | "
                            f"확인시간 {step_hold_sec:.1f}s 충족"
                        ),
                        force=True,
                    )
                    break

                now_m = time.monotonic()
                remain_to_next_dac_s = max(0.0, step_dac_interval_sec - (now_m - last_dac_apply_m))

                if adc_reached_start_ts is None:
                    adc_confirm_elapsed_s = 0.0
                else:
                    adc_confirm_elapsed_s = max(0.0, now_m - adc_reached_start_ts)

                if adc_total < step_target_adc and dac >= dac_max:
                    _raise_engine_failed(
                        step.name,
                        f"EVAP: DAC_MAX({dac_max}) 도달, STEP {step_no} target_adc 안정 도달 실패 "
                        f"(adc={adc_total:.1f}/{step_target_adc:.1f}, rate={rt:.3f})"
                    )

                if adc_total < step_target_adc and (now_m - last_dac_apply_m) >= step_dac_interval_sec:
                    if _read_adc_or_abort(where="ramp_adc_check") >= adc_max:
                        engine._emit_status(
                            message=f"RAMP | ADC 최대값 도달 ({adc_max}) → DAC 증가 중단",
                            force=True,
                        )
                        time.sleep(step_dac_interval_sec)
                    else:
                        dac = min(dac_max, dac + step_dac_step)
                        _evap_apply_dac(engine, use_p1, use_p2, dac, tag=f"EVAP_RAMP_STEP{step_no}")
                        last_dac_apply_m = now_m
                        remain_to_next_dac_s = step_dac_interval_sec

                stable_progress = (
                    f" | rate 안정 확인중 {(now_m - stable_start_ts):.1f}s / {rate_stable_sec:.1f}s"
                    if stable_start_ts is not None else ""
                )

                if adc_total >= step_target_adc:
                    engine._emit_status(
                        message=(
                            f"RAMP STEP {step_no}/{len(ramp_steps)} ADC 확인중 | "
                            f"ADC={adc_total:.1f}/{step_target_adc:.1f} | "
                            f"DAC={dac} | rate={rt:.3f} | "
                            f"확인 {adc_confirm_elapsed_s:.1f}/{step_hold_sec:.1f}s | "
                            f"다음 DAC까지 {remain_to_next_dac_s:.1f}s"
                            f"{stable_progress}"
                        ),
                        force=True,
                    )
                else:
                    engine._emit_status(
                        message=(
                            f"RAMP STEP {step_no}/{len(ramp_steps)} 상승중 | "
                            f"ADC={adc_total:.1f}/{step_target_adc:.1f} | "
                            f"DAC={dac} | rate={rt:.3f} | "
                            f"다음 DAC까지 {remain_to_next_dac_s:.1f}s"
                            f"{stable_progress}"
                        ),
                        force=True,
                    )

                time.sleep(0.1)

            if reached_stable:
                break

        if not reached_stable:
            _raise_engine_failed(
                step.name,
                "EVAP: 모든 ramp_steps 종료 후에도 target_rate 안정 도달 실패"
            )

        # -------------------------
        # 2) 파워 유지
        # -------------------------
        filtered_rate: Optional[float] = None
        filtered_rate = _run_preopen_guard(filtered_rate)

        engine._emit_status(message="EVAP HOLD 진입: MAIN SHUTTER OPEN", force=True)

        if delay_s > 0:
            engine._emit_status(
                message=f"셔터 오픈 대기 중... ({delay_min:.1f}분 / {delay_s:.0f}초)",
                force=True,
            )
            _wait_with_checks(delay_s, label=f"셔터 오픈 대기 ({delay_min:.1f}분)")

        # shutter delay 완료 후 STM Zero — delay 중 증착된 두께를 리셋
        engine._emit_status(message="EVAP HOLD 진입: STM ZERO", force=True)
        zero_mode = str(meta.get("zero_mode", "B") or "B")
        _stm_zero_thickness(engine, recipe, step, mode=zero_mode)

        # ZERO 직후 stale thickness를 baseline으로 잡지 않도록
        # 한 번 반영 시간을 준다.
        _wait_with_checks(hold_control_interval_s, label="STM ZERO 반영 대기")

        # STM Zero 후 IIR 필터 오염값(스파이크 등) 제거
        # Hold PI가 올바른 baseline에서 시작하도록 None으로 리셋
        filtered_rate = None

        engine._plc_write_coil("MAIN_SHUTTER_SW", True, tag="EVAP_MAIN_SHUTTER_OPEN")
        shutter_open = True

        last_hold_control_m = 0.0
        rate_abort_start_ts: Optional[float] = None
        spike_detected_ts: Optional[float] = None
        abort_threshold = target_rate * rate_abort_ratio
        hold_integral = 0.0
        hold_prev_error_ratio: float = 0.0

        engine._tele_event(
            event="HOLD_CONTROL_MODE",
            target="MODE",
            value=hold_control_mode,
            detail=(
                f"control_mode={hold_control_mode}, "
                f"kp={hold_pi_kp:.3f}, ki={hold_pi_ki:.3f}, "
                f"alpha={rate_filter_alpha:.3f}, max_delta={hold_max_dac_delta}, tag=EVAP_HOLD_CONTROL"
            ),
        )

        while True:
            engine._check_stop(recipe, step)
            engine._tick_emit(recipe, step)

            rt = _read_rate_or_abort(where="hold")
            adc_total = _read_adc_or_abort(where="hold")
            th = _read_thickness_or_abort(where="hold")

            jump_guard_abs = max(rate_jump_guard_abs, abs(target_rate) * rate_jump_guard_ratio)
            filtered_rate, control_rate_valid, jump_clamped = _filter_hold_rate(
                rt,
                filtered_rate,
                alpha=rate_filter_alpha,
                max_jump_abs=jump_guard_abs,
            )

            # STM ZERO 이후에는 현재 thickness 자체를 증착 두께로 본다.
            dep_th = max(0.0, th)
            remain_th = max(0.0, target_th - dep_th)

            # abort 조건:
            # dep.rate가 0 이상의 유효값일 때만 저하 판정을 건다.
            # 음수 dep.rate는 센서 이상/순간 튐으로 보고 abort 카운트에 넣지 않는다.

            # 스파이크 감지 (target_rate의 N배 이상이면 센서 오류로 간주)
            if rt > target_rate * spike_abort_ratio:
                spike_detected_ts = time.monotonic()

            # rate_abort 판정
            if rt < 0.0:
                rate_abort_start_ts = None
            elif rt <= abort_threshold:
                now_m = time.monotonic()
                # 스파이크 직후 유예 중이면 abort 카운터 시작 안 함
                if spike_detected_ts is not None and (now_m - spike_detected_ts) < spike_grace_s:
                    rate_abort_start_ts = None
                    engine._emit_status(
                        message=(
                            f"HOLD | dep.rate 저하 감지 — 스파이크 회복 대기 중 "
                            f"({now_m - spike_detected_ts:.1f}/{spike_grace_s:.1f}s)"
                        ),
                        force=True,
                    )
                else:
                    if rate_abort_start_ts is None:
                        rate_abort_start_ts = now_m
                    elif (now_m - rate_abort_start_ts) >= rate_abort_sec:
                        _raise_engine_failed(
                            step.name,
                            f"EVAP: hold 중 dep.rate 저하 abort "
                            f"(rate={rt:.3f} <= {abort_threshold:.3f}, 지속 {rate_abort_sec:.1f}s)"
                        )
            else:
                rate_abort_start_ts = None

            # hold_control_interval_s 주기 제어
            now_m = time.monotonic()
            if (now_m - last_hold_control_m) >= hold_control_interval_s:
                control_delta = 0
                control_error: Optional[float] = None
                control_mode_used = hold_control_mode

                try:
                    if hold_control_mode == "PID":
                        control_delta, hold_integral, hold_prev_error_ratio, control_error, pi_used = _compute_hold_pid_delta(
                            current_dac=dac,
                            target_rate=target_rate,
                            filtered_rate=(filtered_rate if control_rate_valid else None),
                            interval_s=hold_control_interval_s,
                            tol_ratio=rate_tol_ratio,
                            kp=hold_pi_kp,
                            ki=hold_pi_ki,
                            kd=hold_pi_kd,
                            integral_state=hold_integral,
                            prev_error_ratio=hold_prev_error_ratio,
                            integral_limit=hold_integral_limit,
                            max_delta=hold_max_dac_delta,
                            dac_max=dac_max,
                        )
                        if not pi_used:
                            control_mode_used = "PID_WAIT"
                    elif hold_control_mode == "PI":
                        control_delta, hold_integral, control_error, pi_used = _compute_hold_pi_delta(
                            current_dac=dac,
                            target_rate=target_rate,
                            filtered_rate=(filtered_rate if control_rate_valid else None),
                            interval_s=hold_control_interval_s,
                            tol_ratio=rate_tol_ratio,
                            kp=hold_pi_kp,
                            ki=hold_pi_ki,
                            integral_state=hold_integral,
                            integral_limit=hold_integral_limit,
                            max_delta=hold_max_dac_delta,
                            dac_max=dac_max,
                        )
                        if not pi_used:
                            control_mode_used = "PI_WAIT"
                    else:
                        new_dac = _evap_adjust_dac(
                            dac,
                            (filtered_rate if control_rate_valid else None),
                            target_rate,
                            tol_ratio=rate_tol_ratio,
                            fine_step_dac=fine_step_dac,
                            dac_max=dac_max,
                        )
                        control_delta = int(new_dac - dac)
                        if control_rate_valid and filtered_rate is not None:
                            control_error = float(target_rate) - float(filtered_rate)
                except Exception as control_exc:
                    control_mode_used = "STEP_FALLBACK"
                    control_delta = 0
                    control_error = None
                    engine._tele_event(
                        event="HOLD_CONTROL_WARN",
                        target="MODE",
                        value=hold_control_mode,
                        detail=f"fallback=STEP, error={control_exc!r}, tag=EVAP_HOLD_CONTROL",
                    )
                    new_dac = _evap_adjust_dac(
                        dac,
                        (filtered_rate if control_rate_valid else None),
                        target_rate,
                        tol_ratio=rate_tol_ratio,
                        fine_step_dac=fine_step_dac,
                        dac_max=dac_max,
                    )
                    control_delta = int(new_dac - dac)

                if control_delta != 0:
                    # ADC 최대값 초과 시 DAC 증가 억제
                    if control_delta > 0 and adc_total >= adc_max:
                        control_delta = 0
                if control_delta != 0:
                    dac = max(0, min(dac_max, int(dac + control_delta)))
                    _evap_apply_dac(engine, use_p1, use_p2, dac, tag="EVAP_HOLD_CONTROL")

                engine._tele_event(
                    event="HOLD_CONTROL",
                    target="DAC",
                    value=dac,
                    detail=(
                        f"mode={control_mode_used}, "
                        f"control_mode={hold_control_mode}, "
                        f"raw_rate={rt:.6f}, "
                        f"filtered_rate={'' if filtered_rate is None else f'{filtered_rate:.6f}'}, "
                        f"error={'' if control_error is None else f'{control_error:.6f}'}, "
                        f"delta={control_delta}, "
                        f"integral={hold_integral:.6f}, "
                        f"jump_clamped={1 if jump_clamped else 0}, "
                        f"tag=EVAP_HOLD_CONTROL"
                    ),
                )
                last_hold_control_m = now_m

            engine._emit_status(
                message=(
                    f"HOLD | remain {remain_th:.1f}Å "
                    f"( {dep_th:.1f}/{target_th:.1f}Å ) | "
                    f"rate={rt:.3f}"
                    f"{'' if filtered_rate is None else f' | filtered={filtered_rate:.3f}'} | "
                    f"ADC={adc_total:.1f} | DAC={dac}"
                ),
                force=True,
            )

            if target_th > 0 and dep_th >= target_th:
                break

            time.sleep(0.1)

        # -------------------------
        # 3) 파워 하강
        # -------------------------
        _ramp_down_then_shutdown(tag="EVAP_DONE")
        engine._emit_status(message="EVAP 완료", force=True)

    except Exception as ex:
        try:
            from process.engine import EngineStopRequested
            is_stop_requested = isinstance(ex, EngineStopRequested)
        except Exception:
            is_stop_requested = False

        try:
            if is_stop_requested:
                _ramp_down_then_shutdown(tag="EVAP_STOP")
            else:
                _ramp_down_then_shutdown(tag="EVAP_FAIL")
        except Exception:
            pass
        raise
