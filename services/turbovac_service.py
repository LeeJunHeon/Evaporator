# services/turbovac_service.py
from __future__ import annotations

import configparser
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, Signal

from devices.turbovac import Turbovac, TurbovacSnapshot


class TurbovacService(QObject):
    """
    TMP(TURBOVAC) USB 서비스

    역할
    - devices.ini 에서 통신 설정 로드
    - 백그라운드 thread 에서 자동 연결/재연결
    - 주기적 상태 polling
    - UI 연결용 signal emit
    - start / stop / reset 요청 직렬화

    UI 연결 권장:
    - sig_snapshot -> tmpConnEdit/tmpStateEdit/tmpFreqEdit/tmpCurrentEdit/tmpTempEdit/tmpAlarmEdit
    """

    sig_connected = Signal(bool)
    sig_snapshot = Signal(object)   # dict
    sig_error = Signal(str)
    sig_log = Signal(str)

    def __init__(
        self,
        ini_path: str | Path,
        *,
        poll_s: float = 1.0,
        slow_poll_s: float = 5.0,
        reconnect_interval_s: float = 1.0,
    ) -> None:
        super().__init__()

        self._ini_path = Path(ini_path)
        self._poll_s = max(0.2, float(poll_s))
        self._slow_poll_s = max(self._poll_s, float(slow_poll_s))
        self._reconnect_interval_s = max(0.5, float(reconnect_interval_s))

        self._lock = threading.RLock()
        self._cmd_q: Queue[tuple[str, Dict[str, Any]]] = Queue()

        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._dev: Optional[Turbovac] = None
        self._cfg: Dict[str, Any] = {}

        self._running = False
        self._connected = False
        self._last_snapshot: Optional[Dict[str, Any]] = None
        self._last_error_text: str = ""
        self._last_connect_try_ts: float = 0.0

        # poll/read 실패 관리
        self._poll_fail_count: int = 0
        self._poll_fail_threshold: int = 3
        self._last_good_snapshot_ts: float = 0.0

        # reconnect backoff 관리
        self._connect_fail_count: int = 0
        self._reconnect_backoff_s: float = self._reconnect_interval_s
        self._reconnect_backoff_max_s: float = 30.0

        # slow poll 관리
        self._last_slow_poll_ts: float = 0.0

        # 최초 연결/재연결 시 attach 전략 힌트
        # False: idle 기준 attach
        # True : 이미 회전 중인 TMP 상태 유지 기준 attach
        self._connect_hint_running: bool = False

        self._load_config()

    # ---------------------------------------------------------
    # config
    # ---------------------------------------------------------
    def _load_config(self) -> None:
        cp = configparser.ConfigParser()
        if not self._ini_path.exists():
            raise FileNotFoundError(f"devices.ini not found: {self._ini_path}")

        cp.read(self._ini_path, encoding="utf-8")

        if "turbovac" not in cp:
            raise KeyError(f"[turbovac] section not found in {self._ini_path}")

        sec = cp["turbovac"]

        self._cfg = {
            "port": sec.get("port", "").strip(),
            "baudrate": sec.getint("baudrate", fallback=19200),
            "bytesize": sec.getint("bytesize", fallback=8),
            "parity": sec.get("parity", "E").strip().upper(),
            "stopbits": sec.getint("stopbits", fallback=1),
            "timeout_s": sec.getfloat("timeout_s", fallback=0.5),
            "write_timeout_s": sec.getfloat("write_timeout_s", fallback=0.5),
            "rtscts": sec.getboolean("rtscts", fallback=False),
            "dsrdtr": sec.getboolean("dsrdtr", fallback=False),
        }

        if not self._cfg["port"]:
            raise ValueError("[turbovac] port is empty in devices.ini")

    # ---------------------------------------------------------
    # lifecycle
    # ---------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        with self._lock:
            if self._running:
                return

            self._stop_evt.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="TurbovacService",
                daemon=True,
            )
            self._running = True
            self._thread.start()
            self.sig_log.emit("[TMPService] started")

    def stop(self, *, close_device: bool = True) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop_evt.set()

        th = self._thread
        if th and th.is_alive():
            th.join(timeout=3.0)

        self._thread = None

        if close_device:
            self._close_device()

        self._emit_connected(False)
        self.sig_log.emit("[TMPService] stopped")

    # ---------------------------------------------------------
    # public commands
    # ---------------------------------------------------------
    def start_pump(self, *, setpoint_hz: Optional[int] = None) -> None:
        """
        TMP raw start command.
        최종 인터락(RP/FV/Vent 등)은 상위 모듈에서 확인 후 호출해야 한다.
        """
        self._cmd_q.put(("start", {"setpoint_hz": setpoint_hz}))

    def stop_pump(self) -> None:
        self._cmd_q.put(("stop", {}))

    def reset_error(self) -> None:
        self._cmd_q.put(("reset_error", {}))

    def reload_config(self) -> None:
        self._cmd_q.put(("reload_config", {}))

    def request_snapshot(self) -> None:
        self._cmd_q.put(("snapshot", {}))

    def get_last_snapshot(self) -> Optional[Dict[str, Any]]:
        return dict(self._last_snapshot) if self._last_snapshot else None
    
    def set_connect_hint_running(self, running: bool) -> None:
        """
        다음 최초 연결/재연결 시 attach 전략을 지정한다.

        True  -> running hold 기준으로 attach
        False -> idle 기준으로 attach
        """
        with self._lock:
            self._connect_hint_running = bool(running)

    # ---------------------------------------------------------
    # interlock helper
    # ---------------------------------------------------------
    # 아래 helper 들은 외부 인터락 용도이므로 그대로 유지
    def has_snapshot(self) -> bool:
        return self._last_snapshot is not None

    def is_tmp_connected(self) -> bool:
        snap = self._last_snapshot or {}
        return bool(snap.get("connected", False))

    def is_tmp_normal(self) -> bool:
        snap = self._last_snapshot or {}
        return bool(
            snap.get("connected", False)
            and snap.get("normal_operation", False)
            and not self.has_tmp_error()
        )

    def is_tmp_turning(self) -> bool:
        snap = self._last_snapshot or {}
        return bool(snap.get("connected", False) and snap.get("pump_turning", False))

    def is_tmp_stopped(self) -> bool:
        snap = self._last_snapshot or {}
        if not snap.get("connected", False):
            return False
        return (
            not snap.get("pump_turning", False)
            and not snap.get("accelerating", False)
            and not snap.get("decelerating", False)
            and int(snap.get("freq_hz", 0) or 0) <= 0
        )

    def has_tmp_error(self) -> bool:
        snap = self._last_snapshot or {}
        sw = int(snap.get("status_word", 0) or 0)
        # status_word 비트 3: CANopen/장비 표준 에러 플래그 위치
        return bool(sw & (1 << 3))

    def has_tmp_warning(self) -> bool:
        snap = self._last_snapshot or {}
        return bool(
            snap.get("temp_warning", False)
            or snap.get("overload_warning", False)
            or snap.get("collective_warning", False)
            or int(snap.get("warning_bits", 0) or 0) != 0
        )

    def get_alarm_text(self) -> str:
        snap = self._last_snapshot or {}
        text = str(snap.get("alarm_text", "") or "").strip()
        return text if text not in ("", "-") else "---"

    def check_tmp_ready_for_power(self) -> tuple[bool, str]:
        if not self.is_tmp_connected():
            return False, "TMP 연결 안됨"

        if self.has_tmp_error():
            return False, f"TMP 에러 상태: {self.get_alarm_text()}"

        snap = self._last_snapshot or {}

        if snap.get("decelerating", False):
            return False, "TMP 감속 중"

        if not snap.get("normal_operation", False):
            return False, "TMP 정상 운전 아님"

        return True, ""

    def check_tmp_ready_for_mv(self) -> tuple[bool, str]:
        return self.check_tmp_ready_for_power()
    
    def check_tmp_ready_for_start(self) -> tuple[bool, str]:
        """
        TMP 자체 상태만 보고 START command 허용 여부 판단.
        PLC 상태(Air/Water/RP 등)는 상위 모듈에서 별도로 판단한다.

        정책:
        - 강한 차단: 미연결 / 에러 / switch-on lock
        - 감속중(decelerating): 재기동 허용
        - 이미 가속중/회전중인 경우: 중복 START 요청으로 보고 안내 메시지 반환
        """
        if not self.is_tmp_connected():
            return False, "TMP 연결 안됨"

        if self.has_tmp_error():
            return False, f"TMP 에러 상태: {self.get_alarm_text()}"

        snap = self._last_snapshot or {}

        if snap.get("switch_on_lock", False):
            return False, "TMP switch-on lock 상태"

        # 감속중은 재기동 허용
        if snap.get("accelerating", False):
            return False, "TMP 이미 가속 중"

        if snap.get("pump_turning", False):
            return False, "TMP 이미 회전 중입니다. 연결은 정상이며 추가 START는 필요 없습니다."

        return True, ""

    def check_tmp_safe_for_vent(self) -> tuple[bool, str]:
        if not self.is_tmp_connected():
            return False, "TMP 연결 안됨"

        if self.has_tmp_error():
            return False, f"TMP 에러 상태: {self.get_alarm_text()}"

        snap = self._last_snapshot or {}

        if snap.get("pump_turning", False):
            return False, "TMP 회전 중"

        if snap.get("accelerating", False):
            return False, "TMP 가속 중"

        if snap.get("decelerating", False):
            return False, "TMP 감속 중"

        if int(snap.get("freq_hz", 0) or 0) > 0:
            return False, f"TMP 주파수 남아있음: {int(snap.get('freq_hz', 0))} Hz"

        return True, ""

    def check_tmp_safe_for_door(self) -> tuple[bool, str]:
        return self.check_tmp_safe_for_vent()

    # ---------------------------------------------------------
    # worker
    # ---------------------------------------------------------
    def _worker_loop(self) -> None:
        next_poll_ts = 0.0

        while not self._stop_evt.is_set():
            try:
                self._drain_commands(max_count=8)

                if not self._connected:
                    self._ensure_connected()
                    time.sleep(0.05)
                    continue

                now = time.monotonic()
                if now >= next_poll_ts:
                    include_extended = (now - self._last_slow_poll_ts) >= self._slow_poll_s
                    self._poll_once(include_extended=include_extended)

                    if include_extended:
                        self._last_slow_poll_ts = now

                    next_poll_ts = now + self._poll_s

                time.sleep(0.05)

            except Exception as e:
                self._handle_runtime_error(f"[TMPService] worker error: {e!r}")
                time.sleep(0.2)

    def _drain_commands(self, max_count: int = 8) -> None:
        for _ in range(max_count):
            try:
                name, kwargs = self._cmd_q.get_nowait()
            except Empty:
                return

            try:
                if name == "start":
                    self._cmd_start(**kwargs)
                elif name == "stop":
                    self._cmd_stop(**kwargs)
                elif name == "reset_error":
                    self._cmd_reset_error(**kwargs)
                elif name == "reload_config":
                    self._cmd_reload_config(**kwargs)
                elif name == "snapshot":
                    self._cmd_snapshot(**kwargs)
            except Exception as e:
                self._handle_runtime_error(f"[TMPService] command '{name}' failed: {e!r}")

    # ---------------------------------------------------------
    # commands
    # ---------------------------------------------------------
    def _cmd_start(self, *, setpoint_hz: Optional[int] = None) -> None:
        # START 의도가 들어왔으면, 재연결 시에도 running 기준 attach
        self._connect_hint_running = True

        dev = self._require_device(allow_connect=True)
        dev.start_pump(setpoint_hz=setpoint_hz)
        self.sig_log.emit(
            f"[TMPService] start_pump sent"
            + (f" (setpoint_hz={int(setpoint_hz)})" if setpoint_hz is not None else "")
        )
        # 즉시 poll 생략: 다음 주기 poll에서 상태 반영

    def _cmd_stop(self) -> None:
        # STOP 의도가 들어왔으면, 재연결 시 idle 기준 attach
        self._connect_hint_running = False

        dev = self._require_device(allow_connect=True)
        dev.stop_pump()
        self.sig_log.emit("[TMPService] stop_pump sent")
        # 즉시 poll 생략

    def _cmd_reset_error(self) -> None:
        dev = self._require_device(allow_connect=True)
        dev.reset_error()
        self.sig_log.emit("[TMPService] reset_error sent")
        # 즉시 poll 생략

    def _cmd_reload_config(self) -> None:
        self.sig_log.emit("[TMPService] reloading config")
        self._load_config()
        self._close_device()
        self._ensure_connected(force=True)

    def _cmd_snapshot(self) -> None:
        self._poll_once(include_extended=True)

    # ---------------------------------------------------------
    # connect / close / poll
    # ---------------------------------------------------------
    def _build_device(self) -> Turbovac:
        return Turbovac(
            port=self._cfg["port"],
            baudrate=self._cfg["baudrate"],
            bytesize=self._cfg["bytesize"],
            parity=self._cfg["parity"],
            stopbits=self._cfg["stopbits"],
            timeout_s=self._cfg["timeout_s"],
            write_timeout_s=self._cfg["write_timeout_s"],
            rtscts=self._cfg["rtscts"],
            dsrdtr=self._cfg["dsrdtr"],
        )

    def _ensure_connected(self, *, force: bool = False) -> None:
        if self._connected and not force and self._dev is not None:
            return

        now = time.monotonic()
        wait_s = self._reconnect_interval_s if force else self._reconnect_backoff_s
        if not force and (now - self._last_connect_try_ts) < wait_s:
            return
        self._last_connect_try_ts = now

        self._close_device()

        try:
            dev = self._build_device()

            # 핵심:
            # 최초 attach/reconnect 시 현재 의도에 맞는 전략으로 probe
            # assume_running=True 면 이미 회전 중인 TMP와 연결 시 제어권을 안전하게 가져옴
            assume_running = bool(self._connect_hint_running)
            fast = dev.connect_and_probe(assume_running=assume_running)

            self._dev = dev
            self._emit_connected(True)

            # 성공 시 실패 상태 초기화
            self._connect_fail_count = 0
            self._poll_fail_count = 0
            self._reconnect_backoff_s = self._reconnect_interval_s

            # 연결 직후에는 fast probe 결과만 반영하고,
            # 첫 scheduled poll 은 fast-only 로 시작되게 한다.
            self._last_slow_poll_ts = time.monotonic()

            d = self._fast_probe_to_dict(fast)
            self._last_snapshot = d
            self.sig_snapshot.emit(d)
            self._last_good_snapshot_ts = time.time()
            self._update_connect_hint_from_snapshot(d)

            self.sig_log.emit(
                f"[TMPService] connected: port={self._cfg['port']} "
                f"baud={self._cfg['baudrate']} {self._cfg['bytesize']}{self._cfg['parity']}{self._cfg['stopbits']} "
                f"attach={'running' if assume_running else 'idle'} "
                f"state={d.get('state_text', '-')} freq={int(d.get('freq_hz', 0) or 0)} Hz"
            )

        except Exception as e:
            self._dev = None
            self._emit_connected(False)

            self._connect_fail_count += 1
            self._reconnect_backoff_s = min(
                self._reconnect_backoff_max_s,
                max(self._reconnect_interval_s, self._reconnect_backoff_s * 2),
            )

            msg = (
                f"[TMPService] connect failed "
                f"(retry in {self._reconnect_backoff_s:.1f}s): {e!r}"
            )
            if msg != self._last_error_text:
                self._last_error_text = msg
                self.sig_error.emit(msg)
                self.sig_log.emit(msg)

    def _close_device(self) -> None:
        dev = self._dev
        self._dev = None

        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass

        self._emit_connected(False)

    def _poll_once(self, *, include_extended: bool = True) -> None:
        dev = self._require_device(allow_connect=False)
        try:
            snap = dev.read_snapshot(include_extended=include_extended)
            d = self._snapshot_to_dict(snap)
            self._last_snapshot = d
            self.sig_snapshot.emit(d)

            self._last_error_text = ""
            self._emit_connected(True)

            # 현재 실측 상태를 바탕으로 다음 reconnect attach 힌트 갱신
            self._update_connect_hint_from_snapshot(d)

            # 성공 시 실패 카운터 초기화
            self._poll_fail_count = 0
            self._last_good_snapshot_ts = time.time()

        except Exception as e:
            self._poll_fail_count += 1

            msg = (
                f"[TMPService] poll failed "
                f"({self._poll_fail_count}/{self._poll_fail_threshold}): {e!r}"
            )
            if msg != self._last_error_text:
                self._last_error_text = msg
                self.sig_error.emit(msg)
                self.sig_log.emit(msg)

            # threshold 전까지는 연결 유지 / 마지막 정상 snapshot 유지
            if self._poll_fail_count < self._poll_fail_threshold:
                return

            # threshold 초과 시에만 close + backoff reconnect
            self._handle_runtime_error(
                f"[TMPService] poll failed repeatedly "
                f"({self._poll_fail_count} times): {e!r}"
            )

    # ---------------------------------------------------------
    # error handling
    # ---------------------------------------------------------
    def _handle_runtime_error(self, msg: str) -> None:
        # 여기로 들어오는 경우는 연속 poll 실패 threshold 초과 또는 worker fatal
        # 중요: 여기서도 stop_pump() 는 절대 자동 호출하지 않음
        self._close_device()

        self._connect_fail_count += 1
        self._reconnect_backoff_s = min(
            self._reconnect_backoff_max_s,
            max(self._reconnect_interval_s, self._reconnect_backoff_s * 2),
        )

        if msg != self._last_error_text:
            self._last_error_text = msg
            self.sig_error.emit(msg)
            self.sig_log.emit(msg)

        prev_alarm = "-"
        if self._last_snapshot:
            prev_alarm = str(self._last_snapshot.get("alarm_text", "-") or "-")

        linklost_alarm = f"{prev_alarm} / LinkLost" if prev_alarm not in ("", "-", "---") else "LinkLost"

        disconnected = {
            "ts": time.time(),
            "connected": False,
            "state_text": "",
            "freq_hz": 0,
            "current_a": 0.0,
            "motor_temp_c": None,
            "converter_temp_c": None,
            "bearing_temp_c": None,
            "dc_bus_v": None,
            "warning_bits": 0,
            "last_error_code": 0,
            "last_error_freq_hz": None,
            "last_error_hours": None,
            "alarm_text": linklost_alarm,
            "detail_text": "---",
            "status_word": 0,
            "control_word": 0,
            "ready": False,
            "operation_enabled": False,
            "pump_turning": False,
            "normal_operation": False,
            "accelerating": False,
            "decelerating": False,
            "switch_on_lock": False,
            "temp_warning": False,
            "overload_warning": False,
            "collective_warning": False,
            "meta": {},
            "ui": {
                "conn": "Disconnected",
                "state": "DISCONNECTED",
                "freq": "---",
                "current": "---",
                "temp": "---",
                "alarm": linklost_alarm,
                "detail": "---",
            },
        }
        self._last_snapshot = disconnected
        self.sig_snapshot.emit(disconnected)

    # ---------------------------------------------------------
    # helpers
    # ---------------------------------------------------------
    def _require_device(self, *, allow_connect: bool) -> Turbovac:
        if self._dev is None:
            if allow_connect:
                self._ensure_connected(force=True)
            if self._dev is None:
                raise RuntimeError("Turbovac device is not connected")
        return self._dev

    def _emit_connected(self, connected: bool) -> None:
        if self._connected == connected:
            return
        self._connected = connected
        self.sig_connected.emit(connected)

    def _make_tmp_detail_text(self, d: Dict[str, Any]) -> str:
        parts: list[str] = []

        conv = d.get("converter_temp_c", None)
        bear = d.get("bearing_temp_c", None)
        dc = d.get("dc_bus_v", None)

        try:
            if conv is not None:
                parts.append(f"CV {float(conv):.1f}C")
        except Exception:
            pass

        try:
            if bear is not None:
                parts.append(f"BR {float(bear):.1f}C")
        except Exception:
            pass

        try:
            if dc is not None:
                parts.append(f"DC {float(dc):.0f}V")
        except Exception:
            pass

        if parts:
            return " | ".join(parts)

        try:
            warn_bits = int(d.get("warning_bits", 0) or 0)
        except Exception:
            warn_bits = 0

        if warn_bits:
            return f"WARN 0x{warn_bits:04X}"

        try:
            last_error_code = int(d.get("last_error_code", 0) or 0)
        except Exception:
            last_error_code = 0

        if last_error_code:
            return f"LAST ERR {last_error_code}"

        return "---"

    def _snapshot_to_dict(self, snap: TurbovacSnapshot | Dict[str, Any]) -> Dict[str, Any]:
        if is_dataclass(snap):
            d = asdict(snap)
        else:
            d = dict(snap)

        alarm_text = str(d.get("alarm_text", "") or "").strip()
        if alarm_text in ("", "-"):
            alarm_text = "---"

        detail_text = self._make_tmp_detail_text(d)

        ui = {
            "conn": "Connected" if d.get("connected", True) else "Disconnected",
            "state": str(d.get("state_text", "-")),
            "freq": f"{int(d.get('freq_hz', 0))} Hz" if d.get("connected", True) else "---",
            "current": (
                f"{float(d.get('current_a', 0.0)):.1f} A"
                if d.get("connected", True)
                else "---"
            ),
            "temp": (
                f"{int(d['motor_temp_c'])} °C"
                if d.get("connected", True) and d.get("motor_temp_c") is not None
                else "---"
            ),
            "alarm": alarm_text,
            "detail": detail_text,
        }

        d["detail_text"] = detail_text
        d["ui"] = ui
        return d
    
    def _update_connect_hint_from_snapshot(self, snap: Dict[str, Any]) -> None:
        if not snap.get("connected", False):
            return

        # 감속 중이면 stop 의도를 우선
        if snap.get("decelerating", False):
            self._connect_hint_running = False
            return

        if (
            snap.get("pump_turning", False)
            or snap.get("accelerating", False)
            or snap.get("normal_operation", False)
        ):
            self._connect_hint_running = True
            return

        if int(snap.get("freq_hz", 0) or 0) <= 0:
            self._connect_hint_running = False

    def _fast_probe_to_dict(self, fast: Dict[str, Any]) -> Dict[str, Any]:
        d = dict(fast)

        d["ts"] = time.time()
        d["connected"] = True

        d.setdefault("motor_temp_c", None)
        d.setdefault("converter_temp_c", None)
        d.setdefault("bearing_temp_c", None)
        d.setdefault("dc_bus_v", None)

        d.setdefault("warning_bits", 0)
        d.setdefault("last_error_code", 0)
        d.setdefault("last_error_freq_hz", None)
        d.setdefault("last_error_hours", None)
        d.setdefault("alarm_text", "---")

        d.setdefault("ready", False)
        d.setdefault("operation_enabled", False)
        d.setdefault("pump_turning", False)
        d.setdefault("normal_operation", False)
        d.setdefault("accelerating", False)
        d.setdefault("decelerating", False)
        d.setdefault("switch_on_lock", False)
        d.setdefault("temp_warning", False)
        d.setdefault("overload_warning", False)
        d.setdefault("collective_warning", False)

        d["control_word"] = int(getattr(self._dev, "_control_word", 0) or 0)

        meta = dict(d.get("meta") or {})
        meta["source"] = "fast_probe"
        d["meta"] = meta

        return self._snapshot_to_dict(d)