# -*- coding: utf-8 -*-
"""
devices/plc.py

Evaporator PLC 컨트롤러 (Modbus RTU over RS-232) - Async Wrapper

[DEBUG 강화 버전]
- 연결이 왜 안 되는지 찾기 위해, connect/ping/IO 전 과정에 최대한 많은 로그를 남깁니다.
- logger는 기존과 동일하게 외부에서 주입(예: logging.Logger.info 같은 함수) 가능.
"""

from __future__ import annotations

import asyncio
import time
import sys
import platform
import traceback
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union, TYPE_CHECKING, Callable, TypeVar

import minimalmodbus


# ✅ 타입힌트는 TYPE_CHECKING에서만 import (Pylance/IDE용)
if TYPE_CHECKING:
    from config.plc_config import PLCSettings

# ✅ 런타임은 loader만 필요
try:
    from config.plc_config import load_plc_settings
except Exception:
    load_plc_settings = None  # type: ignore


def _default_devices_ini_path() -> Optional[Path]:
    """
    devices/plc.py 기준으로 project_root/config/devices.ini 를 찾는다.
    (실행 위치가 달라도 최대한 찾도록 후보를 여러 개 둠)
    """
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "config" / "devices.ini",  # project_root/config/devices.ini (권장)
        here.parent / "devices.ini",
        Path.cwd() / "config" / "devices.ini",
        Path.cwd() / "devices.ini",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


# ======================================================
# 1) 주소 맵 (canonical key = binder/런타임에서 반드시 이 이름 사용)
# ======================================================

PLC_COIL_MAP: Dict[str, int] = {
    # --- Rotary Pump / Valves / Turbo ---
    "R_P_SW": 0,    # M00000 (RP)
    "R_V_SW": 1,    # M00001 (RV)
    "F_V_SW": 2,    # M00002 (FV)
    "M_V_SW": 3,    # M00003 (MV)
    "V_V_SW": 4,    # M00004 (V/V, Vent)
    "TMP_SW": 5,    # M00005 (TMP)

    # --- Shutters / Thickness Monitor ---
    "SHUTTER_1_SW": 6,     # M00006
    "SHUTTER_2_SW": 7,     # M00007
    "MAIN_SHUTTER_SW": 8,  # M00008
    "POWER_1_SW": 9,       # M00009
    "POWER_2_SW": 10,      # M0000A
    "FTM_SW": 11,          # M0000B
    "DOOR_SW": 12,         # M0000C

    # --- Utilities / Gas ---
    "AIR_SW": 32,      # M00020
    "WATER_SW": 33,    # M00021
    "GAUGE_1_SW": 34,  # M00022
    "GAUGE_2_SW": 35,  # M00023
}

PLC_REG_MAP: Dict[str, int] = {
    "DAC_POWER_1": 0,  # D00000
    "DAC_POWER_2": 1,  # D00001
}


# ======================================================
# 2) 설정
# ======================================================


@dataclass
class PLCConfig:
    # ✅ Serial / Modbus-RTU (기본값 없음: settings/ini에서 반드시 주입)
    port: str
    method: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: int
    unit: int
    timeout_s: float

    # 요청 간격
    inter_cmd_gap_s: float
    pulse_ms: int

    # DAC
    dac_full_scale_code: int
    dac_offset_code: int
    dac_current_min_ma: float
    dac_current_max_ma: float

    # ✅ IO policy (ini/settings에서 주입)
    io_err_allow: int
    io_retry_sleep_s: float
    io_reconnect_max: int
    reconnect_backoff_base_s: float
    reconnect_backoff_factor: float
    reconnect_backoff_max_s: float

    # ===== DEBUG ===== (디버그는 코드 기본값 유지해도 됨)
    debug: bool = True
    debug_stacktrace: bool = True
    debug_io_timing: bool = True
    debug_config_dump: bool = True
    debug_ping_detail: bool = True
    debug_unit_candidates: Tuple[int, ...] = ()


# ======================================================
# 3) Async PLC
# ======================================================


