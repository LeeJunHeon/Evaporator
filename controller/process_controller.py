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

import hashlib
import json
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
from services.plc_service import PLCService, _COIL_LABEL
from services.log_service import LogService

from controller.process_worker import ProcessWorker
from controller.chat_notifier import ChatNotifier

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
        # (object id, signal 이름, slot id) 트리플로 중복 connect 방지 및 안전 disconnect 추적
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

        # Google Chat 알림 (실패해도 공정에 영향 없게 best-effort)
        try:
            self._notifier = ChatNotifier(parent=self)
            self._notifier.start()
        except Exception:
            self._notifier = None

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
    
    def _normalize_ramp_steps(self, proc_cfg: dict[str, Any]) -> list[dict[str, Any]]:
        raw_steps = proc_cfg.get("ramp_steps") or []

        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("process_config.ramp_steps가 비어 있습니다.")

        if len(raw_steps) > 10:
            raise ValueError(
                f"Process Config의 step 개수가 너무 많습니다. "
                f"현재 최대 10개까지 지원합니다. (입력={len(raw_steps)})"
            )

        steps: list[dict[str, Any]] = []
        last_adc = -1.0

        for idx, item in enumerate(raw_steps, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"Process Config step {idx} 형식이 올바르지 않습니다.")

            try:
                target_adc = float(item.get("target_adc", 0.0) or 0.0)
                dac_step = int(item.get("dac_step", 0) or 0)
                dac_interval_sec = float(item.get("dac_interval_sec", 0.0) or 0.0)
                # delay_s 읽기 호환만 허용
                hold_src = item.get("hold_sec", item.get("delay_s", 0.0))
                hold_sec = float(hold_src or 0.0)
            except Exception:
                raise ValueError(f"Process Config step {idx} 값 변환에 실패했습니다.")

            if target_adc <= 0:
                raise ValueError(f"Process Config step {idx}의 target_adc는 0보다 커야 합니다.")
            if dac_step <= 0:
                raise ValueError(f"Process Config step {idx}의 dac_step은 0보다 커야 합니다.")
            if dac_interval_sec <= 0:
                raise ValueError(f"Process Config step {idx}의 dac_interval_sec는 0보다 커야 합니다.")
            if hold_sec < 0:
                raise ValueError(f"Process Config step {idx}의 hold_sec는 0 이상이어야 합니다.")

            if last_adc >= 0 and target_adc < last_adc:
                raise ValueError(
                    f"Process Config step {idx}의 target_adc가 이전 step보다 작습니다. "
                    "step target_adc는 오름차순이어야 합니다."
                )

            steps.append({
                "target_adc": target_adc,
                "dac_step": dac_step,
                "dac_interval_sec": dac_interval_sec,
                "hold_sec": hold_sec,
            })
            last_adc = target_adc

        return steps

    def _prepare_recipe_build_context(self, run_cfg: dict[str, Any]) -> dict[str, Any]:
        """
        UI run_cfg를 기반으로 recipe 생성에 필요한 조립 컨텍스트를 만든다.
        여기서는 '값 정규화/정책 병합'만 수행하고, ProcessRecipe 객체는 만들지 않는다.
        """
        power = self._extract_power_mode(run_cfg)
        material_cfg = self._extract_material_config(run_cfg)
        process_config = self._build_process_config_from_run_cfg(run_cfg)

        runtime_meta = self._build_runtime_evap_meta(
            power=power,
            material_cfg=material_cfg,
            process_config=process_config,
        )

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
            ),
        )
        recipe.validate(strict=True)
        return recipe


    def build_recipe_from_ui(self, run_cfg: dict[str, Any]) -> ProcessRecipe:
        ctx = self._prepare_recipe_build_context(run_cfg)
        return self._build_recipe_from_context(ctx)

    def build_run_profile(self, run_cfg: dict[str, Any]) -> dict[str, Any]:
        ctx = self._prepare_recipe_build_context(run_cfg)
        return self._build_run_profile_from_context(ctx)

    def _build_run_profile_from_context(self, ctx: dict[str, Any]) -> dict[str, Any]:
        process_config = self._copy_exact_process_config(ctx["process_config"])
        hw_mapping = {
            "temp_force_power2_sw": bool(ctx["power"]["temp_force_power2_sw"]),
            "power1_feedback_adc2": bool(ctx["power"]["power1_feedback_adc2"]),
        }
        # process_config를 정렬된 JSON으로 직렬화 후 SHA-256 해시 → 이력 비교/추천 시 레시피 동일성 판별
        process_config_hash = self._hash_canonical_payload(process_config)

        fingerprint = {
            "material_name": ctx["material_cfg"]["material_name"],
            "density": ctx["material_cfg"]["density"],
            "z_factor": ctx["material_cfg"]["z_factor"],
            "target_rate": ctx["material_cfg"]["target_rate"],
            "target_thickness": ctx["material_cfg"]["target_thickness"],
            "delay_min": ctx["material_cfg"]["delay_min"],
            "use_power1": bool(ctx["power"]["use_power1"]),
            "use_power2": bool(ctx["power"]["use_power2"]),
            "hw_mapping": dict(hw_mapping),
            "process_config_hash": process_config_hash,
        }

        return {
            "process_name": ctx["process_name"],
            "recipe_name": ctx["process_name"],
            "material_name": ctx["material_cfg"]["material_name"],
            "density": ctx["material_cfg"]["density"],
            "z_factor": ctx["material_cfg"]["z_factor"],
            "target_rate": ctx["material_cfg"]["target_rate"],
            "target_thickness": ctx["material_cfg"]["target_thickness"],
            "delay_min": ctx["material_cfg"]["delay_min"],
            "use_power1": bool(ctx["power"]["use_power1"]),
            "use_power2": bool(ctx["power"]["use_power2"]),
            "hw_mapping": dict(hw_mapping),
            "process_config": process_config,
            "process_config_hash": process_config_hash,
            "runtime_meta": dict(ctx["runtime_meta"]),
            "fingerprint": fingerprint,
        }

    @staticmethod
    def _canonical_json_payload(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _hash_canonical_payload(cls, payload: Any) -> str:
        data = cls._canonical_json_payload(payload).encode("utf-8")
        return hashlib.sha256(data).hexdigest()
        
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
        tooling_factor = self._to_float(run_cfg.get("tooling_factor", 100.0), "tooling_factor")

        # target_thickness=0이면 두께 도달 조건 없이 Stop 버튼으로만 종료
        # (recipe 불러오기 경로에서 thickness 미입력 시 0으로 전달됨)
        if target_thickness < 0:
            raise ValueError("target_thickness는 0 이상이어야 합니다.")
        if delay_min < 0:
            raise ValueError("delay_min은 0 이상이어야 합니다.")

        return {
            "material_name": material,
            "density": density,
            "z_factor": z_factor,
            "target_rate": target_rate,
            "target_thickness": target_thickness,
            "delay_min": delay_min,
            "tooling_factor": tooling_factor,
        }
    
    def _build_process_config_from_run_cfg(self, run_cfg: dict[str, Any]) -> dict[str, Any]:
        proc_src = dict(run_cfg.get("process_config") or {})
        ramp_steps = self._normalize_ramp_steps(proc_src)

        def _require_int(name: str) -> int:
            if name not in proc_src:
                raise ValueError(f"process_config.{name}가 누락되었습니다.")
            try:
                return int(proc_src[name])
            except Exception:
                raise ValueError(f"process_config.{name} 값 변환에 실패했습니다.")

        def _require_float(name: str) -> float:
            if name not in proc_src:
                raise ValueError(f"process_config.{name}가 누락되었습니다.")
            try:
                return float(proc_src[name])
            except Exception:
                raise ValueError(f"process_config.{name} 값 변환에 실패했습니다.")

        step_count = _require_int("step_count")

        if step_count != len(ramp_steps):
            raise ValueError(
                "process_config.step_count와 ramp_steps 개수가 일치하지 않습니다. "
                f"(step_count={step_count}, ramp_steps={len(ramp_steps)})"
            )

        cfg = {
            "step_count": step_count,
            "ramp_steps": ramp_steps,
            "dac_max": _require_int("dac_max"),
            "rate_tol_ratio": _require_float("rate_tol_ratio"),
            "rate_stable_sec": _require_float("rate_stable_sec"),
            "hold_control_interval_s": _require_float("hold_control_interval_s"),
            "fine_step_dac": _require_int("fine_step_dac"),
            "rate_abort_ratio": _require_float("rate_abort_ratio"),
            "rate_abort_sec": _require_float("rate_abort_sec"),
            "sensor_none_abort_s": _require_float("sensor_none_abort_s"),
            "adc_none_abort_s": _require_float("adc_none_abort_s"),
        }

        if cfg["dac_max"] <= 0:
            raise ValueError("process_config.dac_max는 0보다 커야 합니다.")
        if cfg["rate_tol_ratio"] < 0:
            raise ValueError("process_config.rate_tol_ratio는 0 이상이어야 합니다.")
        if cfg["rate_stable_sec"] < 0:
            raise ValueError("process_config.rate_stable_sec는 0 이상이어야 합니다.")
        if cfg["hold_control_interval_s"] <= 0:
            raise ValueError("process_config.hold_control_interval_s는 0보다 커야 합니다.")
        if cfg["fine_step_dac"] <= 0:
            raise ValueError("process_config.fine_step_dac는 0보다 커야 합니다.")
        if cfg["rate_abort_ratio"] < 0:
            raise ValueError("process_config.rate_abort_ratio는 0 이상이어야 합니다.")
        if cfg["rate_abort_sec"] < 0:
            raise ValueError("process_config.rate_abort_sec는 0 이상이어야 합니다.")
        if cfg["sensor_none_abort_s"] < 0:
            raise ValueError("process_config.sensor_none_abort_s는 0 이상이어야 합니다.")
        if cfg["adc_none_abort_s"] < 0:
            raise ValueError("process_config.adc_none_abort_s는 0 이상이어야 합니다.")

        def _optional_int(name: str, default_val: int, min_v: int | None = None) -> int:
            try:
                value = int(proc_src.get(name, default_val))
            except Exception:
                value = int(default_val)
            if min_v is not None:
                value = max(min_v, value)
            return value

        def _optional_float(name: str, default_val: float, min_v: float | None = None, max_v: float | None = None) -> float:
            try:
                value = float(proc_src.get(name, default_val))
            except Exception:
                value = float(default_val)
            if min_v is not None:
                value = max(min_v, value)
            if max_v is not None:
                value = min(max_v, value)
            return value

        hold_max_dac_delta = _optional_int("hold_max_dac_delta", cfg["fine_step_dac"], 1)
        hold_pi_ki = _optional_float("hold_pi_ki", max(0.0, hold_max_dac_delta * 0.8), 0.0)
        hold_control_mode = str(proc_src.get("hold_control_mode", "PID") or "").strip().upper() or "PID"
        if hold_control_mode not in {"PI", "PID", "STEP"}:
            hold_control_mode = "PID"

        cfg.update({
            "hold_control_mode": hold_control_mode,
            "hold_pi_kp": _optional_float("hold_pi_kp", max(1.0, hold_max_dac_delta * 5.0), 0.0),
            "hold_pi_ki": hold_pi_ki,
            "hold_pi_kd": max(0.0, float(proc_src.get("hold_pi_kd", 0.0) or 0.0)),
            "hold_integral_limit": _optional_float(
                "hold_integral_limit",
                max(1.0, (2.0 * hold_max_dac_delta) / max(hold_pi_ki, 1e-6)),
                0.1,
            ),
            "rate_filter_alpha": _optional_float("rate_filter_alpha", 0.35, 0.01, 1.0),
            "rate_jump_guard_ratio": _optional_float("rate_jump_guard_ratio", 0.50, 0.0),
            "rate_jump_guard_abs": _optional_float("rate_jump_guard_abs", 0.15, 0.0),
            "hold_max_dac_delta": hold_max_dac_delta,
            "spike_abort_ratio": _optional_float("spike_abort_ratio", 3.0, 1.0),
            "spike_grace_s": _optional_float("spike_grace_s", 5.0, 0.0),
            "ramp_spike_pct": _optional_float("ramp_spike_pct", 100.0, 10.0),
            "ramp_spike_abort_sec": _optional_float("ramp_spike_abort_sec", 10.0, 1.0),
            "ramp_spike_abort_ratio": _optional_float("ramp_spike_abort_ratio", 10.0, 1.0),
            "spike_dac_hold_threshold": _optional_float("spike_dac_hold_threshold", 0.3, 0.0),
            "spike_dac_hold_sec": _optional_float("spike_dac_hold_sec", 10.0, 0.0),
            "pre_hold_entry_ratio": _optional_float("pre_hold_entry_ratio", 2.0, 1.0),
            "pre_hold_entry_sec": _optional_float("pre_hold_entry_sec", 5.0, 0.0),
            "pre_hold_ready_ratio": _optional_float("pre_hold_ready_ratio", 0.3, 0.01, 1.0),
            "pre_hold_timeout_sec": _optional_float("pre_hold_timeout_sec", 180.0, 0.0),
        })

        return cfg

    def _build_runtime_evap_meta(
        self,
        *,
        power: dict[str, Any],
        material_cfg: dict[str, Any],
        process_config: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "use_power1": power["use_power1"],
            "use_power2": power["use_power2"],
            "power1_feedback_adc2": power["power1_feedback_adc2"],

            "density": material_cfg["density"],
            "z_factor": material_cfg["z_factor"],
            "target_rate": material_cfg["target_rate"],
            "target_thickness": material_cfg["target_thickness"],
            "delay_min": material_cfg["delay_min"],
            "tooling_factor": material_cfg.get("tooling_factor", 100.0),

            # 핵심: 공정 제어 파라미터는 exact schema로 nested 전달
            "process_config": self._copy_exact_process_config(process_config),
        }
    
    def _copy_exact_process_config(self, process_config: dict[str, Any]) -> dict[str, Any]:
        return {
            "step_count": process_config["step_count"],
            "ramp_steps": process_config["ramp_steps"],
            "dac_max": process_config["dac_max"],
            "rate_tol_ratio": process_config["rate_tol_ratio"],
            "rate_stable_sec": process_config["rate_stable_sec"],
            "hold_control_interval_s": process_config["hold_control_interval_s"],
            "fine_step_dac": process_config["fine_step_dac"],
            "hold_control_mode": process_config["hold_control_mode"],
            "hold_pi_kp": process_config["hold_pi_kp"],
            "hold_pi_ki": process_config["hold_pi_ki"],
            "hold_pi_kd": process_config.get("hold_pi_kd", 0.0),
            "hold_integral_limit": process_config["hold_integral_limit"],
            "rate_filter_alpha": process_config["rate_filter_alpha"],
            "rate_jump_guard_ratio": process_config["rate_jump_guard_ratio"],
            "rate_jump_guard_abs": process_config["rate_jump_guard_abs"],
            "hold_max_dac_delta": process_config["hold_max_dac_delta"],
            "rate_abort_ratio": process_config["rate_abort_ratio"],
            "rate_abort_sec": process_config["rate_abort_sec"],
            "sensor_none_abort_s": process_config["sensor_none_abort_s"],
            "adc_none_abort_s": process_config["adc_none_abort_s"],
            "spike_abort_ratio": process_config.get("spike_abort_ratio", 3.0),
            "spike_grace_s": process_config.get("spike_grace_s", 5.0),
            "ramp_spike_pct": process_config.get("ramp_spike_pct", 100.0),
            "ramp_spike_abort_sec": process_config.get("ramp_spike_abort_sec", 10.0),
            "ramp_spike_abort_ratio": process_config.get("ramp_spike_abort_ratio", 10.0),
            "spike_dac_hold_threshold": process_config.get("spike_dac_hold_threshold", 0.3),
            "spike_dac_hold_sec": process_config.get("spike_dac_hold_sec", 10.0),
            "pre_hold_entry_ratio": process_config.get("pre_hold_entry_ratio", 2.0),
            "pre_hold_entry_sec": process_config.get("pre_hold_entry_sec", 5.0),
            "pre_hold_ready_ratio": process_config.get("pre_hold_ready_ratio", 0.3),
            "pre_hold_timeout_sec": process_config.get("pre_hold_timeout_sec", 180.0),
        }

    def _build_evap_steps(
        self,
        *,
        power: dict[str, Any],
        meta: dict[str, Any],
    ) -> list[ProcessStep]:
        use_p1 = bool(power["use_power1"])
        use_p2 = bool(power["use_power2"])

        if not (use_p1 or use_p2):
            raise ValueError("use_power1/use_power2 중 최소 1개는 True여야 합니다.")

        steps: list[ProcessStep] = [
            ProcessStep(name="MAIN_SHUTTER_CLOSE", type=StepType.PLC_WRITE_COIL, coil="MAIN_SHUTTER_SW", on=False),
            ProcessStep(name="SHUTTER_1_CLOSE_INIT", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_1_SW", on=False),
            ProcessStep(name="SHUTTER_2_CLOSE_INIT", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_2_SW", on=False),

            ProcessStep(name="DAC1_ZERO", type=StepType.PLC_WRITE_REG, reg="DAC_POWER_1", value=0),
            ProcessStep(name="DAC2_ZERO", type=StepType.PLC_WRITE_REG, reg="DAC_POWER_2", value=0),

            ProcessStep(name="POWER1_SET", type=StepType.PLC_WRITE_COIL, coil="POWER_1_SW", on=use_p1),
            ProcessStep(name="POWER2_SET", type=StepType.PLC_WRITE_COIL, coil="POWER_2_SW", on=use_p2),
        ]

        # 현재 UI active path에서는 preflight/start 단계에서 이미 FTM이 ON 되었을 수 있다.
        # 다만 controller 직접 시작 경로를 고려해 여기서는 idempotent ON step으로 유지한다.
        steps.append(ProcessStep(name="FTM_ON", type=StepType.PLC_WRITE_COIL, coil="FTM_SW", on=True))
        steps.append(ProcessStep(name="EVAP_DEPOSITION_CONTROL", type=StepType.MARK, meta=meta))
        return steps
    
    def _sanitize_process_name(self, raw_name: Any, *, fallback_material: str) -> str:
        process_name = str(raw_name or "").strip()
        # 파일 시스템에서 사용 불가한 특수문자 제거 후 공백을 밑줄로 치환
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
    ) -> dict[str, Any]:
        return {
            "process_name": process_name,
            "material_name": material_cfg["material_name"],
            "use_power1": power["use_power1"],
            "use_power2": power["use_power2"],
            "hw_mapping": {
                "temp_force_power2_sw": power["temp_force_power2_sw"],
                "power1_feedback_adc2": power["power1_feedback_adc2"],
            },
            "target_rate": material_cfg["target_rate"],
            "target_thickness": material_cfg["target_thickness"],
            "delay_min": material_cfg["delay_min"],
            "process_config": self._copy_exact_process_config(process_config),
        }

    @staticmethod
    def _to_int_or_none(value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return int(round(float(value)))
        except Exception:
            return None

    def _extract_runtime_recommendation(
        self,
        run_cfg: dict[str, Any],
        *,
        process_config: dict[str, Any],
    ) -> dict[str, Any]:
        return {}
        
    def _extract_power_mode(self, run_cfg: dict[str, Any]) -> dict[str, Any]:
        use_p1 = bool(run_cfg.get("use_power1", False))
        use_p2 = bool(run_cfg.get("use_power2", False))

        if not (use_p1 or use_p2):
            raise ValueError("Power1/Power2 중 최소 1개는 선택되어야 합니다.")

        # 장비 수리 완료 - Power1/Power2 정상 매핑
        temp_force_power2_sw = False
        power1_feedback_adc2 = False

        return {
            "use_power1": use_p1,
            "use_power2": use_p2,
            "temp_force_power2_sw": temp_force_power2_sw,
            "power1_feedback_adc2": power1_feedback_adc2,
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
        
        # ✅ Main Valve(M/V) 개방 인터락: M/V가 실제로 열려 있을 때만 공정 시작
        #    (MV_SW AND MV_interlock == 실제 M/V 출력 P00043)
        if not self.is_main_valve_open():
            self._ui_warn(
                "Main Valve(M/V)가 열려 있지 않아 공정을 시작할 수 없습니다.\n"
                "진공 시퀀스로 M/V를 먼저 개방한 뒤 다시 시도하세요."
            )
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

        # finished 시 워커 참조 정리 + Qt 메모리 관리를 위해 deleteLater도 연결
        w.finished.connect(self._on_worker_finished)
        w.finished.connect(w.deleteLater)

        if hasattr(self.plc, "set_process_logging"):
            self.plc.set_process_logging(True)

        # Google Chat: 공정 시작 알림 (실패해도 공정 흐름에 영향 없음)
        try:
            if self._notifier is not None and r is not None:
                params = self._extract_notifier_params_from_recipe(r)
                self._notifier.notify_process_started(params)
        except Exception:
            pass

        # start/preflight UI 흐름은 ProcessWindow가 담당한다.
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

    # --------------------------------------------------------
    # Internal helpers
    # --------------------------------------------------------
    def _request_engine_stop(self, mode: StopMode) -> None:
        if not self.is_running():
            self._ui_warn("실행 중인 공정이 없습니다.")
            return
        try:
            self._worker.request_stop(mode)
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
            notifier=self._notifier,
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
        
    def is_main_valve_open(self) -> bool:
        """
        공정 시작 전, Main Valve(M/V)가 실제로 '열림' 상태인지 확인.
        - PLC 래더 기준 실제 M/V 출력(P00043) = MV_SW(M00003) AND MV_interlock(M00102).
        - 따라서 두 코일이 모두 True일 때만 '열림'으로 판정한다.
        - 스냅샷이 없거나 코일 정보가 비어 있으면(상태 확인 불가) 안전하게 False 반환.
        """
        try:
            snap = self.plc.get_last_snapshot() if hasattr(self.plc, "get_last_snapshot") else None
            if snap is None:
                return False
            coils = getattr(snap, "coils", None)
            if not coils:
                return False
            mv_sw = bool(coils.get("M_V_SW", False))
            mv_interlock = bool(coils.get("MV_INTERLOCK", False))
            return mv_sw and mv_interlock
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

    def _on_worker_result(self, result: EngineResult) -> None:
        """
        엔진 실행 결과 수신.
        - finished status/log/run close는 ProcessWindow가 담당한다.
        """
        # Google Chat: 공정 종료 알림 (실패해도 공정 흐름에 영향 없음)
        try:
            if self._notifier is not None:
                detail = self._build_finish_detail_from_result(result)
                self._notifier.notify_process_finished_detail(
                    ok=bool(result.ok), detail=detail
                )
        except Exception:
            pass

        self.sig_finished.emit(result)

    def _extract_notifier_params_from_recipe(self, recipe: ProcessRecipe) -> dict:
        """Recipe.meta 에서 Google Chat 알림용 파라미터 추출."""
        params: dict = {}
        meta = getattr(recipe, "meta", None) or {}
        if isinstance(meta, dict):
            for key in (
                "process_name",
                "recipe_name",
                "material_name",
                "target_rate",
                "target_thickness",
                "delay_min",
                "use_power1",
                "use_power2",
            ):
                if key in meta:
                    params[key] = meta[key]

        if "process_name" not in params:
            name = getattr(recipe, "recipe_name", None)
            if name:
                params["process_name"] = name

        return params

    def _build_finish_detail_from_result(self, result: EngineResult) -> dict:
        """EngineResult 에서 알림 payload 에 담을 detail 구성."""
        detail: dict = {
            "recipe_name": getattr(result, "recipe_name", ""),
        }
        try:
            started = float(getattr(result, "started_ts", 0.0) or 0.0)
            finished = float(getattr(result, "finished_ts", 0.0) or 0.0)
            detail["elapsed_sec"] = max(0.0, finished - started)
        except Exception:
            pass

        err = getattr(result, "error", None)
        if err is not None:
            message = str(getattr(err, "message", "") or "")
            where = str(getattr(err, "where", "") or "")
            if not message:
                message = str(getattr(err, "exception_repr", "") or repr(err))
            detail["error_message"] = message
            detail["phase"] = where
            detail["errors"] = [message] if message else []

        # ok=False이고 error가 없으면 사용자가 Stop 버튼을 눌러 중지한 것
        if not getattr(result, "ok", True) and err is None:
            detail["is_user_stop"] = True

        return detail

    def shutdown_notifier(self) -> None:
        """프로그램 종료 시 호출(main.py closeEvent/aboutToQuit 등)."""
        try:
            if self._notifier is not None:
                self._notifier.shutdown()
        except Exception:
            pass

    def _on_worker_finished(self) -> None:
        """
        워커 스레드 종료 cleanup.
        - run log lifecycle은 ProcessWindow가 담당한다.
        """
        if hasattr(self.plc, "set_process_logging"):
            self.plc.set_process_logging(False)

        self._worker = None

    def _emit_process_trace_log(self, *, tag: str, msg: str, ok: bool = True) -> None:
        text = str(msg or "").strip()
        if not text:
            return

        try:
            if ok:
                self.log.info(text, tag=tag, also_ui=True)
            else:
                self.log.warn(text, tag=tag, also_ui=True)
        except Exception:
            prefix = f"[{tag}]"
            if not ok:
                prefix += "[WARN]"
            self.sig_ui_log.emit(f"{prefix} {text}")

    @staticmethod
    def _strip_device_prefix(msg: str, tag: str) -> str:
        text = str(msg or "").strip()
        if not text:
            return ""
        pattern = rf"^(?:\[{re.escape(str(tag or '').strip())}\]\s*)+"
        return re.sub(pattern, "", text, flags=re.IGNORECASE).strip()

    @classmethod
    def _trim_trace_prefix(cls, msg: str, prefix: str) -> str:
        return cls._strip_device_prefix(msg, prefix)

    @staticmethod
    def _format_pressure_torr(value: Any) -> str:
        try:
            return f"{float(value):.3e} Torr"
        except Exception:
            return f"{value} Torr"

    @staticmethod
    def _extract_named_value(detail: str, key: str) -> Optional[str]:
        m = re.search(rf"{re.escape(key)}\s*=\s*([^\s|,]+)", str(detail or ""))
        if not m:
            return None
        return str(m.group(1) or "").strip()

    @staticmethod
    def _extract_detail_paren_text(detail: str) -> str:
        m = re.search(r"\(([^()]*)\)", str(detail or ""))
        if not m:
            return ""
        return str(m.group(1) or "").strip()

    @staticmethod
    def _stm_rx_body(rx: str) -> str:
        body = str(rx or "").strip()
        if body[:1].upper() in ("A", "B") and len(body) > 1:
            body = body[1:].strip()
        return body

    def _format_plc_trace_message(self, d: dict[str, Any]) -> str:
        cmd = str(d.get("event", "") or "")
        target = str(d.get("target", "") or "")
        detail = str(d.get("detail", "") or "").strip()
        value = d.get("value", "")
        tag = str(d.get("tag", "") or "").strip()
        target_upper = target.upper()

        pulse_ms = self._extract_named_value(detail, "pulse_ms")
        # ADC_READBACK는 폴링으로 자주 발생하므로 UI 로그에 표시하지 않음
        if cmd == "ADC_READBACK":
            return ""

        if cmd == "CmdWriteCoil":
            label = _COIL_LABEL.get(target, target)
            if pulse_ms:
                msg = f"{label} PULSE"
                if pulse_ms.isdigit():
                    msg += f" ({pulse_ms} ms)"
            else:
                state = "ON" if bool(value) else "OFF"
                msg = f"{label} {state}"
        elif cmd == "CmdWriteReg" and target_upper in ("DAC_POWER_1", "DAC_POWER_2"):
            ch = "1" if target_upper.endswith("_1") else "2"
            try:
                msg = f"DAC {ch} -> {int(round(float(value)))}"
            except Exception:
                msg = f"DAC {ch} -> {value}"
        elif cmd == "CmdWriteReg":
            msg = f"{target_upper or target} -> {value}"
        elif cmd == "CmdSetDacCurrent":
            ch = target.replace("DAC_CH", "").strip() or target
            msg = f"DAC 전류 CH{ch} -> {value} mA"
        else:
            raw_msg = self._trim_trace_prefix(str(d.get("msg", "") or ""), "PLC")
            msg = raw_msg or f"{cmd or 'PLC'} {target} {value}".strip()

        extra_parts: list[str] = []
        cleaned_detail = detail
        if pulse_ms:
            cleaned_detail = re.sub(r"(?:^|,)\s*(?:momentary=1,?)?\s*pulse_ms=\d+\s*", "", cleaned_detail).strip(" ,|")
        if cleaned_detail:
            extra_parts.append(cleaned_detail)
        if tag and tag not in ("POLL",):
            extra_parts.append(f"tag={tag}")
        if extra_parts:
            msg = f"{msg} ({'; '.join(extra_parts)})"
        return msg

    def _format_stm_trace_message(self, d: dict[str, Any]) -> str:
        tx = str(d.get("tx", "") or "")
        rx = str(d.get("rx", "") or "")
        detail = str(d.get("detail", "") or "").strip()
        token = tx[:1].upper() if tx else ""
        body = self._stm_rx_body(rx)

        if token == "U":
            try:
                hz = int(body)
                return f"Sensor frequency {hz:,} Hz ({hz / 1_000_000.0:.6f} MHz)"
            except Exception:
                pass
        elif token == "V":
            try:
                return f"Crystal life {float(body):.1f}%"
            except Exception:
                pass

        raw_msg = self._trim_trace_prefix(str(d.get("msg", "") or ""), "STM")
        if raw_msg:
            return raw_msg

        msg = f"명령 {tx}" if tx else "STM 응답"
        if body:
            msg += f" -> {body}"
        elif rx:
            msg += f" -> {rx}"
        if detail:
            msg += f" ({detail})"
        return msg

    def _format_stm_error_message(self, s: str) -> str:
        text = str(s or "").strip()
        text = re.sub(r"^\[STMService\]\s*", "", text)
        lower = text.lower()

        if "connect failed" in lower:
            return f"연결 실패: {text}"
        if "poll failed" in lower:
            return f"측정값 읽기 실패: {text}"
        if "apply_material_params failed" in lower:
            return f"막 재료 파라미터 적용 실패: {text}"
        if "zero_thickness failed" in lower:
            return f"두께 초기화 실패: {text}"
        if "read_crystal_health failed" in lower:
            return f"Crystal health 확인 실패: {text}"
        if "reload ini" in lower:
            return f"설정 다시 읽음: {text}"
        return text or "STM 오류"

    def _format_acs_trace_message(self, d: dict[str, Any]) -> str:
        token = str(d.get("token", "") or "").strip().upper()
        detail = str(d.get("detail", "") or "").strip()
        tx = str(d.get("tx", "") or "").strip()
        rx = str(d.get("rx", "") or "").strip()

        pressure_text = self._extract_named_value(detail, "pressure")
        if pressure_text is not None:
            return f"챔버 압력 {self._format_pressure_torr(pressure_text)}"

        if token == "CON_STATUS":
            state = self._extract_detail_paren_text(detail) or detail
            return f"게이지 상태 {state}".strip()
        if token == "CON_STREAM" and detail:
            return f"스트림 상태 {detail}"
        if token.endswith("_PRESSURE") and rx:
            return f"챔버 압력 {self._format_pressure_torr(rx)}"

        raw_msg = self._trim_trace_prefix(str(d.get("msg", "") or ""), "ACS")
        if raw_msg:
            return raw_msg

        lead = token or tx or "ACS"
        if detail:
            return f"{lead}: {detail}"
        if rx:
            return f"{lead}: {rx}"
        return lead

    def _on_plc_cmd_trace(self, obj: object) -> None:
        try:
            d = dict(obj or {})
        except Exception:
            return

        # HOLD 구간 DAC 변경 로그는 [POLL]에 통합되어 있으므로 억제
        # ok=True인 HOLD_CONTROL CmdWriteReg만 억제, 실패 시에는 유지
        _tag_str = str(d.get("tag", "") or "").strip()
        if (
            "HOLD_CONTROL" in _tag_str
            and str(d.get("event", "")) == "CmdWriteReg"
            and bool(d.get("ok", True))
        ):
            return

        msg = self._strip_device_prefix(d.get("msg", ""), "PLC")
        if not msg:
            msg = self._format_plc_trace_message(d)
        if not msg:
            return

        self._emit_process_trace_log(
            tag="PLC",
            msg=msg,
            ok=bool(d.get("ok", True)),
        )

    def _on_stm_io_trace(self, obj: object) -> None:
        try:
            d = dict(obj or {})
        except Exception:
            return

        token = (str(d.get("tx", "") or "")[:1].upper() if d.get("tx") else "")
        if token in ("S", "T"):
            return

        msg = self._strip_device_prefix(d.get("msg", ""), "STM")
        if not msg:
            msg = self._format_stm_trace_message(d)
        if not msg:
            return

        self._emit_process_trace_log(
            tag="STM",
            msg=msg,
            ok=bool(d.get("ok", True)),
        )

    def _on_stm_connected(self, connected: bool) -> None:
        self._emit_process_trace_log(
            tag="STM",
            msg=("연결됨" if connected else "연결 해제"),
            ok=bool(connected),
        )

    def _on_stm_error(self, s: str) -> None:
        text = re.sub(r"^\[STMService\]\s*", "", str(s or "").strip())
        self._emit_process_trace_log(
            tag="STM",
            msg=self._strip_device_prefix(self._format_stm_error_message(text), "STM"),
            ok=False,
        )

    def _on_acs_io_trace(self, obj: object) -> None:
        if not self.is_running():
            return

        try:
            d = dict(obj or {})
        except Exception:
            return

        # acs_service에서 msg=""로 suppression한 경우 → fallback 없이 바로 return
        # (정상 polling trace는 [POLL] 통합 로그로 대체됨)
        raw_msg = d.get("msg", None)
        if raw_msg is not None and str(raw_msg).strip() == "":
            return

        msg = self._strip_device_prefix(raw_msg or "", "ACS")
        if not msg:
            msg = self._format_acs_trace_message(d)
        if not msg:
            return

        self._emit_process_trace_log(
            tag="ACS",
            msg=msg,
            ok=bool(d.get("ok", True)),
        )

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
