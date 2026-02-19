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
# Import (패키지/단일 실행 둘 다 대응)
# ------------------------------------------------------------
try:
    # 패키지 실행: python -m Evaporator_Program.main 형태
    from ..process.models import (
        ProcessRecipe,
        ProcessError,
        StopMode,
        ProcessStep,
        StepType,
    )
    from ..process.recipe_io import (
        load_recipe,
        save_recipe,
    )
    from ..process.engine import ProcessEngine, EngineResult
    from ..services.plc_service import PLCService
    from ..services.log_service import LogService

    from .process_worker import ProcessWorker

except ImportError:
    # 단일 실행/경로 설정이 다른 경우 fallback
    from process.models import (
        ProcessRecipe,
        ProcessError,
        StopMode,
        ProcessStep,   # ✅ 추가
        StepType,      # ✅ 추가
    )
    from process.recipe_io import (
        load_recipe,
        save_recipe,
    )
    from process.engine import ProcessEngine, EngineResult
    from services.plc_service import PLCService
    from services.log_service import LogService

    # 실행 방식에 따라 controller.process_worker 또는 process_worker로 접근될 수 있음
    try:
        from controller.process_worker import ProcessWorker
    except ImportError:
        from process_worker import ProcessWorker


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
            self.log.sig_line.connect(self.sig_ui_log)
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
        - 실제 '동적 DAC/dep.rate 제어'는 다음 단계에서 engine.py가 수행
        - 여기서는 엔진이 실행 가능한 형태로 '스켈레톤 레시피'를 만든다.
        """
        use_p1 = bool(run_cfg.get("use_power1", False))
        use_p2 = bool(run_cfg.get("use_power2", False))
        if not (use_p1 or use_p2):
            raise ValueError("Power1/Power2 중 최소 1개는 선택되어야 합니다.")

        material = str(run_cfg.get("material_name", "")).strip() or "UNKNOWN"
        density = float(run_cfg.get("density", 0.0) or 0.0)
        z_factor = float(run_cfg.get("z_factor", 0.0) or 0.0)

        target_rate = float(run_cfg.get("target_rate", 0.0) or 0.0)
        target_th = float(run_cfg.get("target_thickness", 0.0) or 0.0)
        delay_min = float(run_cfg.get("delay_min", 0.0) or 0.0)

        if target_rate <= 0:
            raise ValueError("target_rate는 0보다 커야 합니다.")
        if target_th <= 0:
            raise ValueError("target_thickness는 0보다 커야 합니다.")
        if delay_min < 0:
            raise ValueError("delay_min은 0 이상이어야 합니다.")

        # ✅ 엔진에서 사용할 파라미터는 meta로 넘겨둔다.
        meta = {
            "ui_run": True,
            "material_name": material,
            "density": density,
            "z_factor": z_factor,
            "use_power1": use_p1,
            "use_power2": use_p2,
            "target_rate": target_rate,
            "target_thickness": target_th,
            "delay_min": delay_min,
        }

        steps: list[ProcessStep] = [
            # -------------------------
            # 안전 초기화(시작 시점)
            # -------------------------
            ProcessStep(
                name="MAIN_SHUTTER_CLOSE",
                type=StepType.PLC_WRITE_COIL,
                coil="MAIN_SHUTTER_SW",
                on=False,
            ),
            ProcessStep(
                name="DAC1_0",
                type=StepType.PLC_WRITE_REG,
                reg="DAC_POWER_1",
                value=0,
            ),
            ProcessStep(
                name="DAC2_0",
                type=StepType.PLC_WRITE_REG,
                reg="DAC_POWER_2",
                value=0,
            ),

            # -------------------------
            # POWER 스위치 상태 명확히 세팅(선택 안 했으면 OFF)
            # -------------------------
            ProcessStep(
                name="POWER1_SET",
                type=StepType.PLC_WRITE_COIL,
                coil="POWER_1_SW",
                on=use_p1,
            ),
            ProcessStep(
                name="POWER2_SET",
                type=StepType.PLC_WRITE_COIL,
                coil="POWER_2_SW",
                on=use_p2,
            ),

            # -------------------------
            # STM 관련(다음 단계 구현 예정: 엔진/STMService에서 처리)
            # -------------------------
            ProcessStep(
                name="STM_SET_FILM_PARAMS",
                type=StepType.MARK,
                meta={"density": density, "z_factor": z_factor, "material_name": material},
            ),
            ProcessStep(
                name="STM_ZERO_THICKNESS",
                type=StepType.MARK,
                meta={},
            ),

            # -------------------------
            # EVAP 메인 루프(엔진에서 실행)
            # -------------------------
            ProcessStep(
                name="EVAP_DEPOSITION_CONTROL",
                type=StepType.MARK,
                meta=meta,
            ),
        ]

        recipe = ProcessRecipe(
            recipe_name=f"UI_{material}",
            version=1,
            meta=meta,
            steps=steps,
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
        안전 출력:
        1) MAIN_SHUTTER close
        2) DAC 0
        3) POWER off
        - UI thread에서도 blocking 없이 enqueue로만 수행
        """
        # 서비스가 안 떠 있으면 올려둔다(큐가 살아 있어야 명령이 들어감)
        try:
            self._ensure_services_running()
        except Exception:
            pass

        # ✅ 요청 순서 보장: enqueue는 워커 큐 순서를 그대로 탄다.
        try:
            self.plc.enqueue_write_coil("MAIN_SHUTTER_SW", False, tag=tag)
        except Exception as e:
            self._ui_warn(f"MAIN_SHUTTER close 실패(enqueue): {e!r}")

        try:
            self.plc.enqueue_write_reg("DAC_POWER_1", 0, tag=tag)
            self.plc.enqueue_write_reg("DAC_POWER_2", 0, tag=tag)
        except Exception as e:
            self._ui_warn(f"DAC=0 실패(enqueue): {e!r}")

        try:
            self.plc.enqueue_write_coil("POWER_1_SW", False, tag=tag)
            self.plc.enqueue_write_coil("POWER_2_SW", False, tag=tag)
        except Exception as e:
            self._ui_warn(f"POWER OFF 실패(enqueue): {e!r}")

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
