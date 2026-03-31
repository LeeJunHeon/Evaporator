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
from configparser import ConfigParser

from PySide6.QtCore import QObject, QThread, Signal

from config.serial_config import load_settings
from devices.acs2000 import ACS2000


# ============================================================
# ACS io_trace 포맷 헬퍼
# ============================================================

def _fmt_pressure_human(pressure: float) -> str:
    """
    지수 표기 압력을 사람이 읽기 쉬운 형태로 변환.
    예) 4.26e-06 → "4.26 × 10⁻⁶ Torr"
    """
    _SUP = {"0":"⁰","1":"¹","2":"²","3":"³","4":"⁴","5":"⁵","6":"⁶","7":"⁷","8":"⁸","9":"⁹","-":"⁻"}

    try:
        p = float(pressure)
        if p == 0.0:
            return "0 Torr"
        import math
        # 지수 부분을 유니코드 위첨자로 변환하여 사람이 읽기 쉬운 형태로 표시
        exp = int(math.floor(math.log10(abs(p))))
        mantissa = p / (10.0 ** exp)
        sup_str = "".join(_SUP.get(c, c) for c in str(exp))
        return f"{mantissa:.2f} × 10{sup_str} Torr"
    except Exception:
        return f"{pressure:.3e} Torr"


def _format_acs_io_trace(d: dict) -> str:
    """
    ACS2000 io_trace dict → 사람이 읽기 쉬운 로그 문자열.
    pressure 필드가 있으면 "[ACS] 챔버 압력: X × 10ⁿ Torr" 형태로 출력.
    """
    ok = bool(d.get("ok", True))
    pressure = d.get("pressure", None)

    if pressure is not None:
        try:
            p = float(pressure)
            human = _fmt_pressure_human(p)
            if not ok:
                return f"[ACS] 챔버 압력(오류): {human}"
            return f"[ACS] 챔버 압력: {human}"
        except Exception:
            pass

    # pressure 없으면 raw 출력
    tx = d.get("tx", "")
    rx = d.get("rx", "")
    token = d.get("token", "")
    detail = d.get("detail", "")
    if not ok:
        return f"[ACS] 통신 오류 — token={token!r} tx={tx!r} rx={rx!r} {detail}".strip()
    return f"[ACS] token={token!r} rx={rx!r}"


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
    sig_io_trace = Signal(object)   # ✅ {"dev":"ACS2000","ok":bool,"token":...,"tx":...,"rx":...,"detail":...,"ts":...}

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
        a = int(stream_interval_a)
        if a not in (0, 1, 2):
            a = 1
        self._stream_interval_a = a

        self._stop_evt = threading.Event()
        self._cmd_q: "queue.Queue[object]" = queue.Queue()

        self._acs: Optional[ACS2000] = None
        self._connected: bool = False

        self._last_pressure: Optional[float] = None
        self._last_snapshot: Optional[ACSSnapshot] = None

        self._fail_count: int = 0
        self._next_try: float = 0.0

        self._conn_backoff_base_s = float(self._reconnect_interval_s)  # ini 로딩 전 임시
        self._conn_backoff_factor = 1.7
        self._conn_backoff_max_s = 5.0
        self._conn_backoff_s = float(self._reconnect_interval_s)

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
            a = int(stream_interval_a)
            if a not in (0, 1, 2):
                a = 1
            self._cmd_q.put_nowait(_CmdSetMode(bool(stream), a))
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
            self._publish_disconnected_snapshot(reason="worker_exit")

    # ---------- internals ----------
    def _build_device_from_ini(self) -> ACS2000:
        s = load_settings(self._ini_path, "acs2000")

        pol = self._load_io_policy_from_ini()

        # ✅ connect 단계 백오프도 동일 정책으로 통일
        self._conn_backoff_base_s = max(0.2, float(pol["reconnect_backoff_base_s"]))
        self._conn_backoff_factor = max(1.0, float(pol["reconnect_backoff_factor"]))
        self._conn_backoff_max_s = max(self._conn_backoff_base_s, float(pol["reconnect_backoff_max_s"]))
        self._conn_backoff_s = self._conn_backoff_base_s  # reset

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

            # ✅ io_policy (devices.ini [io_policy] 공유)
            io_err_allow=int(pol["io_err_allow"]),
            io_retry_sleep_s=float(pol["io_retry_sleep_s"]),
            io_reconnect_max=int(pol["io_reconnect_max"]),
            reconnect_backoff_base_s=float(pol["reconnect_backoff_base_s"]),
            reconnect_backoff_factor=float(pol["reconnect_backoff_factor"]),
            reconnect_backoff_max_s=float(pol["reconnect_backoff_max_s"]),

            # ✅ ACS2000 통신/압력 변화 trace를 서비스로 전달
            io_trace_cb=self._on_acs_io_trace,
        )
    
    def _load_io_policy_from_ini(self) -> Dict[str, Any]:
        """
        devices.ini:
        - [acs2000]에 값이 있으면 우선
        - 없으면 [io_policy] fallback
        """
        cfg = ConfigParser(interpolation=None)
        cfg.read(self._ini_path, encoding="utf-8")

        dev = "acs2000"
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
    
    def _on_acs_io_trace(self, d: dict) -> None:
        """
        devices/acs2000.py에서 io_trace_cb로 올라오는 trace를
        서비스 시그널로 밖에 전달.
        "msg" 필드에 사람이 읽기 쉬운 요약 문자열을 추가해서 emit.
        """
        try:
            d2 = dict(d or {})
            d2.setdefault("dev", "ACS2000")
            d2.setdefault("ts", time.time())
            # 인간 가독형 메시지 추가
            try:
                d2["msg"] = _format_acs_io_trace(d2)
            except Exception:
                d2["msg"] = f"[ACS] pressure={d2.get('pressure')}"
            self.sig_io_trace.emit(d2)
        except Exception:
            pass

    def _publish_disconnected_snapshot(
        self,
        *,
        reason: str,
        extra_meta: Optional[Dict[str, Any]] = None,
        emit_pressure_signal: bool = True,
    ) -> None:
        """
        장비 disconnect/reconnect/reload 시점에
        stale pressure / stale snapshot 이 남지 않도록
        캐시와 UI 표시용 signal/snapshot을 명시적으로 비운다.
        """
        self._last_pressure = None

        if emit_pressure_signal:
            try:
                self.sig_pressure.emit(None)
            except Exception:
                pass

        meta: Dict[str, Any] = {
            "reason": reason,
            "fail_count": self._fail_count,
            "channel": self._channel,
            "use_stream": self._use_stream,
            "stream_interval_a": self._stream_interval_a,
        }
        if extra_meta:
            meta.update(extra_meta)

        snap = ACSSnapshot(
            ts=time.time(),
            connected=False,
            pressure=None,
            meta=meta,
        )
        self._last_snapshot = snap
        try:
            self.sig_snapshot.emit(snap)
        except Exception:
            pass

    def _safe_close(self) -> None:
        if self._acs is None:
            self._last_pressure = None
            return
        try:
            # stream 모드든 아니든 close가 가장 안전
            self._acs.close()
        except Exception:
            pass
        self._acs = None
        self._last_pressure = None

    def _set_connected(self, v: bool) -> None:
        v = bool(v)
        if self._connected != v:
            self._connected = v
            self.sig_connected.emit(v)

    def _apply_stream_mode_if_needed(self) -> None:
        """
        연결 직후 stream 모드 설정이 켜져 있으면 stream 시작.
        stale 제거를 위해 시간 기반 드레인을 추가로 돌리지 않는다.
        connect/_txrx 단계에서 이미 input buffer reset이 수행되므로,
        여기서 추가 드레인을 하면 최신 샘플까지 버릴 수 있다.
        """
        if not self._use_stream:
            return
        if not self._connected or self._acs is None:
            return

        try:
            self._acs.start_pressure_stream(interval_a=self._stream_interval_a)
            self.sig_error.emit(f"[ACSService] stream mode ON (A={self._stream_interval_a})")
        except Exception as e:
            self.sig_error.emit(f"[ACSService] stream start failed -> reconnect: {e!r}")
            self._safe_close()
            self._set_connected(False)
            self._publish_disconnected_snapshot(
                reason="stream_start_failed",
                extra_meta={"err": repr(e)},
            )
            self._next_try = time.time() + 0.1

    def _handle_reload(self, ini_path: Path) -> None:
        self._ini_path = Path(ini_path)
        self._safe_close()
        self._set_connected(False)
        self._fail_count = 0
        self._next_try = 0.0
        self._publish_disconnected_snapshot(reason="reload_ini")
        self.sig_error.emit(f"[ACSService] reload ini -> {self._ini_path}")

    def _handle_set_mode(self, cmd: _CmdSetMode) -> None:
        self._use_stream = bool(cmd.stream)
        a = int(cmd.stream_interval_a)
        if a not in (0, 1, 2):
            a = 1
        self._stream_interval_a = a

        # 이미 연결돼 있다면 즉시 적용(연결 상태에서만)
        if self._connected and self._acs is not None:
            if self._use_stream:
                self._apply_stream_mode_if_needed()
            else:
                # stream 끄는 가장 안전한 방법: close -> reconnect
                self.sig_error.emit("[ACSService] stream mode OFF -> reconnect to reset state")
                self._safe_close()
                self._set_connected(False)
                self._publish_disconnected_snapshot(reason="stream_mode_off_reconnect")
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
                self._publish_disconnected_snapshot(
                    reason="build_device_failed",
                    extra_meta={"err": repr(e)},
                )

                self._next_try = now + float(self._conn_backoff_s)
                self._conn_backoff_s = min(
                    float(self._conn_backoff_s) * float(self._conn_backoff_factor),
                    float(self._conn_backoff_max_s),
                )
                return

        try:
            self._acs.connect()
            self._set_connected(True)
            self._fail_count = 0

            # ✅ 성공하면 connect backoff reset
            self._conn_backoff_s = float(self._conn_backoff_base_s)

            self._apply_stream_mode_if_needed()
        except Exception as e:
            self.sig_error.emit(f"[ACSService] connect failed: {e!r}")
            self._safe_close()
            self._set_connected(False)
            self._publish_disconnected_snapshot(
                reason="connect_failed",
                extra_meta={"err": repr(e)},
            )

            # ✅ 백오프 적용
            self._next_try = now + float(self._conn_backoff_s)
            self._conn_backoff_s = min(
                float(self._conn_backoff_s) * float(self._conn_backoff_factor),
                float(self._conn_backoff_max_s),
            )

    @staticmethod
    def _parse_pressure_any(s: str) -> float:
        """
        stream 라인/응답 문자열에서 압력(float)을 최대한 robust하게 추출.
        예) "$PRD,1,1.23E-6" / "PRD,1,1.23E-6" / "$1.23E-6" 등
        """
        raw = (s or "").strip().replace("$", "").strip()
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if tokens:
            # CSV 형식: 마지막 토큰이 압력값인 경우가 많으므로 역순으로 파싱 시도
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
                sample = self._acs.read_stream_sample_latest(
                    timeout_s=1.5,
                    drain_timeout_s=0.02,
                    max_drain_lines=100,
                )

                if sample.get("ok"):
                    pr = float(sample["pressure"])
                    self._last_pressure = pr
                    self.sig_pressure.emit(pr)
                    pressure_for_snap: Optional[float] = pr
                else:
                    pressure_for_snap = None
                    self.sig_pressure.emit(None)

                snap = ACSSnapshot(
                    ts=now,
                    connected=True,
                    pressure=pressure_for_snap,
                    meta={
                        "fail_count": self._fail_count,
                        "channel": self._channel,
                        "use_stream": True,
                        "stream_interval_a": self._stream_interval_a,
                        "status": sample.get("status"),
                        "status_text": sample.get("status_text"),
                        "raw": sample.get("raw"),
                        "pressure_last": self._last_pressure,
                        "ok": bool(sample.get("ok")),
                        "drained": sample.get("drained", 0),
                    },
                )
                self._last_snapshot = snap
                self.sig_snapshot.emit(snap)

                self._fail_count = 0
                return bool(sample.get("ok"))

            # ---- query mode ----
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
                    "use_stream": False,
                },
            )
            self._last_snapshot = snap
            self.sig_snapshot.emit(snap)

            self._fail_count = 0
            return True

        except Exception as e:
            self._fail_count += 1
            self.sig_error.emit(f"[ACSService] poll failed: {e!r} (fail={self._fail_count})")

            # UI 숫자 stale 방지
            try:
                self.sig_pressure.emit(None)
            except Exception:
                pass

            snap = ACSSnapshot(
                ts=now,
                connected=bool(self._connected),
                pressure=None,
                meta={
                    "fail_count": self._fail_count,
                    "pressure_last_known": self._last_pressure,
                    "channel": self._channel,
                    "use_stream": self._use_stream,
                    "stream_interval_a": self._stream_interval_a,
                    "err": repr(e),
                },
            )
            self._last_snapshot = snap
            self.sig_snapshot.emit(snap)

            if self._fail_count >= self._max_fail_before_close:
                self._safe_close()
                self._set_connected(False)
                self._publish_disconnected_snapshot(
                    reason="poll_fail_disconnect",
                    extra_meta={"err": repr(e)},
                    emit_pressure_signal=False,  # 바로 위에서 이미 emit(None) 했음
                )

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

            self._stop_evt.wait(0.01)


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
    sig_io_trace = Signal(object)   # ✅ ACS trace를 밖으로

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
        self._worker.sig_io_trace.connect(self.sig_io_trace)  # ✅ 추가

    def start(self) -> None:
        if not self._worker.isRunning():
            self._worker.start()

    def is_connected(self) -> bool:
        snap = self._worker.get_last_snapshot()
        if snap is None:
            return False
        try:
            return bool(snap.connected)
        except Exception:
            try:
                return bool(getattr(snap, "connected", False))
            except Exception:
                return False

    def stop(self, wait_ms: int = 3000) -> None:
        try:
            self._worker.stop()
        except Exception:
            pass
        # 워커의 _main_loop이 _stop_evt를 체크하고 자체 종료하도록 대기.
        # 워커 run() finally에서 _safe_close()로 포트를 안전하게 닫음.
        try:
            self._worker.wait(int(wait_ms))
        except Exception:
            pass
        # wait() 타임아웃 시에만 포트 강제 닫기 (워커가 serial.read()에 블로킹된 경우)
        if self._worker.isRunning():
            try:
                acs_dev = getattr(self._worker, "_acs", None)
                if acs_dev is not None:
                    acs_dev.close()
            except Exception:
                pass
            try:
                self._worker.wait(2000)
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
