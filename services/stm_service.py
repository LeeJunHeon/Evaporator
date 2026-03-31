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
from configparser import ConfigParser
from concurrent.futures import Future

from PySide6.QtCore import QObject, QThread, Signal

from config.serial_config import load_settings
from devices.stm100 import STM100, STM100ProtocolError, STM100ValueUnavailableError


# ============================================================
# STM io_trace 포맷 헬퍼
# ============================================================

def _format_stm_io_trace_legacy0(d: dict) -> str:
    """
    STM100 TX/RX trace dict → 사람이 읽기 쉬운 로그 문자열.

    TX=V / RX=A092.6   → "[STM] 증착률: 0.926 Å/s"
    TX=U / RX=A5926441 → "[STM] 누적 두께: 592.6 Å"
    파싱 실패 시 raw 출력 (WARNING 레벨 표시)
    """
    tx = str(d.get("tx", "")).strip().upper()
    rx = str(d.get("rx", "")).strip()
    ok = bool(d.get("ok", True))

    # dep.rate: TX=V, RX=A<value> (값은 1/1000 Å/s 단위)
    # STM100 프로토콜: 증착률은 "V" 커맨드로 조회, 응답 앞의 "A" 접두사 제거 후 100 나눔
    if tx == "V":
        try:
            if rx.startswith("A") or rx.startswith("a"):
                val = float(rx[1:])
                rate = val / 100.0  # STM100은 0.01 Å/s 단위
                return f"[STM] 증착률: {rate:.3f} Å/s"
        except Exception:
            pass
        level = "WARNING" if ok else "ERROR"
        return f"[STM][{level}] 증착률 파싱 실패 — tx={tx!r} rx={rx!r}"

    # 두께: TX=U, RX=A<value> (값은 0.1 Å 단위)
    if tx == "U":
        try:
            if rx.startswith("A") or rx.startswith("a"):
                val = float(rx[1:])
                thickness = val / 10.0  # 0.1 Å 단위 → Å
                return f"[STM] 누적 두께: {thickness:.1f} Å"
        except Exception:
            pass
        level = "WARNING" if ok else "ERROR"
        return f"[STM][{level}] 두께 파싱 실패 — tx={tx!r} rx={rx!r}"

    # 기타 명령: raw 출력
    if not ok:
        detail = str(d.get("detail", "")).strip()
        return f"[STM] TX={tx!r} → ERROR: rx={rx!r} {detail}".strip()

    return f"[STM] TX={tx!r} → RX={rx!r}"


def _format_stm_io_trace_legacy1(d: dict) -> str:
    tx = str(d.get("tx", "")).strip().upper()
    rx = str(d.get("rx", "")).strip()
    ok = bool(d.get("ok", True))
    detail = str(d.get("detail", "")).strip()

    body = rx
    if body[:1].upper() in ("A", "B") and len(body) > 1:
        body = body[1:].strip()

    if tx == "U":
        try:
            hz = int(body)
            return f"[STM] Sensor frequency: {hz:,} Hz ({hz / 1_000_000.0:.6f} MHz)"
        except Exception:
            level = "WARNING" if ok else "ERROR"
            return f"[STM][{level}] Sensor frequency parse failed: tx={tx!r} rx={rx!r} {detail}".strip()

    if tx == "V":
        try:
            return f"[STM] Crystal life: {float(body):.1f}%"
        except Exception:
            level = "WARNING" if ok else "ERROR"
            return f"[STM][{level}] Crystal life parse failed: tx={tx!r} rx={rx!r} {detail}".strip()

    if not ok:
        return f"[STM] TX={tx!r} ERROR: rx={rx!r} {detail}".strip()

    return f"[STM] TX={tx!r} RX={rx!r}"


