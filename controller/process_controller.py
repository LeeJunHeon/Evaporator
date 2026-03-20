# -*- coding: utf-8 -*-
"""
controller/process_controller.py

ProcessController
- UI에서 공정 시작/정지/일시정지/재개/레시피 로드/저장을 담당하는 상위 컨트롤러
- ProcessEngine은 QThread(=ProcessWorker)에서 실행

주의
- UI 위젯을 직접 접근하지 않는다(=signals로만 상태 전달)
- PLC/센서 서비스는 외부에서 주입(inject)받는 형태를 기본으로 한다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union, Any

from PySide6.QtCore import QObject, Signal


# ------------------------------------------------------------
# Import (패키지 실행만 지원)
# ------------------------------------------------------------
from process.models import (
    ProcessRecipe,
    StopMode,
    ProcessStep,
    StepType,
)
from process.recipe_io import load_recipe, save_recipe
from process.engine import ProcessEngine, EngineResult
from services.plc_service import PLCService
from services.log_service import LogService

from controller.process_worker import ProcessWorker

_UNSET = object()

class ProcessController(QObject):
    """
    UI가 직접 사용하는 컨트롤러.

    Signals:
    - sig_ui_log(str)            : UI 로그창에 append 할 문자열
    - sig_status(ProcessStatus)  : 공정 상태(현재 step, phase, 센서값 포함)
    - sig_step(StepStatus)       : step 시작/종료 이벤트
    - sig_error(ProcessError)    : 공정 에러 이벤트
    - sig_finished(EngineResult) : 공정 종료(성공/실패/정지 포함)
    - sig_recipe_loaded(ProcessRecipe)
    """

    sig_ui_log = Signal(str)
    sig_status = Signal(object)
    sig_step = Signal(object)
    sig_error = Signal(object)
    sig_finished = Signal(object)
    sig_recipe_loaded = Signal(object)

    def __init__(
        self,
        plc: PLCService,
        log: LogService,
        *,
        stm: Optional[Any] = None,
        acs: Optional[Any] = None,
        turbovac: Optional[Any] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)

        self.plc = plc
        self.stm = None
        self.acs = None
        self.turbovac = turbovac
        self.log = log
        self._bound_signal_keys: set[tuple[int, str, int]] = set()

        self._recipe: Optional[ProcessRecipe] = None
        self._recipe_path: Optional[Path] = None

        self._worker: Optional[ProcessWorker] = None

        # log_service -> controller -> UI
        try:
            self.log.sig_line.connect(self.sig_ui_log.emit)
            self.log.sig_error.connect(lambda s: self.sig_ui_log.emit(f"[LOG_ERR] {s}"))
        except Exception:
            pass

        # --- PLC trace -> Process Window ---
        try:
            if hasattr(self.plc, "sig_cmd_trace"):
                self.plc.sig_cmd_trace.connect(self._on_plc_cmd_trace)
        except Exception:
            pass

        # --- STM/ACS runtime device signal bind ---
        try:
            self._rebind_stm_device(stm, emit_log=False)
        except Exception:
            pass

        try:
            self._rebind_acs_device(acs, emit_log=False)
        except Exception:
            pass

    def _signal_bind_key(self, obj: Any, signal_name: str, slot: Any) -> tuple[int, str, int]:
        return (id(obj), signal_name, id(slot))

    def _safe_disconnect(self, obj: Any, signal_name: str, slot: Any) -> None:
        if obj is None:
            return
        key = self._signal_bind_key(obj, signal_name, slot)
        if key not in self._bound_signal_keys:
            return
        if not hasattr(obj, signal_name):
            self._bound_signal_keys.discard(key)
            return
        try:
            getattr(obj, signal_name).disconnect(slot)
        except Exception:
            pass
        finally:
            self._bound_signal_keys.discard(key)

    def _safe_connect(self, obj: Any, signal_name: str, slot: Any) -> None:
        if obj is None or not hasattr(obj, signal_name):
            return
        key = self._signal_bind_key(obj, signal_name, slot)
        if key in self._bound_signal_keys:
            return
        sig = getattr(obj, signal_name)
        try:
            sig.connect(slot)
            self._bound_signal_keys.add(key)
        except Exception:
            pass

    def _rebind_stm_device(self, stm: Optional[Any], *, emit_log: bool = True) -> None:
        old = getattr(self, "stm", None)
        if old is stm:
            return

        if old is not None:
            self._safe_disconnect(old, "sig_io_trace", self._on_stm_io_trace)
            self._safe_disconnect(old, "sig_error", self._on_stm_error)
            self._safe_disconnect(old, "sig_connected", self._on_stm_connected)

        self.stm = stm

        if self.stm is not None:
            self._safe_connect(self.stm, "sig_io_trace", self._on_stm_io_trace)
            self._safe_connect(self.stm, "sig_error", self._on_stm_error)
            self._safe_connect(self.stm, "sig_connected", self._on_stm_connected)

        if emit_log:
            name = type(self.stm).__name__ if self.stm is not None else "None"
            self._ui_info(f"STM runtime device 교체: {name}")

    def _rebind_acs_device(self, acs: Optional[Any], *, emit_log: bool = True) -> None:
        old = getattr(self, "acs", None)
        if old is acs:
            return

        if old is not None:
            self._safe_disconnect(old, "sig_io_trace", self._on_acs_io_trace)

        self.acs = acs

        if self.acs is not None:
            self._safe_connect(self.acs, "sig_io_trace", self._on_acs_io_trace)

        if emit_log:
            name = type(self.acs).__name__ if self.acs is not None else "None"
            self._ui_info(f"ACS runtime device 교체: {name}")

    def replace_runtime_devices(
        self,
        *,
        stm: Any = _UNSET,
        acs: Any = _UNSET,
        turbovac: Any = _UNSET,
    ) -> None:
        """
        공정 시작 전/idle 상태에서 runtime device를 공식적으로 교체한다.
        실행 중인 공정(engine)은 start 시점의 device 참조를 사용하므로,
        실행 중 교체는 허용하지 않는다.
        """
        if self.is_running():
            self._ui_warn("공정 실행 중에는 runtime device를 교체할 수 없습니다.")
            return

        if stm is not _UNSET:
            self._rebind_stm_device(stm)

        if acs is not _UNSET:
            self._rebind_acs_device(acs)

        if turbovac is not _UNSET:
            self.turbovac = turbovac
            name = type(self.turbovac).__name__ if self.turbovac is not None else "None"
            self._ui_info(f"Turbovac runtime device 교체: {name}")

    # --------------------------------------------------------
    # Recipe I/O
    # --------------------------------------------------------
    def load_recipe_file(self, path: Union[str, Path], *, strict: bool = True) -> ProcessRecipe:
        p = Path(path)
        recipe = load_recipe(p, strict=strict)
        self._recipe = recipe
        self._recipe_path = p
        self.sig_recipe_loaded.emit(recipe)
        self._ui_info(f"레시피 로드: {p}")
        return recipe

    def save_recipe_file(self, path: Optional[Union[str, Path]] = None) -> None:
        if self._recipe is None:
            raise RuntimeError("No recipe loaded")
        p = Path(path) if path else (self._recipe_path or Path.cwd() / f"{self._recipe.recipe_name}.json")
        save_recipe(p, self._recipe)
        self._recipe_path = p
        self._ui_info(f"레시피 저장: {p}")

    def set_recipe(self, recipe: ProcessRecipe, *, path: Optional[Union[str, Path]] = None) -> None:
        recipe.validate(strict=True)
        self._recipe = recipe
        self._recipe_path = Path(path) if path else None
        self.sig_recipe_loaded.emit(recipe)
        self._ui_info(f"레시피 설정: {recipe.recipe_name}")

    def get_recipe(self) -> Optional[ProcessRecipe]:
        return self._recipe
    
    def _normalize_ramp_steps(self, run_cfg: dict[str, Any]) -> list[dict[str, float]]:
        raw_steps = (
            run_cfg.get("ramp_steps")
            or (run_cfg.get("process_config") or {}).get("ramp_steps")
            or []
        )

        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("Process Config의 ramp_steps가 비어 있습니다.")

        if len(raw_steps) > 10:
            raise ValueError(
                f"Process Config의 step 개수가 너무 많습니다. "
                f"현재 최대 10개까지 지원합니다. (입력={len(raw_steps)})"
            )

        steps: list[dict[str, float]] = []
        last_adc = -1.0

        for idx, item in enumerate(raw_steps, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Process Config step {idx} 형식이 올바르지 않습니다.")

            try:
                target_adc = float(item.get("target_adc", 0.0) or 0.0)
                delay_s = float(item.get("delay_s", 0.0) or 0.0)
            except Exception:
                raise ValueError(f"Process Config step {idx} 값 변환에 실패했습니다.")

            if target_adc <= 0:
                raise ValueError(f"Process Config step {idx}의 target_adc는 0보다 커야 합니다.")
            if delay_s < 0:
                raise ValueError(f"Process Config step {idx}의 delay_s는 0 이상이어야 합니다.")

            if last_adc >= 0 and target_adc < last_adc:
                raise ValueError(
                    f"Process Config step {idx}의 target_adc가 이전 step보다 작습니다. "
                    "step target_adc는 일반적으로 오름차순이어야 합니다."
                )

            steps.append(
                {
                    "step_no": idx,
                    "target_adc": float(target_adc),
                    "delay_s": float(delay_s),
                }
            )
            last_adc = target_adc

        if not steps:
            raise ValueError("최소 1개의 process step이 필요합니다.")

        return steps
    
    def _normalize_process_policy(
        self,
        run_cfg: dict[str, Any],
        *,
        last_step_adc: float,
    ) -> dict[str, Any]:
        proc_cfg = run_cfg.get("process_config") or {}

        policy = str(
            run_cfg.get("after_last_step_policy")
            or proc_cfg.get("after_last_step_policy")
            or "extra_ramp"
        ).strip().lower()

        if policy not in {"extra_ramp", "stop"}:
            raise ValueError(
                f"after_last_step_policy 값이 올바르지 않습니다: {policy!r} "
                "(허용: 'extra_ramp', 'stop')"
            )

        extra_src = (
            run_cfg.get("extra_ramp")
            or proc_cfg.get("extra_ramp")
            or {}
        )
        if not isinstance(extra_src, dict):
            raise ValueError("extra_ramp 설정 형식이 올바르지 않습니다. dict 형태여야 합니다.")

        enabled = bool(extra_src.get("enabled", True))
        if policy != "extra_ramp":
            enabled = False

        try:
            max_adc = float(extra_src.get("max_adc", last_step_adc) or last_step_adc)
        except Exception:
            raise ValueError(f"extra_ramp.max_adc 값 변환에 실패했습니다: {extra_src.get('max_adc')!r}")

        try:
            step_max = float(extra_src.get("step_max", 50.0) or 50.0)
        except Exception:
            raise ValueError(f"extra_ramp.step_max 값 변환에 실패했습니다: {extra_src.get('step_max')!r}")

        try:
            interval_s = float(extra_src.get("interval_s", 5.0) or 5.0)
        except Exception:
            raise ValueError(f"extra_ramp.interval_s 값 변환에 실패했습니다: {extra_src.get('interval_s')!r}")

        if max_adc < last_step_adc:
            raise ValueError(
                f"extra_ramp.max_adc는 마지막 step target_adc보다 작을 수 없습니다. "
                f"(last_step_adc={last_step_adc}, max_adc={max_adc})"
            )

        if not (1.0 <= step_max <= 100.0):
            raise ValueError(f"extra_ramp.step_max는 1.0 ~ 100.0 범위여야 합니다. (입력={step_max})")

        if interval_s < 0.1:
            raise ValueError(f"extra_ramp.interval_s는 0.1초 이상이어야 합니다. (입력={interval_s})")

        reach_main_on_rate = bool(
            proc_cfg.get("reach_main_on_rate", run_cfg.get("reach_main_on_rate", True))
        )

        return {
            "reach_main_on_rate": reach_main_on_rate,
            "after_last_step_policy": policy,
            "extra_ramp": {
                "enabled": enabled,
                "max_adc": max_adc,
                "step_max": step_max,
                "interval_s": interval_s,
            },
        }

    def _prepare_recipe_build_context(self, run_cfg: dict[str, Any]) -> dict[str, Any]:
        """
        UI run_cfg를 기반으로 recipe 생성에 필요한 조립 컨텍스트를 만든다.
        여기서는 '값 정규화/정책 병합'만 수행하고, ProcessRecipe 객체는 만들지 않는다.
        """
        power = self._extract_power_mode(run_cfg)
        material_cfg = self._extract_material_config(run_cfg)
        process_config = self._build_process_config_from_run_cfg(run_cfg)

        runtime_meta = self._build_runtime_evap_meta(
            run_cfg,
            power=power,
            material_cfg=material_cfg,
            process_config=process_config,
        )
        self._apply_material_ramp_overrides(runtime_meta, run_cfg.get("ramp"))

        process_name = self._sanitize_process_name(
            run_cfg.get("process_name"),
            fallback_material=material_cfg["material_name"],
        )

        return {
            "power": power,
            "material_cfg": material_cfg,
            "process_config": process_config,
            "runtime_meta": runtime_meta,
            "process_name": process_name,
        }

    def _build_recipe_from_context(
        self,
        ctx: dict[str, Any],
        *,
        run_cfg: dict[str, Any],
    ) -> ProcessRecipe:
        steps = self._build_evap_steps(
            power=ctx["power"],
            meta=ctx["runtime_meta"],
        )

        recipe = ProcessRecipe(
            recipe_name=ctx["process_name"],
            steps=steps,
            telemetry_interval_s=1.0,
            meta=self._build_recipe_trace_meta(
                process_name=ctx["process_name"],
                power=ctx["power"],
                material_cfg=ctx["material_cfg"],
                process_config=ctx["process_config"],
                runtime_meta=ctx["runtime_meta"],
                run_cfg=run_cfg,
            ),
        )
        recipe.validate(strict=True)
        return recipe


    def build_recipe_from_ui(self, run_cfg: dict[str, Any]) -> ProcessRecipe:
        ctx = self._prepare_recipe_build_context(run_cfg)
        return self._build_recipe_from_context(ctx, run_cfg=run_cfg)
    
    def _to_float(self, value: Any, field_name: str) -> float:
        try:
            return float(value or 0.0)
        except Exception:
            raise ValueError(f"{field_name} 값 변환에 실패했습니다.")

    def _extract_material_config(self, run_cfg: dict[str, Any]) -> dict[str, Any]:
        material = str(run_cfg.get("material_name", "")).strip()
        if not material:
            raise ValueError("material_name은 필수입니다.")

        density = self._to_float(run_cfg.get("density", 0.0), "density")
        z_factor = self._to_float(run_cfg.get("z_factor", 0.0), "z_factor")
        if density <= 0 or z_factor <= 0:
            raise ValueError("density / z_factor 값이 올바르지 않습니다. (0보다 커야 함)")

        target_rate = self._to_float(run_cfg.get("target_rate", 0.0), "target_rate")
        if target_rate <= 0:
            raise ValueError("target_rate(목표 Dep.rate)는 0보다 커야 합니다.")

        target_thickness = self._to_float(run_cfg.get("target_thickness", 0.0), "target_thickness")
        delay_min = self._to_float(run_cfg.get("delay_min", 0.0), "delay_min")

        if target_thickness <= 0:
            raise ValueError("target_thickness는 0보다 커야 합니다.")
        if delay_min < 0:
            raise ValueError("delay_min은 0 이상이어야 합니다.")

        return {
            "material_name": material,
            "density": density,
            "z_factor": z_factor,
            "target_rate": target_rate,
            "target_thickness": target_thickness,
            "delay_min": delay_min,
        }
    
    def _build_process_config_from_run_cfg(self, run_cfg: dict[str, Any]) -> dict[str, Any]:
        proc_src = dict(run_cfg.get("process_config") or {})

        def _cfg_int(name: str, default: int) -> int:
            try:
                return int(proc_src.get(name, default))
            except Exception:
                return int(default)

        def _cfg_float(name: str, default: float) -> float:
            try:
                return float(proc_src.get(name, default))
            except Exception:
                return float(default)

        ramp_steps = self._normalize_ramp_steps(run_cfg)
        last_step_adc = float(ramp_steps[-1]["target_adc"])
        proc_policy = self._normalize_process_policy(run_cfg, last_step_adc=last_step_adc)

        return {
            "step_count": len(ramp_steps),
            "ramp_steps": ramp_steps,
            "reach_main_on_rate": bool(proc_policy["reach_main_on_rate"]),
            "after_last_step_policy": str(proc_policy["after_last_step_policy"]),
            "extra_ramp": dict(proc_policy["extra_ramp"]),
            "last_step_target_adc": last_step_adc,

            "ramp_seg1_max_dac": _cfg_int("ramp_seg1_max_dac", 700),
            "ramp_interval_seg1_s": _cfg_float("ramp_interval_seg1_s", 10.0),
            "ramp_seg2_max_dac": _cfg_int("ramp_seg2_max_dac", 2000),
            "ramp_interval_seg2_s": _cfg_float("ramp_interval_seg2_s", 30.0),
            "ramp_interval_after_seg2_s": _cfg_float("ramp_interval_after_seg2_s", 30.0),

            "pre_rate": _cfg_float("pre_rate", 0.4),

            "dac_adjust_interval_s": _cfg_float("dac_adjust_interval_s", 10.0),
            "fine_step_dac": _cfg_int("fine_step_dac", 10),

            "material_shortage_dac": _cfg_int("material_shortage_dac", 2000),
            "material_shortage_rate_max": _cfg_float("material_shortage_rate_max", 0.0),
            "material_shortage_time_s": _cfg_float("material_shortage_time_s", 10.0),

            "rate_filter_window": _cfg_int("rate_filter_window", 5),
            "rate_stable_sec": _cfg_float("rate_stable_sec", 3.0),
            "rate_drop_ratio": _cfg_float("rate_drop_ratio", 0.50),
            "rate_drop_count": _cfg_int("rate_drop_count", 3),
        }
    
    def _build_power_runtime_meta(self, power: dict[str, Any]) -> dict[str, Any]:
        return {
            "use_power1": power["use_power1"],
            "use_power2": power["use_power2"],
            "power1_feedback_adc2": power["power1_feedback_adc2"],
        }

    def _build_material_runtime_meta(self, material_cfg: dict[str, Any]) -> dict[str, Any]:
        return {
            "density": material_cfg["density"],
            "z_factor": material_cfg["z_factor"],
            "target_rate": material_cfg["target_rate"],
            "target_thickness": material_cfg["target_thickness"],
            "delay_min": material_cfg["delay_min"],
        }

    def _build_control_runtime_meta(
        self,
        run_cfg: dict[str, Any],
        process_config: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "dac_max": 4000,
            "sensor_none_abort_s": 5.0,
            "adc_none_abort_s": 5.0,

            "ramp_seg1_max_dac": int(process_config["ramp_seg1_max_dac"]),
            "ramp_seg2_max_dac": int(process_config["ramp_seg2_max_dac"]),
            "ramp_interval_seg1_s": float(process_config["ramp_interval_seg1_s"]),
            "ramp_interval_seg2_s": float(process_config["ramp_interval_seg2_s"]),
            "ramp_interval_after_seg2_s": float(process_config["ramp_interval_after_seg2_s"]),
            "fine_step_dac": int(process_config["fine_step_dac"]),

            # 기존 로직과의 호환용
            "rate_tol_ratio": float(run_cfg.get("rate_tol_ratio", 0.05) or 0.05),
            "target_ramp_stable_hits": int(run_cfg.get("target_ramp_stable_hits", 3) or 3),
            "target_stable_hits": int(run_cfg.get("target_stable_hits", 5) or 5),
            "target_stable_interval_s": float(run_cfg.get("target_stable_interval_s", 1.0) or 1.0),

            # 신규 dep.rate 판단 파라미터
            "rate_filter_window": int(process_config["rate_filter_window"]),
            "rate_stable_sec": float(process_config["rate_stable_sec"]),
            "rate_drop_ratio": float(process_config["rate_drop_ratio"]),
            "rate_drop_count": int(process_config["rate_drop_count"]),

            "pre_rate": float(process_config["pre_rate"]),
            "dac_adjust_interval_s": float(process_config["dac_adjust_interval_s"]),

            "material_shortage_dac": int(process_config["material_shortage_dac"]),
            "material_shortage_rate_max": float(process_config["material_shortage_rate_max"]),
            "material_shortage_time_s": float(process_config["material_shortage_time_s"]),

            "zero_mode": "B",
            "adc_control_mode": str(run_cfg.get("adc_control_mode", "adc") or "adc"),
            "tune_timeout_s": float(run_cfg.get("tune_timeout_s", 120.0) or 120.0),
        }

    def _build_process_runtime_meta(self, process_config: dict[str, Any]) -> dict[str, Any]:
        last_step_adc = float(process_config["last_step_target_adc"])

        return {
            "process_config": {
                "ramp_steps": process_config["ramp_steps"],
                "reach_main_on_rate": process_config["reach_main_on_rate"],
                "after_last_step_policy": process_config["after_last_step_policy"],
                "extra_ramp": dict(process_config["extra_ramp"]),
            },

            "ramp_steps": process_config["ramp_steps"],
            "reach_main_on_rate": process_config["reach_main_on_rate"],
            "after_last_step_policy": process_config["after_last_step_policy"],
            "extra_ramp": dict(process_config["extra_ramp"]),
            "last_step_target_adc": last_step_adc,
            "adc_dynamic_step_cap": float(process_config["extra_ramp"].get("step_max", 50.0)),
        }

    def _build_runtime_evap_meta(
        self,
        run_cfg: dict[str, Any],
        *,
        power: dict[str, Any],
        material_cfg: dict[str, Any],
        process_config: dict[str, Any],
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        meta.update(self._build_power_runtime_meta(power))
        meta.update(self._build_material_runtime_meta(material_cfg))
        meta.update(self._build_control_runtime_meta(run_cfg, process_config))
        meta.update(self._build_process_runtime_meta(process_config))
        return meta
        
    def _apply_material_ramp_overrides(self, meta: dict[str, Any], ramp_cfg: Any) -> None:
        if ramp_cfg is None:
            return
        if not isinstance(ramp_cfg, dict):
            raise ValueError("ramp 설정 형식이 올바르지 않습니다. dict 형태여야 합니다.")
        if not ramp_cfg:
            return

        nested = meta.get("process_config")
        if not isinstance(nested, dict):
            nested = None

        def _set_meta(k: str, value: Any) -> None:
            meta[k] = value
            if nested is not None:
                nested[k] = value

        def _apply(k: str, cast) -> bool:
            if k not in ramp_cfg:
                return False

            v = ramp_cfg.get(k)
            if v is None or v == "":
                return False

            try:
                converted = cast(v)
                _set_meta(k, converted)
                return True
            except Exception:
                raise ValueError(f"ramp 설정값 변환 실패: {k}={v!r}")

        _apply("ramp_seg1_max_dac", lambda x: int(float(x)))
        _apply("ramp_seg2_max_dac", lambda x: int(float(x)))
        _apply("fine_step_dac", lambda x: int(float(x)))
        _apply("material_shortage_dac", lambda x: int(float(x)))
        _apply("rate_filter_window", lambda x: int(float(x)))
        _apply("rate_drop_count", lambda x: int(float(x)))
        _apply("target_ramp_stable_hits", lambda x: int(float(x)))
        _apply("target_stable_hits", lambda x: int(float(x)))

        _apply("ramp_interval_seg1_s", float)
        seg2_over = _apply("ramp_interval_seg2_s", float)
        after_over = _apply("ramp_interval_after_seg2_s", float)
        if seg2_over and not after_over:
            _set_meta("ramp_interval_after_seg2_s", float(meta["ramp_interval_seg2_s"]))

        _apply("pre_rate", float)
        _apply("rate_tol_ratio", float)
        _apply("target_stable_interval_s", float)

        _apply("dac_adjust_interval_s", float)

        _apply("material_shortage_rate_max", float)
        _apply("material_shortage_time_s", float)

        _apply("rate_stable_sec", float)
        _apply("rate_drop_ratio", float)

    def _build_evap_steps(
        self,
        *,
        power: dict[str, Any],
        meta: dict[str, Any],
    ) -> list[ProcessStep]:
        use_p1 = bool(power["use_power1"])
        use_p2 = bool(power["use_power2"])
        temp_force_power2_sw = bool(power["temp_force_power2_sw"])

        steps: list[ProcessStep] = [
            ProcessStep(name="MAIN_SHUTTER_CLOSE", type=StepType.PLC_WRITE_COIL, coil="MAIN_SHUTTER_SW", on=False),
            ProcessStep(name="SHUTTER_1_CLOSE_INIT", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_1_SW", on=False),
            ProcessStep(name="SHUTTER_2_CLOSE_INIT", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_2_SW", on=False),

            ProcessStep(name="DAC1_ZERO", type=StepType.PLC_WRITE_REG, reg="DAC_POWER_1", value=0),
            ProcessStep(name="DAC2_ZERO", type=StepType.PLC_WRITE_REG, reg="DAC_POWER_2", value=0),

            ProcessStep(name="POWER1_SET", type=StepType.PLC_WRITE_COIL, coil="POWER_1_SW", on=use_p1),
            ProcessStep(name="POWER2_SET", type=StepType.PLC_WRITE_COIL, coil="POWER_2_SW", on=temp_force_power2_sw),
        ]

        if use_p1:
            steps.append(ProcessStep(name="SHUTTER_1_OPEN", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_1_SW", on=True))
        if use_p2:
            steps.append(ProcessStep(name="SHUTTER_2_OPEN", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_2_SW", on=True))

        steps.append(ProcessStep(name="FTM_ON", type=StepType.PLC_WRITE_COIL, coil="FTM_SW", on=True))
        steps.append(
            ProcessStep(
                name="WAIT_FTM_STM",
                type=StepType.WAIT_SECONDS,
                seconds=float(meta.get("wait_after_ftm_on_s", 1.5)),
                message="FTM/STM 안정화 대기",
            )
        )
        steps.append(ProcessStep(name="EVAP_DEPOSITION_CONTROL", type=StepType.MARK, meta=meta))

        if use_p1:
            steps.append(ProcessStep(name="SHUTTER_1_CLOSE", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_1_SW", on=False))
        if use_p2:
            steps.append(ProcessStep(name="SHUTTER_2_CLOSE", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_2_SW", on=False))
        steps.append(ProcessStep(name="FTM_OFF", type=StepType.PLC_WRITE_COIL, coil="FTM_SW", on=False))

        return steps
    
    def _sanitize_process_name(self, raw_name: Any, *, fallback_material: str) -> str:
        process_name = str(raw_name or "").strip()
        process_name = re.sub(r'[<>:"/\\|?*]+', "_", process_name)
        process_name = re.sub(r"\s+", "_", process_name).strip("._ ")

        if not process_name:
            process_name = f"EVAP_{fallback_material}"
        return process_name
    
    def _build_recipe_trace_meta(
        self,
        *,
        process_name: str,
        power: dict[str, Any],
        material_cfg: dict[str, Any],
        process_config: dict[str, Any],
        runtime_meta: dict[str, Any],
        run_cfg: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "process_name": process_name,
            "material_name": material_cfg["material_name"],
            "use_power1": power["use_power1"],
            "use_power2": power["use_power2"],
            "target_rate": material_cfg["target_rate"],
            "target_thickness": material_cfg["target_thickness"],
            "delay_min": material_cfg["delay_min"],
            "adc_control_mode": str(run_cfg.get("adc_control_mode", "adc") or "adc"),
            "step_count": process_config["step_count"],
            "ramp_steps": process_config["ramp_steps"],
            "after_last_step_policy": process_config["after_last_step_policy"],
            "extra_ramp": dict(process_config["extra_ramp"]),

            # 실제 실행 메타 일부 snapshot
            "effective_runtime": {
                "ramp_seg1_max_dac": runtime_meta.get("ramp_seg1_max_dac"),
                "ramp_seg2_max_dac": runtime_meta.get("ramp_seg2_max_dac"),
                "ramp_interval_seg1_s": runtime_meta.get("ramp_interval_seg1_s"),
                "ramp_interval_seg2_s": runtime_meta.get("ramp_interval_seg2_s"),
                "ramp_interval_after_seg2_s": runtime_meta.get("ramp_interval_after_seg2_s"),
                "fine_step_dac": runtime_meta.get("fine_step_dac"),
                "pre_rate": runtime_meta.get("pre_rate"),
                "dac_adjust_interval_s": runtime_meta.get("dac_adjust_interval_s"),
                "rate_tol_ratio": runtime_meta.get("rate_tol_ratio"),
                "target_ramp_stable_hits": runtime_meta.get("target_ramp_stable_hits"),
                "target_stable_hits": runtime_meta.get("target_stable_hits"),
                "target_stable_interval_s": runtime_meta.get("target_stable_interval_s"),
                "rate_filter_window": runtime_meta.get("rate_filter_window"),
                "rate_stable_sec": runtime_meta.get("rate_stable_sec"),
                "rate_drop_ratio": runtime_meta.get("rate_drop_ratio"),
                "rate_drop_count": runtime_meta.get("rate_drop_count"),
                "material_shortage_dac": runtime_meta.get("material_shortage_dac"),
                "material_shortage_rate_max": runtime_meta.get("material_shortage_rate_max"),
                "material_shortage_time_s": runtime_meta.get("material_shortage_time_s"),
                "dac_max": runtime_meta.get("dac_max"),
                "sensor_none_abort_s": runtime_meta.get("sensor_none_abort_s"),
                "adc_none_abort_s": runtime_meta.get("adc_none_abort_s"),
                "zero_mode": runtime_meta.get("zero_mode"),
                "tune_timeout_s": runtime_meta.get("tune_timeout_s"),
            },
        }
        
    def _extract_power_mode(self, run_cfg: dict[str, Any]) -> dict[str, Any]:
        use_p1 = bool(run_cfg.get("use_power1", False))
        use_p2 = bool(run_cfg.get("use_power2", False))

        if not (use_p1 or use_p2):
            raise ValueError("Power1/Power2 중 최소 1개는 선택되어야 합니다.")

        # 현재 장비 임시 제약
        if use_p2:
            raise ValueError(
                "현재 장비 상태에서는 Power 2를 사용할 수 없습니다.\n"
                "임시로 Power 1만 사용해 주세요."
            )

        temp_force_power2_sw = use_p1 and (not use_p2)
        power1_feedback_adc2 = use_p1 and (not use_p2)

        source_shutter_coils: list[str] = []
        if use_p1:
            source_shutter_coils.append("SHUTTER_1_SW")
        if use_p2:
            source_shutter_coils.append("SHUTTER_2_SW")

        return {
            "use_power1": use_p1,
            "use_power2": use_p2,
            "temp_force_power2_sw": temp_force_power2_sw,
            "power1_feedback_adc2": power1_feedback_adc2,
            "source_shutter_coils": source_shutter_coils,
        }

    def start_from_ui(self, run_cfg: dict[str, Any], *, run_id: Optional[str] = None) -> None:
        """
        UI 입력값으로 공정 시작.
        """
        try:
            r = self.build_recipe_from_ui(run_cfg)
            self.set_recipe(r)
            self.start(run_id=run_id, recipe=r)
        except Exception as e:
            self._ui_warn(f"공정 시작 준비 실패: {e}")
            raise

    # --------------------------------------------------------
    # Run control
    # --------------------------------------------------------
    def is_running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def start(self, *, run_id: Optional[str] = None, recipe: Optional[ProcessRecipe] = None) -> None:
        """
        공정 시작.
        - recipe 인자 없으면 현재 로드된 레시피 사용
        """
        if self.is_running():
            self._ui_warn("이미 공정 실행 중입니다.")
            return

        r = recipe or self._recipe
        if r is None:
            raise RuntimeError("No recipe to run. Load or set recipe first.")
        r.validate(strict=True)

        # 서비스 실행 보장(중복 호출 안전하게 작성)
        self._ensure_services_running(start_stm=False)

        # ✅ PLC 연결 상태 최종 확인
        if not self._is_plc_ready():
            self._ui_warn("PLC가 아직 연결되지 않아 공정을 시작할 수 없습니다.")
            return

        # 엔진 구성
        engine = self._create_engine()

        # 워커 생성 (✅ 여기서 ProcessWorker 사용)
        w = ProcessWorker(engine=engine, recipe=r, run_id=run_id)
        self._worker = w

        # 워커 -> 컨트롤러 시그널 연결
        w.sig_status.connect(self.sig_status)
        w.sig_step.connect(self.sig_step)
        w.sig_error.connect(self.sig_error)
        w.sig_result.connect(self._on_worker_result)

        # 워커 종료 처리 / 메모리 정리
        w.finished.connect(self._on_worker_finished)
        w.finished.connect(w.deleteLater)

        self._ui_info("공정 시작 요청")
        w.start()

    def stop(self) -> None:
        """
        정상 정지 요청.
        실제 안전 종료 순서(셔터 닫기 -> DAC ramp-down -> power off)는
        engine.py / safety.py 경로에서 수행한다.
        """
        if self.is_running():
            self._request_engine_stop(StopMode.STOP)
        else:
            self._ui_info("실행 중 공정 없음")

    def abort(self) -> None:
        if self.is_running():
            self._request_engine_stop(StopMode.ABORT)
        else:
            self._ui_info("실행 중 공정 없음")

    def estop(self) -> None:
        if self.is_running():
            self._request_engine_stop(StopMode.ESTOP)
        else:
            self._ui_info("실행 중 공정 없음")

    def pause(self) -> None:
        if not self.is_running():
            return
        try:
            self._worker.request_pause()
            self._ui_info("일시정지 요청")
        except Exception as e:
            self._ui_warn(f"pause 실패: {e!r}")

    def resume(self) -> None:
        if not self.is_running():
            return
        try:
            self._worker.request_resume()
            self._ui_info("재개 요청")
        except Exception as e:
            self._ui_warn(f"resume 실패: {e!r}")

    # --------------------------------------------------------
    # Internal helpers
    # --------------------------------------------------------
    def _request_engine_stop(self, mode: StopMode) -> None:
        if not self.is_running():
            self._ui_warn("실행 중인 공정이 없습니다.")
            return
        try:
            self._worker.request_stop(mode)
            self._ui_warn(f"정지 요청: {mode.value}")
        except Exception as e:
            self._ui_warn(f"stop 요청 실패: {e!r}")

    def _create_engine(self) -> ProcessEngine:
        return ProcessEngine(
            plc=self.plc,
            stm=self.stm,
            acs=self.acs,
            turbovac=self.turbovac,
            log=self.log,
            callbacks=None,
        )

    def _is_plc_ready(self) -> bool:
        """
        공정 시작/안전정지 전에 PLC가 실제 I/O 가능한 상태인지 확인.
        """
        try:
            if hasattr(self.plc, "is_running") and not self.plc.is_running():
                return False

            if hasattr(self.plc, "is_connected"):
                return bool(self.plc.is_connected())

            snap = self.plc.get_last_snapshot() if hasattr(self.plc, "get_last_snapshot") else None
            return bool(getattr(snap, "connected", False)) if snap is not None else False
        except Exception:
            return False

    def _ensure_services_running(self, *, start_stm: bool = False) -> None:
        """
        PLC/센서/로그 서비스가 실행 중인지 확인하고, 꺼져 있으면 켠다.
        - 각 서비스는 start()가 중복 호출되어도 안전해야 함(우리가 만든 구조 기준)
        """
        try:
            if hasattr(self.log, "start") and hasattr(self.log, "is_running"):
                if not self.log.is_running():
                    self.log.start()
            elif hasattr(self.log, "start"):
                self.log.start()
        except Exception:
            pass

        try:
            if hasattr(self.plc, "start") and hasattr(self.plc, "is_running"):
                if not self.plc.is_running():
                    self.plc.start()
            elif hasattr(self.plc, "start"):
                self.plc.start()
        except Exception as e:
            self._ui_warn(f"PLC 서비스 start 실패: {e!r}")

        # STM은 ProcessWindow 쪽 preflight/start 경로에서 관리할 예정이므로
        # 여기서는 기본적으로 자동 start 하지 않는다.
        if start_stm and self.stm is not None:
            try:
                if hasattr(self.stm, "start") and hasattr(self.stm, "is_running"):
                    if not self.stm.is_running():
                        self.stm.start()
                elif hasattr(self.stm, "start"):
                    self.stm.start()
            except Exception as e:
                self._ui_warn(f"STM 서비스 start 실패: {e!r}")

        if self.acs is not None:
            try:
                if hasattr(self.acs, "start") and hasattr(self.acs, "is_running"):
                    if not self.acs.is_running():
                        self.acs.start()
                elif hasattr(self.acs, "start"):
                    self.acs.start()
            except Exception as e:
                self._ui_warn(f"ACS 서비스 start 실패: {e!r}")

        if self.turbovac is not None:
            try:
                if hasattr(self.turbovac, "start") and hasattr(self.turbovac, "is_running"):
                    if not self.turbovac.is_running():
                        self.turbovac.start()
                elif hasattr(self.turbovac, "start"):
                    self.turbovac.start()
            except Exception as e:
                self._ui_warn(f"Turbovac 서비스 start 실패: {e!r}")

    def _on_worker_result(self, result: EngineResult) -> None:
        """
        엔진 실행 결과 수신.
        """
        self.sig_finished.emit(result)

        if result.ok:
            self._ui_info(f"공정 완료: run_id={result.run_id}")
        else:
            if result.error:
                self._ui_warn(f"공정 종료(실패/중단): {result.error.where} | {result.error.message}")
            else:
                self._ui_warn(f"공정 종료(정지/중단): run_id={result.run_id}")

    def _on_worker_finished(self) -> None:
        """
        워커 스레드 종료 cleanup.
        """
        self._worker = None
        self._ui_info("공정 스레드 종료")

    def _on_plc_cmd_trace(self, obj: object) -> None:
        try:
            d = dict(obj or {})
        except Exception:
            return

        ok = bool(d.get("ok", True))
        cmd = str(d.get("event", "") or "")
        target = str(d.get("target", "") or "")
        value = d.get("value", "")
        tag = str(d.get("tag", "") or "")
        detail = str(d.get("detail", "") or "")

        # Cmd 이름 -> 사람이 보기 좋은 event명으로 변환(원하면 그대로 CmdWriteCoil로 찍어도 됨)
        event = cmd
        if cmd == "CmdWriteCoil":
            event = "PULSE_COIL" if ("pulse_ms" in detail or "momentary=1" in detail) else "WRITE_COIL"
            if isinstance(value, bool):
                value = int(value)
        elif cmd == "CmdWriteReg":
            tu = target.upper()
            event = "SET_DAC" if tu in ("DAC_POWER_1", "DAC_POWER_2") else "WRITE_REG"
        elif cmd == "CmdSetDacCurrent":
            event = "SET_DAC_MA"

        prefix = f"{tag} " if tag else ""
        msg = f"{prefix}{event} {target} = {value}"
        if detail:
            msg += f" ({detail})"

        # ✅ Process Window에 출력
        try:
            if ok:
                self.log.info(msg, tag="PLC", also_ui=True)
            else:
                self.log.warn(msg, tag="PLC", also_ui=True)
        except Exception:
            # fallback
            self.sig_ui_log.emit(f"[PLC]{'[WARN]' if not ok else ''} {msg}")

    def _on_stm_io_trace(self, obj: object) -> None:
        try:
            d = dict(obj or {})
        except Exception:
            return

        ok = bool(d.get("ok", True))
        tx = str(d.get("tx", "") or "")
        rx = str(d.get("rx", "") or "")
        detail = str(d.get("detail", "") or "")

        # 혹시라도 S/T 폴링이 넘어오면 한 번 더 안전하게 차단(스팸 방지)
        token = (tx[:1].upper() if tx else "")
        if token in ("S", "T"):
            return

        msg = f"TX={tx}"
        if rx:
            msg += f" | RX={rx}"
        if detail:
            msg += f" ({detail})"

        try:
            if ok:
                self.log.info(msg, tag="STM", also_ui=True)
            else:
                self.log.warn(msg, tag="STM", also_ui=True)
        except Exception:
            self.sig_ui_log.emit(f"[STM]{'[WARN]' if not ok else ''} {msg}")

    def _on_stm_connected(self, connected: bool) -> None:
        """
        STM 연결/해제 상태를 ProcessWindowLog에 남김.
        """
        try:
            if connected:
                self.log.info("connected", tag="STM", also_ui=True)
            else:
                self.log.warn("disconnected", tag="STM", also_ui=True)
        except Exception:
            self.sig_ui_log.emit(f"[STM]{' connected' if connected else ' disconnected'}")

    def _on_stm_error(self, s: str) -> None:
        """
        STMService에서 올라오는 모든 오류를 ProcessWindowLog에 남김.
        (poll 실패/재연결/SET 실패 등 포함)
        """
        msg = str(s)
        try:
            self.log.warn(msg, tag="STM", also_ui=True)
        except Exception:
            self.sig_ui_log.emit(f"[STM][WARN] {msg}")

    def _on_acs_io_trace(self, obj: object) -> None:
        # ✅ ACS2000(압력)은 프로그램 부팅부터 CON 스트림을 켜두기 때문에
        #    공정이 돌지 않아도(=idle) 압력 변화가 계속 발생한다.
        #    Process 창(logWindow)은 “공정 중” 로그만 보여야 하므로,
        #    공정 미실행 상태에서는 ACS trace를 UI에 올리지 않는다.
        #    (연결/오류는 main.py에서 HMI 로그창으로 이미 표시됨)
        if not self.is_running():
            return

        try:
            d = dict(obj or {})
        except Exception:
            return

        ok = bool(d.get("ok", True))
        token = str(d.get("token", "") or "")
        detail = str(d.get("detail", "") or "")
        tx = str(d.get("tx", "") or "")
        rx = str(d.get("rx", "") or "")

        msg = f"{token} {detail}".strip()
        if not msg:
            msg = f"TX={tx} | RX={rx}".strip()

        if ok:
            self.log.info(msg, tag="ACS", also_ui=True)
        else:
            self.log.warn(msg, tag="ACS", also_ui=True)

    # --------------------------------------------------------
    # UI log helpers
    # --------------------------------------------------------
    def _ui_info(self, msg: str) -> None:
        try:
            self.log.info(msg, tag="CTRL", also_ui=True)
        except Exception:
            self.sig_ui_log.emit(f"[CTRL][INFO] {msg}")

    def _ui_warn(self, msg: str) -> None:
        try:
            self.log.warn(msg, tag="CTRL", also_ui=True)
        except Exception:
            self.sig_ui_log.emit(f"[CTRL][WARN] {msg}")
