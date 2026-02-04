# -*- coding: utf-8 -*-
"""
controller/process_worker.py

ProcessWorker (QThread)
- ProcessEngine.run()을 별도 스레드에서 실행
- EngineCallbacks(on_status/on_step/on_error)를 Qt Signal로 브릿지
- Controller(UI)는 이 Worker의 signal을 받아서 화면 갱신

주의:
- UI 위젯을 여기서 직접 건드리지 말 것
- engine.run()은 블로킹이므로 반드시 워커에서 실행
"""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QThread, Signal, QObject

# ------------------------------------------------------------
# Import (패키지/단일 실행 둘 다 대응)
# ------------------------------------------------------------
try:
    # 패키지 실행(권장): python -m Evaporator_Program.main
    from ..process.engine import ProcessEngine, EngineCallbacks, EngineResult
    from ..process.models import ProcessRecipe, ProcessError, StopMode
except ImportError:
    # 단일 실행/경로가 꼬인 경우 fallback
    from process.engine import ProcessEngine, EngineCallbacks, EngineResult
    from process.models import ProcessRecipe, ProcessError, StopMode


class ProcessWorker(QThread):
    """
    공정 엔진 실행 워커.

    Signals
    - sig_status(ProcessStatus)
    - sig_step(StepStatus)
    - sig_error(ProcessError)
    - sig_result(EngineResult)
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

        # 엔진 콜백을 Worker 시그널로 브릿지
        self._engine.callbacks = EngineCallbacks(
            on_status=lambda st: self.sig_status.emit(st),
            on_step=lambda st: self.sig_step.emit(st),
            on_error=lambda err: self.sig_error.emit(err),
        )

    @property
    def engine(self) -> ProcessEngine:
        return self._engine

    @property
    def recipe(self) -> ProcessRecipe:
        return self._recipe

    def request_stop(self, mode: StopMode = StopMode.STOP) -> None:
        """Controller가 호출하는 stop 래퍼"""
        try:
            self._engine.request_stop(mode)
        except Exception:
            pass

    def request_pause(self) -> None:
        try:
            self._engine.request_pause()
        except Exception:
            pass

    def request_resume(self) -> None:
        try:
            self._engine.request_resume()
        except Exception:
            pass

    def run(self) -> None:
        """
        QThread entry.
        """
        t0 = time.time()
        try:
            # 엔진 실행(블로킹)
            result = self._engine.run(self._recipe, run_id=self._run_id)
        except Exception as e:
            # engine.run 내부에서 대부분 처리하지만, 혹시 여기까지 올라오면 최소한 result 형태로 마무리
            t1 = time.time()
            result = EngineResult(
                ok=False,
                run_id=self._run_id or "",
                recipe_name=getattr(self._recipe, "recipe_name", ""),
                started_ts=t0,
                finished_ts=t1,
                error=ProcessError(where="ProcessWorker.run", message=str(e), exception_repr=repr(e)),
            )
        self.sig_result.emit(result)