def _format_stm_io_trace_legacy2(d: dict) -> str:
    tx = str(d.get("tx", "") or "").strip().upper()
    rx = str(d.get("rx", "") or "").strip()
    ok = bool(d.get("ok", True))
    detail = str(d.get("detail", "") or "").strip()

    body = rx
    if body[:1].upper() in ("A", "B") and len(body) > 1:
        body = body[1:].strip()

    if not ok:
        suffix = f" ({detail})" if detail else ""
        return f"[STM] 통신 오류: tx={tx!r} rx={rx!r}{suffix}"

    if tx == "U":
        try:
            hz = int(body)
            return f"[STM] 센서 주파수: {hz / 1_000_000.0:.6f} MHz"
        except Exception:
            suffix = f" ({detail})" if detail else ""
            return f"[STM] 센서 주파수 파싱 실패: tx={tx!r} rx={rx!r}{suffix}"

    if tx == "V":
        try:
            return f"[STM] 크리스탈 수명: {float(body):.1f}%"
        except Exception:
            suffix = f" ({detail})" if detail else ""
            return f"[STM] 크리스탈 수명 파싱 실패: tx={tx!r} rx={rx!r}{suffix}"

    if detail:
        return f"[STM] TX={tx!r} RX={rx!r} ({detail})"
    return f"[STM] TX={tx!r} RX={rx!r}"


def _format_stm_io_trace(d: dict) -> str:
    tx = str(d.get("tx", "") or "").strip().upper()
    rx = str(d.get("rx", "") or "").strip()
    ok = bool(d.get("ok", True))
    detail = str(d.get("detail", "") or "").strip()

    body = rx
    if body[:1].upper() in ("A", "B") and len(body) > 1:
        body = body[1:].strip()

    if not ok:
        suffix = f" ({detail})" if detail else ""
        return f"[STM] 통신 오류: tx={tx!r} rx={rx!r}{suffix}"

    if tx == "U":
        try:
            hz = int(body)
            return f"[STM] 센서 주파수: {hz / 1_000_000.0:.6f} MHz"
        except Exception:
            suffix = f" ({detail})" if detail else ""
            return f"[STM] 센서 주파수 파싱 실패: tx={tx!r} rx={rx!r}{suffix}"

    if tx == "V":
        try:
            return f"[STM] 크리스탈 수명: {float(body):.1f}%"
        except Exception:
            suffix = f" ({detail})" if detail else ""
            return f"[STM] 크리스탈 수명 파싱 실패: tx={tx!r} rx={rx!r}{suffix}"

    if detail:
        return f"[STM] TX={tx!r} RX={rx!r} ({detail})"
    return f"[STM] TX={tx!r} RX={rx!r}"


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


@dataclass(frozen=True)
class _CmdReadCrystalHealth:
    """
    공정 시작 전 점검용: LIFE(%)/FREQ(Hz)/CRYSTAL FAIL 상태를 한 번에 읽는다.
    결과는 devices/stm100.py의 get_crystal_health_snapshot() dict 그대로 반환.
    """
    future: Future


# ============================================================
# Worker Thread
# ============================================================

