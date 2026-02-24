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

from pathlib import Path
from typing import Optional, Union, Any

from PySide6.QtCore import QObject, Signal


# ------------------------------------------------------------
# Import (패키지 실행만 지원)
# ------------------------------------------------------------
from process.models import (
    ProcessRecipe,
    ProcessError,
    StopMode,
    ProcessStep,
    StepType,
)
from process.recipe_io import load_recipe, save_recipe
from process.engine import ProcessEngine, EngineResult
from services.plc_service import PLCService
from services.log_service import LogService

from controller.process_worker import ProcessWorker


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
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)

        self.plc = plc
        self.stm = stm
        self.acs = acs
        self.log = log

        self._recipe: Optional[ProcessRecipe] = None
        self._recipe_path: Optional[Path] = None

        self._worker: Optional[ProcessWorker] = None

        # log_service -> controller -> UI
        try:
            self.log.sig_line.connect(self.sig_ui_log.emit)
            self.log.sig_error.connect(lambda s: self.sig_ui_log.emit(f"[LOG_ERR] {s}"))
        except Exception:
            pass

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
    
    def build_recipe_from_ui(self, run_cfg: dict[str, Any]) -> ProcessRecipe:
        """
        UI 입력(run_cfg)을 ProcessRecipe로 변환.

        ✅ 2파워 동시 사용 지원(동일 물질 가정)
        - Power는 1개 또는 2개 허용(둘 다 해제 금지)
        - 채널별 Target Dep.rate 필수:
            use_power1이면 target_rate_1 > 0
            use_power2이면 target_rate_2 > 0
        - 선택된 채널 source shutter open + FTM ON 이후 1.5초 대기
        - 실제 램프/dep.rate 제어는 engine.py의 _evap_deposition_control()에서 수행(엔진 수정 필요)
        """
        use_p1 = bool(run_cfg.get("use_power1", False))
        use_p2 = bool(run_cfg.get("use_power2", False))

        if not (use_p1 or use_p2):
            raise ValueError("Power1/Power2 중 최소 1개는 선택되어야 합니다.")

        material = str(run_cfg.get("material_name", "")).strip()
        if not material:
            raise ValueError("material_name은 필수입니다.")

        density = float(run_cfg.get("density", 0.0) or 0.0)
        z_factor = float(run_cfg.get("z_factor", 0.0) or 0.0)
        if density <= 0 or z_factor <= 0:
            raise ValueError("density / z_factor 값이 올바르지 않습니다. (0보다 커야 함)")

        # ✅ dep.rate는 1개만 사용 (두 파워는 동일 DAC로 같이 제어)
        target_rate = float(run_cfg.get("target_rate", 0.0) or 0.0)

        # (안전) main.py가 아직 target_rate_1/2 또는 total만 넘기는 경우를 대비한 최소 fallback
        # - main.py를 target_rate 하나로 통일하면 아래 fallback은 삭제해도 됨
        if target_rate <= 0:
            target_rate = float(run_cfg.get("target_rate_total", 0.0) or 0.0)

        if target_rate <= 0:
            r1 = float(run_cfg.get("target_rate_1", 0.0) or 0.0)
            r2 = float(run_cfg.get("target_rate_2", 0.0) or 0.0)
            target_rate = r1 if r1 > 0 else r2

        if target_rate <= 0:
            raise ValueError("target_rate(목표 Dep.rate)는 0보다 커야 합니다.")

        target_th = float(run_cfg.get("target_thickness", 0.0) or 0.0)
        delay_min = float(run_cfg.get("delay_min", 0.0) or 0.0)

        if target_th <= 0:
            raise ValueError("target_thickness는 0보다 커야 합니다.")
        if delay_min < 0:
            raise ValueError("delay_min은 0 이상이어야 합니다.")

        # ✅ 호환 제거: 단일 coil 키 제거하고 리스트만 사용
        source_shutter_coils: list[str] = []
        if use_p1:
            source_shutter_coils.append("SHUTTER_1_SW")
        if use_p2:
            source_shutter_coils.append("SHUTTER_2_SW")

        meta = {
            # --- 공정 기본 ---
            "use_power1": use_p1,
            "use_power2": use_p2,
            "material_name": material,
            "density": density,
            "z_factor": z_factor,

            "target_rate": target_rate,

            "target_thickness": target_th,
            "delay_min": delay_min,

            # --- 안전/제한 ---
            "dac_max": 4000,
            "sensor_none_abort_s": 5.0,

            # --- 램프업 파라미터 ---
            "ramp_step_dac": 100,
            "fine_step_dac": 50,
            "ramp_fast_until_dac": 1000,
            "ramp_interval_fast_s": 1.0,
            "ramp_interval_slow_s": 10.0,

            # --- 목표 도달/보정 ---
            "rate_tol_ratio": 0.05,
            "rate_drop_ratio": 0.30,
            "rate_drop_count": 3,

            # --- pre-rate 홀드 ---
            "pre_rate": 0.4,
            "pre_hold_s": 120.0,

            # --- stuck 가드 ---
            "stuck_dac_guard": 1500,
            "stuck_rate_abs": 0.05,
            "stuck_time_s": 60.0,

            # --- shutter delay 후 STM zero 모드 ---
            "zero_mode": "B",

            # --- FTM/STM 안정화 ---
            "wait_after_ftm_on_s": 1.5,

            # ✅ 셔터 리스트만
            "source_shutter_coils": source_shutter_coils,
        }

        # ✅ steps는 리스트 만든 다음 조건부 append로 구성(문법 오류 방지)
        steps: list[ProcessStep] = [
            # 0) 안전 초기화
            ProcessStep(name="MAIN_SHUTTER_CLOSE", type=StepType.PLC_WRITE_COIL, coil="MAIN_SHUTTER_SW", on=False),
            ProcessStep(name="SHUTTER_1_CLOSE_INIT", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_1_SW", on=False),
            ProcessStep(name="SHUTTER_2_CLOSE_INIT", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_2_SW", on=False),

            # 1) DAC 0
            ProcessStep(name="DAC1_ZERO", type=StepType.PLC_WRITE_REG, reg="DAC_POWER_1", value=0),
            ProcessStep(name="DAC2_ZERO", type=StepType.PLC_WRITE_REG, reg="DAC_POWER_2", value=0),

            # 2) 선택된 파워 ON
            ProcessStep(name="POWER1_SET", type=StepType.PLC_WRITE_COIL, coil="POWER_1_SW", on=use_p1),
            ProcessStep(name="POWER2_SET", type=StepType.PLC_WRITE_COIL, coil="POWER_2_SW", on=use_p2),
        ]

        # 3) 선택된 source shutter open
        if use_p1:
            steps.append(ProcessStep(name="SHUTTER_1_OPEN", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_1_SW", on=True))
        if use_p2:
            steps.append(ProcessStep(name="SHUTTER_2_OPEN", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_2_SW", on=True))

        # 4) FTM ON + 1.5초 대기
        steps.append(ProcessStep(name="FTM_ON", type=StepType.PLC_WRITE_COIL, coil="FTM_SW", on=True))
        steps.append(ProcessStep(name="WAIT_FTM_STM", type=StepType.WAIT_SECONDS, seconds=float(meta["wait_after_ftm_on_s"])))

        # 5) deposition 제어(엔진 내부 루프)
        steps.append(ProcessStep(name="EVAP_DEPOSITION_CONTROL", type=StepType.MARK, meta=meta))

        # 6) 후처리(best-effort): shutter close + FTM OFF
        #    (안전상 둘 다 닫아도 무해하지만, 선택된 것만 닫게 유지)
        if use_p1:
            steps.append(ProcessStep(name="SHUTTER_1_CLOSE", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_1_SW", on=False))
        if use_p2:
            steps.append(ProcessStep(name="SHUTTER_2_CLOSE", type=StepType.PLC_WRITE_COIL, coil="SHUTTER_2_SW", on=False))
        steps.append(ProcessStep(name="FTM_OFF", type=StepType.PLC_WRITE_COIL, coil="FTM_SW", on=False))

        recipe = ProcessRecipe(
            recipe_name=f"EVAP_{material}",
            steps=steps,
            telemetry_interval_s=1.0,
            meta={
                "material_name": material,
                "use_power1": use_p1,
                "use_power2": use_p2,

                "target_rate": target_rate,

                "target_thickness": target_th,
                "delay_min": delay_min,
            },
        )
        recipe.validate(strict=True)
        return recipe

    def start_from_ui(self, run_cfg: dict[str, Any], *, run_id: Optional[str] = None) -> None:
        """
        UI 입력값으로 공정 시작.
        """
        r = self.build_recipe_from_ui(run_cfg)
        self.set_recipe(r)
        self.start(run_id=run_id, recipe=r)

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
        self._ensure_services_running()

        # 엔진 구성
        engine = ProcessEngine(
            plc=self.plc,
            stm=self.stm,
            acs=self.acs,
            log=self.log,
            callbacks=None,  # worker에서 콜백 브릿지로 교체됨
        )

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
        정상 정지(안전정지).
        - 공정 실행 중이 아니어도 '안전 출력'은 항상 수행
        """
        self._issue_safe_stop_outputs(tag="STOP_BTN")
        if self.is_running():
            self._request_engine_stop(StopMode.STOP)
        else:
            self._ui_info("실행 중 공정 없음 → 안전 출력만 수행")

    def abort(self) -> None:
        self._issue_safe_stop_outputs(tag="ABORT_BTN")
        if self.is_running():
            self._request_engine_stop(StopMode.ABORT)
        else:
            self._ui_info("실행 중 공정 없음 → 안전 출력만 수행")


    def estop(self) -> None:
        self._issue_safe_stop_outputs(tag="ESTOP_BTN")
        if self.is_running():
            self._request_engine_stop(StopMode.ESTOP)
        else:
            self._ui_info("실행 중 공정 없음 → 안전 출력만 수행")

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
    def _issue_safe_stop_outputs(self, *, tag: str = "SAFE_STOP") -> None:
        """
        안전 출력(best-effort, enqueue만 사용):
        1) MAIN_SHUTTER close
        2) SOURCE SHUTTER close (1/2)
        3) DAC 0
        4) POWER off
        5) FTM off
        """
        try:
            self._ensure_services_running()
        except Exception:
            pass

        # 1) Shutters close
        for coil in ("MAIN_SHUTTER_SW", "SHUTTER_1_SW", "SHUTTER_2_SW"):
            try:
                self.plc.enqueue_write_coil(coil, False, tag=tag)
                self._csv_event(event="WRITE_COIL", target=coil, value=0, detail=f"ENQ tag={tag}")
            except Exception as e:
                self._csv_event(event="WRITE_COIL", target=coil, value=0, detail=f"ERR tag={tag} {e!r}")
                self._ui_warn(f"{coil} close 실패(enqueue): {e!r}")

        # 2) DAC=0
        for reg in ("DAC_POWER_1", "DAC_POWER_2"):
            try:
                self.plc.enqueue_write_reg(reg, 0, tag=tag)
                self._csv_event(event="SET_DAC", target=reg, value=0, detail=f"ENQ tag={tag}")
            except Exception as e:
                self._csv_event(event="SET_DAC", target=reg, value=0, detail=f"ERR tag={tag} {e!r}")
                self._ui_warn(f"{reg} 0 실패(enqueue): {e!r}")

        # 3) Power off
        for coil in ("POWER_1_SW", "POWER_2_SW"):
            try:
                self.plc.enqueue_write_coil(coil, False, tag=tag)
                self._csv_event(event="WRITE_COIL", target=coil, value=0, detail=f"ENQ tag={tag}")
            except Exception as e:
                self._csv_event(event="WRITE_COIL", target=coil, value=0, detail=f"ERR tag={tag} {e!r}")
                self._ui_warn(f"{coil} off 실패(enqueue): {e!r}")

        # 4) FTM off
        try:
            self.plc.enqueue_write_coil("FTM_SW", False, tag=tag)
            self._csv_event(event="WRITE_COIL", target="FTM_SW", value=0, detail=f"ENQ tag={tag}")
        except Exception as e:
            self._csv_event(event="WRITE_COIL", target="FTM_SW", value=0, detail=f"ERR tag={tag} {e!r}")
            self._ui_warn(f"FTM off 실패(enqueue): {e!r}")

    def _request_engine_stop(self, mode: StopMode) -> None:
        if not self.is_running():
            self._ui_warn("실행 중인 공정이 없습니다.")
            return
        try:
            self._worker.request_stop(mode)
            self._ui_warn(f"정지 요청: {mode.value}")
        except Exception as e:
            self._ui_warn(f"stop 요청 실패: {e!r}")

    def _ensure_services_running(self) -> None:
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

        # STM/ACS는 옵션
        if self.stm is not None:
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

    def _csv_event(self, *, event: str, target: str, value: Any = "", detail: str = "") -> None:
        """
        공정 CSV(telemetry)에 이벤트 1줄 추가.
        - 공정이 실행 중일 때만 의미가 있음(=run open 상태에서만 기록됨)
        """
        if not self.is_running():
            return
        try:
            self.log.telemetry({
                "step": f"CTRL:{event}",   # step을 컨트롤러 이벤트로 표시
                "event": str(event),
                "target": str(target),
                "value": value,
                "detail": str(detail),
            })
        except Exception:
            pass

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
