# -*- coding: utf-8 -*-
# plc.py
"""
Evaporator PLC (Modbus RTU / RS-232) - Async Wrapper

✅ 이번 버전은 "구버전 호환 제거" (pymodbus 3.x 기준)
- framer=FramerType.RTU 고정
- unit/slave 파라미터는 slave 사용
- RS-232 안정화: (1) 연결 직후 버퍼 reset (2) 매 트랜잭션 전 입력버퍼 reset
- QThread(PlcWorker)에서 asyncio loop로 안전하게 사용 가능하도록 lock/to_thread 유지
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from pymodbus.client import ModbusSerialClient
from pymodbus import FramerType
from pymodbus.exceptions import ModbusException
from pymodbus.pdu import ExceptionResponse


# ======================================================
# 1) 주소 맵 (너가 쓰던 그대로 유지)
# ======================================================

PLC_COIL_MAP: Dict[str, int] = {
    "R_P_SW": 0,    # M00000
    "R_V_SW": 1,    # M00001
    "F_V_SW": 2,    # M00002
    "M_V_SW": 3,    # M00003
    "V_V_SW": 4,    # M00004
    "TMP_SW": 5,    # M00005

    "SHUTTER_1_SW": 6,     # M00006
    "SHUTTER_2_SW": 7,     # M00007
    "MAIN_SHUTTER_SW": 8,  # M00008
    "POWER_1_SW": 9,       # M00009
    "POWER_2_SW": 10,      # M0000A
    "FTM_SW": 11,          # M0000B
    "DOOR_SW": 12,         # M0000C

    "AIR_SW": 32,          # M00020
    "WATER_SW": 33,        # M00021
    "GAS_1_SW": 34,        # M00022
    "GAS_2_SW": 35,        # M00023
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
    port: str = "COM8"
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    unit: int = 0
    timeout_s: float = 0.5

    # RS-232에서 너무 빠르게 때리면 간헐 timeout이 늘어날 수 있어 gap을 둔다
    inter_cmd_gap_s: float = 0.02

    # heartbeat는 있어도 되고 없어도 되는데, 일단 유지(너 기존 구조 호환)
    heartbeat_s: float = 10.0

    pulse_ms: int = 180
    lock_warn_ms: float = 800.0
    io_warn_ms: float = 1500.0

    # ✅ RS-232 안정화(찌꺼기 바이트 제거)
    reset_buffers_on_connect: bool = True
    reset_input_buffer_each_io: bool = True

    # ===== DAC (4~20mA 전용) =====
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
        *,
        port: str,
        baudrate: int,
        bytesize: int,
        parity: str,
        stopbits: int,
        unit: int,
        timeout_s: float,
        pulse_ms: int = 180,

        dac_full_scale_code: int = 4000,
        dac_offset_code: int = 0,
        dac_current_min_ma: float = 4.0,
        dac_current_max_ma: float = 20.0,

        logger=None,
    ):
        self.cfg = PLCConfig(
            port=port,
            baudrate=int(baudrate),
            bytesize=int(bytesize),
            parity=str(parity),
            stopbits=int(stopbits),
            unit=int(unit),
            timeout_s=float(timeout_s),
            pulse_ms=int(pulse_ms),

            dac_full_scale_code=int(dac_full_scale_code),
            dac_offset_code=int(dac_offset_code),
            dac_current_min_ma=float(dac_current_min_ma),
            dac_current_max_ma=float(dac_current_max_ma),
        )

        self._client: Optional[ModbusSerialClient] = None
        self._lock = asyncio.Lock()
        self._last_io_ts = 0.0

        self._hb_task: Optional[asyncio.Task] = None
        self._hb_paused: bool = False
        self._closed: bool = False

        _raw_logger = logger or (lambda *_a, **_k: None)
        def _log(fmt: object, *args: object) -> None:
            try:
                msg = (str(fmt) % args) if args else str(fmt)
            except Exception:
                msg = str(fmt)
            try:
                _raw_logger(msg)
            except Exception:
                pass
        self.log = _log

        self._SYNONYMS: Dict[str, str] = self._build_synonyms()

    # --------------------------
    # lifecycle
    # --------------------------
    async def connect(self) -> None:
        self._closed = False
        async with self._io_lock("connect"):
            await asyncio.to_thread(self._ensure_connected_sync, False)

        self.log(
            "PLC connect ok: port=%s baud=%s 8N1 unit=%s timeout=%.2fs",
            self.cfg.port, self.cfg.baudrate, self.cfg.unit, self.cfg.timeout_s
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

        self.log("PLC closed")

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
    def _make_client(self) -> ModbusSerialClient:
        return ModbusSerialClient(
            port=self.cfg.port,
            framer=FramerType.RTU,
            baudrate=self.cfg.baudrate,
            bytesize=self.cfg.bytesize,
            parity=self.cfg.parity,
            stopbits=self.cfg.stopbits,
            timeout=self.cfg.timeout_s,
        )

    def _get_serial(self):
        """pymodbus 내부 pyserial 객체(가능하면)"""
        c = self._client
        if c is None:
            return None
        # pymodbus 3.x serial은 보통 client.socket
        s = getattr(c, "socket", None)
        if s is not None:
            return s
        return None

    def _reset_serial_input_buffer(self) -> None:
        ser = self._get_serial()
        if ser is None:
            return
        try:
            if hasattr(ser, "reset_input_buffer"):
                ser.reset_input_buffer()
            elif hasattr(ser, "flushInput"):
                ser.flushInput()
        except Exception:
            pass

    def _reset_serial_output_buffer(self) -> None:
        ser = self._get_serial()
        if ser is None:
            return
        try:
            if hasattr(ser, "reset_output_buffer"):
                ser.reset_output_buffer()
            elif hasattr(ser, "flushOutput"):
                ser.flushOutput()
        except Exception:
            pass

    def _ensure_connected_sync(self, force_recreate: bool) -> None:
        if force_recreate or (self._client is None):
            self._close_sync()
            self._client = self._make_client()

        # 이미 연결돼 있으면 OK
        if getattr(self._client, "connected", False):
            return

        ok = self._client.connect()
        if not ok:
            self._close_sync()
            raise RuntimeError("Modbus RTU connect failed")

        # 연결 직후 버퍼 정리(찌꺼기 제거)
        if self.cfg.reset_buffers_on_connect:
            self._reset_serial_input_buffer()
            self._reset_serial_output_buffer()

        self._last_io_ts = time.monotonic()

    def _close_sync(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            # 혹시 pyserial이 남아 있으면 한 번 더 닫기
            try:
                ser = getattr(self._client, "socket", None)
                if ser is not None and hasattr(ser, "close"):
                    ser.close()
            except Exception:
                pass
        self._client = None

    def _ensure_ok(self, resp):
        if resp is None:
            raise ModbusException("No response(None)")
        if isinstance(resp, ExceptionResponse):
            raise ModbusException(f"ExceptionResponse: {resp}")
        if hasattr(resp, "isError") and resp.isError():
            raise ModbusException(str(resp))
        return resp

    # --------------------------
    # lock + throttle + heartbeat
    # --------------------------
    @asynccontextmanager
    async def _io_lock(self, op: str, **meta):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await self._lock.acquire()
        try:
            waited_ms = (loop.time() - t0) * 1000.0
            if waited_ms >= self.cfg.lock_warn_ms:
                self.log("WARN lock-wait %.0f ms (op=%s)", waited_ms, op)

            # ✅ 트랜잭션 전 입력버퍼 reset (RS-232에서 매우 중요)
            if self.cfg.reset_input_buffer_each_io:
                self._reset_serial_input_buffer()

            t_in = loop.time()
            yield
            io_ms = (loop.time() - t_in) * 1000.0
            if io_ms >= self.cfg.io_warn_ms:
                self.log("WARN in-lock IO %.0f ms (op=%s)", io_ms, op)
        finally:
            self._lock.release()

    async def _throttle(self) -> None:
        now = time.monotonic()
        delta = now - self._last_io_ts
        if delta < self.cfg.inter_cmd_gap_s:
            await asyncio.sleep(self.cfg.inter_cmd_gap_s - delta)
        self._last_io_ts = time.monotonic()

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(max(1.0, self.cfg.heartbeat_s / 3.0))
                if self._closed or self._hb_paused:
                    continue
                try:
                    async with self._io_lock("heartbeat"):
                        await asyncio.to_thread(self._ensure_connected_sync, False)
                        # 가벼운 1bit read
                        resp = await asyncio.to_thread(self._client.read_coils, 0, 1, slave=self.cfg.unit)
                        self._ensure_ok(resp)
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
    # name/address parsing (너 기존 유지)
    # --------------------------
    def _build_synonyms(self) -> Dict[str, str]:
        def norm(s: str) -> str:
            return s.strip().upper().replace(" ", "").replace("_", "").replace("-", "").replace("/", "")
        syn: Dict[str, str] = {}
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
        syn[norm("TMP")] = "TMP_SW"

        syn[norm("SHUTTER1")] = "SHUTTER_1_SW"
        syn[norm("SHUTTER2")] = "SHUTTER_2_SW"
        syn[norm("MAINSHUTTER")] = "MAIN_SHUTTER_SW"

        syn[norm("AIR")] = "AIR_SW"
        syn[norm("WATER")] = "WATER_SW"
        syn[norm("G1")] = "GAS_1_SW"
        syn[norm("G2")] = "GAS_2_SW"

        syn[norm("POWER1")] = "POWER_1_SW"
        syn[norm("POWER2")] = "POWER_2_SW"
        syn[norm("DOOR")] = "DOOR_SW"
        syn[norm("FTM")] = "FTM_SW"

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

        nk = key_raw.upper().replace(" ", "").replace("_", "").replace("-", "").replace("/", "")
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
        nk = s.upper().replace(" ", "").replace("_", "").replace("-", "").replace("/", "")
        if nk in self._SYNONYMS and self._SYNONYMS[nk] in PLC_REG_MAP:
            return True
        return s.upper().startswith("D")

    # --------------------------
    # low-level I/O (slave override 지원)
    # --------------------------
    async def ping(self, *, slave: Optional[int] = None) -> None:
        """가벼운 1bit read로 실제 응답 확인."""
        uid = self.cfg.unit if slave is None else int(slave)
        async with self._io_lock("ping"):
            await asyncio.to_thread(self._ensure_connected_sync, False)
            await self._throttle()
            resp = await asyncio.to_thread(self._client.read_coils, 0, 1, slave=uid)
            self._ensure_ok(resp)

    async def read_coils_block(self, start_addr: int, count: int, *, slave: Optional[int] = None) -> list[bool]:
        start_addr = int(start_addr)
        count = max(1, int(count))
        uid = self.cfg.unit if slave is None else int(slave)

        async with self._io_lock("read_coils_block", addr=start_addr, count=count):
            await asyncio.to_thread(self._ensure_connected_sync, False)
            await self._throttle()
            resp = await asyncio.to_thread(self._client.read_coils, start_addr, count, slave=uid)
            self._ensure_ok(resp)
            bits = list(getattr(resp, "bits", []) or [])
            if len(bits) < count:
                bits.extend([False] * (count - len(bits)))
            return [bool(b) for b in bits[:count]]

    async def write_coil(self, addr: int, value: bool, *, slave: Optional[int] = None) -> None:
        addr = int(addr)
        uid = self.cfg.unit if slave is None else int(slave)

        async with self._io_lock("write_coil", addr=addr):
            await asyncio.to_thread(self._ensure_connected_sync, False)
            await self._throttle()
            resp = await asyncio.to_thread(self._client.write_coil, addr, bool(value), slave=uid)
            self._ensure_ok(resp)

    async def read_reg(self, addr: int, *, slave: Optional[int] = None) -> int:
        addr = int(addr)
        uid = self.cfg.unit if slave is None else int(slave)

        async with self._io_lock("read_reg", addr=addr):
            await asyncio.to_thread(self._ensure_connected_sync, False)
            await self._throttle()
            resp = await asyncio.to_thread(self._client.read_holding_registers, addr, 1, slave=uid)
            self._ensure_ok(resp)
            return int(resp.registers[0])

    async def write_reg(self, addr: int, value: int, *, slave: Optional[int] = None) -> None:
        addr = int(addr)
        uid = self.cfg.unit if slave is None else int(slave)

        async with self._io_lock("write_reg", addr=addr):
            await asyncio.to_thread(self._ensure_connected_sync, False)
            await self._throttle()
            resp = await asyncio.to_thread(self._client.write_register, addr, int(value), slave=uid)
            self._ensure_ok(resp)

    # --------------------------
    # high-level helpers (너 기존 유지)
    # --------------------------
    async def pulse(self, addr: int, *, ms: Optional[int] = None) -> None:
        width = self.cfg.pulse_ms if ms is None else int(ms)
        await self.write_coil(addr, True)
        await asyncio.sleep(max(0.01, width / 1000.0))
        await self.write_coil(addr, False)

    async def write_switch(self, name_or_addr: Any, on: bool, *, momentary: bool = False, pulse_ms: Optional[int] = None) -> None:
        addr = self._addr(name_or_addr)
        if self._is_reg_name(name_or_addr):
            raise TypeError(f"write_switch는 COIL 전용입니다: {name_or_addr}")
        if momentary:
            await self.pulse(addr, ms=pulse_ms)
        else:
            await self.write_coil(addr, bool(on))

    async def read_bit(self, name_or_addr: Any) -> bool:
        addr = self._addr(name_or_addr)
        if self._is_reg_name(name_or_addr):
            raise TypeError(f"read_bit은 COIL 전용입니다: {name_or_addr}")
        bits = await self.read_coils_block(addr, 1)
        return bool(bits[0])

    async def read_reg_name(self, name_or_addr: Any) -> int:
        addr = self._addr(name_or_addr)
        return int(await self.read_reg(addr))

    async def write_reg_name(self, name_or_addr: Any, value: int) -> None:
        addr = self._addr(name_or_addr)
        await self.write_reg(addr, int(value))

    # 기존 short API 유지
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

    # ---- DAC helpers (너 기존 유지)
    def _clamp_dac_code(self, code: int) -> int:
        fs = int(self.cfg.dac_full_scale_code)
        if fs <= 0:
            raise ValueError(f"dac_full_scale_code must be > 0 (now={fs})")
        offset = int(self.cfg.dac_offset_code)
        lo, hi = offset, offset + fs
        c = int(code)
        return lo if c < lo else hi if c > hi else c

    async def set_dac_power(self, ch: int, code: int) -> None:
        key = "DAC_POWER_1" if int(ch) == 1 else "DAC_POWER_2" if int(ch) == 2 else None
        if key is None:
            raise ValueError("DAC channel must be 1 or 2")
        await self.write_reg_name(key, self._clamp_dac_code(code))

    async def set_dac_current(self, ch: int, ma: float) -> int:
        mn = float(self.cfg.dac_current_min_ma)
        mx = float(self.cfg.dac_current_max_ma)
        if mx <= mn:
            raise ValueError(f"Invalid current range: {mn}..{mx}")
        i = float(ma)
        if i < mn: i = mn
        if i > mx: i = mx
        x = (i - mn) / (mx - mn)
        fs = int(self.cfg.dac_full_scale_code)
        offset = int(self.cfg.dac_offset_code)
        code = int(round(x * fs)) + offset
        code = self._clamp_dac_code(code)
        await self.set_dac_power(ch, code)
        return code
