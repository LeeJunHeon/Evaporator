# -*- coding: utf-8 -*-
"""
controller/process_start_worker.py

ProcessStartWorker
- ProcessWindow에서 Start 버튼을 눌렀을 때,
  STM 연결 대기 / crystal health preflight를
  UI thread를 막지 않고 별도 QThread에서 수행한다.

핵심 목표
- time.sleep(), future.result(timeout=...) 같은 blocking 작업을 UI thread 밖으로 이동
- 진행 상태를 signal로 UI에 전달
- 취소 가능
- STMService 구현이 조금 달라도(hasattr 기반) 최대한 안전하게 동작

주의
- 이 worker는 "실제 공정 시작"을 하지 않는다.
- 이 worker는 "공정 시작 전 준비(STM preflight)"만 담당한다.
- 성공하면 ProcessWindow 쪽에서 controller.start_from_ui(...)를 호출해야 한다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from PySide6.QtCore import QThread, Signal


@dataclass(slots=True)
class STMPreflightConfig:
    """
    STM preflight 동작 파라미터
    """
    ftm_settle_s: float = 1.5
    connected_timeout_s: float = 8.0
    health_timeout_s: float = 3.0
    poll_interval_ms: int = 100

    # crystal health check
    require_health_check: bool = True
    allow_skip_if_not_supported: bool = True


@dataclass(slots=True)
class STMPreflightResult:
    """
    worker 결과
    """
    ok: bool
    message: str

    cancelled: bool = False
    connected: bool = False

    life_ok: Optional[bool] = None
    freq_ok: Optional[bool] = None
    crystal_fail: Optional[bool] = None

    raw: Any = None


class ProcessStartWorker(QThread):
    """
    Start 버튼 이후의 STM preflight 전용 worker

    Signals
    -------
    sig_progress(str):
        UI에 진행 상태 메시지 전달

    sig_result(object):
        STMPreflightResult 전달
    """

    sig_progress = Signal(str)
    sig_result = Signal(object)

    def __init__(
        self,
        *,
        stm: Any,
        config: Optional[STMPreflightConfig] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.stm = stm
        self.config = config or STMPreflightConfig()
        self._cancel_requested = False

    # ---------------------------------------------------------
    # Public API
    # ---------------------------------------------------------
    def request_cancel(self) -> None:
        self._cancel_requested = True

    # ---------------------------------------------------------
    # QThread
    # ---------------------------------------------------------
    def run(self) -> None:
        try:
            result = self._run_impl()
        except Exception as e:
            result = STMPreflightResult(
                ok=False,
                message=f"STM preflight 예외: {e!r}",
                raw={"exception": repr(e)},
            )

        self.sig_result.emit(result)

    # ---------------------------------------------------------
    # Internal
    # ---------------------------------------------------------
    def _run_impl(self) -> STMPreflightResult:
        if self.stm is None:
            return STMPreflightResult(
                ok=False,
                message="STM 객체가 없어 preflight를 수행할 수 없습니다.",
            )

        self.sig_progress.emit("STM preflight started")

        # 1) FTM 안정화 대기
        if self.config.ftm_settle_s > 0:
            self.sig_progress.emit("FTM 안정화 대기 중...")
            if not self._sleep_cancelable(self.config.ftm_settle_s):
                return STMPreflightResult(
                    ok=False,
                    cancelled=True,
                    message="STM preflight가 취소되었습니다.",
                )

        # 2) STM start 보장(방어)
        self._ensure_stm_started()

        # 3) 연결 대기
        self.sig_progress.emit("STM 연결 대기 중...")
        connected = self._wait_until_connected(timeout_s=self.config.connected_timeout_s)
        if self._cancel_requested:
            return STMPreflightResult(
                ok=False,
                cancelled=True,
                connected=bool(self._read_connected_state()),
                message="STM preflight가 취소되었습니다.",
            )

        if not connected:
            return STMPreflightResult(
                ok=False,
                connected=False,
                message=f"STM 연결 대기 timeout ({self.config.connected_timeout_s:.1f}s)",
            )

        # 4) crystal health check
        if not self.config.require_health_check:
            return STMPreflightResult(
                ok=True,
                connected=True,
                message="STM preflight 성공 (health check 생략)",
            )

        self.sig_progress.emit("STM 크리스탈 상태 점검 중...")
        raw = self._read_crystal_health(timeout_s=self.config.health_timeout_s)

        if raw is _NOT_SUPPORTED:
            if self.config.allow_skip_if_not_supported:
                return STMPreflightResult(
                    ok=True,
                    connected=True,
                    message="STM preflight 성공 (health check 미지원, 생략)",
                )
            return STMPreflightResult(
                ok=False,
                connected=True,
                message="STM crystal health check를 지원하지 않습니다.",
            )

        if self._cancel_requested:
            return STMPreflightResult(
                ok=False,
                cancelled=True,
                connected=True,
                raw=raw,
                message="STM preflight가 취소되었습니다.",
            )

        parsed = self._parse_health_result(raw)

        # crystal fail 우선
        if parsed["crystal_fail"] is True:
            return STMPreflightResult(
                ok=False,
                connected=True,
                life_ok=parsed["life_ok"],
                freq_ok=parsed["freq_ok"],
                crystal_fail=True,
                raw=raw,
                message="STM crystal fail 상태입니다.",
            )

        # 개별 항목 이상
        if parsed["life_ok"] is False:
            return STMPreflightResult(
                ok=False,
                connected=True,
                life_ok=False,
                freq_ok=parsed["freq_ok"],
                crystal_fail=parsed["crystal_fail"],
                raw=raw,
                message="STM crystal life 상태가 비정상입니다.",
            )

        if parsed["freq_ok"] is False:
            return STMPreflightResult(
                ok=False,
                connected=True,
                life_ok=parsed["life_ok"],
                freq_ok=False,
                crystal_fail=parsed["crystal_fail"],
                raw=raw,
                message="STM crystal frequency 상태가 비정상입니다.",
            )

        return STMPreflightResult(
            ok=True,
            connected=True,
            life_ok=parsed["life_ok"],
            freq_ok=parsed["freq_ok"],
            crystal_fail=parsed["crystal_fail"],
            raw=raw,
            message="STM preflight 성공",
        )

    def _ensure_stm_started(self) -> None:
        """
        ProcessWindow에서 보통 stm.start()를 먼저 하겠지만,
        방어적으로 여기서도 한 번 확인한다.
        """
        try:
            if hasattr(self.stm, "is_running"):
                try:
                    running = bool(self.stm.is_running())
                except Exception:
                    running = False

                if not running and hasattr(self.stm, "start"):
                    self.stm.start()
                    return

            if hasattr(self.stm, "start"):
                # is_running이 없으면 그냥 start 시도
                self.stm.start()
        except Exception:
            # 이미 실행 중인 서비스에서 start() 재호출 시 예외가 발생할 수 있어 무시
            pass

    def _wait_until_connected(self, *, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(0.1, float(timeout_s))

        while time.monotonic() < deadline:
            if self._cancel_requested:
                return False

            if self._read_connected_state():
                return True

            self.msleep(max(10, int(self.config.poll_interval_ms)))

        # 타임아웃 직후 마지막으로 한 번 더 확인 (루프 조건 경계에서 놓치는 경우 방지)
        return bool(self._read_connected_state())

    def _read_connected_state(self) -> bool:
        """
        STM 서비스 구현이 달라도 최대한 흡수하도록 방어적으로 읽는다.
        우선순위:
        1) is_connected()
        2) connected 속성
        3) _connected 속성
        4) get_last_snapshot().connected
        """
        stm = self.stm

        try:
            if hasattr(stm, "is_connected"):
                v = stm.is_connected()
                return bool(v() if callable(v) else v)
        except Exception:
            pass

        for name in ("connected", "_connected"):
            try:
                if hasattr(stm, name):
                    return bool(getattr(stm, name))
            except Exception:
                pass

        try:
            if hasattr(stm, "get_last_snapshot"):
                snap = stm.get_last_snapshot()
                if snap is not None:
                    return bool(getattr(snap, "connected", False))
        except Exception:
            pass

        return False

    def _read_crystal_health(self, *, timeout_s: float) -> Any:
        """
        STM crystal health를 읽는다.

        지원 우선순위:
        1) submit_read_crystal_health() -> future.result(timeout=...)
        2) read_crystal_health(timeout_s=...)
        3) read_crystal_health()
        없으면 _NOT_SUPPORTED 반환
        """
        stm = self.stm

        # 1) submit_read_crystal_health
        try:
            if hasattr(stm, "submit_read_crystal_health"):
                fut = stm.submit_read_crystal_health()
                if hasattr(fut, "result"):
                    return fut.result(timeout=float(timeout_s))
                return fut
        except Exception as e:
            return {"ok": False, "where": "submit_read_crystal_health", "error": repr(e)}

        # 2) read_crystal_health(timeout_s=...)
        try:
            if hasattr(stm, "read_crystal_health"):
                fn = stm.read_crystal_health
                try:
                    return fn(timeout_s=float(timeout_s))
                except TypeError:
                    return fn()
        except Exception as e:
            return {"ok": False, "where": "read_crystal_health", "error": repr(e)}

        return _NOT_SUPPORTED

    def _parse_health_result(self, raw: Any) -> dict[str, Optional[bool]]:
        """
        다양한 반환 형식을 최대한 흡수한다.

        기대하는 대표 형식 예:
        - {"life_ok": True, "freq_ok": True, "crystal_fail": False}
        - {"life": True, "freq": True, "fail": False}
        - (True, True, False)
        - {"ok": True}
        """
        life_ok: Optional[bool] = None
        freq_ok: Optional[bool] = None
        crystal_fail: Optional[bool] = None

        if isinstance(raw, dict):
            life_ok = self._pick_bool(raw, "life_ok", "life", "life_good", "life_status_ok")
            freq_ok = self._pick_bool(raw, "freq_ok", "freq", "frequency_ok", "freq_status_ok")
            crystal_fail = self._pick_bool(raw, "crystal_fail", "fail", "crystal_error")

            # {"ok": True} 같은 최소 응답만 오는 경우
            if life_ok is None and freq_ok is None and crystal_fail is None:
                ok = self._pick_bool(raw, "ok", "success")
                if ok is True:
                    return {
                        "life_ok": None,
                        "freq_ok": None,
                        "crystal_fail": False,
                    }
                if ok is False:
                    return {
                        "life_ok": False,
                        "freq_ok": False,
                        "crystal_fail": True,
                    }

        elif isinstance(raw, (tuple, list)):
            vals = list(raw)
            if len(vals) >= 1:
                life_ok = self._to_bool_or_none(vals[0])
            if len(vals) >= 2:
                freq_ok = self._to_bool_or_none(vals[1])
            if len(vals) >= 3:
                crystal_fail = self._to_bool_or_none(vals[2])

        elif isinstance(raw, bool):
            if raw:
                crystal_fail = False
            else:
                crystal_fail = True

        return {
            "life_ok": life_ok,
            "freq_ok": freq_ok,
            "crystal_fail": crystal_fail,
        }

    def _pick_bool(self, d: dict[str, Any], *keys: str) -> Optional[bool]:
        for k in keys:
            if k in d:
                return self._to_bool_or_none(d.get(k))
        return None

    def _to_bool_or_none(self, v: Any) -> Optional[bool]:
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in {"1", "true", "yes", "y", "on", "ok", "pass"}:
                return True
            if s in {"0", "false", "no", "n", "off", "fail", "error"}:
                return False
        return None

    def _sleep_cancelable(self, seconds: float) -> bool:
        """
        취소 가능한 sleep.
        True  -> 정상 완료
        False -> 취소됨
        """
        remain_ms = max(0, int(float(seconds) * 1000))
        step_ms = max(10, int(self.config.poll_interval_ms))

        while remain_ms > 0:
            if self._cancel_requested:
                return False
            chunk = min(step_ms, remain_ms)
            self.msleep(chunk)
            remain_ms -= chunk

        return not self._cancel_requested


# sentinel: crystal health check API가 없는 STM 서비스 구현체를 구분하는 고유 객체
_NOT_SUPPORTED = object()