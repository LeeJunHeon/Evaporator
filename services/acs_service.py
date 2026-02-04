# -*- coding: utf-8 -*-
"""
services/acs_service.py

ACSService
- ACS-2000(압력) 장비를 단일 QThread에서 폴링/재연결/설정 리로드까지 관리
- UI/공정은 이 서비스를 통해 최신값(signal/snapshot)만 구독하는 구조

특징
- 장비 객체(ACS2000)는 워커 스레드 내부에서만 생성/사용/close (스레드 공유 금지)
- poll_s 주기로 pressure 읽기 (query 모드)
- 옵션: stream 모드 지원(원하면 start_stream 사용)
- 실패 누적 시 close하고 reconnect_interval_s마다 재연결 시도
"""

from __future__ import annotations

import time
import threading
import queue
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any

from PySide6.QtCore import QObject, QThread, Signal

from config.serial_config import load_settings
from devices.acs2000 import ACS2000


# ============================================================
# Snapshot
# ============================================================

@dataclass(frozen=True)
class ACSSnapshot:
    ts: float
    connected: bool
    pressure: Optional[float]
    meta: Dict[str, Any]


# ============================================================
# Worker command
# ============================================================

@dataclass(frozen=True)
class _CmdReload:
    ini_path: Path


@dataclass(frozen=True)
class _CmdSetMode:
    stream: bool
    stream_interval_a: int = 1


# ============================================================
# Worker Thread
# ============================================================

