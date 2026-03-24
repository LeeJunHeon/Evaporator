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

    if abs(err) <= tol:
        return dac

    step = max(1, int(fine_step_dac))
    if err > 0:
        return min(int(dac_max), dac + step)
    return max(0, dac - step)


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
    # 현재 active path는 Power1 only 운전이지만,
    # 실제 Power1 feedback은 임시로 ADC2 값을 사용한다.
    # dual-power 복구 전까지는 이 우회를 유지한다.
    if power1_feedback_adc2 and use_p1 and (not use_p2):
        return a2, a1, a2

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
        "sensor_none_abort_s": sensor_none_abort_s,
        "adc_none_abort_s": adc_none_abort_s,
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

    if target_rate <= 0:
        _raise_engine_failed(step.name, "EVAP: target_rate must be > 0")
    if target_th <= 0:
        _raise_engine_failed(step.name, "EVAP: target_thickness must be > 0")

    proc_cfg = _load_runtime_process_config(meta, step_name=step.name)

    ramp_steps = list(proc_cfg["ramp_steps"])
    dac_max = int(proc_cfg["dac_max"])
    rate_tol_ratio = float(proc_cfg["rate_tol_ratio"])
    rate_stable_sec = float(proc_cfg["rate_stable_sec"])
    hold_control_interval_s = float(proc_cfg["hold_control_interval_s"])
    fine_step_dac = int(proc_cfg["fine_step_dac"])
    rate_abort_ratio = float(proc_cfg["rate_abort_ratio"])
    rate_abort_sec = float(proc_cfg["rate_abort_sec"])
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
            engine._check_stop_pause(recipe, step)
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
            engine._check_stop_pause(recipe, step)
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
            engine._check_stop_pause(recipe, step)
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
            engine._check_stop_pause(recipe, step)
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
        if abs(rt - target_rate) <= tol:
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

        engine._safe_shutdown_sequence(tag=tag)

        shutdown_done = True
        setattr(engine, "_shutdown_already_executed", True)

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
                engine._check_stop_pause(recipe, step)
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
                    dac = min(dac_max, dac + step_dac_step)
                    _evap_apply_dac(engine, use_p1, use_p2, dac, tag=f"EVAP_RAMP_STEP{step_no}")
                    last_dac_apply_m = now_m
                    remain_to_next_dac_s = step_dac_interval_sec

                if adc_total >= step_target_adc:
                    engine._emit_status(
                        message=(
                            f"RAMP STEP {step_no}/{len(ramp_steps)} ADC 확인중 | "
                            f"ADC={adc_total:.1f}/{step_target_adc:.1f} | "
                            f"DAC={dac} | rate={rt:.3f} | "
                            f"확인 {adc_confirm_elapsed_s:.1f}/{step_hold_sec:.1f}s | "
                            f"다음 DAC까지 {remain_to_next_dac_s:.1f}s"
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
        engine._emit_status(message="EVAP HOLD 진입: STM ZERO + MAIN SHUTTER OPEN", force=True)

        zero_mode = str(meta.get("zero_mode", "B") or "B")
        _stm_zero_thickness(engine, recipe, step, mode=zero_mode)
        th0 = _read_thickness_or_abort(where="hold_thickness_baseline")

        engine._plc_write_coil("MAIN_SHUTTER_SW", True, tag="EVAP_MAIN_SHUTTER_OPEN")
        shutter_open = True

        last_hold_control_m = 0.0
        rate_abort_start_ts: Optional[float] = None
        abort_threshold = target_rate * rate_abort_ratio

        while True:
            engine._check_stop_pause(recipe, step)
            engine._tick_emit(recipe, step)

            rt = _read_rate_or_abort(where="hold")
            adc_total = _read_adc_or_abort(where="hold")
            th = _read_thickness_or_abort(where="hold")

            dep_th = max(0.0, th - th0)
            remain_th = max(0.0, target_th - dep_th)

            # abort 조건: dep.rate 장시간 과도 저하
            if rt <= abort_threshold:
                now_m = time.monotonic()
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
                new_dac = _evap_adjust_dac(
                    dac,
                    rt,
                    target_rate,
                    tol_ratio=rate_tol_ratio,
                    fine_step_dac=fine_step_dac,
                    dac_max=dac_max,
                )
                if new_dac != dac:
                    dac = int(new_dac)
                    _evap_apply_dac(engine, use_p1, use_p2, dac, tag="EVAP_HOLD_CONTROL")
                last_hold_control_m = now_m

            engine._emit_status(
                message=(
                    f"HOLD | remain {remain_th:.1f}Å "
                    f"( {dep_th:.1f}/{target_th:.1f}Å ) | "
                    f"rate={rt:.3f} | ADC={adc_total:.1f} | DAC={dac}"
                ),
                force=True,
            )

            if dep_th >= target_th:
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