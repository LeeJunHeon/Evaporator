# -*- coding: utf-8 -*-
"""
process/safety.py

engine 안전정지 정책을 ProcessStep 리스트로 정의하는 모듈.

목표
- 종료 정책/순서를 safety.py 한 곳에서 관리
- engine.py는 safety plan의 실행만 담당
- DAC 종료는 즉시 0이 아니라 ramp-down marker로 정의
- STOP / ABORT / ESTOP 정책을 분리 가능하게 유지

기본 정책:
1) MAIN_SHUTTER close
2) SHUTTER_1 / SHUTTER_2 close
3) DAC pair ramp-down marker
4) POWER_1 / POWER_2 off
5) FTM off
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
# Engine shutdown step builders
# ============================================================

def _build_engine_shutdown_steps(*, prefix: str) -> List[ProcessStep]:
    """
    engine 안전정지 기본 순서를 ProcessStep으로 생성.

    현재 공통 정책:
    1) MAIN_SHUTTER close
    2) SHUTTER_1/2 close
    3) DAC pair ramp-down (dynamic action marker)
    4) POWER_1/2 off
    5) FTM off

    주의:
    - DAC ramp-down은 현재 DAC 값을 알아야 하므로
      여기서는 marker step만 정의하고,
      실제 실행은 engine.py가 담당한다.
    """
    return [
        ProcessStep(
            name=f"{prefix}_MAIN_SHUTTER_CLOSE",
            type=StepType.PLC_WRITE_COIL,
            coil="MAIN_SHUTTER_SW",
            on=False,
            meta={"reason": "engine_safety_v2"},
        ),
        ProcessStep(
            name=f"{prefix}_SHUTTER_1_CLOSE",
            type=StepType.PLC_WRITE_COIL,
            coil="SHUTTER_1_SW",
            on=False,
            meta={"reason": "engine_safety_v2"},
        ),
        ProcessStep(
            name=f"{prefix}_SHUTTER_2_CLOSE",
            type=StepType.PLC_WRITE_COIL,
            coil="SHUTTER_2_SW",
            on=False,
            meta={"reason": "engine_safety_v2"},
        ),
        ProcessStep(
            name=f"{prefix}_RAMP_DOWN_DAC_PAIR",
            type=StepType.LOG,
            message="[SAFETY] DAC pair ramp-down",
            meta={
                "reason": "engine_safety_v2",
                "action": "ramp_down_dac_pair",
                "step_dac": 100,
                "interval_s": 1.0,
            },
        ),
        ProcessStep(
            name=f"{prefix}_PWR1_OFF",
            type=StepType.PLC_WRITE_COIL,
            coil="POWER_1_SW",
            on=False,
            meta={"reason": "engine_safety_v2"},
        ),
        ProcessStep(
            name=f"{prefix}_PWR2_OFF",
            type=StepType.PLC_WRITE_COIL,
            coil="POWER_2_SW",
            on=False,
            meta={"reason": "engine_safety_v2"},
        ),
        ProcessStep(
            name=f"{prefix}_FTM_OFF",
            type=StepType.PLC_WRITE_COIL,
            coil="FTM_SW",
            on=False,
            meta={"reason": "engine_safety_v2"},
        ),
    ]


def default_safety_plan() -> SafetyPlan:
    """
    engine 기본 safety plan.

    - STOP  : shutter close -> DAC pair ramp-down -> power off
    - ABORT : shutter close -> DAC pair ramp-down -> power off
    - ESTOP : 엔진 강제동작 최소(로그만)
    """
    stop_steps = _build_engine_shutdown_steps(prefix="STOP")
    abort_steps = _build_engine_shutdown_steps(prefix="ABORT")

    estop_steps = [
        ProcessStep(
            name="ESTOP_LOG_ONLY",
            type=StepType.LOG,
            message="[SAFETY] ESTOP requested: engine does not force actions (hardware/PLC interlock first)",
            meta={"reason": "engine_safety_v2"},
        )
    ]

    plan = SafetyPlan(
        stop_steps=stop_steps,
        abort_steps=abort_steps,
        estop_steps=estop_steps,
        meta={
            "version": 2,
            "mode": "engine_safety_rampdown_v1",
            "note": "STOP/ABORT use safety plan with DAC pair ramp-down marker before power-off",
        },
    )
    return plan


# ============================================================
# Public builders
# ============================================================

def build_engine_safe_shutdown_steps() -> List[ProcessStep]:
    """
    engine 공통 안전정지 step 리스트를 반환.
    - EVAP_DONE / generic shutdown 에서 공통으로 사용할 수 있는 기본 안전정지 정책
    - DAC 종료는 marker step으로 정의되며 실제 실행은 engine.py가 담당
    """
    return _build_engine_shutdown_steps(prefix="ENGINE")


def build_safety_steps(mode: StopMode, plan: Optional[SafetyPlan] = None) -> List[ProcessStep]:
    """
    StopMode에 따라 실행할 안전정지 step 리스트 반환.

    현재 기본 정책:
    - STOP  : 공통 shutdown step 사용
    - ABORT : 현재는 STOP과 동일한 shutdown step 사용
    - ESTOP : 로그만 남김 (hardware/PLC interlock 우선)

    즉, 현재 구현에서는 STOP/ABORT의 실제 step 순서는 동일하고,
    향후 mode별 정책 분리가 필요하면 SafetyPlan에서 각 그룹을 다르게 정의하면 된다.
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