# STM이 측정값 준비 중일 때 일시적으로 ValueUnavailable을 반환할 수 있어 즉시 재연결하지 않고 허용
_MAX_UNAVAILABLE_BEFORE_RECONNECT = 10  # 연속 N회 ValueUnavailable → fail 로 간주


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
    sig_io_trace = Signal(object)   # ✅ {"dev":"STM100","ok":bool,"tx":str,"rx":str,"detail":str,"ts":float}

    def __init__(
        self,
        ini_path: str | Path,
        *,
        poll_s: float = 1.0,
        reconnect_interval_s: float = 1.0,
        max_fail_before_close: int = 6,  # ✅ 기본 2 → 6 (io_policy 철학과 정렬)
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
        self._unavailable_count: int = 0
        self._next_try: float = 0.0

        self._conn_backoff_base_s = float(self._reconnect_interval_s)  # ini 로딩 전 임시
        self._conn_backoff_factor = 1.7
        self._conn_backoff_max_s = max(self._conn_backoff_base_s, 5.0)
        self._conn_backoff_s = float(self._reconnect_interval_s)

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

    def request_read_crystal_health(self) -> Future:
        """
        공정 시작 전 점검용: 워커 스레드에서만 STM100을 만지도록 Future 기반으로 읽기 요청
        """
        fut: Future = Future()
        try:
            self._cmd_q.put_nowait(_CmdReadCrystalHealth(future=fut))
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
            try:
                self._cancel_pending_futures("STMService stopped")
            except Exception:
                pass
            self._safe_close()
            self._set_connected(False)

    # ---------- internals ----------
    def _cancel_pending_futures(self, reason: str) -> None:
        """
        워커 종료 시 큐에 남아있는 명령들의 Future를 예외로 완료 처리.
        (engine 쪽에서 fut.result(timeout=...) 기다릴 때 깔끔하게 빠지도록)
        """
        while True:
            try:
                cmd = self._cmd_q.get_nowait()
            except queue.Empty:
                return

            if cmd is None:
                continue

            if isinstance(cmd, _CmdApplyMaterialParams):
                fut = cmd.future
                try:
                    if fut is not None and not fut.done():
                        fut.set_exception(RuntimeError(reason))
                except Exception:
                    pass
                continue

            if isinstance(cmd, _CmdZeroThickness):
                fut = cmd.future
                try:
                    if fut is not None and not fut.done():
                        fut.set_exception(RuntimeError(reason))
                except Exception:
                    pass
                continue

            if isinstance(cmd, _CmdReadCrystalHealth):
                fut = cmd.future
                try:
                    if fut is not None and not fut.done():
                        fut.set_exception(RuntimeError(reason))
                except Exception:
                    pass
                continue

    def _load_io_policy_from_ini(self) -> Dict[str, Any]:
        """
        devices.ini:
        - [stm100]에 값이 있으면 우선
        - 없으면 [io_policy] fallback
        """
        cfg = ConfigParser(interpolation=None)
        cfg.read(self._ini_path, encoding="utf-8")

        dev = "stm100"
        io = "io_policy"

        def _get_int(key: str, default: int) -> int:
            if cfg.has_option(dev, key):
                return cfg.getint(dev, key)
            if cfg.has_option(io, key):
                return cfg.getint(io, key)
            return default

        def _get_float(key: str, default: float) -> float:
            if cfg.has_option(dev, key):
                return cfg.getfloat(dev, key)
            if cfg.has_option(io, key):
                return cfg.getfloat(io, key)
            return default

        return {
            "io_err_allow": _get_int("io_err_allow", 5),
            "io_retry_sleep_s": _get_float("io_retry_sleep_s", 0.05),
            "io_reconnect_max": _get_int("io_reconnect_max", 2),
            "reconnect_backoff_base_s": _get_float("reconnect_backoff_base_s", 0.6),
            "reconnect_backoff_factor": _get_float("reconnect_backoff_factor", 1.7),
            "reconnect_backoff_max_s": _get_float("reconnect_backoff_max_s", 5.0),
        }

    def _build_device_from_ini(self) -> STM100:
        s = load_settings(self._ini_path, "stm100")
        pol = self._load_io_policy_from_ini()

        # ✅ 서비스의 connect 백오프도 io_policy로 통일
        self._conn_backoff_base_s = max(0.2, float(pol["reconnect_backoff_base_s"]))
        self._conn_backoff_factor = max(1.0, float(pol["reconnect_backoff_factor"]))
        self._conn_backoff_max_s = max(self._conn_backoff_base_s, float(pol["reconnect_backoff_max_s"]))
        self._conn_backoff_s = self._conn_backoff_base_s  # reset

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

            # ✅ io_policy (devices.ini [io_policy] 공유)
            io_err_allow=int(pol["io_err_allow"]),
            io_retry_sleep_s=float(pol["io_retry_sleep_s"]),
            io_reconnect_max=int(pol["io_reconnect_max"]),
            reconnect_backoff_base_s=float(pol["reconnect_backoff_base_s"]),
            reconnect_backoff_factor=float(pol["reconnect_backoff_factor"]),
            reconnect_backoff_max_s=float(pol["reconnect_backoff_max_s"]),

            # ✅ STM100 통신(TX/RX) trace를 서비스로 전달
            io_trace_cb=self._on_stm_io_trace,
            io_trace_skip_tokens={"S", "T"},   # (stm100.py 기본도 S/T skip이지만, 여기서도 명시)

            # U(센서 주파수)/V(크리스탈 수명) 자동 읽기 비활성화
            # - get_rate_angstrom_per_s() 호출 시 _maybe_trace_uv()가 2초 간격으로 U/V를
            #   추가 읽어 로그에 출력하는 동작을 완전히 중단
            # - preflight의 _CmdReadCrystalHealth에서 이미 1회 읽으므로 공정 중 불필요
            uv_trace_enable=False,
        )

    def _safe_close(self) -> None:
        if self._stm is None:
            return
        try:
            self._stm.close()
        except Exception:
            pass
        self._stm = None
        self._unavailable_count = 0

    def _on_stm_io_trace(self, d: dict) -> None:
        """
        devices/stm100.py에서 io_trace_cb로 올라오는 통신 trace를
        STMServiceWorker 시그널로 밖으로 전달.
        "msg" 필드에 사람이 읽기 쉬운 요약 문자열을 추가해서 emit.
        """
        try:
            d2 = dict(d or {})
            d2.setdefault("dev", "STM100")
            d2.setdefault("ts", time.time())
            # 인간 가독형 메시지 추가 (수신측에서 d["msg"] 로 표시 가능)
            try:
                d2["msg"] = _format_stm_io_trace(d2)
            except Exception:
                d2["msg"] = f"[STM] tx={d2.get('tx')!r} rx={d2.get('rx')!r}"
            self.sig_io_trace.emit(d2)
        except Exception:
            pass

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
        self._unavailable_count = 0
        self._next_try = 0.0
        self._conn_backoff_s = float(self._conn_backoff_base_s)  # ✅ 추가
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

            if isinstance(cmd, _CmdReadCrystalHealth):
                fut = cmd.future
                try:
                    self._ensure_connected_for_cmd()
                    assert self._stm is not None

                    errors = []

                    # 1) crystal fail 상태
                    crystal_ok = None
                    try:
                        crystal_ok = self._stm.get_crystal_fail_status()  # True=GOOD, False=FAIL
                    except STM100ProtocolError as e:
                        errors.append(f"crystal_fail_status: {e!r}")
                        crystal_ok = None

                    # 2) freq (Hz)
                    freq_hz = None
                    try:
                        freq_hz = self._stm.get_sensor_frequency_hz()
                    except STM100ProtocolError as e:
                        # 빈값/--------/형식 이상 등: 값 없음으로 처리 (disconnect X)
                        errors.append(f"freq_hz: {e!r}")
                        freq_hz = None

                    # 3) life (%)
                    life = None
                    try:
                        life = self._stm.get_crystal_life_percent()
                    except STM100ProtocolError as e:
                        errors.append(f"life_percent: {e!r}")
                        life = None

                    snap = {
                        "crystal_ok": crystal_ok,
                        "crystal_fail": (None if crystal_ok is None else (not crystal_ok)),
                        "freq_hz": freq_hz,
                        "freq_mhz": (freq_hz / 1_000_000.0) if freq_hz is not None else None,
                        "life_percent": life,
                    }
                    if errors:
                        snap["errors"] = errors  # (원하면 main.py에서 로그로 출력)

                    fut.set_result(snap)

                except Exception as e:
                    # 여기로 오는 건 "연결/포트/치명적 통신" 쪽만 남기고 싶음
                    try:
                        fut.set_exception(e)
                    except Exception:
                        pass

                    self.sig_error.emit(f"[STMService] read_crystal_health failed: {e!r}")

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
                self._next_try = now + float(self._conn_backoff_s)
                self._conn_backoff_s = min(float(self._conn_backoff_s) * float(self._conn_backoff_factor),
                                        float(self._conn_backoff_max_s))
                return

        try:
            self._stm.connect()
            self._set_connected(True)
            self._fail_count = 0
            self._unavailable_count = 0
            self._conn_backoff_s = float(self._conn_backoff_base_s)  # ✅ 성공 시 reset

            # ✅ (1) power lost flag(B) 정리: L ack (실패해도 연결은 유지)
            # 전원 재공급 후 장비가 내부적으로 세운 "전원 손실" 플래그를 해제해야 정상 측정 가능
            try:
                self._stm.ack_power_failure_flag()
            except Exception as e:
                self.sig_error.emit(f"[STMService] ack_power_failure_flag failed: {e!r}")

            # ✅ (2) 1회 프라임 read: 첫 응답 ''/-------- 완화 (실패해도 연결은 유지)
            # 연결 직후 첫 폴링에서 빈 응답이 오는 경우를 처리하기 위한 워밍업 읽기
            try:
                _ = self._stm.get_thickness_angstrom()
                _ = self._stm.get_rate_angstrom_per_s()
            except Exception as e:
                self.sig_error.emit(f"[STMService] prime read failed: {e!r}")
        except Exception as e:
            self._set_connected(False)
            self.sig_error.emit(f"[STMService] connect failed: {e!r}")
            self._safe_close()

            self._next_try = now + float(self._conn_backoff_s)
            self._conn_backoff_s = min(float(self._conn_backoff_s) * float(self._conn_backoff_factor),
                                    float(self._conn_backoff_max_s))

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
            self._unavailable_count = 0
            return True

        except Exception as e:
            if isinstance(e, STM100ValueUnavailableError):
                self._unavailable_count += 1
                # 일시적 "값 없음"은 허용; 연속 N회 초과 시에만 실제 fail 로 간주
                if self._unavailable_count > _MAX_UNAVAILABLE_BEFORE_RECONNECT:
                    self._fail_count += 1
            else:
                self._unavailable_count = 0
                self._fail_count += 1

            self.sig_error.emit(f"[STMService] poll failed: {e!r} (fail={self._fail_count})")

            # ✅ 이번 폴링은 값 없음 → None으로 상태 갱신 + 외부로 emit
            self._last_thickness = None
            self._last_rate = None

            try:
                self.sig_thickness.emit(None)
                self.sig_rate.emit(None)
            except Exception:
                pass

            snap = STMSnapshot(
                ts=now,
                connected=bool(self._connected),   # 아직 close 전이면 True 유지
                thickness_angstrom=None,
                rate_angstrom_per_s=None,
                meta={"fail_count": self._fail_count, "error": repr(e)},
            )
            self._last_snapshot = snap
            try:
                self.sig_snapshot.emit(snap)
            except Exception:
                pass

            # ✅ 연결 실패 누적일 때만 close/reconnect
            if self._fail_count >= self._max_fail_before_close:
                self._safe_close()
                self._set_connected(False)

                self._next_try = now + float(self._conn_backoff_s)
                self._conn_backoff_s = min(
                    float(self._conn_backoff_s) * float(self._conn_backoff_factor),
                    float(self._conn_backoff_max_s),
                )

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
    sig_io_trace = Signal(object)   # ✅ STM100 TX/RX trace

    def __init__(
        self,
        ini_path: str | Path,
        *,
        poll_s: float = 1.0,
        reconnect_interval_s: float = 1.0,
        max_fail_before_close: int = 6,
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
        self._worker.sig_io_trace.connect(self.sig_io_trace)  # ✅ 추가

    def start(self) -> None:
        if not self._worker.isRunning():
            self._worker.start()

    def stop(self, wait_ms: int = 3000) -> None:
        try:
            self._worker.stop()
        except Exception:
            pass
        # 워커가 serial read에서 블로킹 중일 수 있으므로
        # 메인 스레드에서 직접 포트를 닫아 블로킹을 강제 해제한다
        try:
            stm_dev = getattr(self._worker, "_stm", None)
            if stm_dev is not None:
                stm_dev.close()
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


    def submit_read_crystal_health(self) -> Future:
        """
        공정 시작 전 점검용: LIFE(%)/FREQ(Hz)/CRYSTAL FAIL snapshot을 Future로 반환
        """
        if not self.is_running():
            fut: Future = Future()
            fut.set_exception(RuntimeError("STMService is not running"))
            return fut
        return self._worker.request_read_crystal_health()

