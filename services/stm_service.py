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
from concurrent.futures import Future

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


@dataclass(frozen=True)
class _CmdApplyMaterialParams:
    density_g_cm3: float
    z_factor: float
    film_no: Optional[int]
    do_zero_thickness: bool
    future: Future


@dataclass(frozen=True)
class _CmdZeroThickness:
    mode: str   # "C" thickness only, "B" thickness+timer
    future: Future


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

    def request_apply_material_params(
        self,
        *,
        density_g_cm3: float,
        z_factor: float,
        film_no: Optional[int] = None,
        do_zero_thickness: bool = False,
    ) -> Future:
        fut: Future = Future()
        try:
            self._cmd_q.put_nowait(
                _CmdApplyMaterialParams(
                    density_g_cm3=float(density_g_cm3),
                    z_factor=float(z_factor),
                    film_no=film_no,
                    do_zero_thickness=bool(do_zero_thickness),
                    future=fut,
                )
            )
        except Exception as e:
            fut.set_exception(e)
        return fut

    def request_zero_thickness(self, *, mode: str = "C") -> Future:
        fut: Future = Future()
        try:
            self._cmd_q.put_nowait(_CmdZeroThickness(mode=str(mode), future=fut))
        except Exception as e:
            fut.set_exception(e)
        return fut

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

    def _ensure_connected_for_cmd(self) -> None:
        """
        명령 실행(SET/ZERO)은 연결이 있어야 한다.
        연결이 아니면 즉시 재연결 1회 시도 후 실패면 예외.
        """
        now = time.time()
        if (not self._connected) or (self._stm is None):
            # 즉시 재시도
            self._next_try = 0.0
            self._try_connect(now)

        if (not self._connected) or (self._stm is None):
            raise RuntimeError("STM not connected")

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
                continue

            if isinstance(cmd, _CmdApplyMaterialParams):
                fut = cmd.future
                try:
                    self._ensure_connected_for_cmd()
                    assert self._stm is not None

                    # devices/stm100.py에 이미 구현된 "한 방" API 사용
                    self._stm.apply_material_params(
                        density_g_cm3=float(cmd.density_g_cm3),
                        z_factor=float(cmd.z_factor),
                        film_no=cmd.film_no,
                        do_zero_thickness=bool(cmd.do_zero_thickness),
                    )
                    fut.set_result(True)
                except Exception as e:
                    try:
                        fut.set_exception(e)
                    except Exception:
                        pass
                    self.sig_error.emit(f"[STMService] apply_material_params failed: {e!r}")

                    # 끊김/오류로 보고 재연결 사이클로
                    self._safe_close()
                    self._set_connected(False)
                    self._next_try = time.time() + self._reconnect_interval_s
                continue

            if isinstance(cmd, _CmdZeroThickness):
                fut = cmd.future
                try:
                    self._ensure_connected_for_cmd()
                    assert self._stm is not None

                    m = (cmd.mode or "C").strip().upper()
                    if m == "C":
                        self._stm.zero_thickness()
                    elif m == "B":
                        self._stm.zero_timer_and_thickness()
                    else:
                        raise ValueError(f"invalid zero mode: {cmd.mode!r} (use 'C' or 'B')")

                    fut.set_result(True)
                except Exception as e:
                    try:
                        fut.set_exception(e)
                    except Exception:
                        pass
                    self.sig_error.emit(f"[STMService] zero_thickness failed: {e!r}")

                    self._safe_close()
                    self._set_connected(False)
                    self._next_try = time.time() + self._reconnect_interval_s
                continue

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


# ============================================================
# Public Service Wrapper
# ============================================================

class STMService(QObject):
    """
    앱에서 사용하는 STM100 서비스 Wrapper.

    - start/stop
    - reload_from_ini()
    - 최신 스냅샷 조회
    """

    sig_error = Signal(str)
    sig_connected = Signal(bool)
    sig_thickness = Signal(object)
    sig_rate = Signal(object)
    sig_snapshot = Signal(object)

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

        self._worker = STMServiceWorker(
            ini_path=ini_path,
            poll_s=poll_s,
            reconnect_interval_s=reconnect_interval_s,
            max_fail_before_close=max_fail_before_close,
        )

        self._worker.sig_error.connect(self.sig_error)
        self._worker.sig_connected.connect(self.sig_connected)
        self._worker.sig_thickness.connect(self.sig_thickness)
        self._worker.sig_rate.connect(self.sig_rate)
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

    def get_last_snapshot(self) -> Optional[STMSnapshot]:
        return self._worker.get_last_snapshot()

    def reload_from_ini(self, ini_path: str | Path) -> None:
        self._worker.request_reload(ini_path)

    # ✅ engine.py가 호출하는 API (Future 반환)
    def submit_apply_material_params(
        self,
        *,
        density_g_cm3: float,
        z_factor: float,
        film_no: Optional[int] = None,
        do_zero_thickness: bool = False,
    ) -> Future:
        if not self.is_running():
            fut: Future = Future()
            fut.set_exception(RuntimeError("STMService is not running"))
            return fut
        return self._worker.request_apply_material_params(
            density_g_cm3=density_g_cm3,
            z_factor=z_factor,
            film_no=film_no,
            do_zero_thickness=do_zero_thickness,
        )

    def submit_zero_thickness(self, *, mode: str = "C") -> Future:
        if not self.is_running():
            fut: Future = Future()
            fut.set_exception(RuntimeError("STMService is not running"))
            return fut
        return self._worker.request_zero_thickness(mode=mode)

