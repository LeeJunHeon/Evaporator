# -*- coding: utf-8 -*-
# plc.py
"""
PLC.py — Evaporator용 Modbus-RTU(RS-232) PLC 컨트롤러 (Async)

- asyncio + pymodbus 동기클라이언트 안전 사용: 직렬화(lock), to_thread, 명령 간격 보장,
  heartbeat, unit/slave 자동 판별, 연결 끊김 재연결
- 주소맵(Mxxxx, Dxxxx)에 맞춘 COIL/REGISTER 맵 제공
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException
from pymodbus.pdu import ExceptionResponse

# ✅ pymodbus 3.x: framer 사용, 구버전: method 사용 → 둘 다 되게 처리
try:
    from pymodbus import FramerType
    RTU_FRAMER = FramerType.RTU
except Exception:
    RTU_FRAMER = "rtu"


# ======================================================
# 1) 주소 맵
# ======================================================
# ⚠️ LS PLC(XG5000)에서 M 디바이스 주소는 16진 표기인 경우가 많습니다.
#    예) M0000B = 0x0B = 11, M00020 = 0x20 = 32
# 이 dict 값은 pymodbus에 전달되는 "Modbus coil address (0-based int)" 입니다.

PLC_COIL_MAP: Dict[str, int] = {
    # --- Rotary Pump / Valves / Turbo ---
    "R_P_SW": 0,    # M00000 (RP)
    "R_V_SW": 1,    # M00001 (RV)
    "F_V_SW": 2,    # M00002 (FV)
    "M_V_SW": 3,    # M00003 (MV)
    "V_V_SW": 4,    # M00004 (V/V)
    "TMP_SW": 5,    # M00005 (TMP)

    # --- Shutters / Thickness Monitor ---
    "SHUTTER_1_SW": 6,    # M00006 (Shutter1)
    "SHUTTER_2_SW": 7,    # M00007 (Shutter2)
    "MAIN_SHUTTER_SW": 8, # M00008 (Main Shutter)
    "POWER_1_SW": 9,      # M00009 (POWER1)
    "POWER_2_SW": 10,     # M0000A (POWER2)
    "FTM_SW": 11,         # M0000B (FTM)
    "DOOR_SW": 12,        # M0000C (DOOR)

    # --- Utilities / Gas ---
    "AIR_SW": 32,    # M00020 (Air)
    "WATER_SW": 33,  # M00021 (Water)
    "GAS_1_SW": 34,  # M00022 (G1)
    "GAS_2_SW": 35,  # M00023 (G2)
}

# D영역(아날로그 출력)도 PLC 문서 기준 주석을 명확히 남깁니다.
PLC_REG_MAP: Dict[str, int] = {
    "DAC_POWER_1": 0,  # D00000
    "DAC_POWER_2": 1,  # D00001
}


# ======================================================
# 2) 설정
# ======================================================

@dataclass
class PLCConfig:
    port: str = "COM5"
    method: str = "rtu"       # (구버전용) 보통 rtu
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    unit: int = 1
    timeout_s: float = 2.0

    inter_cmd_gap_s: float = 0.12
    heartbeat_s: float = 15.0
    pulse_ms: int = 180

    lock_warn_ms: float = 800.0
    io_warn_ms: float = 1500.0

    # ===== DAC (4~20mA 전용) =====
    # full_scale_code: 4~20mA의 "span" 코드값 (예: 4000 또는 4095 등)
    # offset_code    : 4mA에 해당하는 최소 코드값 (대부분 0이지만 장비/모듈에 따라 다를 수 있음)
    dac_full_scale_code: int = 4000
    dac_offset_code: int = 0

    dac_current_min_ma: float = 4.0
    dac_current_max_ma: float = 20.0


# ======================================================
# 3) Async PLC 클래스
# ======================================================

class AsyncPLC:
    def __init__(
        self,
        port: str = "COM5",
        *,
        method: str = "rtu",
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        unit: int = 1,
        timeout_s: float = 2.0,
        pulse_ms: int = 180,

        # ✅ DAC(4~20mA) 스케일 파라미터(ini에서 주입)
        dac_full_scale_code: int = 4000,
        dac_offset_code: int = 0,
        dac_current_min_ma: float = 4.0,
        dac_current_max_ma: float = 20.0,

        logger=None,
    ):
        self.cfg = PLCConfig(
            port=port,
            method=method,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            unit=unit,
            timeout_s=timeout_s,
            pulse_ms=pulse_ms,

            # ✅ 주입
            dac_full_scale_code=int(dac_full_scale_code),
            dac_offset_code=int(dac_offset_code),
            dac_current_min_ma=float(dac_current_min_ma),
            dac_current_max_ma=float(dac_current_max_ma),
        )

        self._client: Optional[ModbusSerialClient] = None
        self._uid_kw: Optional[str] = None  # 'unit' or 'slave'

        self._lock = asyncio.Lock()
        self._last_io_ts = 0.0

        self._hb_task: Optional[asyncio.Task] = None
        self._hb_paused: bool = False
        self._closed: bool = False

        self.log = logger or (lambda *a, **k: None)
        self._SYNONYMS: Dict[str, str] = self._build_synonyms()

    # --------------------------
    # lifecycle
    # --------------------------
    async def connect(self) -> None:
        self._closed = False
        async with self._io_lock("connect"):
            await asyncio.to_thread(self._connect_sync)

        self.log(
            "Serial(Modbus-RTU) 연결 성공: port=%s baud=%s parity=%s stopbits=%s (unit=%s)",
            self.cfg.port, self.cfg.baudrate, self.cfg.parity, self.cfg.stopbits, self.cfg.unit
        )

        if self._hb_task is None or self._hb_task.done():
            self._hb_task = asyncio.create_task(self._heartbeat_loop(), name="PLCHeartbeat")

    async def close(self) -> None:
        self._closed = True

        if self._hb_task:
            self._hb_task.cancel()
            try:
                await self._hb_task
            except Exception:
                pass
            self._hb_task = None

        async with self._io_lock("close"):
            await asyncio.to_thread(self._close_sync)

        self.log("Serial(Modbus-RTU) 연결 종료")

    def is_connected(self) -> bool:
        try:
            return bool(self._client) and bool(getattr(self._client, "connected", False))
        except Exception:
            return False

    async def __aenter__(self) -> "AsyncPLC":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # --------------------------
    # sync connect/close
    # --------------------------
    def _connect_sync(self) -> None:
        if self._client is None:
            kwargs = dict(
                port=self.cfg.port,
                baudrate=self.cfg.baudrate,
                bytesize=self.cfg.bytesize,
                parity=self.cfg.parity,
                stopbits=self.cfg.stopbits,
                timeout=self.cfg.timeout_s,
            )
            # ✅ pymodbus 3.x: framer, 구버전: method
            try:
                self._client = ModbusSerialClient(**kwargs, framer=RTU_FRAMER)
            except TypeError:
                self._client = ModbusSerialClient(**kwargs, method=self.cfg.method)

        if not self._client.connect():
            raise RuntimeError("Modbus RTU(RS-232) 연결 실패")

        self._last_io_ts = time.monotonic()

        # device_id/slave/unit 키워드 자동 판별(한 번만)
        if self._uid_kw is None:
            try:
                sig = inspect.signature(self._client.write_coil)
                if "device_id" in sig.parameters:
                    self._uid_kw = "device_id"
                elif "slave" in sig.parameters:
                    self._uid_kw = "slave"
                elif "unit" in sig.parameters:
                    self._uid_kw = "unit"
                else:
                    self._uid_kw = None
            except Exception:
                self._uid_kw = None

    def _close_sync(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None

    def _uid_kwargs(self) -> dict:
        if self._uid_kw:
            return {self._uid_kw: self.cfg.unit}
        return {}

    def _ensure_ok(self, resp):
        if resp is None:
            raise ModbusException("응답 없음(None)")
        if isinstance(resp, ExceptionResponse):
            raise ModbusException(f"Modbus ExceptionResponse: {resp}")
        if hasattr(resp, "isError") and resp.isError():
            raise ModbusException(str(resp))
        return resp

    def _is_reset_err(self, e: Exception) -> bool:
        s = str(e).lower()
        return ("10054" in s) or ("reset by peer" in s) or ("connectionreseterror" in s)

    # --------------------------
    # lock + throttle + heartbeat
    # --------------------------
    @asynccontextmanager
    async def _io_lock(self, op: str, **meta):
        """I/O 직렬화 + 디버깅(락 대기/임계구역 IO 시간 경고). meta에 addr/count 등 확장 가능."""
        loop = asyncio.get_running_loop()
        t0 = loop.time()

        await self._lock.acquire()
        try:
            waited_ms = (loop.time() - t0) * 1000.0

            addr = meta.get("addr", None)
            count = meta.get("count", None)
            extra = []
            if addr is not None:
                extra.append(f"addr={addr}")
            if count is not None:
                extra.append(f"count={count}")
            extra_s = (" [" + ", ".join(extra) + "]") if extra else ""

            if waited_ms >= self.cfg.lock_warn_ms:
                self.log("WARN lock-wait %.0f ms (op=%s)%s", waited_ms, op, extra_s)

            t_in = loop.time()
            yield
            io_ms = (loop.time() - t_in) * 1000.0
            if io_ms >= self.cfg.io_warn_ms:
                self.log("WARN in-lock IO %.0f ms (op=%s)%s", io_ms, op, extra_s)
        finally:
            self._lock.release()

    async def _throttle_and_heartbeat(self) -> None:
        now = time.monotonic()
        delta = now - self._last_io_ts

        if delta < self.cfg.inter_cmd_gap_s:
            await asyncio.sleep(self.cfg.inter_cmd_gap_s - delta)

        if (delta > self.cfg.heartbeat_s) and (not self._hb_paused) and (self._client is not None):
            try:
                await asyncio.to_thread(self._client.read_coils, 0, 1, **self._uid_kwargs())
            except Exception:
                pass

        self._last_io_ts = time.monotonic()

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(max(1.0, self.cfg.heartbeat_s / 3.0))
                if self._closed or self._hb_paused:
                    continue
                try:
                    async with self._io_lock("heartbeat", addr=0):
                        if self._client is None:
                            continue
                        await asyncio.to_thread(self._client.read_coils, 0, 1, **self._uid_kwargs())
                        self._last_io_ts = time.monotonic()
                except Exception:
                    continue
        except asyncio.CancelledError:
            return

    @asynccontextmanager
    async def pause_heartbeat(self):
        old = self._hb_paused
        self._hb_paused = True
        try:
            yield
        finally:
            self._hb_paused = old

    # --------------------------
    # name/address parsing
    # --------------------------
    def _build_synonyms(self) -> Dict[str, str]:
        syn: Dict[str, str] = {}

        def norm(s: str) -> str:
            return (
                s.strip().upper()
                .replace(" ", "")
                .replace("_", "")
                .replace("-", "")
                .replace("/", "")
            )

        for k in PLC_COIL_MAP.keys():
            syn[norm(k)] = k
        for k in PLC_REG_MAP.keys():
            syn[norm(k)] = k

        syn[norm("RP")] = "R_P_SW"
        syn[norm("RV")] = "R_V_SW"
        syn[norm("FV")] = "F_V_SW"
        syn[norm("MV")] = "M_V_SW"
        syn[norm("VV")] = "V_V_SW"
        syn[norm("V/V")] = "V_V_SW"
        syn[norm("VENT")] = "V_V_SW"
        syn[norm("TMP")] = "TMP_SW"

        syn[norm("SHUTTER1")] = "SHUTTER_1_SW"
        syn[norm("SHUTTER2")] = "SHUTTER_2_SW"
        syn[norm("MAINSHUTTER")] = "MAIN_SHUTTER_SW"
        syn[norm("MS")] = "MAIN_SHUTTER_SW"

        syn[norm("AIR")] = "AIR_SW"
        syn[norm("WATER")] = "WATER_SW"

        syn[norm("G1")] = "GAS_1_SW"
        syn[norm("G2")] = "GAS_2_SW"
        syn[norm("GAS1")] = "GAS_1_SW"
        syn[norm("GAS2")] = "GAS_2_SW"

        syn[norm("POWER1")] = "POWER_1_SW"
        syn[norm("POWER2")] = "POWER_2_SW"
        syn[norm("DOOR")] = "DOOR_SW"
        syn[norm("FTM")] = "FTM_SW"

        syn[norm("DACPOWER1")] = "DAC_POWER_1"
        syn[norm("DACPOWER2")] = "DAC_POWER_2"
        syn[norm("DAC1")] = "DAC_POWER_1"
        syn[norm("DAC2")] = "DAC_POWER_2"

        return syn

    def _parse_m_device_to_coil(self, s: str) -> int:
        t = s.strip().upper()
        if not t.startswith("M"):
            raise ValueError(f"not M device: {s}")
        return int(t[1:], 16)

    def _parse_d_device_to_reg(self, s: str) -> int:
        t = s.strip().upper()
        if not t.startswith("D"):
            raise ValueError(f"not D device: {s}")
        num = t[1:]
        base = 16 if any(c in "ABCDEF" for c in num) else 10
        return int(num, base)

    def _addr(self, name_or_addr: Any) -> int:
        if isinstance(name_or_addr, int):
            return name_or_addr

        key_raw = str(name_or_addr).strip()
        if not key_raw:
            raise ValueError("empty address/name")

        if key_raw in PLC_COIL_MAP:
            return PLC_COIL_MAP[key_raw]
        if key_raw in PLC_REG_MAP:
            return PLC_REG_MAP[key_raw]

        nk = (
            key_raw.upper()
            .replace(" ", "")
            .replace("_", "")
            .replace("-", "")
            .replace("/", "")
        )
        if nk in self._SYNONYMS:
            canonical = self._SYNONYMS[nk]
            if canonical in PLC_COIL_MAP:
                return PLC_COIL_MAP[canonical]
            if canonical in PLC_REG_MAP:
                return PLC_REG_MAP[canonical]

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
        if s in PLC_REG_MAP:
            return True
        nk = (
            s.upper()
            .replace(" ", "")
            .replace("_", "")
            .replace("-", "")
            .replace("/", "")
        )
        if nk in self._SYNONYMS and self._SYNONYMS[nk] in PLC_REG_MAP:
            return True
        if s.upper().startswith("D"):
            return True
        return False

    # --------------------------
    # low-level I/O
    # --------------------------
    async def read_coil(self, addr: int) -> bool:
        async with self._io_lock("read_coil", addr=addr):
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_and_heartbeat()
            resp = await asyncio.to_thread(self._client.read_coils, addr, 1, **self._uid_kwargs())
            self._ensure_ok(resp)
            return bool(resp.bits[0])

    async def read_coils_block(self, start_addr: int, count: int) -> list[bool]:
        start_addr = int(start_addr)
        count = max(1, int(count))

        async with self._io_lock("read_coils_block", addr=start_addr, count=count):
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_and_heartbeat()
            resp = await asyncio.to_thread(self._client.read_coils, start_addr, count, **self._uid_kwargs())
            self._ensure_ok(resp)
            bits = list(getattr(resp, "bits", []) or [])
            if len(bits) < count:
                bits.extend([False] * (count - len(bits)))
            return [bool(b) for b in bits[:count]]

    async def write_coil(self, addr: int, value: bool) -> None:
        async with self._io_lock("write_coil", addr=addr):
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_and_heartbeat()
            resp = await asyncio.to_thread(self._client.write_coil, addr, bool(value), **self._uid_kwargs())
            self._ensure_ok(resp)

    async def read_reg(self, addr: int) -> int:
        async with self._io_lock("read_reg", addr=addr):
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_and_heartbeat()
            resp = await asyncio.to_thread(self._client.read_holding_registers, addr, 1, **self._uid_kwargs())
            self._ensure_ok(resp)
            return int(resp.registers[0])

    async def write_reg(self, addr: int, value: int) -> None:
        async with self._io_lock("write_reg", addr=addr):
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_and_heartbeat()
            resp = await asyncio.to_thread(self._client.write_register, addr, int(value), **self._uid_kwargs())
            self._ensure_ok(resp)

    # --------------------------
    # high-level helpers
    # --------------------------
    async def pulse(self, addr: int, *, ms: Optional[int] = None) -> None:
        width = self.cfg.pulse_ms if ms is None else int(ms)
        await self.write_coil(addr, True)
        await asyncio.sleep(max(0.01, width / 1000.0))
        await self.write_coil(addr, False)

    async def write_switch(self, name_or_addr: Any, on: bool, *, momentary: bool = False, pulse_ms: Optional[int] = None) -> None:
        addr = self._addr(name_or_addr)

        if self._is_reg_name(name_or_addr):
            raise TypeError(f"write_switch는 COIL 전용입니다. register로 보이는 입력: {name_or_addr}")

        if momentary:
            await self.pulse(addr, ms=pulse_ms)
        else:
            await self.write_coil(addr, bool(on))

    async def read_bit(self, name_or_addr: Any) -> bool:
        addr = self._addr(name_or_addr)
        if self._is_reg_name(name_or_addr):
            raise TypeError(f"read_bit은 COIL 전용입니다. register로 보이는 입력: {name_or_addr}")
        return bool(await self.read_coil(addr))

    async def read_reg_name(self, name_or_addr: Any) -> int:
        addr = self._addr(name_or_addr)
        return int(await self.read_reg(addr))

    async def write_reg_name(self, name_or_addr: Any, value: int) -> None:
        addr = self._addr(name_or_addr)
        await self.write_reg(addr, int(value))

    # --------------------------------------------------
    # 고수준 API(너 기존 그대로)
    # --------------------------------------------------
    async def rp(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("RP", on, momentary=momentary)
    async def rv(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("RV", on, momentary=momentary)
    async def fv(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("FV", on, momentary=momentary)
    async def mv(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("MV", on, momentary=momentary)
    async def vv(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("V/V", on, momentary=momentary)
    async def tmp(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("TMP", on, momentary=momentary)

    async def air(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("AIR", on, momentary=momentary)
    async def water(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("WATER", on, momentary=momentary)
    async def gas1(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("G1", on, momentary=momentary)
    async def gas2(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("G2", on, momentary=momentary)

    async def shutter1(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("SHUTTER1", on, momentary=momentary)
    async def shutter2(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("SHUTTER2", on, momentary=momentary)
    async def main_shutter(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("MAINSHUTTER", on, momentary=momentary)
    async def ftm(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("FTM", on, momentary=momentary)

    async def power1(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("POWER1", on, momentary=momentary)
    async def power2(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("POWER2", on, momentary=momentary)
    async def door(self, on: bool = True, *, momentary: bool = False) -> None: await self.write_switch("DOOR", on, momentary=momentary)

    def _clamp_dac_code(self, code: int) -> int:
        fs = int(self.cfg.dac_full_scale_code)
        if fs <= 0:
            raise ValueError(f"dac_full_scale_code must be > 0 (now={fs})")

        offset = int(self.cfg.dac_offset_code)
        lo, hi = offset, offset + fs  # ✅ offset 포함 범위

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

        # ✅ 안전: 범위 강제
        await self.write_reg_name(key, self._clamp_dac_code(code))

    async def set_dac_current(self, ch: int, ma: float) -> int:
        """
        4~20mA(Current) → DAC 코드로 변환해서 D00000/D00001에 기록
        """
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

        await self.set_dac_power(ch, code)
        return code
