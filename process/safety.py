# -*- coding: utf-8 -*-
"""
process/safety.py

현재 engine.py의 legacy 안전정지 시퀀스를 ProcessStep 리스트로 분리한 모듈.

목표
- engine.py 안의 하드코딩 안전정지 순서를 safety.py로 이동
- 기존 engine 동작과 "완전히 동일한 순서/동작" 유지
- 나중에 safety plan을 바꾸고 싶을 때 engine.py가 아니라 이 파일만 수정

현재 legacy 엔진 시퀀스(기존과 동일):
1) MAIN_SHUTTER_SW  -> OFF
2) DAC_POWER_1      -> 0
3) DAC_POWER_2      -> 0
4) SHUTTER_1_SW     -> OFF
5) SHUTTER_2_SW     -> OFF
6) POWER_1_SW       -> OFF
7) POWER_2_SW       -> OFF
8) FTM_SW           -> OFF

주의
- 본 파일의 default plan은 의도적으로 STOP/ABORT를 동일하게 둔다.
- 이유: 현재 engine.py는 STOP / ERROR / EVAP_DONE 모두 같은 하드코딩 시퀀스를 사용하고 있기 때문.
- 즉, "기존과 완전히 동일"이 이번 목표다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from process.models import (
    ProcessStep,
    ProcessRecipe,
    StepType,
    StopMode,
)


# ============================================================
# Data model
# ============================================================

@dataclass
class SafetyPlan:
    stop_steps: List[ProcessStep] = field(default_factory=list)
    abort_steps: List[ProcessStep] = field(default_factory=list)
    estop_steps: List[ProcessStep] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def validate(self, strict: bool = True) -> None:
        for group_name, steps in (
            ("STOP", self.stop_steps),
            ("ABORT", self.abort_steps),
            ("ESTOP", self.estop_steps),
        ):
            for i, st in enumerate(steps):
                if not isinstance(st, ProcessStep):
                    raise ValueError(
                        f"[SafetyPlan] {group_name} steps[{i}] is not ProcessStep: {type(st)}"
                    )
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
# Legacy engine sequence
# ============================================================

def _legacy_engine_shutdown_steps(*, prefix: str) -> List[ProcessStep]:
    """
    기존 engine.py 하드코딩 안전정지 순서를 그대로 ProcessStep으로 생성.
    순서가 가장 중요하므로 절대 바꾸지 않는다.
    """
    return [
        ProcessStep(
            name=f"{prefix}_MAIN_SHUTTER_CLOSE",
            type=StepType.PLC_WRITE_COIL,
            coil="MAIN_SHUTTER_SW",
            on=False,
            meta={"reason": "legacy_engine_safety"},
        ),
        ProcessStep(
            name=f"{prefix}_DAC1_0",
            type=StepType.PLC_WRITE_REG,
            reg="DAC_POWER_1",
            value=0,
            meta={"reason": "legacy_engine_safety"},
        ),
        ProcessStep(
            name=f"{prefix}_DAC2_0",
            type=StepType.PLC_WRITE_REG,
            reg="DAC_POWER_2",
            value=0,
            meta={"reason": "legacy_engine_safety"},
        ),
        ProcessStep(
            name=f"{prefix}_SHUTTER_1_CLOSE",
            type=StepType.PLC_WRITE_COIL,
            coil="SHUTTER_1_SW",
            on=False,
            meta={"reason": "legacy_engine_safety"},
        ),
        ProcessStep(
            name=f"{prefix}_SHUTTER_2_CLOSE",
            type=StepType.PLC_WRITE_COIL,
            coil="SHUTTER_2_SW",
            on=False,
            meta={"reason": "legacy_engine_safety"},
        ),
        ProcessStep(
            name=f"{prefix}_PWR1_OFF",
            type=StepType.PLC_WRITE_COIL,
            coil="POWER_1_SW",
            on=False,
            meta={"reason": "legacy_engine_safety"},
        ),
        ProcessStep(
            name=f"{prefix}_PWR2_OFF",
            type=StepType.PLC_WRITE_COIL,
            coil="POWER_2_SW",
            on=False,
            meta={"reason": "legacy_engine_safety"},
        ),
        ProcessStep(
            name=f"{prefix}_FTM_OFF",
            type=StepType.PLC_WRITE_COIL,
            coil="FTM_SW",
            on=False,
            meta={"reason": "legacy_engine_safety"},
        ),
    ]


def default_safety_plan() -> SafetyPlan:
    """
    현재 engine.py의 legacy 동작을 그대로 보존하는 기본 safety plan.

    - STOP  : legacy engine과 동일
    - ABORT : legacy engine과 동일
    - ESTOP : 엔진 강제동작 최소(로그만)
    """
    stop_steps = _legacy_engine_shutdown_steps(prefix="STOP")
    abort_steps = _legacy_engine_shutdown_steps(prefix="ABORT")

    estop_steps = [
        ProcessStep(
            name="ESTOP_LOG_ONLY",
            type=StepType.LOG,
            message="[SAFETY] ESTOP requested: engine does not force actions (hardware/PLC interlock first)",
            meta={"reason": "legacy_engine_safety"},
        )
    ]

    plan = SafetyPlan(
        stop_steps=stop_steps,
        abort_steps=abort_steps,
        estop_steps=estop_steps,
        meta={
            "version": 1,
            "mode": "legacy_engine_exact",
            "note": "STOP/ABORT follow current engine.py hardcoded shutdown sequence exactly",
        },
    )
    return plan


# ============================================================
# Public builders
# ============================================================

def build_engine_safe_shutdown_steps() -> List[ProcessStep]:
    """
    현재 engine.py가 쓰던 legacy 안전정지 순서를 그대로 반환.
    - STOP / ERROR / EVAP_DONE 모두 동일 시퀀스를 쓰던 기존 구조 보존용
    """
    return _legacy_engine_shutdown_steps(prefix="LEGACY")


def build_safety_steps(mode: StopMode, plan: Optional[SafetyPlan] = None) -> List[ProcessStep]:
    """
    StopMode에 따라 실행할 안전정지 step 리스트 반환.
    현재 default plan은 STOP/ABORT가 legacy engine과 동일하다.
    """
    plan = plan or default_safety_plan()

    if mode == StopMode.STOP:
        return list(plan.stop_steps)
    if mode == StopMode.ABORT:
        return list(plan.abort_steps)
    return list(plan.estop_steps)


def build_safety_recipe(
    mode: StopMode,
    *,
    plan: Optional[SafetyPlan] = None,
    recipe_name_prefix: str = "safety",
) -> ProcessRecipe:
    steps = build_safety_steps(mode, plan=plan)
    name = f"{recipe_name_prefix}_{mode.value.lower()}"
    r = ProcessRecipe(
        recipe_name=name,
        steps=steps,
        default_poll_s=0.2,
        telemetry_interval_s=1.0,
        operator="",
        note=f"safety recipe for {mode.value}",
        meta={"mode": mode.value},
    )
    r.validate(strict=True)
    return r


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
    plan.validate(strict=strict)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")