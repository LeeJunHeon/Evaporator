# -*- coding: utf-8 -*-
"""
process/safety.py

안전정지(Safety) 시퀀스를 "ProcessStep 리스트"로 생성하는 모듈.

설계 의도
- 장비마다 안전정지(Stop/Abort/EStop) 시퀀스가 다르므로,
  엔진에서 하드코딩하지 않고, 별도 모듈에서 "스텝(plan)"을 생성하도록 분리한다.
- 엔진은 생성된 steps를 그대로 실행(PLC_WRITE_COIL / WAIT_SECONDS / LOG)하면 된다.

기본 제공
- 기본 SafetyPlan (매우 보수적, 최소한만 OFF)
  * STOP : GAS OFF -> 짧게 대기 -> POWER OFF
  * ABORT: GAS OFF + POWER OFF (딜레이 최소)
  * ESTOP: 엔진이 관여하지 않고 LOG만 남김(하드웨어/PLC 인터락 우선)
- 사용자 커스텀 plan을 JSON으로 로드/저장 가능
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from process.models import (
    ProcessStep,
    ProcessRecipe,
    StepType,
    StopMode,
    OnTimeout,
    KNOWN_COILS,
)


# ============================================================
# Data models
# ============================================================

@dataclass
class SafetyPlan:
    """
    StopMode별로 실행할 ProcessStep 리스트를 보관하는 계획.

    - stop_steps  : 정상 정지(안전 시퀀스)
    - abort_steps : 중단/에러 시 빠른 안전화
    - estop_steps : 비상정지(보통 하드웨어/PLC에서 처리 → 엔진은 최소 개입)
    """
    stop_steps: List[ProcessStep] = field(default_factory=list)
    abort_steps: List[ProcessStep] = field(default_factory=list)
    estop_steps: List[ProcessStep] = field(default_factory=list)

    meta: Dict[str, Any] = field(default_factory=dict)

    def validate(self, strict: bool = True) -> None:
        """
        plan 내부 step 검증.
        - strict=True면 coil 이름이 KNOWN_COILS에 있는지 체크
        """
        for group_name, steps in (
            ("STOP", self.stop_steps),
            ("ABORT", self.abort_steps),
            ("ESTOP", self.estop_steps),
        ):
            for i, st in enumerate(steps):
                if not isinstance(st, ProcessStep):
                    raise ValueError(f"[SafetyPlan] {group_name} steps[{i}] is not ProcessStep: {type(st)}")
                # safety step는 coil 오타가 치명적이므로 기본적으로 strict 권장
                st.validate(idx=i, strict=strict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "meta": dict(self.meta or {}),
            "stop_steps": [s.to_dict() for s in self.stop_steps],
            "abort_steps": [s.to_dict() for s in self.abort_steps],
            "estop_steps": [s.to_dict() for s in self.estop_steps],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SafetyPlan":
        if not isinstance(d, dict):
            raise ValueError("SafetyPlan must be dict")

        def _steps(key: str) -> List[ProcessStep]:
            raw = d.get(key, [])
            if raw is None:
                return []
            if not isinstance(raw, list):
                raise ValueError(f"{key} must be list")
            return [ProcessStep.from_dict(x) for x in raw]

        return SafetyPlan(
            stop_steps=_steps("stop_steps"),
            abort_steps=_steps("abort_steps"),
            estop_steps=_steps("estop_steps"),
            meta=dict(d.get("meta") or {}),
        )


# ============================================================
# Default plan (매우 보수적)
# ============================================================

def default_safety_plan() -> SafetyPlan:
    """
    기본 안전정지 plan.
    - 확실히 안전하다고 단정하기 어려운(밸브/펌프/door/vent 등) 동작은 기본에서 제외.
    - 최소한의 위험 요소(가스/파워)를 끄는 방향으로만 구성.
    """
    # 기본 OFF 대상 (네 프로젝트 KNOWN_COILS에 존재하는 이름만)
    gas_off = ["GAS_1_SW", "GAS_2_SW"]
    power_off = ["POWER_1_SW", "POWER_2_SW"]

    stop_steps: List[ProcessStep] = []
    abort_steps: List[ProcessStep] = []
    estop_steps: List[ProcessStep] = []

    # STOP: GAS OFF -> (0.2s) -> POWER OFF
    stop_steps += _make_log("SAFETY_STOP_BEGIN", "SAFETY", "STOP: GAS OFF -> POWER OFF")
    stop_steps += _make_off_steps(gas_off, prefix="STOP_GAS_OFF")
    stop_steps += [_make_wait("STOP_WAIT_0P2", 0.2)]
    stop_steps += _make_off_steps(power_off, prefix="STOP_POWER_OFF")
    stop_steps += _make_log("SAFETY_STOP_END", "SAFETY", "STOP safety sequence finished")

    # ABORT: GAS OFF + POWER OFF (빠르게)
    abort_steps += _make_log("SAFETY_ABORT_BEGIN", "SAFETY", "ABORT: GAS OFF + POWER OFF (fast)")
    abort_steps += _make_off_steps(gas_off, prefix="ABORT_GAS_OFF")
    abort_steps += _make_off_steps(power_off, prefix="ABORT_POWER_OFF")
    abort_steps += _make_log("SAFETY_ABORT_END", "SAFETY", "ABORT safety sequence finished")

    # ESTOP: 엔진 개입 최소 (하드웨어/PLC 인터락 우선)
    estop_steps += _make_log("SAFETY_ESTOP", "SAFETY", "ESTOP requested: engine does not force actions (hardware/PLC interlock first)")

    plan = SafetyPlan(
        stop_steps=stop_steps,
        abort_steps=abort_steps,
        estop_steps=estop_steps,
        meta={"version": 1, "note": "default conservative plan: only gas/power off"},
    )
    return plan


# ============================================================
# Build helpers
# ============================================================

def build_safety_steps(mode: StopMode, plan: Optional[SafetyPlan] = None) -> List[ProcessStep]:
    """
    StopMode에 따라 실행할 안전정지 step 리스트 반환.

    - mode == STOP  -> plan.stop_steps
    - mode == ABORT -> plan.abort_steps
    - mode == ESTOP -> plan.estop_steps
    """
    plan = plan or default_safety_plan()

    if mode == StopMode.STOP:
        return list(plan.stop_steps)
    if mode == StopMode.ABORT:
        return list(plan.abort_steps)
    # ESTOP 포함 기타는 estop_steps로
    return list(plan.estop_steps)


def build_safety_recipe(
    mode: StopMode,
    *,
    plan: Optional[SafetyPlan] = None,
    recipe_name_prefix: str = "safety",
) -> ProcessRecipe:
    """
    안전정지 시퀀스를 ProcessRecipe로 만들어서 반환.
    엔진에서 "recipe 실행" 기능으로 돌리고 싶을 때 유용.
    """
    steps = build_safety_steps(mode, plan=plan)
    name = f"{recipe_name_prefix}_{mode.value.lower()}"
    r = ProcessRecipe(
        recipe_name=name,
        steps=steps,
        default_poll_s=0.2,
        telemetry_interval_s=0.5,
        operator="",
        note=f"safety recipe for {mode.value}",
        meta={"mode": mode.value},
    )
    # 안전레시피는 오타가 치명적 → strict 검증 권장
    r.validate(strict=True)
    return r


def sanitize_plan(plan: SafetyPlan) -> SafetyPlan:
    """
    plan에 들어있는 coil 이름 중 KNOWN_COILS에 없는 항목을 제거하여 반환.
    - 운영에서 "일단 돌게" 만들 때 쓰는 함수
    - 그러나 안전정지는 오타를 조용히 무시하는게 더 위험할 수 있으니,
      기본은 validate(strict=True)로 잡아내는 것을 권장.
    """
    def _filter_steps(steps: List[ProcessStep]) -> List[ProcessStep]:
        out: List[ProcessStep] = []
        for st in steps:
            if st.type in (StepType.PLC_WRITE_COIL, StepType.PLC_PULSE_COIL, StepType.WAIT_COIL_IS):
                c = (st.coil or "").strip()
                if c and c not in KNOWN_COILS:
                    continue
            out.append(st)
        return out

    return SafetyPlan(
        stop_steps=_filter_steps(plan.stop_steps),
        abort_steps=_filter_steps(plan.abort_steps),
        estop_steps=_filter_steps(plan.estop_steps),
        meta=dict(plan.meta or {}),
    )


# ============================================================
# JSON I/O
# ============================================================

def load_safety_plan_json(path: str | Path, *, strict: bool = True) -> SafetyPlan:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"safety plan not found: {p}")

    data = json.loads(p.read_text(encoding="utf-8"))
    plan = SafetyPlan.from_dict(data)
    plan.validate(strict=strict)
    return plan


def save_safety_plan_json(path: str | Path, plan: SafetyPlan, *, strict: bool = True) -> None:
    p = Path(path)
    # 저장 전 검증(오타 방지)
    plan.validate(strict=strict)

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# Internal step builders
# ============================================================

def _make_log(name: str, tag: str, message: str) -> List[ProcessStep]:
    return [
        ProcessStep(
            name=name,
            type=StepType.LOG,
            message=f"[{tag}] {message}",
            meta={},
        )
    ]


def _make_wait(name: str, seconds: float) -> ProcessStep:
    return ProcessStep(
        name=name,
        type=StepType.WAIT_SECONDS,
        seconds=float(seconds),
        meta={},
    )


def _make_off_steps(coils: Sequence[str], *, prefix: str) -> List[ProcessStep]:
    """
    coil OFF 스텝 생성.
    - 존재하지 않는 coil 이름이면 validate(strict=True)에서 잡히도록 그대로 둔다.
    """
    out: List[ProcessStep] = []
    for i, c in enumerate(coils):
        out.append(
            ProcessStep(
                name=f"{prefix}_{i+1:02d}",
                type=StepType.PLC_WRITE_COIL,
                coil=str(c),
                on=False,
                meta={"reason": "safety_off"},
            )
        )
    return out
