# -*- coding: utf-8 -*-
"""
process/evap_runtime.py

EVAP 전용 증착 runtime 로직.
- 기존 process/engine.py 안에 있던 EVAP 관련 함수들을 그대로 분리
- engine 인스턴스를 받아서 engine의 공용 helper를 그대로 사용함
- 즉, 로직 변경 없이 파일만 분리하는 목적

포함 함수:
- run_evap_deposition_control(engine, recipe, step)
- _stm_wait_connected(engine, ...)
- _stm_apply_material_params(engine, ...)
- _stm_zero_thickness(engine, ...)
- _evap_apply_dac(engine, ...)
- _evap_adjust_dac(...)
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


# --------------------------------------------------------
# EVAP deposition control
# --------------------------------------------------------
def run_evap_deposition_control(engine, recipe: ProcessRecipe, step: ProcessStep) -> None:
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

    def _meta_f(key: str, default: float) -> float:
        v = meta.get(key, None)
        return float(default) if v is None else float(v)

    def _meta_i(key: str, default: int) -> int:
        v = meta.get(key, None)
        return int(default) if v is None else int(float(v))

    use_p1 = bool(meta.get("use_power1", False))
    use_p2 = bool(meta.get("use_power2", False))

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

    # --- 공정 파라미터(기본값은 요구사항 기준) ---
    pre_rate = float(meta.get("pre_rate", 0.4) or 0.4)    # Å/s
    pre_hold_s = _meta_f("pre_hold_s", 120.0)             # s

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

    # ✅ pre_hold 중 dep.rate 급감 감지
    # 예: pre_rate=0.4, pre_drop_ratio=0.5 이면 0.2 미만이 연속 발생할 때 중단
    pre_drop_ratio = float(meta.get("pre_drop_ratio", 0.50) or 0.50)
    pre_drop_count = int(meta.get("pre_drop_count", 3) or 3)

    ramp_step_dac = int(meta.get("ramp_step_dac", 100) or 100)

    # ✅ 사용자 요구: DAC 구간별 램프 템포
    ramp_seg1_max_dac = int(meta.get("ramp_seg1_max_dac", 700) or 700)
    ramp_seg2_max_dac = int(meta.get("ramp_seg2_max_dac", 2000) or 2000)
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
    stuck_dac_guard = int(meta.get("stuck_dac_guard", 2000) or 2000)  # 이 이상 DAC인데도
    stuck_rate_abs = float(meta.get("stuck_rate_abs", 0.05) or 0.05)  # rate가 이 값 미만이면
    stuck_time_s = float(meta.get("stuck_time_s", 60.0) or 60.0)      # 이 시간 지속 시 중단

    # ✅ material shortage guard (요구사항)
    # - DAC가 2000 이상인데도 dep.rate가 0 이하이면 "물질 부족"으로 중단
    material_shortage_dac = int(meta.get("material_shortage_dac", 2000) or 2000)
    material_shortage_rate_max = float(meta.get("material_shortage_rate_max", 0.0) or 0.0)  # <=
    # "계속"을 엄밀히 하려면 지속시간을 쓰면 됨(초). 10이면 10초 대기 후 중단.
    material_shortage_time_s = _meta_f("material_shortage_time_s", 10.0)  # ✅ 0이면 즉시 중단 로직이 살아남

    # delay는 분 단위 입력(기존 UI) → 초로 변환
    delay_s = delay_min * 60.0

    if ramp_step_dac <= 0:
        _raise_engine_failed(step.name, "EVAP: ramp_step_dac must be > 0")
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

            # ✅ sleep 중에도 rate를 읽어 material shortage 감시(10초 카운트가 정확해짐)
            rt = engine._get_rate()
            if rt is not None:
                shortage_start_ts = _material_shortage_guard(float(rt), shortage_start_ts, where=where)

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
                engine._emit_status(
                    message=(
                        f"[남은 {engine._fmt_hms(remain_s, ceil=True)}] IGNITE WAIT | "
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
                engine._emit_status(message=f"IGNITE OK: rate={rtf:.3f} (hits={ignite_stable_hits})", force=True)
                return False

            if ignite_wait_start_m is not None and (now_m - float(ignite_wait_start_m)) >= float(ignite_timeout_s):
                _raise_engine_failed(step.name, f"IGNITE WAIT TIMEOUT: DAC={dac}, rate={rtf:.3f} < {ignite_rate_min:.3f}")

            return True

        # ✅ 대기 시작 조건(요구사항: rate==0일 때)
        if rtf <= float(ignite_trigger_rate_max):
            ignite_wait_active = True
            ignite_wait_start_m = time.monotonic()
            ignite_ok_hits = 0
            ignite_next_ui_m = ignite_wait_start_m  # 시작 즉시 표시되게
            engine._emit_status(
                message=(
                    f"[남은 {engine._fmt_hms(float(ignite_timeout_s), ceil=True)}] IGNITE WAIT START | "
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

    def read_rate_or_abort(*, where: str) -> float:
        nonlocal shortage_start_ts
        t0 = time.monotonic()
        while True:
            engine._check_stop_pause(recipe, step)
            engine._tick_emit(recipe, step)

            rt = engine._get_rate()
            if rt is not None:
                rt_f = float(rt)

                # ✅ 모든 공정 구간에서 material shortage 감시
                shortage_start_ts = _material_shortage_guard(rt_f, shortage_start_ts, where=where)

                return rt_f

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

    last_dac_apply_m: float = 0.0
    pending_dac: Optional[int] = None

    def apply_dac() -> None:
        """즉시 DAC 적용(램프업 등에서 사용) + 마지막 적용 시각 갱신."""
        nonlocal last_dac_apply_m, pending_dac
        _evap_apply_dac(engine, use_p1, use_p2, dac, tag="EVAP_DAC")
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
    #   - 700~2000 : 30초에 +100
    #   - 2000에서 rate=0이면 rate>=0.1 될 때까지 대기(최대 5분)
    engine._emit_status(message=f"EVAP ramp-up 시작: pre_rate={pre_rate} Å/s")
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
            engine._emit_status(
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

        # ✅ [추가] ignite_dac(2000 기본)에서 rate=0이면 0.1 될 때까지 대기(최대 5분)
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
                _raise_engine_failed(
                    step.name,
                    f"EVAP: rate 상승 없음(stuck/pre) rt={rt:.3f} < {stuck_rate_abs:.3f} "
                    f"@ dac={dac} (>= {stuck_dac_guard}) for {stuck_time_s:.0f}s"
                )
        else:
            stuck_start_ts_pre = None

        if (now - t_ramp0) > ramp_timeout_s:
            _raise_engine_failed(step.name, f"EVAP: pre_rate ramp timeout {ramp_timeout_s}s (rt={rt:.3f})")

        if dac >= dac_max:
            _raise_engine_failed(step.name, f"EVAP: DAC_MAX({dac_max}) 도달했지만 pre_rate 미도달 (rt={rt:.3f})")

        # ✅ 램프업(+100) 후 구간별 템포 sleep
        dac = min(dac_max, dac + ramp_step_dac)
        apply_dac()
        engine._emit_status(
            message=f"POWER RAMP UP (PRE) / DAC={dac} / rate={rt:.3f}/{pre_rate:.3f} Å/s",
            force=True,
        )
        _ramp_sleep_by_dac(dac, where="pre_ramp_sleep")

    # 4) pre_rate 유지(1~2분)
    # - 요청사항: pre_rate(0.4) 도달 후 1~2분 "대기" → 그 다음 target로 ramp
    if pre_hold_s > 0:
        engine._emit_status(message=f"EVAP pre_rate 유지: {pre_hold_s:.0f}s (mode={pre_hold_mode})")
        end_m = time.monotonic() + float(pre_hold_s)
        next_ui_m = time.monotonic()

        # ✅ pre_hold 중 급감 연속 감지
        pre_drop_hits = 0
        pre_drop_threshold = float(pre_rate) * float(pre_drop_ratio)

        while True:
            now_m = time.monotonic()
            remain_s = end_m - now_m
            if remain_s <= 0:
                break

            rt = read_rate_or_abort(where="pre_hold")

            # ✅ pre_hold 중 dep.rate 급감 감지
            if rt < pre_drop_threshold:
                pre_drop_hits += 1
                if pre_drop_hits >= max(1, int(pre_drop_count)):
                    _raise_engine_failed(
                        step.name,
                        f"EVAP: pre_hold 중 dep.rate 급감 감지 "
                        f"(rt={rt:.3f} < {pre_drop_threshold:.3f}, hits={pre_drop_hits}/{pre_drop_count})"
                    )
            else:
                pre_drop_hits = 0

            # control 모드만 pre_rate 근처 유지 제어 (단, DAC 변경 텀 적용)
            if pre_hold_mode == "control":
                new_dac = _evap_adjust_dac(
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
                engine._emit_status(
                    message=(
                        f"EVAP PRE 안정화 대기({pre_hold_mode}) | 남은시간 {engine._fmt_hms(remain_s, ceil=True)} | "
                        f"DAC={dac} | rate {rt:.3f}/{pre_rate:.3f} Å/s"
                    ),
                    force=True,
                )
                next_ui_m = now_m + 1.0

            _sleep_with_checks(min(0.5, max(0.0, remain_s)))

    # 5) target_rate까지 ramp-up (일단은 계속 +100)
    engine._emit_status(message=f"EVAP target_rate ramp-up: target_rate={target_rate} Å/s")
    t_ramp1 = time.monotonic()

    stuck_start_ts_target: Optional[float] = None

    # ✅ target_ramp 탈출도 "연속 N회" 확인 후 통과(기본 3회)
    target_ramp_ok_hits: int = 0
    target_ramp_threshold = float(target_rate) * (1.0 - float(rate_tol_ratio))

    while True:
        rt = read_rate_or_abort(where="target_ramp")

        if rt >= target_ramp_threshold:
            target_ramp_ok_hits += 1
            engine._emit_status(
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
                _raise_engine_failed(
                    step.name,
                    f"EVAP: rate 상승 없음(stuck/target) rt={rt:.3f} < {stuck_rate_abs:.3f} "
                    f"@ dac={dac} (>= {stuck_dac_guard}) for {stuck_time_s:.0f}s"
                )
        else:
            stuck_start_ts_target = None

        if (now - t_ramp1) > ramp_timeout_s:
            _raise_engine_failed(step.name, f"EVAP: target_rate ramp timeout {ramp_timeout_s}s (rt={rt:.3f})")

        if dac >= dac_max:
            _raise_engine_failed(step.name, f"EVAP: DAC_MAX({dac_max}) 도달했지만 target_rate 미도달 (rt={rt:.3f})")

        dac = min(dac_max, dac + ramp_step_dac)
        apply_dac()
        engine._emit_status(
            message=f"POWER RAMP UP (TARGET) / DAC={dac} / rate={rt:.3f}/{target_rate:.3f} Å/s",
            force=True,
        )
        _ramp_sleep_by_dac(dac, where="target_ramp_sleep")

    # 6) target_rate ±5% band 안으로 fine tune
    engine._emit_status(message=f"EVAP fine tune: tol=±{rate_tol_ratio*100:.1f}% / stable_hits={target_stable_hits}")
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
            _raise_engine_failed(step.name, f"EVAP: target_rate fine tune timeout {tune_timeout_s}s (rt={rt:.3f})")

        new_dac = _evap_adjust_dac(
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
        engine._emit_status(message=f"SHUTTER DELAY / 0.00/{total_min:.2f} min", force=True)

        while True:
            rt = read_rate_or_abort(where="shutter_delay")

            # rate 급감 감지(셔터 열기 전)
            if rt < target_rate * rate_drop_ratio:
                _raise_engine_failed(
                    step.name,
                    f"EVAP: dep.rate 급감(셔터 전) rt={rt:.3f} < {target_rate*rate_drop_ratio:.3f}"
                )

            new_dac = _evap_adjust_dac(
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
                engine._emit_status(
                    message=f"SHUTTER DELAY / {elapsed_min:.2f}/{total_min:.2f} min (remain {remain_min:.2f}) / DAC={dac}",
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

    # 9) 증착 루프
    drop_hits = 0
    main_drop_threshold = float(target_rate) * float(rate_drop_ratio)

    while True:
        engine._check_stop_pause(recipe, step)
        engine._tick_emit(recipe, step)

        rt = read_rate_or_abort(where="main_process")
        th = read_th_or_abort()

        dep_th = th - th0
        if dep_th < 0:
            dep_th = 0.0

        # ✅ 급감 감지는 첫 샘플 baseline이 아니라 target_rate 기준으로 통일
        if rt < main_drop_threshold:
            drop_hits += 1
            if drop_hits >= max(1, int(rate_drop_count)):
                _raise_engine_failed(
                    step.name,
                    f"EVAP: dep.rate 급감 감지 → 중단 "
                    f"(rt={rt:.3f} < {main_drop_threshold:.3f}, hits={drop_hits}/{rate_drop_count})"
                )
        else:
            drop_hits = 0

        # rate 유지
        new_dac = _evap_adjust_dac(
            dac=dac,
            rate=rt,
            target_rate=target_rate,
            tol_ratio=rate_tol_ratio,
            step_up=fine_step_dac,
            step_dn=fine_step_dac,
            dac_max=dac_max,
        )
        maybe_apply_dac(new_dac, min_interval_s=_dac_min_interval_by_dac(max(int(dac), int(new_dac))))

        remain_th = max(0.0, float(target_th) - float(dep_th))
        engine._emit_status(
            message=f"MAIN PROCESSING / remain {remain_th:.1f}Å ( {dep_th:.1f}/{target_th:.1f}Å ) / DAC={dac}",
        )

        if dep_th >= target_th:
            break

        _sleep_with_checks(0.5)

    # 10) 종료
    engine._emit_status(message="EVAP 완료: shutter close + power off")
    engine._safe_shutdown_sequence(tag="EVAP_DONE")
    engine._emit_status(message="EVAP 완료")