class ACSServiceWorker(QThread):
    """
    ACS2000 폴링 워커.

    - ini에서 설정 읽고 ACS2000 생성/연결
    - poll_s 주기로 pressure 읽어서 signal emit
    - 실패 누적 시 close하고 reconnect_interval_s마다 재연결
    """

    sig_error = Signal(str)
    sig_connected = Signal(bool)
    sig_pressure = Signal(object)   # float|None
    sig_snapshot = Signal(object)   # ACSSnapshot

    def __init__(
        self,
        ini_path: str | Path,
        *,
        poll_s: float = 0.25,
        reconnect_interval_s: float = 1.0,
        max_fail_before_close: int = 2,
        channel: int = 1,                # 1 or 2
        use_stream: bool = False,
        stream_interval_a: int = 1,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._ini_path = Path(ini_path)

        self._poll_s = max(0.05, float(poll_s))
        self._reconnect_interval_s = max(0.2, float(reconnect_interval_s))
        self._max_fail_before_close = max(1, int(max_fail_before_close))

        self._channel = int(channel) if int(channel) in (1, 2) else 1
        self._use_stream = bool(use_stream)
        self._stream_interval_a = max(1, int(stream_interval_a))

        self._stop_evt = threading.Event()
        self._cmd_q: "queue.Queue[object]" = queue.Queue()

        self._acs: Optional[ACS2000] = None
        self._connected: bool = False

        self._last_pressure: Optional[float] = None
        self._last_snapshot: Optional[ACSSnapshot] = None

        self._fail_count: int = 0
        self._next_try: float = 0.0

    # ---------- public (main thread) ----------
    def stop(self) -> None:
        self._stop_evt.set()
        try:
            self._cmd_q.put_nowait(None)
        except Exception:
            pass

    def request_reload(self, ini_path: str | Path) -> None:
        try:
            self._cmd_q.put_nowait(_CmdReload(Path(ini_path)))
        except Exception:
            pass

    def request_set_mode(self, *, stream: bool, stream_interval_a: int = 1) -> None:
        try:
            self._cmd_q.put_nowait(_CmdSetMode(bool(stream), max(1, int(stream_interval_a))))
        except Exception:
            pass

    def get_last_snapshot(self) -> Optional[ACSSnapshot]:
        return self._last_snapshot

    # ---------- thread entry ----------
    def run(self) -> None:
        try:
            self._main_loop()
        except Exception as e:
            try:
                self.sig_error.emit(f"[ACSService] worker crashed: {e!r}")
            except Exception:
                pass
        finally:
            self._safe_close()
            self._set_connected(False)

    # ---------- internals ----------
    def _build_device_from_ini(self) -> ACS2000:
        s = load_settings(self._ini_path, "acs2000")
        return ACS2000(
            port=s.port,
            baudrate=s.baudrate,
            bytesize=s.bytesize,
            parity=s.parity,
            stopbits=s.stopbits,
            timeout_s=s.timeout_s,
            write_timeout_s=s.write_timeout_s,
            rtscts=s.rtscts,
            dsrdtr=s.dsrdtr,
            eom=s.eom or "CR",
        )

    def _safe_close(self) -> None:
        if self._acs is None:
            return
        try:
            # stream 모드든 아니든 close가 가장 안전
            self._acs.close()
        except Exception:
            pass
        self._acs = None

    def _set_connected(self, v: bool) -> None:
        v = bool(v)
        if self._connected != v:
            self._connected = v
            self.sig_connected.emit(v)

    def _apply_stream_mode_if_needed(self) -> None:
        """
        연결 직후 stream 모드 설정이 켜져 있으면 stream 시작.
        실패하면 query 모드로 자동 폴백.
        """
        if not self._use_stream:
            return
        if not self._connected or self._acs is None:
            return

        try:
            self._acs.start_pressure_stream(interval_a=self._stream_interval_a)
            self.sig_error.emit(f"[ACSService] stream mode ON (A={self._stream_interval_a})")
        except Exception as e:
            # stream 실패 -> query 폴백
            self.sig_error.emit(f"[ACSService] stream start failed -> fallback to query: {e!r}")
            self._use_stream = False

    def _handle_reload(self, ini_path: Path) -> None:
        self._ini_path = Path(ini_path)
        self._safe_close()
        self._set_connected(False)
        self._fail_count = 0
        self._next_try = 0.0
        self.sig_error.emit(f"[ACSService] reload ini -> {self._ini_path}")

    def _handle_set_mode(self, cmd: _CmdSetMode) -> None:
        self._use_stream = bool(cmd.stream)
        self._stream_interval_a = max(1, int(cmd.stream_interval_a))

        # 이미 연결돼 있다면 즉시 적용(연결 상태에서만)
        if self._connected and self._acs is not None:
            if self._use_stream:
                self._apply_stream_mode_if_needed()
            else:
                # stream 끄는 가장 안전한 방법: close -> reconnect
                self.sig_error.emit("[ACSService] stream mode OFF -> reconnect to reset state")
                self._safe_close()
                self._set_connected(False)
                self._next_try = time.time() + 0.1

    def _drain_commands(self) -> None:
        while True:
            try:
                cmd = self._cmd_q.get_nowait()
            except queue.Empty:
                return

            if cmd is None:
                continue

            if isinstance(cmd, _CmdReload):
                self._handle_reload(cmd.ini_path)
            elif isinstance(cmd, _CmdSetMode):
                self._handle_set_mode(cmd)

    def _try_connect(self, now: float) -> None:
        if self._connected:
            return
        if now < self._next_try:
            return

        if self._acs is None:
            try:
                self._acs = self._build_device_from_ini()
            except Exception as e:
                self.sig_error.emit(f"[ACSService] build device failed: {e!r} (ini={self._ini_path})")
                self._next_try = now + self._reconnect_interval_s
                return

        try:
            self._acs.connect()
            self._set_connected(True)
            self._fail_count = 0
            self._apply_stream_mode_if_needed()
        except Exception as e:
            self._set_connected(False)
            self.sig_error.emit(f"[ACSService] connect failed: {e!r}")
            self._safe_close()
            self._next_try = now + self._reconnect_interval_s

    @staticmethod
    def _parse_pressure_any(s: str) -> float:
        """
        stream 라인/응답 문자열에서 압력(float)을 최대한 robust하게 추출.
        예) "$PRD,1,1.23E-6" / "PRD,1,1.23E-6" / "$1.23E-6" 등
        """
        raw = (s or "").strip().replace("$", "").strip()
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if tokens:
            for t in reversed(tokens):
                try:
                    return float(t)
                except ValueError:
                    continue

        for t in reversed(raw.split()):
            try:
                return float(t)
            except ValueError:
                continue

        return float(raw)

    def _poll_once(self, now: float) -> bool:
        if not self._connected or self._acs is None:
            return False

        try:
            if self._use_stream:
                line = self._acs.read_stream_line(timeout_s=1.5)
                pr = float(self._parse_pressure_any(line))
            else:
                pr = float(self._acs.query_pressure(channel=self._channel))

            self._last_pressure = pr
            self.sig_pressure.emit(pr)

            snap = ACSSnapshot(
                ts=now,
                connected=True,
                pressure=pr,
                meta={
                    "fail_count": self._fail_count,
                    "channel": self._channel,
                    "use_stream": self._use_stream,
                    "stream_interval_a": self._stream_interval_a,
                },
            )
            self._last_snapshot = snap
            self.sig_snapshot.emit(snap)

            self._fail_count = 0
            return True

        except Exception as e:
            self._fail_count += 1
            self.sig_error.emit(f"[ACSService] poll failed: {e!r} (fail={self._fail_count})")

            if self._fail_count >= self._max_fail_before_close:
                self._safe_close()
                self._set_connected(False)
                self._next_try = now + self._reconnect_interval_s

            return False

    def _main_loop(self) -> None:
        next_poll = time.time()

        while not self._stop_evt.is_set():
            now = time.time()

            # 1) command
            self._drain_commands()

            # 2) connect/reconnect
            self._try_connect(now)

            # 3) poll
            if now >= next_poll:
                self._poll_once(now)
                next_poll = now + self._poll_s

            time.sleep(0.01)


# ============================================================
# Public Service
# ============================================================

class ACSService(QObject):
    """
    앱에서 사용하는 ACS2000 서비스 Wrapper.

    - start/stop
    - reload_from_ini()
    - (옵션) set_stream_mode()
    - 최신 스냅샷 조회
    """

    sig_error = Signal(str)
    sig_connected = Signal(bool)
    sig_pressure = Signal(object)
    sig_snapshot = Signal(object)

    def __init__(
        self,
        ini_path: str | Path,
        *,
        poll_s: float = 0.25,
        reconnect_interval_s: float = 1.0,
        max_fail_before_close: int = 2,
        channel: int = 1,
        use_stream: bool = False,
        stream_interval_a: int = 1,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)

        self._worker = ACSServiceWorker(
            ini_path=ini_path,
            poll_s=poll_s,
            reconnect_interval_s=reconnect_interval_s,
            max_fail_before_close=max_fail_before_close,
            channel=channel,
            use_stream=use_stream,
            stream_interval_a=stream_interval_a,
        )

        self._worker.sig_error.connect(self.sig_error)
        self._worker.sig_connected.connect(self.sig_connected)
        self._worker.sig_pressure.connect(self.sig_pressure)
        self._worker.sig_snapshot.connect(self.sig_snapshot)

    def start(self) -> None:
        if not self._worker.isRunning():
            self._worker.start()

    def stop(self, wait_ms: int = 3000) -> None:
        try:
            self._worker.stop()
        except Exception:
            pass
        try:
            self._worker.wait(int(wait_ms))
        except Exception:
            pass

    def is_running(self) -> bool:
        return bool(self._worker.isRunning())

    def get_last_snapshot(self) -> Optional[ACSSnapshot]:
        return self._worker.get_last_snapshot()

    def reload_from_ini(self, ini_path: str | Path) -> None:
        self._worker.request_reload(ini_path)

    def set_stream_mode(self, *, stream: bool, stream_interval_a: int = 1) -> None:
        self._worker.request_set_mode(stream=stream, stream_interval_a=stream_interval_a)
