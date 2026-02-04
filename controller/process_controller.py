# -*- coding: utf-8 -*-
"""
controller/process_controller.py

ProcessController
- UI에서 공정 시작/정지/일시정지/재개/레시피 로드/저장을 담당하는 상위 컨트롤러
- ProcessEngine은 QThread에서 돌린다(Worker 내부에서 engine.run 호출)

주의
- UI 위젯을 직접 접근하지 않는다(=signals로만 상태 전달)
- PLC/센서 서비스는 외부에서 주입(inject)받는 형태를 기본으로 한다.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Optional, Union, Any, Dict

from PySide6.QtCore import QObject, QThread, Signal


# ------------------------------------------------------------
# Import (패키지/단일 실행 둘 다 대응)
# ------------------------------------------------------------
try:
    # 패키지 실행: python -m Evaporator_Program.main 형태
    from ..process.models import (
        ProcessRecipe,
        ProcessStatus,
        StepStatus,
        ProcessError,
        StopMode,
        ProcessPhase,
    )
    from ..process.recipe_io import load_recipe, save_recipe, write_recipe_csv_template, make_sample_recipe
    from ..process.engine import ProcessEngine, EngineCallbacks, EngineResult
    from ..services.plc_service import PLCService
    from ..services.log_service import LogService
    # STM/ACS는 옵션일 수 있음
    from ..services.stm_service import STMSnapshot  # type: ignore
    from ..services.acs_service import ACSSnapshot  # type: ignore
except Exception:
    # 단일 실행/경로 설정이 다른 경우 fallback
    from process.models import (
        ProcessRecipe,
        ProcessStatus,
        StepStatus,
        ProcessError,
        StopMode,
        ProcessPhase,
    )
    from process.recipe_io import load_recipe, save_recipe, write_recipe_csv_template, make_sample_recipe
    from process.engine import ProcessEngine, EngineCallbacks, EngineResult
    from services.plc_service import PLCService
    from services.log_service import LogService
    from services.stm_service import STMSnapshot  # type: ignore
    from services.acs_service import ACSSnapshot  # type: ignore


# ============================================================
# Engine Worker (QThread)
# ============================================================

class _EngineWorker(QThread):
    """
    엔진을 별도 스레드에서 실행하는 워커.
    - engine callbacks -> worker signals로 브릿지
    """

    sig_status = Signal(object)   # ProcessStatus
    sig_step = Signal(object)     # StepStatus
    sig_error = Signal(object)    # ProcessError
    sig_result = Signal(object)   # EngineResult

    def __init__(
        self,
        engine: ProcessEngine,
        recipe: ProcessRecipe,
        *,
        run_id: Optional[str] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._engine = engine
        self._recipe = recipe
        self._run_id = run_id

        # callbacks를 worker 시그널로 연결
        self._engine.callbacks = EngineCallbacks(
            on_status=lambda st: self.sig_status.emit(st),
            on_step=lambda st: self.sig_step.emit(st),
            on_error=lambda err: self.sig_error.emit(err),
        )

    @property
    def engine(self) -> ProcessEngine:
        return self._engine

    def run(self) -> None:
        # 엔진은 블로킹 실행
        result = self._engine.run(self._recipe, run_id=self._run_id)
        self.sig_result.emit(result)


# ============================================================
# Process Controller
# ============================================================

class ProcessController(QObject):
    """
    UI가 직접 사용하는 컨트롤러.

    Signals:
    - sig_ui_log(str)            : UI 로그창에 append 할 문자열(가공된 한 줄)
    - sig_status(ProcessStatus)  : 공정 상태(현재 step, phase, 센서값 포함)
    - sig_step(StepStatus)       : step 시작/종료 이벤트(진행표/히스토리용)
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

        self._worker: Optional[_EngineWorker] = None

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

    def make_and_set_sample_recipe(self) -> ProcessRecipe:
        r = make_sample_recipe()
        self.set_recipe(r)
        return r

    def write_csv_template(self, path: Union[str, Path]) -> None:
        write_recipe_csv_template(path)
        self._ui_info(f"CSV 템플릿 생성: {path}")

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
            callbacks=None,  # worker에서 교체
        )

        # 워커 생성
        w = _EngineWorker(engine=engine, recipe=r, run_id=run_id)
        self._worker = w

        # 워커 -> 컨트롤러 시그널 연결
        w.sig_status.connect(self.sig_status)
        w.sig_step.connect(self.sig_step)
        w.sig_error.connect(self.sig_error)
        w.sig_result.connect(self._on_worker_result)

        # 워커 종료 처리
        w.finished.connect(self._on_worker_finished)

        self._ui_info("공정 시작 요청")
        w.start()

    def stop(self) -> None:
        """정상 정지(안전정지 의도)"""
        self._request_engine_stop(StopMode.STOP)

    def abort(self) -> None:
        """빠른 중단/에러 대응"""
        self._request_engine_stop(StopMode.ABORT)

    def estop(self) -> None:
        """비상정지(개념상). 실제는 PLC/하드웨어 인터락이 우선"""
        self._request_engine_stop(StopMode.ESTOP)

    def pause(self) -> None:
        if not self.is_running():
            return
        try:
            self._worker.engine.request_pause()
            self._ui_info("일시정지 요청")
        except Exception as e:
            self._ui_warn(f"pause 실패: {e!r}")

    def resume(self) -> None:
        if not self.is_running():
            return
        try:
            self._worker.engine.request_resume()
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
            self._worker.engine.request_stop(mode)
            self._ui_warn(f"정지 요청: {mode.value}")
        except Exception as e:
            self._ui_warn(f"stop 요청 실패: {e!r}")

    def _ensure_services_running(self) -> None:
        """
        PLC/센서/로그 서비스가 실행 중인지 확인하고, 꺼져 있으면 켠다.
        - 각 서비스는 start()가 중복 호출되어도 안전해야 함(우리가 만든 구조 기준)
        """
        try:
            if hasattr(self.log, "start") and not self.log.is_running():
                self.log.start()
        except Exception:
            # log가 죽으면 UI 로그로만이라도 남김
            pass

        try:
            if hasattr(self.plc, "start") and not self.plc.is_running():
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
            # STOP/ABORT/ERROR 모두 ok=False일 수 있음
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
