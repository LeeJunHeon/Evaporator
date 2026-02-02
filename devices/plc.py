# -*- coding: utf-8 -*-
# plc.py
"""
Evaporator용 PLC 컨트롤러 (Modbus-RTU / RS-232) — minimalmodbus 기반 (pymodbus 제거)

CH.K_program의 PLC 스타일(=minimalmodbus + 포트 유지 + 버퍼 클리어)을 Evaporator에도 적용:
- minimalmodbus.Instrument 사용 (MODE_RTU)
- close_port_after_each_call = False  (포트를 매번 닫지 않음 → RS-232 안정)
- clear_buffers_before_each_transaction = True (찌꺼기 프레임 제거)
- asyncio 환경에서 UI 멈춤 방지: 모든 I/O는 asyncio.to_thread + asyncio.Lock 직렬화
- Cnet 설정과 일치: 115200 / 8-N-1
- unit(국번) 0이 실제로 응답이 없을 수 있어 ping으로 자동 탐색(0 → 1 등)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, List

import minimalmodbus
import serial  # pyserial (minimalmodbus 내부도 사용)


# ======================================================
# 1) 주소 맵
# ======================================================
# ⚠️ LS PLC(XG5000)에서 M 디바이스 주소는 16진 표기인 경우가 많습니다.
#    예) M0000B = 0x0B = 11, M00020 = 0x20 = 32
# 아래 dict 값은 "Modbus coil address (0-based int)" 입니다.

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
    timeout_s: float = 0.3

    # RS-232에서 너무 빡빡한 연속 요청은 불안정할 수 있어 약간의 간격 권장
    inter_cmd_gap_s: float = 0.02

    # Cnet의 "문자간 대기시간(10ms 단위)"에 대응하는 느낌으로 inter_byte_timeout을 둠
    inter_byte_timeout_s: float = 0.01

    pulse_ms: int = 180

    # ===== DAC (4~20mA 전용) =====
    dac_full_scale_code: int = 4000
    dac_offset_code: int = 0
    dac_current_min_ma: float = 4.0
    dac_current_max_ma: float = 20.0


# ======================================================
# 3) Async PLC 클래스 (minimalmodbus wrapper)
# ======================================================

class AsyncPLC:
    def __init__(
        self,
        port: str = "COM8",
        *,
        method: str = "rtu",   # PLCSettings 호환용(실제로는 RTU 고정)
        baudrate: int = 115200,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        unit: int = 0,
        timeout_s: float = 0.3,
        pulse_ms: int = 180,

        # DAC
        dac_full_scale_code: int = 4000,
        dac_offset_code: int = 0,
        dac_current_min_ma: float = 4.0,
        dac_current_max_ma: float = 20.0,

        logger=None,
    ):
        self.cfg = PLCConfig(
            port=str(port),
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

        self._inst: Optional[minimalmodbus.Instrument] = None
        self._lock = asyncio.Lock()
        self._last_io_ts = 0.0
        self._closed = False

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
    # helpers
    # --------------------------
    def _normalize_port(self, port: str) -> str:
        """
        Windows에서 COM10 이상은 '\\\\.\\COM10' 형식이 안전.
        COM1~COM9는 그대로 사용 가능.
        """
        p = str(port).strip()
        up = p.upper()
        if up.startswith("COM"):
            try:
                n = int(up[3:])
                if n >= 10 and not p.startswith("\\\\.\\"):
                    return f"\\\\.\\{up}"
            except Exception:
                pass
        return p

    def _new_instrument(self, unit: int) -> minimalmodbus.Instrument:
        inst = minimalmodbus.Instrument(
            self._normalize_port(self.cfg.port),
            int(unit),
            mode=minimalmodbus.MODE_RTU,
        )
        inst.serial.baudrate = int(self.cfg.baudrate)
        inst.serial.bytesize = int(self.cfg.bytesize)
        inst.serial.parity = str(self.cfg.parity)
        inst.serial.stopbits = int(self.cfg.stopbits)
        inst.serial.timeout = float(self.cfg.timeout_s)
        inst.serial.write_timeout = float(self.cfg.timeout_s)

        # inter_byte_timeout은 환경에 따라 없을 수 있어 안전하게
        try:
            inst.serial.inter_byte_timeout = float(self.cfg.inter_byte_timeout_s)
        except Exception:
            pass

        inst.close_port_after_each_call = False
        inst.clear_buffers_before_each_transaction = True
        inst.handle_local_echo = False
        return inst

    def _connect_sync(self) -> None:
        """
        - Instrument 생성 + 포트 open
        - unit 후보를 ping으로 확인해서 살아있는 unit으로 확정
        """
        if self._inst is not None:
            return

        # unit 후보: 설정값 우선, 그 다음 1, 0, 2 순으로 짧게 탐색
        candidates: List[int] = []
        for u in (self.cfg.unit, 1, 0, 2):
            u = int(u)
            if u not in candidates:
                candidates.append(u)

        last_err: Optional[Exception] = None

        for u in candidates:
            inst = None
            try:
                inst = self._new_instrument(u)

                # 포트 선오픈(여기서 권한/점유 문제를 빨리 잡음)
                try:
                    if hasattr(inst, "serial") and inst.serial and (not inst.serial.is_open):
                        inst.serial.open()
                except Exception:
                    # minimalmodbus가 내부에서 열기도 하므로 여기서 실패해도 아래 ping에서 판별
                    pass

                # ✅ ping: coil 0 1개 읽기 (응답 없으면 예외)
                _ = inst.read_bits(0, 1, functioncode=1)

                # 성공 → 확정
                self._inst = inst
                if self.cfg.unit != u:
                    self.log("PLC unit auto-adjust: %s -> %s", self.cfg.unit, u)
                    self.cfg.unit = u
                return

            except Exception as e:
                last_err = e
                # 실패한 inst는 정리
                try:
                    if inst and inst.serial and inst.serial.is_open:
                        inst.serial.close()
                except Exception:
                    pass
                continue

        raise RuntimeError(f"PLC connect/ping failed (port={self.cfg.port}, baud={self.cfg.baudrate}): {last_err!r}")

    def _close_sync(self) -> None:
        inst = self._inst
        self._inst = None
        if inst is None:
            return
        try:
            if inst.serial and inst.serial.is_open:
                inst.serial.close()
        except Exception:
            pass

    async def connect(self) -> None:
        self._closed = False
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)
        self.log(
            "PLC connected: port=%s baud=%s 8%s%s (unit=%s)",
            self.cfg.port, self.cfg.baudrate, self.cfg.parity, self.cfg.stopbits, self.cfg.unit
        )

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            await asyncio.to_thread(self._close_sync)
        self.log("PLC closed")

    def is_connected(self) -> bool:
        try:
            return bool(self._inst) and bool(self._inst.serial) and bool(self._inst.serial.is_open)
        except Exception:
            return False

    # --------------------------
    # addressing & synonyms
    # --------------------------
    def _build_synonyms(self) -> Dict[str, str]:
        def norm(x: str) -> str:
            return (
                x.upper()
                .replace(" ", "")
                .replace("_", "")
                .replace("-", "")
                .replace("/", "")
            )

        syn: Dict[str, str] = {}

        syn[norm("RP")] = "R_P_SW"
        syn[norm("RV")] = "R_V_SW"
        syn[norm("FV")] = "F_V_SW"
        syn[norm("MV")] = "M_V_SW"

        syn[norm("VV")] = "V_V_SW"
        syn[norm("V/V")] = "V_V_SW"
        syn[norm("V_V")] = "V_V_SW"
        syn[norm("V_V_SW")] = "V_V_SW"
        syn[norm("VV_SW")] = "V_V_SW"   # ✅ binder 실수 방어(그래도 binder는 고치는게 정답)

        syn[norm("TMP")] = "TMP_SW"
        syn[norm("DOOR")] = "DOOR_SW"
        syn[norm("FTM")] = "FTM_SW"
        syn[norm("MAINSHUTTER")] = "MAIN_SHUTTER_SW"
        syn[norm("SHUTTER1")] = "SHUTTER_1_SW"
        syn[norm("SHUTTER2")] = "SHUTTER_2_SW"
        syn[norm("POWER1")] = "POWER_1_SW"
        syn[norm("POWER2")] = "POWER_2_SW"

        syn[norm("AIR")] = "AIR_SW"
        syn[norm("WATER")] = "WATER_SW"
        syn[norm("G1")] = "GAS_1_SW"
        syn[norm("G2")] = "GAS_2_SW"

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
            return int(name_or_addr)

        key_raw = str(name_or_addr).strip()
        if not key_raw:
            raise ValueError("empty address/name")

        if key_raw in PLC_COIL_MAP:
            return int(PLC_COIL_MAP[key_raw])
        if key_raw in PLC_REG_MAP:
            return int(PLC_REG_MAP[key_raw])

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
        if s in PLC_REG_MAP:
            return True
        nk = s.upper().replace(" ", "").replace("_", "").replace("-", "").replace("/", "")
        if nk in self._SYNONYMS and self._SYNONYMS[nk] in PLC_REG_MAP:
            return True
        if s.upper().startswith("D"):
            return True
        return False

    # --------------------------
    # throttle
    # --------------------------
    async def _throttle_locked(self) -> None:
        now = time.monotonic()
        gap = float(self.cfg.inter_cmd_gap_s)
        dt = now - self._last_io_ts
        if dt < gap:
            await asyncio.sleep(gap - dt)

    def _touch_io_ts(self) -> None:
        self._last_io_ts = time.monotonic()

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
                bits = await asyncio.to_thread(self._inst.read_bits, addr, 1, 1)
                self._touch_io_ts()
                return bool(bits[0])
            except Exception:
                # 통신 예외 시 포트 정리해서 상위에서 reconnect 유도
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
                bits = await asyncio.to_thread(self._inst.read_bits, start_addr, count, 1)  # functioncode=1 (Coils)
                self._touch_io_ts()
                # minimalmodbus는 길이를 맞춰주지만 안전 보강
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
                # functioncode=5 (Write Single Coil)
                await asyncio.to_thread(self._inst.write_bit, addr, 1 if value else 0, 5)
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
                # FC6 (Write Single Register)
                await asyncio.to_thread(self._inst.write_register, addr, value, 0, 6, False)
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

    async def write_switch(self, name_or_addr: Any, on: bool, *, momentary: bool = False, pulse_ms: Optional[int] = None) -> None:
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
    # 기존 고수준 API 유지 (호출부 수정 최소화)
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
        await self.write_reg_name(key, self._clamp_dac_code(code))

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

        await self.set_dac_power(ch, code)
        return code