class AsyncPLC:
    def __init__(
        self,
        port: Optional[str] = None,
        *,
        method: Optional[str] = None,
        baudrate: Optional[int] = None,
        bytesize: Optional[int] = None,
        parity: Any = None,
        stopbits: Optional[int] = None,
        unit: Optional[int] = None,
        timeout_s: Optional[float] = None,
        pulse_ms: Optional[int] = None,
        inter_cmd_gap_s: Optional[float] = None,

        # DAC
        dac_full_scale_code: Optional[int] = None,
        dac_offset_code: Optional[int] = None,
        dac_current_min_ma: Optional[float] = None,
        dac_current_max_ma: Optional[float] = None,

        # ✅ 단일 소스(ini/settings)
        settings: Optional["PLCSettings"] = None,
        ini_path: Optional[Union[str, Path]] = None,

        # debug
        debug: bool = True,
        debug_stacktrace: bool = True,
        debug_io_timing: bool = True,
        debug_config_dump: bool = True,
        debug_ping_detail: bool = True,
        debug_unit_candidates: Tuple[int, ...] = (),

        logger=None,
    ) -> None:
        
        self.log = logger or (lambda *a, **k: None)

        # 1) settings 결정: 인자로 들어오면 그걸 쓰고, 아니면 devices.ini에서 로드
        base = settings
        if base is None:
            if load_plc_settings is None:
                raise RuntimeError("PLC settings loader import failed: config.plc_config.load_plc_settings")

            p = Path(ini_path) if ini_path is not None else _default_devices_ini_path()
            if p is None:
                raise RuntimeError("devices.ini not found (ini_path not provided and default path not found)")
            try:
                # plc_config.py가 ini_path를 필수로 받는 구조라서 Path/str 둘 다 대응
                try:
                    base = load_plc_settings(p)          # 타입이 Path 허용하면 OK
                except TypeError:
                    base = load_plc_settings(str(p))     # 대부분 이 케이스
            except Exception as e:
                raise RuntimeError(f"load_plc_settings failed: {e!r}") from e

        if base is None:
            # 최후 fallback (이 경우는 프로젝트 구조가 깨진 경우라, 가능한 한 안 오게 해야 함)
            raise RuntimeError("PLC settings load failed: plc_config.py import/load_plc_settings unavailable")

        # 2) merge: 명시 인자 > settings(ini) 우선순위
        port_v = str(port if port is not None else base.port)
        method_v = str(method if method is not None else base.method).lower()
        if method_v not in ("rtu",):
            raise ValueError(f"Unsupported PLC method={method_v!r} (only 'rtu' supported)")

        baud_v = int(baudrate if baudrate is not None else base.baudrate)
        bytesize_v = int(bytesize if bytesize is not None else base.bytesize)

        parity_raw = parity if parity is not None else getattr(base, "parity", "N")
        parity_v = self._normalize_parity(parity_raw)

        stop_v = int(stopbits if stopbits is not None else base.stopbits)

        unit_v = int(unit if unit is not None else base.unit)
        if unit_v <= 0:
            unit_v = 1  # ✅ 브로드캐스트 방지

        timeout_v = float(timeout_s if timeout_s is not None else base.timeout_s)
        pulse_v = int(pulse_ms if pulse_ms is not None else base.pulse_ms)

        # inter_cmd_gap_s는 plc_config.py에 추가했을 수도/아직 없을 수도 → getattr fallback
        gap_v = float(inter_cmd_gap_s if inter_cmd_gap_s is not None else base.inter_cmd_gap_s)

        dac_fs_v = int(dac_full_scale_code if dac_full_scale_code is not None else base.dac_full_scale_code)
        dac_off_v = int(dac_offset_code if dac_offset_code is not None else base.dac_offset_code)
        dac_min_v = float(dac_current_min_ma if dac_current_min_ma is not None else base.dac_current_min_ma)
        dac_max_v = float(dac_current_max_ma if dac_current_max_ma is not None else base.dac_current_max_ma)

        io_err_allow_v = int(base.io_err_allow)
        io_retry_sleep_v = float(base.io_retry_sleep_s)
        io_reconnect_max_v = int(base.io_reconnect_max)

        reconnect_base_v = float(base.reconnect_backoff_base_s)
        reconnect_factor_v = float(base.reconnect_backoff_factor)
        reconnect_max_v = float(base.reconnect_backoff_max_s)

        # sanity (숫자 하드코딩 없이, settings 값 기반으로만 보정)
        if io_err_allow_v < 0: io_err_allow_v = 0
        if io_retry_sleep_v < 0: io_retry_sleep_v = 0.0
        if io_reconnect_max_v < 0: io_reconnect_max_v = 0
        if reconnect_base_v <= 0: reconnect_base_v = float(base.reconnect_interval_s)
        if reconnect_factor_v < 1.0: reconnect_factor_v = 1.0
        if reconnect_max_v < reconnect_base_v: reconnect_max_v = reconnect_base_v

        self.cfg = PLCConfig(
            port=port_v,
            method=method_v,
            baudrate=baud_v,
            bytesize=bytesize_v,
            parity=parity_v,
            stopbits=stop_v,
            unit=unit_v,
            timeout_s=timeout_v,
            inter_cmd_gap_s=gap_v,
            pulse_ms=pulse_v,
            dac_full_scale_code=dac_fs_v,
            dac_offset_code=dac_off_v,
            dac_current_min_ma=dac_min_v,
            dac_current_max_ma=dac_max_v,
            # ✅ io_policy
            io_err_allow=io_err_allow_v,
            io_retry_sleep_s=io_retry_sleep_v,
            io_reconnect_max=io_reconnect_max_v,
            reconnect_backoff_base_s=reconnect_base_v,
            reconnect_backoff_factor=reconnect_factor_v,
            reconnect_backoff_max_s=reconnect_max_v,
            debug=bool(debug),
            debug_stacktrace=bool(debug_stacktrace),
            debug_io_timing=bool(debug_io_timing),
            debug_config_dump=bool(debug_config_dump),
            debug_ping_detail=bool(debug_ping_detail),
            debug_unit_candidates=tuple(int(x) for x in (debug_unit_candidates or ())),
        )

        self._inst: Optional[minimalmodbus.Instrument] = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._last_io_ts = 0.0
        self._SYNONYMS: Dict[str, str] = self._build_synonyms()


    # ======================================================
    # logging helpers (logger 형태가 다양해도 최대한 깨지지 않게)
    # ======================================================

    def _emit(self, prefix: str, msg: str, *args) -> None:
        if not self.cfg.debug and prefix.startswith("[DBG"):
            return
        line = f"[PLC]{prefix} {msg}"
        try:
            # logging.Logger.info 스타일(printf) 지원
            self.log(line, *args)
        except TypeError:
            # 단일 문자열만 받는 logger도 지원
            try:
                self.log(line % args if args else line)
            except Exception:
                pass
        except Exception:
            # logger 자체가 문제여도 PLC 로직은 계속
            pass

    def _dbg(self, msg: str, *args) -> None:
        self._emit("[DBG]", msg, *args)

    def _inf(self, msg: str, *args) -> None:
        self._emit("[INF]", msg, *args)

    def _wrn(self, msg: str, *args) -> None:
        self._emit("[WRN]", msg, *args)

    def _err(self, msg: str, *args) -> None:
        self._emit("[ERR]", msg, *args)

    def _exc_text(self) -> str:
        if not self.cfg.debug_stacktrace:
            return ""
        return traceback.format_exc()

    # ======================================================
    # lifecycle
    # ======================================================

    async def connect(self) -> None:
        """포트를 열고 기본 통신(ping)이 되는지 확인."""
        self._closed = False

        if self.cfg.debug_config_dump:
            self._dump_env()
            self._dump_cfg("connect() called")

        async with self._lock:
            t0 = time.monotonic()
            try:
                await asyncio.to_thread(self._connect_sync)
            except Exception as e:
                self._err("connect FAILED: %r", e)
                et = self._exc_text()
                if et:
                    self._err("traceback:\n%s", et)
                raise
            finally:
                if self.cfg.debug_io_timing:
                    self._dbg("connect() total=%.1f ms", (time.monotonic() - t0) * 1000.0)

        self._inf(
            "PLC connected OK: port=%s baud=%s bytesize=%s parity=%s stopbits=%s unit=%s timeout=%.3fs",
            self.cfg.port,
            self.cfg.baudrate,
            self.cfg.bytesize,
            self.cfg.parity,
            self.cfg.stopbits,
            self.cfg.unit,
            self.cfg.timeout_s,
        )
        self._dump_serial_state("after connect")

    async def close(self) -> None:
        """포트 닫기."""
        self._closed = True
        async with self._lock:
            await asyncio.to_thread(self._close_sync)
        self._inf("PLC closed")

    def is_connected(self) -> bool:
        try:
            return bool(self._inst) and bool(self._inst.serial) and bool(self._inst.serial.is_open)
        except Exception:
            return False

    async def ping(self) -> None:
        """연결 확인용(응답성 체크)."""
        async with self._lock:
            t0 = time.monotonic()
            try:
                await asyncio.to_thread(self._connect_sync)
                await self._throttle_locked(op="ping")
                await asyncio.to_thread(self._ping_sync)
                self._touch_io_ts()
                self._inf("PING OK")
            except Exception as e:
                self._err("PING FAILED: %r", e)
                et = self._exc_text()
                if et:
                    self._err("traceback:\n%s", et)
                await asyncio.to_thread(self._close_sync)
                raise
            finally:
                if self.cfg.debug_io_timing:
                    self._dbg("ping() total=%.1f ms", (time.monotonic() - t0) * 1000.0)

    # ======================================================
    # internal: normalize / connect / close
    # ======================================================

    @staticmethod
    def _normalize_parity(parity: Any) -> str:
        """
        parity가 None/'None'/'NONE' 같은 형태로 들어오면 실제 시리얼 parity가 깨질 수 있으므로
        무조건 pyserial이 기대하는 1글자 형태로 정규화.
        """
        if parity is None:
            return "N"
        p = str(parity).strip().upper()
        if p in ("", "NONE", "NO", "N", "0"):
            return "N"
        if p in ("EVEN", "E", "2"):
            return "E"
        if p in ("ODD", "O", "1"):
            return "O"
        if p in ("MARK", "M"):
            return "M"
        if p in ("SPACE", "S"):
            return "S"
        return "N"

    @staticmethod
    def _normalize_port(port: str) -> str:
        """
        Windows에서 COM10 이상은 '\\\\.\\COM10' 형태가 안전합니다.
        COM9 이하도 그대로 써도 되지만, 일관성 위해 COM10+만 보정.
        """
        p = str(port).strip()
        if p.upper().startswith("COM") and len(p) > 4:
            return f"\\\\.\\{p}"
        return p

    def _new_instrument(self, unit: int) -> minimalmodbus.Instrument:
        port_norm = self._normalize_port(self.cfg.port)
        self._dbg("create Instrument: port(raw)=%r port(norm)=%r unit=%s", self.cfg.port, port_norm, unit)

        inst = minimalmodbus.Instrument(port_norm, int(unit))

        # Serial 기본 설정
        inst.serial.baudrate = int(self.cfg.baudrate)
        inst.serial.bytesize = int(self.cfg.bytesize)
        inst.serial.parity = str(self.cfg.parity)  # 이미 1글자 정규화됨
        inst.serial.stopbits = int(self.cfg.stopbits)
        inst.serial.timeout = float(self.cfg.timeout_s)
        inst.serial.write_timeout = float(self.cfg.timeout_s)

        # Flow control OFF
        inst.serial.rtscts = False
        inst.serial.dsrdtr = False

        # RTU 권장 옵션
        inst.mode = minimalmodbus.MODE_RTU
        inst.clear_buffers_before_each_transaction = True
        inst.close_port_after_each_call = False

        self._dbg(
            "serial cfg applied: baud=%s bytesize=%s parity=%r stopbits=%s timeout=%.3f write_timeout=%.3f",
            inst.serial.baudrate,
            inst.serial.bytesize,
            inst.serial.parity,
            inst.serial.stopbits,
            inst.serial.timeout,
            getattr(inst.serial, "write_timeout", None),
        )
        return inst

    def _connect_sync(self) -> None:
        if self._closed:
            raise RuntimeError("PLC is closed")

        # 이미 열려 있으면 그대로 사용
        if self._inst is not None and getattr(self._inst.serial, "is_open", False):
            self._dbg("_connect_sync: already open -> reuse (unit=%s)", getattr(self._inst, "address", None))
            return

        # 후보 unit 구성
        candidates: List[int] = []
        candidates.append(int(self.cfg.unit))

        # cfg.debug_unit_candidates로 추가 지정 가능
        for u in self.cfg.debug_unit_candidates:
            if int(u) not in candidates:
                candidates.append(int(u))

        # 그래도 1은 보정 후보로
        if 1 not in candidates:
            candidates.append(1)

        self._inf("_connect_sync: start (port=%r unit_candidates=%s)", self.cfg.port, candidates)

        last_err: Optional[Exception] = None
        for uid in candidates:
            self._inf("_connect_sync: try unit=%s", uid)
            try:
                inst = self._new_instrument(uid)

                # 명시적으로 open
                if not inst.serial.is_open:
                    self._dbg("serial.open() ...")
                    inst.serial.open()
                    self._dbg("serial.open() done (is_open=%s)", inst.serial.is_open)
                else:
                    self._dbg("serial already open (is_open=%s)", inst.serial.is_open)

                self._inst = inst
                self._dump_serial_state(f"after open (unit={uid})")

                # 최소 ping(응답 확인)
                self._ping_sync()

                # 성공 → unit 확정
                self.cfg.unit = int(uid)
                self._touch_io_ts()
                self._inf("_connect_sync: SUCCESS unit=%s", uid)
                return

            except Exception as e:
                last_err = e
                self._err("_connect_sync: FAIL unit=%s err=%r", uid, e)
                et = self._exc_text()
                if et:
                    self._err("traceback:\n%s", et)

                # 현재 인스턴스 닫기
                try:
                    if self._inst and self._inst.serial and self._inst.serial.is_open:
                        self._dbg("serial.close() due to connect fail")
                        self._inst.serial.close()
                except Exception as e2:
                    self._wrn("serial.close() failed: %r", e2)

                self._inst = None

        raise RuntimeError(f"Modbus RTU connect/ping failed (last={last_err!r})")

    def _close_sync(self) -> None:
        if self._inst is not None:
            try:
                if self._inst.serial and self._inst.serial.is_open:
                    self._dbg("_close_sync: serial.close()")
                    self._inst.serial.close()
            except Exception as e:
                self._wrn("_close_sync: close failed: %r", e)
        self._inst = None

    def _ping_sync(self) -> None:
        """
        ping은 '읽기 1회'로 끝내는 게 가장 안전합니다.
        - 환경에 따라 coil 0이 막혀 있을 수 있으니 0/32/5 순서로 짧게 시도합니다.
        """
        assert self._inst is not None

        addrs = (0, 32, 5)
        self._inf("_ping_sync: start (try coils=%s, fc=1)", addrs)

        last: Optional[Exception] = None
        for addr in addrs:
            try:
                t0 = time.monotonic()
                if self.cfg.debug_ping_detail:
                    self._dbg("_ping_sync: read_bits(addr=%s, count=1, fc=1)", addr)

                bits = self._inst.read_bits(int(addr), 1, functioncode=1)  # FC1 Read Coils

                if self.cfg.debug_io_timing:
                    self._dbg("_ping_sync: addr=%s done in %.1f ms -> %r", addr, (time.monotonic() - t0) * 1000.0, bits)

                if bits and len(bits) >= 1:
                    self._inf("_ping_sync: OK (addr=%s -> %r)", addr, bits)
                    return
            except Exception as e:
                last = e
                if self.cfg.debug_ping_detail:
                    self._wrn("_ping_sync: addr=%s failed: %r", addr, e)
        raise IOError(f"No response on ping (tried coils {addrs}, last={last!r})")

    # ======================================================
    # throttle
    # ======================================================

    async def _throttle_locked(self, op: str = "io") -> None:
        gap = float(self.cfg.inter_cmd_gap_s)
        dt = time.monotonic() - float(self._last_io_ts)
        if dt < gap:
            sleep_s = gap - dt
            if self.cfg.debug:
                self._dbg("throttle(%s): dt=%.4fs < gap=%.4fs -> sleep %.4fs", op, dt, gap, sleep_s)
            await asyncio.sleep(sleep_s)
        else:
            if self.cfg.debug:
                self._dbg("throttle(%s): dt=%.4fs >= gap=%.4fs -> no sleep", op, dt, gap)

    def _touch_io_ts(self) -> None:
        self._last_io_ts = time.monotonic()

    # ======================================================
    # addressing & synonyms
    # ======================================================

    def _build_synonyms(self) -> Dict[str, str]:
        def norm(x: str) -> str:
            return x.upper().replace(" ", "").replace("_", "").replace("-", "").replace("/", "")

        syn: Dict[str, str] = {}

        syn[norm("RP")] = "R_P_SW"
        syn[norm("RV")] = "R_V_SW"
        syn[norm("FV")] = "F_V_SW"
        syn[norm("MV")] = "M_V_SW"
        syn[norm("VENT")] = "V_V_SW"
        syn[norm("VV")] = "V_V_SW"
        syn[norm("V/V")] = "V_V_SW"
        syn[norm("TMP")] = "TMP_SW"

        syn[norm("SHUTTER1")] = "SHUTTER_1_SW"
        syn[norm("SHUTTER2")] = "SHUTTER_2_SW"
        syn[norm("MAINSHUTTER")] = "MAIN_SHUTTER_SW"
        syn[norm("FTM")] = "FTM_SW"
        syn[norm("DOOR")] = "DOOR_SW"
        syn[norm("POWER1")] = "POWER_1_SW"
        syn[norm("POWER2")] = "POWER_2_SW"

        syn[norm("AIR")] = "AIR_SW"
        syn[norm("WATER")] = "WATER_SW"
        syn[norm("G1")] = "GAUGE_1_SW"
        syn[norm("G2")] = "GAUGE_2_SW"

        syn[norm("DAC1")] = "DAC_POWER_1"
        syn[norm("DAC2")] = "DAC_POWER_2"
        syn[norm("DACPOWER1")] = "DAC_POWER_1"
        syn[norm("DACPOWER2")] = "DAC_POWER_2"
        return syn

    @staticmethod
    def _parse_m_device_to_coil(s: str) -> int:
        t = s.strip().upper()
        if not t.startswith("M"):
            raise ValueError(f"not M device: {s}")
        return int(t[1:], 16)

    @staticmethod
    def _parse_d_device_to_reg(s: str) -> int:
        t = s.strip().upper()
        if not t.startswith("D"):
            raise ValueError(f"not D device: {s}")
        num = t[1:]
        base = 16 if any(c in "ABCDEF" for c in num) else 10
        return int(num, base)

    def _addr(self, name_or_addr: Any) -> int:
        if isinstance(name_or_addr, int):
            return int(name_or_addr)

        key_raw = str(name_or_addr).strip()
        if not key_raw:
            raise ValueError("empty address/name")

        if key_raw in PLC_COIL_MAP:
            return int(PLC_COIL_MAP[key_raw])
        if key_raw in PLC_REG_MAP:
            return int(PLC_REG_MAP[key_raw])

        nk = key_raw.upper().replace(" ", "").replace("_", "").replace("-", "").replace("/", "")
        if nk in self._SYNONYMS:
            canonical = self._SYNONYMS[nk]
            if canonical in PLC_COIL_MAP:
                return int(PLC_COIL_MAP[canonical])
            if canonical in PLC_REG_MAP:
                return int(PLC_REG_MAP[canonical])

        up = key_raw.upper()
        if up.startswith("M"):
            return self._parse_m_device_to_coil(up)
        if up.startswith("D"):
            return self._parse_d_device_to_reg(up)

        return int(key_raw, 0)

    def _is_reg_name(self, name: Any) -> bool:
        if isinstance(name, int):
            return False

        s = str(name).strip()

        # ✅ 코일 맵에 있는 이름은 무조건 COIL (DOOR_SW 같은 케이스 보호)
        if s in PLC_COIL_MAP:
            return False

        # ✅ 레지스터 맵에 있는 이름만 REG (현재는 DAC_POWER_1/2만 해당)
        if s in PLC_REG_MAP:
            return True

        nk = s.upper().replace(" ", "").replace("_", "").replace("-", "").replace("/", "")
        if nk in self._SYNONYMS and self._SYNONYMS[nk] in PLC_REG_MAP:
            return True

        # ✅ "D00000" 처럼 'D' 다음이 전부 숫자일 때만 REG로 본다
        up = s.upper()
        tail = up[1:]
        if up.startswith("D") and tail and all(c in "0123456789ABCDEF" for c in tail):
            return True

        return False
    
    # ======================================================
    # I/O 공통 헬퍼
    # ======================================================
    
    _T = TypeVar("_T")

    def _clear_serial_buffers_sync(self) -> None:
        """연결 유지 재시도 시, 버퍼만 정리(가능할 때). 실패해도 무시."""
        try:
            if not self._inst:
                return
            ser = getattr(self._inst, "serial", None)
            if not ser:
                return
            # pyserial 표준 API
            if hasattr(ser, "reset_input_buffer"):
                ser.reset_input_buffer()
            if hasattr(ser, "reset_output_buffer"):
                ser.reset_output_buffer()
        except Exception:
            return

    async def _io_with_policy_locked(self, op: str, fn: Callable[[], _T]) -> _T:
        """
        ✅ 정책:
        - 동일 I/O 호출에서 에러 1~5회: 연결 유지하며 재시도(버퍼만 정리 + 짧은 sleep)
        - 6회째: close→reconnect(backoff) 후 재시도
        - io_reconnect_max만큼 재연결 시도 후에도 실패하면 raise
        """
        allow = int(self.cfg.io_err_allow)
        sleep_s = float(self.cfg.io_retry_sleep_s)
        reconnect_left = int(self.cfg.io_reconnect_max)
        backoff_s = float(self.cfg.reconnect_backoff_base_s)
        factor = float(self.cfg.reconnect_backoff_factor)
        backoff_max = float(self.cfg.reconnect_backoff_max_s)

        err_count = 0

        while True:
            try:
                await asyncio.to_thread(self._connect_sync)
                await self._throttle_locked(op=op)

                # fn은 sync 함수(Instrument 호출)여야 함
                out = await asyncio.to_thread(fn)
                self._touch_io_ts()
                return out

            except Exception as e:
                err_count += 1
                self._wrn("%s fail #%d err=%r", op, err_count, e)

                # 1~5회: 연결 유지 재시도(버퍼만 정리)
                if err_count <= allow:
                    await asyncio.to_thread(self._clear_serial_buffers_sync)
                    if sleep_s > 0:
                        await asyncio.sleep(sleep_s)
                    continue

                # 6회째부터: reconnect
                if reconnect_left <= 0:
                    self._err("%s failed: errors=%d, reconnect exhausted -> raise", op, err_count)
                    # 마지막은 닫아두는 게 안전
                    await asyncio.to_thread(self._close_sync)
                    raise

                reconnect_left -= 1
                self._wrn("%s: errors=%d -> reconnect (left=%d), backoff=%.2fs",
                        op, err_count, reconnect_left, backoff_s)

                await asyncio.to_thread(self._close_sync)
                await asyncio.sleep(backoff_s)

                # backoff 업데이트
                backoff_s = min(backoff_s * factor, backoff_max)

                # reconnect 후에는 에러 카운트 리셋
                err_count = 0
                continue

    # ======================================================
    # low-level I/O (로그 최대)
    # ======================================================

    async def read_coil(self, addr: int) -> bool:
        addr = int(addr)
        async with self._lock:
            t0 = time.monotonic()
            self._dbg("READ_COIL start addr=%s (FC1)", addr)
            try:
                def _call():
                    assert self._inst is not None
                    return self._inst.read_bits(int(addr), 1, functioncode=1)  # FC1

                bits = await self._io_with_policy_locked(f"READ_COIL(addr={addr})", _call)
                v = bool(bits[0]) if bits else False
                self._dbg("READ_COIL ok addr=%s -> %s (raw=%r)", addr, v, bits)
                return v
            finally:
                if self.cfg.debug_io_timing:
                    self._dbg("READ_COIL time=%.1f ms", (time.monotonic() - t0) * 1000.0)

    async def read_coils_block(self, start_addr: int, count: int) -> List[bool]:
        start_addr = int(start_addr)
        count = max(1, int(count))
        async with self._lock:
            t0 = time.monotonic()
            self._dbg("READ_COILS_BLOCK start start=%s count=%s (FC1)", start_addr, count)
            try:
                def _call():
                    assert self._inst is not None
                    return self._inst.read_bits(int(start_addr), int(count), functioncode=1)  # FC1

                bits = await self._io_with_policy_locked(
                    f"READ_COILS_BLOCK(start={start_addr},count={count})",
                    _call,
                )

                if len(bits) < count:
                    bits = list(bits) + [0] * (count - len(bits))
                out = [bool(b) for b in bits[:count]]
                self._dbg("READ_COILS_BLOCK ok start=%s count=%s -> %r", start_addr, count, out)
                return out
            finally:
                if self.cfg.debug_io_timing:
                    self._dbg("READ_COILS_BLOCK time=%.1f ms", (time.monotonic() - t0) * 1000.0)

    async def write_coil(self, addr: int, value: bool) -> None:
        addr = int(addr)
        value = bool(value)
        async with self._lock:
            t0 = time.monotonic()
            self._dbg("WRITE_COIL start addr=%s value=%s (FC5)", addr, value)
            try:
                def _call():
                    assert self._inst is not None
                    return self._inst.write_bit(int(addr), 1 if value else 0, functioncode=5)  # FC5

                await self._io_with_policy_locked(f"WRITE_COIL(addr={addr})", _call)
                self._dbg("WRITE_COIL ok addr=%s value=%s", addr, value)
            finally:
                if self.cfg.debug_io_timing:
                    self._dbg("WRITE_COIL time=%.1f ms", (time.monotonic() - t0) * 1000.0)

    async def read_reg(self, addr: int) -> int:
        addr = int(addr)
        async with self._lock:
            t0 = time.monotonic()
            self._dbg("READ_REG start addr=%s (FC3)", addr)
            try:
                def _call():
                    assert self._inst is not None
                    return self._inst.read_register(int(addr), 0, 3, False)  # FC3

                v = await self._io_with_policy_locked(f"READ_REG(addr={addr})", _call)
                out = int(v)
                self._dbg("READ_REG ok addr=%s -> %s", addr, out)
                return out
            finally:
                if self.cfg.debug_io_timing:
                    self._dbg("READ_REG time=%.1f ms", (time.monotonic() - t0) * 1000.0)

    async def write_reg(self, addr: int, value: int) -> None:
        addr = int(addr)
        value = int(value)
        async with self._lock:
            t0 = time.monotonic()
            self._dbg("WRITE_REG start addr=%s value=%s (FC6)", addr, value)
            try:
                def _call():
                    assert self._inst is not None
                    return self._inst.write_register(int(addr), int(value), 0, 6, False)  # FC6

                await self._io_with_policy_locked(f"WRITE_REG(addr={addr})", _call)
                self._dbg("WRITE_REG ok addr=%s value=%s", addr, value)
            finally:
                if self.cfg.debug_io_timing:
                    self._dbg("WRITE_REG time=%.1f ms", (time.monotonic() - t0) * 1000.0)

    # ======================================================
    # high-level helpers
    # ======================================================

    async def pulse(self, addr: int, *, ms: Optional[int] = None) -> None:
        width = self.cfg.pulse_ms if ms is None else int(ms)
        self._dbg("PULSE start addr=%s width=%sms", addr, width)
        await self.write_coil(int(addr), True)
        await asyncio.sleep(max(0.01, width / 1000.0))
        await self.write_coil(int(addr), False)
        self._dbg("PULSE done addr=%s", addr)

    async def write_switch(
        self,
        name_or_addr: Any,
        on: bool,
        *,
        momentary: bool = False,
        pulse_ms: Optional[int] = None,
    ) -> None:
        if self._is_reg_name(name_or_addr):
            raise TypeError(f"write_switch는 COIL 전용입니다. register로 보이는 입력: {name_or_addr}")

        addr = self._addr(name_or_addr)
        self._dbg("WRITE_SWITCH name=%r -> addr=%s on=%s momentary=%s", name_or_addr, addr, on, momentary)
        if momentary:
            await self.pulse(addr, ms=pulse_ms)
        else:
            await self.write_coil(addr, bool(on))

    async def read_bit(self, name_or_addr: Any) -> bool:
        if self._is_reg_name(name_or_addr):
            raise TypeError(f"read_bit은 COIL 전용입니다. register로 보이는 입력: {name_or_addr}")
        addr = self._addr(name_or_addr)
        self._dbg("READ_BIT name=%r -> addr=%s", name_or_addr, addr)
        return bool(await self.read_coil(addr))

    async def read_reg_name(self, name_or_addr: Any) -> int:
        addr = self._addr(name_or_addr)
        self._dbg("READ_REG_NAME name=%r -> addr=%s", name_or_addr, addr)
        return int(await self.read_reg(addr))

    async def write_reg_name(self, name_or_addr: Any, value: int) -> None:
        addr = self._addr(name_or_addr)
        self._dbg("WRITE_REG_NAME name=%r -> addr=%s value=%s", name_or_addr, addr, value)
        await self.write_reg(addr, int(value))

    # --------------------------------------------------
    # 고수준 API (호출부 수정 최소화)
    # --------------------------------------------------
    async def rp(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("RP", on, momentary=momentary)
    async def rv(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("RV", on, momentary=momentary)
    async def fv(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("FV", on, momentary=momentary)
    async def mv(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("MV", on, momentary=momentary)
    async def vv(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("V/V", on, momentary=momentary)
    async def tmp(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("TMP", on, momentary=momentary)

    async def air(self) -> bool: 
        return bool(await self.read_bit("AIR"))

    async def water(self) -> bool:
        return bool(await self.read_bit("WATER"))

    async def gauge1(self) -> bool:
        return bool(await self.read_bit("G1"))

    async def gauge2(self) -> bool:
        return bool(await self.read_bit("G2"))

    async def shutter1(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("SHUTTER1", on, momentary=momentary)
    async def shutter2(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("SHUTTER2", on, momentary=momentary)
    async def main_shutter(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("MAINSHUTTER", on, momentary=momentary)
    async def ftm(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("FTM", on, momentary=momentary)

    async def power1(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("POWER1", on, momentary=momentary)
    async def power2(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("POWER2", on, momentary=momentary)
    async def door(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("DOOR", on, momentary=momentary)

    # ======================================================
    # DAC helpers (4~20mA)
    # ======================================================

    def _clamp_dac_code(self, code: int) -> int:
        fs = int(self.cfg.dac_full_scale_code)
        if fs <= 0:
            raise ValueError(f"dac_full_scale_code must be > 0 (now={fs})")

        offset = int(self.cfg.dac_offset_code)
        lo, hi = offset, offset + fs

        c = int(code)
        if c < lo:
            c = lo
        elif c > hi:
            c = hi
        return c

    async def set_dac_power(self, ch: int, code: int) -> None:
        key = "DAC_POWER_1" if int(ch) == 1 else "DAC_POWER_2" if int(ch) == 2 else None
        if key is None:
            raise ValueError("DAC channel must be 1 or 2")
        self._dbg("SET_DAC_POWER ch=%s code=%s", ch, code)
        await self.write_reg_name(key, self._clamp_dac_code(int(code)))

    async def set_dac_current(self, ch: int, ma: float) -> int:
        mn = float(self.cfg.dac_current_min_ma)
        mx = float(self.cfg.dac_current_max_ma)
        if mx <= mn:
            raise ValueError(f"Invalid current range: {mn}..{mx}")

        i = float(ma)
        if i < mn:
            i = mn
        elif i > mx:
            i = mx

        x = (i - mn) / (mx - mn)  # 0..1
        fs = int(self.cfg.dac_full_scale_code)
        offset = int(self.cfg.dac_offset_code)

        code = int(round(x * fs)) + offset
        code = self._clamp_dac_code(code)

        self._dbg("SET_DAC_CURRENT ch=%s ma=%.3f -> code=%s (mn=%.3f mx=%.3f fs=%s offset=%s)",
                  ch, ma, code, mn, mx, fs, offset)

        await self.set_dac_power(ch, code)
        return code

    # ======================================================
    # debug dumps
    # ======================================================

    def _dump_env(self) -> None:
        self._inf("ENV python=%s platform=%s", sys.version.replace("\n", " "), platform.platform())
        self._inf("ENV minimalmodbus=%s", getattr(minimalmodbus, "__version__", "unknown"))

    def _dump_cfg(self, title: str) -> None:
        self._inf("CFG DUMP (%s)", title)
        self._inf("  port=%r (norm=%r)", self.cfg.port, self._normalize_port(self.cfg.port))
        self._inf("  baudrate=%s bytesize=%s parity=%r stopbits=%s unit=%s timeout_s=%.3f",
                  self.cfg.baudrate, self.cfg.bytesize, self.cfg.parity, self.cfg.stopbits, self.cfg.unit, self.cfg.timeout_s)
        self._inf("  inter_cmd_gap_s=%.4f pulse_ms=%s", self.cfg.inter_cmd_gap_s, self.cfg.pulse_ms)
        self._inf("  debug=%s stack=%s timing=%s ping_detail=%s extra_units=%s",
                  self.cfg.debug, self.cfg.debug_stacktrace, self.cfg.debug_io_timing, self.cfg.debug_ping_detail, self.cfg.debug_unit_candidates)

    def _dump_serial_state(self, where: str) -> None:
        try:
            if not self._inst:
                self._dbg("SERIAL STATE (%s): inst=None", where)
                return
            ser = getattr(self._inst, "serial", None)
            if not ser:
                self._dbg("SERIAL STATE (%s): serial=None", where)
                return
            self._dbg("SERIAL STATE (%s): is_open=%s port=%r baud=%s bytesize=%s parity=%r stopbits=%s timeout=%r write_timeout=%r",
                      where,
                      getattr(ser, "is_open", None),
                      getattr(ser, "port", None),
                      getattr(ser, "baudrate", None),
                      getattr(ser, "bytesize", None),
                      getattr(ser, "parity", None),
                      getattr(ser, "stopbits", None),
                      getattr(ser, "timeout", None),
                      getattr(ser, "write_timeout", None))
        except Exception as e:
            self._wrn("_dump_serial_state failed: %r", e)
