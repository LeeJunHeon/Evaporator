# -*- coding: utf-8 -*-
"""
devices/plc.py

Evaporator PLC 컨트롤러 (Modbus RTU over RS-232) - Async Wrapper

핵심 목표
- UI/공정 스레드가 멈추지 않게: asyncio + to_thread + 단일 락으로 직렬화
- Modbus-RTU는 minimalmodbus(= 내부적으로 pyserial 사용)로 처리
- 주소맵(Mxxxx, Dxxxx) 기반으로 COIL / HOLDING REGISTER 접근 제공
- HMI/공정 코드에서 호출하던 고수준 API(rp(), rv(), vv(), set_dac_current() 등)는 유지

주의
- LS(XG5000) PLC의 M 디바이스는 16진 주소를 쓰는 경우가 많습니다.
  예) M0000B = 0x0B = 11, M00020 = 0x20 = 32
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import minimalmodbus


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
    "AIR_SW": 32,    # M00020
    "WATER_SW": 33,  # M00021
    "GAS_1_SW": 34,  # M00022
    "GAS_2_SW": 35,  # M00023
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
    # Serial / Modbus-RTU
    port: str = "COM5"
    method: str = "rtu"        # ✅ ini 호환용(실제로는 minimalmodbus가 RTU만 지원)
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    unit: int = 1
    timeout_s: float = 2.0

    # 요청 간격(너무 빠르면 PLC가 응답을 놓치거나 프레임이 꼬일 수 있음)
    inter_cmd_gap_s: float = 0.05

    # momentary(pulse) 사용 시 펄스폭(ms)
    pulse_ms: int = 180

    # ===== DAC (4~20mA 전용) =====
    dac_full_scale_code: int = 4000
    dac_offset_code: int = 0
    dac_current_min_ma: float = 4.0
    dac_current_max_ma: float = 20.0


# ======================================================
# 3) Async PLC
# ======================================================

class AsyncPLC:
    """
    minimalmodbus.Instrument 를 asyncio에서 안전하게 쓰기 위한 래퍼.

    - 모든 I/O는 단일 asyncio.Lock으로 직렬화(폴링/버튼 동시 접근 방지)
    - 실제 serial I/O는 asyncio.to_thread(...) 로 실행
    - 예외가 나면 포트를 닫아두고, 다음 호출에서 재연결되도록 설계
    """

    def __init__(
        self,
        port: str = "COM5",
        *,
        # ✅ 기존 코드/ini 호출부와의 호환을 위해 method는 받되, 내부에서는 사용하지 않습니다.
        method: str = "rtu",
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        unit: int = 1,
        timeout_s: float = 2.0,
        pulse_ms: int = 180,
        inter_cmd_gap_s: float = 0.05,

        # DAC
        dac_full_scale_code: int = 4000,
        dac_offset_code: int = 0,
        dac_current_min_ma: float = 4.0,
        dac_current_max_ma: float = 20.0,

        logger=None,
    ) -> None:
        self.cfg = PLCConfig(
            port=str(port),
            method=str(method),
            baudrate=int(baudrate),
            bytesize=int(bytesize),
            parity=str(parity).upper(),
            stopbits=int(stopbits),
            unit=int(unit),
            timeout_s=float(timeout_s),
            pulse_ms=int(pulse_ms),
            inter_cmd_gap_s=float(inter_cmd_gap_s),

            dac_full_scale_code=int(dac_full_scale_code),
            dac_offset_code=int(dac_offset_code),
            dac_current_min_ma=float(dac_current_min_ma),
            dac_current_max_ma=float(dac_current_max_ma),
        )

        self.log = logger or (lambda *a, **k: None)

        self._inst: Optional[minimalmodbus.Instrument] = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._last_io_ts = 0.0

        self._SYNONYMS: Dict[str, str] = self._build_synonyms()

    # --------------------------
    # lifecycle
    # --------------------------
    async def connect(self) -> None:
        """포트를 열고 기본 통신(ping)이 되는지 확인."""
        self._closed = False
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)

        self.log(
            "PLC connected: port=%s baud=%s 8%s%s (unit=%s, timeout=%.2fs)",
            self.cfg.port,
            self.cfg.baudrate,
            self.cfg.parity,
            self.cfg.stopbits,
            self.cfg.unit,
            self.cfg.timeout_s,
        )

    async def close(self) -> None:
        """포트 닫기."""
        self._closed = True
        async with self._lock:
            await asyncio.to_thread(self._close_sync)
        self.log("PLC closed")

    def is_connected(self) -> bool:
        try:
            return bool(self._inst) and bool(self._inst.serial) and bool(self._inst.serial.is_open)
        except Exception:
            return False

    async def ping(self) -> None:
        """연결 확인용(응답성 체크). connect 직후/재연결 직후에 호출하기 좋음."""
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_locked()
            await asyncio.to_thread(self._ping_sync)
            self._touch_io_ts()

    # --------------------------
    # internal: connect/close
    # --------------------------
    @staticmethod
    def _normalize_port(port: str) -> str:
        # Windows에서 COM10 이상은 "\\\\.\\COM10" 형태가 안전합니다.
        p = str(port).strip()
        if p.upper().startswith("COM") and len(p) > 4:
            return r"\\.\\" + p
        return p

    def _new_instrument(self, unit: int) -> minimalmodbus.Instrument:
        inst = minimalmodbus.Instrument(self._normalize_port(self.cfg.port), int(unit))
        # Serial 기본 설정
        inst.serial.baudrate = int(self.cfg.baudrate)
        inst.serial.bytesize = int(self.cfg.bytesize)
        inst.serial.parity = str(self.cfg.parity)
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
        return inst

    def _connect_sync(self) -> None:
        if self._closed:
            raise RuntimeError("PLC is closed")

        # 이미 열려 있으면 그대로 사용
        if self._inst is not None and getattr(self._inst.serial, "is_open", False):
            return

        # unit이 틀리면 "No response"가 반복되므로, 아주 제한적으로만 자동 보정(설정값, 1)
        candidates: List[int] = [int(self.cfg.unit)]
        if self.cfg.unit != 1:
            candidates.append(1)

        last_err: Optional[Exception] = None
        for uid in candidates:
            try:
                inst = self._new_instrument(uid)

                # minimalmodbus는 트랜잭션 시 자동 open도 하지만,
                # 상태를 명확히 하기 위해 여기서 open해둠
                if not inst.serial.is_open:
                    inst.serial.open()

                self._inst = inst

                # 최소 ping(응답 확인)
                self._ping_sync()

                # 성공 → unit 확정
                self.cfg.unit = int(uid)
                self._touch_io_ts()
                return
            except Exception as e:
                last_err = e
                try:
                    if self._inst and self._inst.serial and self._inst.serial.is_open:
                        self._inst.serial.close()
                except Exception:
                    pass
                self._inst = None

        raise RuntimeError(f"Modbus RTU connect/ping failed (last={last_err!r})")

    def _close_sync(self) -> None:
        if self._inst is not None:
            try:
                if self._inst.serial and self._inst.serial.is_open:
                    self._inst.serial.close()
            except Exception:
                pass
        self._inst = None

    def _ping_sync(self) -> None:
        """
        ping은 '읽기 1회'로 끝내는 게 가장 안전합니다.
        - 환경에 따라 coil 0이 막혀 있을 수 있으니 0/32/5 순서로 짧게 시도합니다.
        """
        assert self._inst is not None

        for addr in (0, 32, 5):
            try:
                bits = self._inst.read_bits(int(addr), 1, functioncode=1)  # FC1 Read Coils
                if bits and len(bits) >= 1:
                    return
            except Exception:
                continue
        raise IOError("No response on ping (tried coils 0,32,5)")

    # --------------------------
    # throttle
    # --------------------------
    async def _throttle_locked(self) -> None:
        gap = float(self.cfg.inter_cmd_gap_s)
        dt = time.monotonic() - float(self._last_io_ts)
        if dt < gap:
            await asyncio.sleep(gap - dt)

    def _touch_io_ts(self) -> None:
        self._last_io_ts = time.monotonic()

    # --------------------------
    # addressing & synonyms
    # --------------------------
    def _build_synonyms(self) -> Dict[str, str]:
        def norm(x: str) -> str:
            return x.upper().replace(" ", "").replace("_", "").replace("-", "").replace("/", "")

        syn: Dict[str, str] = {}

        # 사람이 입력하기 쉬운 별칭
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
        syn[norm("G1")] = "GAS_1_SW"
        syn[norm("G2")] = "GAS_2_SW"

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

        # 1) canonical key
        if key_raw in PLC_COIL_MAP:
            return int(PLC_COIL_MAP[key_raw])
        if key_raw in PLC_REG_MAP:
            return int(PLC_REG_MAP[key_raw])

        # 2) synonyms
        nk = key_raw.upper().replace(" ", "").replace("_", "").replace("-", "").replace("/", "")
        if nk in self._SYNONYMS:
            canonical = self._SYNONYMS[nk]
            if canonical in PLC_COIL_MAP:
                return int(PLC_COIL_MAP[canonical])
            if canonical in PLC_REG_MAP:
                return int(PLC_REG_MAP[canonical])

        # 3) "M00020" / "D00001"
        up = key_raw.upper()
        if up.startswith("M"):
            return self._parse_m_device_to_coil(up)
        if up.startswith("D"):
            return self._parse_d_device_to_reg(up)

        # 4) numeric (e.g. "0x20", "32")
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
        if s.upper().startswith("D"):
            return True
        return False

    # --------------------------
    # low-level I/O
    # --------------------------
    async def read_coil(self, addr: int) -> bool:
        addr = int(addr)
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_locked()
            try:
                assert self._inst is not None
                bits = await asyncio.to_thread(self._inst.read_bits, addr, 1, 1)  # FC1
                self._touch_io_ts()
                return bool(bits[0])
            except Exception:
                await asyncio.to_thread(self._close_sync)
                raise

    async def read_coils_block(self, start_addr: int, count: int) -> List[bool]:
        start_addr = int(start_addr)
        count = max(1, int(count))
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_locked()
            try:
                assert self._inst is not None
                bits = await asyncio.to_thread(self._inst.read_bits, start_addr, count, 1)  # FC1
                self._touch_io_ts()
                if len(bits) < count:
                    bits = list(bits) + [0] * (count - len(bits))
                return [bool(b) for b in bits[:count]]
            except Exception:
                await asyncio.to_thread(self._close_sync)
                raise

    async def write_coil(self, addr: int, value: bool) -> None:
        addr = int(addr)
        value = bool(value)
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_locked()
            try:
                assert self._inst is not None
                await asyncio.to_thread(self._inst.write_bit, addr, 1 if value else 0, 5)  # FC5
                self._touch_io_ts()
            except Exception:
                await asyncio.to_thread(self._close_sync)
                raise

    async def read_reg(self, addr: int) -> int:
        addr = int(addr)
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_locked()
            try:
                assert self._inst is not None
                v = await asyncio.to_thread(self._inst.read_register, addr, 0, 3, False)  # FC3
                self._touch_io_ts()
                return int(v)
            except Exception:
                await asyncio.to_thread(self._close_sync)
                raise

    async def write_reg(self, addr: int, value: int) -> None:
        addr = int(addr)
        value = int(value)
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_locked()
            try:
                assert self._inst is not None
                await asyncio.to_thread(self._inst.write_register, addr, value, 0, 6, False)  # FC6
                self._touch_io_ts()
            except Exception:
                await asyncio.to_thread(self._close_sync)
                raise

    # --------------------------
    # high-level helpers
    # --------------------------
    async def pulse(self, addr: int, *, ms: Optional[int] = None) -> None:
        width = self.cfg.pulse_ms if ms is None else int(ms)
        await self.write_coil(int(addr), True)
        await asyncio.sleep(max(0.01, width / 1000.0))
        await self.write_coil(int(addr), False)

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
        if momentary:
            await self.pulse(addr, ms=pulse_ms)
        else:
            await self.write_coil(addr, bool(on))

    async def read_bit(self, name_or_addr: Any) -> bool:
        if self._is_reg_name(name_or_addr):
            raise TypeError(f"read_bit은 COIL 전용입니다. register로 보이는 입력: {name_or_addr}")
        addr = self._addr(name_or_addr)
        return bool(await self.read_coil(addr))

    async def read_reg_name(self, name_or_addr: Any) -> int:
        addr = self._addr(name_or_addr)
        return int(await self.read_reg(addr))

    async def write_reg_name(self, name_or_addr: Any, value: int) -> None:
        addr = self._addr(name_or_addr)
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

    # --------------------------
    # DAC helpers (4~20mA)
    # --------------------------
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
        await self.write_reg_name(key, self._clamp_dac_code(int(code)))

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
