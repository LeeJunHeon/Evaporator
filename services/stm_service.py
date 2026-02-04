# -*- coding: utf-8 -*-
"""
services/stm_service.py

STMService
- STM-100(두께/레이트) 장비를 단일 QThread에서 폴링/재연결/설정 리로드까지 관리
- UI/공정은 이 서비스를 통해 최신값(signal/snapshot)만 구독하는 구조

특징
- 장비 객체(STM100)는 워커 스레드 내부에서만 생성/사용/close (스레드 공유 금지)
- read 실패 누적 시 close -> 일정 간격 재연결
- devices.ini 저장 후 reload_from_ini()로 안전하게 재로딩 가능
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
from devices.stm100 import STM100


# ============================================================
# Snapshot
# ============================================================

@dataclass(frozen=True)
class STMSnapshot:
    ts: float
    connected: bool
    thickness_angstrom: Optional[float]
    rate_angstrom_per_s: Optional[float]
    meta: Dict[str, Any]


# ============================================================
# Worker command
# ============================================================

@dataclass(frozen=True)
class _CmdReload:
    ini_path: Path


# ============================================================
# Worker Thread
# ============================================================

class STMServiceWorker(QThread):
    """
    STM100 폴링 워커.

    - ini에서 설정 읽고 STM100 생성/연결
    - poll_s 주기로 thickness/rate 읽어서 signal emit
    - 실패 누적 시 close하고 reconnect_interval_s마다 재연결 시도
    """

    sig_error = Signal(str)
    sig_connected = Signal(bool)
    sig_thickness = Signal(object)  # float|None
    sig_rate = Signal(object)       # float|None
    sig_snapshot = Signal(object)   # STMSnapshot

    def __init__(
        self,
        ini_path: str | Path,
        *,
        poll_s: float = 0.25,
        reconnect_interval_s: float = 1.0,
        max_fail_before_close: int = 2,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._ini_path = Path(ini_path)

        self._poll_s = max(0.05, float(poll_s))
        self._reconnect_interval_s = max(0.2, float(reconnect_interval_s))
        self._max_fail_before_close = max(1, int(max_fail_before_close))

        self._stop_evt = threading.Event()
        self._cmd_q: "queue.Queue[object]" = queue.Queue()

        self._stm: Optional[STM100] = None
        self._connected: bool = False

        self._last_thickness: Optional[float] = None
        self._last_rate: Optional[float] = None
        self._last_snapshot: Optional[STMSnapshot] = None

        self._fail_count: int = 0
        self._next_try: float = 0.0

    # ---------- public (main thread) ----------
    def stop(self) -> None:
        self._stop_evt.set()
        try:
            self._cmd_q.put_nowait(None)  # wake up
        except Exception:
            pass

    def request_reload(self, ini_path: str | Path) -> None:
        try:
            self._cmd_q.put_nowait(_CmdReload(Path(ini_path)))
        except Exception:
            pass

    def get_last_snapshot(self) -> Optional[STMSnapshot]:
        return self._last_snapshot

    # ---------- thread entry ----------
    def run(self) -> None:
        try:
            self._main_loop()
        except Exception as e:
            try:
                self.sig_error.emit(f"[STMService] worker crashed: {e!r}")
            except Exception:
                pass
        finally:
            self._safe_close()
            self._set_connected(False)

    # ---------- internals ----------
    def _build_device_from_ini(self) -> STM100:
        s = load_settings(self._ini_path, "stm100")
        return STM100(
            port=s.port,
            baudrate=s.baudrate,
            bytesize=s.bytesize,
            parity=s.parity,
            stopbits=s.stopbits,
            timeout_s=s.timeout_s,
            write_timeout_s=s.write_timeout_s,
            rtscts=s.rtscts,
            dsrdtr=s.dsrdtr,
        )

    def _safe_close(self) -> None:
        if self._stm is None:
            return
        try:
            self._stm.close()
        except Exception:
            pass
        self._stm = None

    def _set_connected(self, v: bool) -> None:
        v = bool(v)
        if self._connected != v:
            self._connected = v
            self.sig_connected.emit(v)

    def _handle_reload(self, ini_path: Path) -> None:
        self._ini_path = Path(ini_path)
        # close -> 재생성 -> 다음 루프에서 connect 시도
        self._safe_close()
        self._set_connected(False)
        self._fail_count = 0
        self._next_try = 0.0
        self.sig_error.emit(f"[STMService] reload ini -> {self._ini_path}")

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

    def _try_connect(self, now: float) -> None:
        if self._connected:
            return
        if now < self._next_try:
            return

        # 장비 객체가 없으면 새로 만든다
        if self._stm is None:
            try:
                self._stm = self._build_device_from_ini()
            except Exception as e:
                self.sig_error.emit(f"[STMService] build device failed: {e!r} (ini={self._ini_path})")
                self._next_try = now + self._reconnect_interval_s
                return

        try:
            self._stm.connect()
            self._set_connected(True)
            self._fail_count = 0
        except Exception as e:
            self._set_connected(False)
            self.sig_error.emit(f"[STMService] connect failed: {e!r}")
            self._safe_close()
            self._next_try = now + self._reconnect_interval_s

    def _poll_once(self, now: float) -> bool:
        """
        성공 시 True(업데이트 있음), 실패 시 False
        """
        if not self._connected or self._stm is None:
            return False

        try:
            th = float(self._stm.get_thickness_angstrom())
            rt = float(self._stm.get_rate_angstrom_per_s())

            self._last_thickness = th
            self._last_rate = rt

            self.sig_thickness.emit(th)
            self.sig_rate.emit(rt)

            snap = STMSnapshot(
                ts=now,
                connected=True,
                thickness_angstrom=th,
                rate_angstrom_per_s=rt,
                meta={"fail_count": self._fail_count},
            )
            self._last_snapshot = snap
            self.sig_snapshot.emit(snap)

            self._fail_count = 0
            return True

        except Exception as e:
            self._fail_count += 1
            self.sig_error.emit(f"[STMService] poll failed: {e!r} (fail={self._fail_count})")

            if self._fail_count >= self._max_fail_before_close:
                # 완전 끊김으로 보고 close 후 재연결 사이클로
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
