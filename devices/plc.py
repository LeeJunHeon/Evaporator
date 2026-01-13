# -*- coding: utf-8 -*-
# plc.py
"""
PLC.py — Evaporator용 Modbus-TCP PLC 컨트롤러 (Async)

목표
- 기존 CH1/CH2에서 쓰던 plc.py 스타일을 최대한 유지
  (asyncio + pymodbus 동기클라이언트 안전 사용: 직렬화(lock), to_thread, 명령 간격 보장,
   heartbeat, unit/slave 자동 판별, 연결 끊김 재연결)
- 이번에 받은 주소맵(Mxxxx, Dxxxx)에 맞춘 COIL/REGISTER 맵 제공
- 고수준 API를 단순하게 제공 (rp/rv/fv/mv/vv/tmp, shutter1/2, main_shutter, air/water/gas1/2, power1/2, door, dac_power1/2)

⚠️ 주소 해석 규칙(매우 중요)
- M 주소는 "16진수(hex)"로 해석해서 coil address로 변환합니다.
  예) M00020 = 0x20 = 32, M0000B = 0x0B = 11, M0000A = 0x0A = 10
- D 주소는 일반적으로 10진수로 사용합니다. (D00000 = 0, D00001 = 1)
  다만 문자열에 A~F가 섞인 경우엔 hex로도 파싱 가능하게 해둠.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from pymodbus.pdu import ExceptionResponse


# ======================================================
# 1) 주소 맵 (이번에 받은 맵 기반)
# ======================================================

# === M (Coils) — FC1/FC5 ===
# ※ "M000xx"는 16진수로 해석해서 coil address로 씁니다.
PLC_COIL_MAP: Dict[str, int] = {
    # Vacuum / Valve / Pump
    "R_P_SW": 0x0000,   # M00000 : RP
    "R_V_SW": 0x0001,   # M00001 : RV
    "F_V_SW": 0x0002,   # M00002 : FV
    "M_V_SW": 0x0003,   # M00003 : MV
    "V_V_SW": 0x0004,   # M00004 : V/V (Vent Valve로 쓰는 경우가 많아서 별칭도 제공)
    "TMP_SW": 0x0005,   # M00005 : TMP

    # Shutters / FTM
    "SHUTTER_1_SW": 0x0006,      # M00006 : Shutter1
    "SHUTTER_2_SW": 0x0007,      # M00007 : Shutter2
    "MAIN_SHUTTER_SW": 0x0008,   # M00008 : Main Shutter
    "FTM_SW": 0x000B,            # M0000B : FTM (0x0B = 11)

    # Power / Door
    "POWER_1_SW": 0x0009,        # M00009 : POWER1
    "POWER_2_SW": 0x000A,        # M0000A : POWER2 (0x0A = 10)
    "DOOR_SW": 0x000C,           # M0000C : DOOR (0x0C = 12)

    # Utilities / Gas
    "AIR_SW": 0x0020,            # M00020 : Air (0x20 = 32)
    "WATER_SW": 0x0021,          # M00021 : Water
    "GAS_1_SW": 0x0022,          # M00022 : G1
    "GAS_2_SW": 0x0023,          # M00023 : G2
}

# === D (Holding Registers) — FC3/FC6 ===
PLC_REG_MAP: Dict[str, int] = {
    "DAC_POWER_1": 0,            # D00000 : DAC POWER1
    "DAC_POWER_2": 1,            # D00001 : DAC POWER2
}


# ======================================================
# 2) 설정(기존 plc.py 스타일 유지)
# ======================================================

@dataclass
class PLCConfig:
    ip: str = "192.168.1.2"
    port: int = 502
    unit: int = 1                 # Modbus unit id (pymodbus 버전에 따라 unit/slave)
    timeout_s: float = 2.0
    inter_cmd_gap_s: float = 0.12  # 너무 빡세면 PLC가 버벅일 수 있음. 필요시 0.15~0.25로
    heartbeat_s: float = 15.0      # 오래 I/O 없으면 0번 코일 읽어서 keepalive
    pulse_ms: int = 180            # momentary=True일 때 펄스폭(ms)

    # 락/IO 경고(디버깅용)
    lock_warn_ms: float = 800.0
    io_warn_ms: float = 1500.0

    # DAC 스케일링(장비에 따라 달라서 "기본값"만 제공)
    dac_full_scale_code: int = 4095   # 12bit DAC 가정(0~4095)
    dac_full_scale_volt: float = 10.0 # 0~10V 가정
    dac_offset_code: int = 0          # 오프셋 필요하면 조정


# ======================================================
# 3) Async PLC 클래스
# ======================================================

class AsyncPLC:
    """
    AsyncPLC
    - connect/close: TCP 연결 관리 + heartbeat task
    - read/write: coil/register 읽기/쓰기
    - write_switch: 순간펄스(momentary) 또는 래치(set/reset) 지원
    - cmd: UI/상위 로직에서 문자열로 제어 가능한 범용 엔드포인트
    """

    def __init__(
        self,
        ip: str = "192.168.1.2",
        port: int = 502,
        unit: int = 1,
        *,
        timeout_s: float = 2.0,
        inter_cmd_gap_s: float = 0.12,
        heartbeat_s: float = 15.0,
        pulse_ms: int = 180,
        logger=None,
    ):
        self.cfg = PLCConfig(
            ip=ip,
            port=port,
            unit=unit,
            timeout_s=timeout_s,
            inter_cmd_gap_s=inter_cmd_gap_s,
            heartbeat_s=heartbeat_s,
            pulse_ms=pulse_ms,
        )

        # pymodbus 동기 클라이언트(Async에서 to_thread로 호출)
        self._client: Optional[ModbusTcpClient] = None

        # pymodbus 2.x는 slave=, 3.x는 unit= 를 주로 씀 → 자동 판별
        self._uid_kw: Optional[str] = None  # 'unit' or 'slave'

        # I/O 직렬화를 위한 lock
        self._lock = asyncio.Lock()
        self._last_io_ts = 0.0

        # heartbeat task
        self._hb_task: Optional[asyncio.Task] = None
        self._hb_paused: bool = False
        self._closed: bool = False

        # logger: 기존 프로젝트 스타일처럼 self.log("fmt", args...) 형태를 허용
        self.log = logger or (lambda *a, **k: None)

        # ---- 별칭/정규화 테이블 만들기 ----
        # "RP" 같은 짧은 이름, "V/V" 표기 등 다양하게 들어와도 동작하게 함
        self._SYNONYMS: Dict[str, str] = self._build_synonyms()

    # --------------------------------------------------
    # 수명주기
    # --------------------------------------------------

    async def connect(self) -> None:
        """TCP 연결 + heartbeat 시작"""
        self._closed = False
        async with self._io_lock("connect"):
            await asyncio.to_thread(self._connect_sync)

        self.log("TCP 연결 성공: %s:%s (unit=%s)", self.cfg.ip, self.cfg.port, self.cfg.unit)

        if self._hb_task is None or self._hb_task.done():
            self._hb_task = asyncio.create_task(self._heartbeat_loop(), name="PLCHeartbeat")

    async def close(self) -> None:
        """heartbeat 종료 후 TCP 연결 종료"""
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

        self.log("TCP 연결 종료")

    def is_connected(self) -> bool:
        try:
            return bool(self._client) and bool(self._client.connected)
        except Exception:
            return False

    async def __aenter__(self) -> "AsyncPLC":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # --------------------------------------------------
    # 내부(동기) 연결/종료
    # --------------------------------------------------

    def _connect_sync(self) -> None:
        if self._client is None:
            self._client = ModbusTcpClient(self.cfg.ip, port=self.cfg.port, timeout=self.cfg.timeout_s)

        if not self._client.connect():
            raise RuntimeError("Modbus TCP 연결 실패")

        # keepalive 옵션(가능하면)
        try:
            import socket
            self._client.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass

        self._last_io_ts = time.monotonic()

        # unit/slave 키워드 자동 판별(한 번만)
        if self._uid_kw is None:
            # pymodbus 버전에 따라 메서드 시그니처가 다름 → write_coil 시그니처를 보고 결정
            try:
                sig = inspect.signature(self._client.write_coil)
                if "unit" in sig.parameters:
                    self._uid_kw = "unit"
                elif "slave" in sig.parameters:
                    self._uid_kw = "slave"
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
        """pymodbus 2.x/3.x 호환(unit/slave)"""
        if self._uid_kw:
            return {self._uid_kw: self.cfg.unit}
        return {}

    def _ensure_ok(self, resp):
        """pymodbus 응답 오류 처리 통일"""
        if resp is None:
            raise ModbusException("응답 없음(None)")
        if isinstance(resp, ExceptionResponse):
            raise ModbusException(f"Modbus ExceptionResponse: {resp}")
        if hasattr(resp, "isError") and resp.isError():
            raise ModbusException(str(resp))
        return resp

    def _is_reset_err(self, e: Exception) -> bool:
        """연결 reset 류 에러를 문자열 기반으로 감지(현장에서는 이게 제일 잘 먹힘)"""
        s = str(e).lower()
        return ("10054" in s) or ("reset by peer" in s) or ("connectionreseterror" in s)

    # --------------------------------------------------
    # I/O 보호(락 + 간격 + heartbeat)
    # --------------------------------------------------

    @asynccontextmanager
    async def _io_lock(self, op: str, *, addr: Optional[int] = None):
        """I/O 직렬화 + 디버깅(락 대기, 임계구역 내 시간 경고)"""
        loop = asyncio.get_running_loop()
        t0 = loop.time()

        await self._lock.acquire()
        try:
            waited_ms = (loop.time() - t0) * 1000.0
            if waited_ms >= self.cfg.lock_warn_ms:
                if addr is None:
                    self.log("WARN lock-wait %.0f ms (op=%s)", waited_ms, op)
                else:
                    self.log("WARN lock-wait %.0f ms (op=%s, addr=%s)", waited_ms, op, addr)

            t_in = loop.time()
            yield
            io_ms = (loop.time() - t_in) * 1000.0
            if io_ms >= self.cfg.io_warn_ms:
                if addr is None:
                    self.log("WARN in-lock IO %.0f ms (op=%s)", io_ms, op)
                else:
                    self.log("WARN in-lock IO %.0f ms (op=%s, addr=%s)", io_ms, op, addr)
        finally:
            self._lock.release()

    async def _throttle_and_heartbeat(self) -> None:
        """
        - inter_cmd_gap_s: 연속 명령 간 최소 간격 보장
        - heartbeat_s: 너무 오래 I/O 없으면 read_coils(0)으로 keepalive
        """
        now = time.monotonic()
        delta = now - self._last_io_ts

        # 최소 간격
        if delta < self.cfg.inter_cmd_gap_s:
            await asyncio.sleep(self.cfg.inter_cmd_gap_s - delta)

        # 하트비트(너무 오래 아무 I/O 없을 때만)
        if (delta > self.cfg.heartbeat_s) and (not self._hb_paused) and (self._client is not None):
            try:
                await asyncio.to_thread(self._client.read_coils, address=0, count=1, **self._uid_kwargs())
            except Exception:
                # heartbeat 실패는 바로 예외로 올리지 말고, 다음 실제 I/O에서 재연결 로직으로 회복
                pass

        self._last_io_ts = time.monotonic()

    async def _heartbeat_loop(self) -> None:
        """백그라운드 keepalive. connect 후 자동 시작"""
        try:
            while not self._closed:
                await asyncio.sleep(max(1.0, self.cfg.heartbeat_s / 3.0))
                if self._closed:
                    break
                if self._hb_paused:
                    continue
                # heartbeat는 read_coil(0)와 동일한 효과로만 사용 (예외는 삼킴)
                try:
                    async with self._io_lock("heartbeat", addr=0):
                        if self._client is None:
                            continue
                        await asyncio.to_thread(self._client.read_coils, address=0, count=1, **self._uid_kwargs())
                        self._last_io_ts = time.monotonic()
                except Exception:
                    # 실제 read/write에서 재연결하도록 둠
                    continue
        except asyncio.CancelledError:
            return

    @asynccontextmanager
    async def pause_heartbeat(self):
        """연속 명령/민감 구간에서 heartbeat가 끼어들지 않도록 잠깐 정지"""
        old = self._hb_paused
        self._hb_paused = True
        try:
            yield
        finally:
            self._hb_paused = old

    # --------------------------------------------------
    # 주소/이름 해석
    # --------------------------------------------------

    def _build_synonyms(self) -> Dict[str, str]:
        """
        다양한 입력(name)을 PLC_COIL_MAP/PLC_REG_MAP의 canonical key로 매핑
        - 대소문자/공백/언더스코어/슬래시 등 정규화
        - RP/RV/FV/MV/VV/TMP 같은 약어 지원
        - Shutter1/2, MainShutter, Gas1/2, DACPower1/2 지원
        """
        syn: Dict[str, str] = {}

        def norm(s: str) -> str:
            return (
                s.strip()
                .upper()
                .replace(" ", "")
                .replace("_", "")
                .replace("-", "")
                .replace("/", "")
            )

        # 기본: 맵 키 자체도 정규화해서 인식되게
        for k in PLC_COIL_MAP.keys():
            syn[norm(k)] = k
        for k in PLC_REG_MAP.keys():
            syn[norm(k)] = k

        # 사람이 치는 약칭들
        # Vacuum / valve
        syn[norm("RP")] = "R_P_SW"
        syn[norm("RV")] = "R_V_SW"
        syn[norm("FV")] = "F_V_SW"
        syn[norm("MV")] = "M_V_SW"
        syn[norm("VV")] = "V_V_SW"
        syn[norm("V/V")] = "V_V_SW"
        syn[norm("VENT")] = "V_V_SW"
        syn[norm("TMP")] = "TMP_SW"

        # Shutter
        syn[norm("SHUTTER1")] = "SHUTTER_1_SW"
        syn[norm("SHUTTER2")] = "SHUTTER_2_SW"
        syn[norm("MAINSHUTTER")] = "MAIN_SHUTTER_SW"
        syn[norm("MS")] = "MAIN_SHUTTER_SW"

        # Utilities
        syn[norm("AIR")] = "AIR_SW"
        syn[norm("WATER")] = "WATER_SW"

        # Gas
        syn[norm("G1")] = "GAS_1_SW"
        syn[norm("G2")] = "GAS_2_SW"
        syn[norm("GAS1")] = "GAS_1_SW"
        syn[norm("GAS2")] = "GAS_2_SW"

        # Power / Door / FTM
        syn[norm("POWER1")] = "POWER_1_SW"
        syn[norm("POWER2")] = "POWER_2_SW"
        syn[norm("DOOR")] = "DOOR_SW"
        syn[norm("FTM")] = "FTM_SW"

        # DAC registers
        syn[norm("DACPOWER1")] = "DAC_POWER_1"
        syn[norm("DACPOWER2")] = "DAC_POWER_2"
        syn[norm("DAC1")] = "DAC_POWER_1"
        syn[norm("DAC2")] = "DAC_POWER_2"

        return syn

    def _parse_m_device_to_coil(self, s: str) -> int:
        """
        'M0000B' 같은 문자열을 coil address(int)로 변환
        - M 디바이스 번호는 hex로 해석(기존 CH1/CH2 plc.py와 동일한 관례)
        """
        t = s.strip().upper()
        if not t.startswith("M"):
            raise ValueError(f"not M device: {s}")
        num = t[1:]
        # num에 A-F가 포함될 수 있으므로 base=16
        return int(num, 16)

    def _parse_d_device_to_reg(self, s: str) -> int:
        """
        'D00001' 같은 문자열을 holding register address(int)로 변환
        - 일반적으로 D는 decimal로 쓰지만, 혹시 A-F가 섞이면 hex로 파싱
        """
        t = s.strip().upper()
        if not t.startswith("D"):
            raise ValueError(f"not D device: {s}")
        num = t[1:]
        base = 16 if any(c in "ABCDEF" for c in num) else 10
        return int(num, base)

    def _addr(self, name_or_addr: Any) -> int:
        """
        주소 해석 우선순위
        1) int면 그대로
        2) 맵 키면 맵에서
        3) 별칭/정규화(synonyms)로 canonical 키 찾기
        4) "M00020" / "D00000" 같은 디바이스 문자열 직접 파싱
        5) "0x20" 같은 숫자 문자열도 허용
        """
        if isinstance(name_or_addr, int):
            return name_or_addr

        key_raw = str(name_or_addr).strip()
        if not key_raw:
            raise ValueError("empty address/name")

        # 2) 맵에 직접 존재
        if key_raw in PLC_COIL_MAP:
            return PLC_COIL_MAP[key_raw]
        if key_raw in PLC_REG_MAP:
            return PLC_REG_MAP[key_raw]

        # 3) synonyms로 canonical 키 찾기
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

        # 4) Mxxxx / Dxxxx 직접 파싱
        up = key_raw.upper()
        if up.startswith("M"):
            return self._parse_m_device_to_coil(up)
        if up.startswith("D"):
            return self._parse_d_device_to_reg(up)

        # 5) 0x.. / 숫자 문자열 허용
        try:
            return int(key_raw, 0)
        except Exception:
            raise KeyError(f"알 수 없는 주소/이름: {name_or_addr}")

    def _is_reg_name(self, name: Any) -> bool:
        """name이 레지스터 맵(holding register)인지 간단 판별"""
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

    # --------------------------------------------------
    # 저수준 I/O (coil / register)
    # --------------------------------------------------

    async def read_coil(self, addr: int) -> bool:
        async with self._io_lock("read_coil", addr=addr):
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_and_heartbeat()
            try:
                resp = await asyncio.to_thread(self._client.read_coils, addr, count=1, **self._uid_kwargs())
            except Exception as e:
                if self._is_reset_err(e):
                    await asyncio.to_thread(self._close_sync)
                    await asyncio.to_thread(self._connect_sync)
                    await self._throttle_and_heartbeat()
                    resp = await asyncio.to_thread(self._client.read_coils, addr, count=1, **self._uid_kwargs())
                else:
                    raise
            self._ensure_ok(resp)
            return bool(resp.bits[0])

    async def write_coil(self, addr: int, value: bool) -> None:
        async with self._io_lock("write_coil", addr=addr):
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_and_heartbeat()
            try:
                resp = await asyncio.to_thread(self._client.write_coil, addr, bool(value), **self._uid_kwargs())
            except Exception as e:
                if self._is_reset_err(e):
                    await asyncio.to_thread(self._close_sync)
                    await asyncio.to_thread(self._connect_sync)
                    await self._throttle_and_heartbeat()
                    resp = await asyncio.to_thread(self._client.write_coil, addr, bool(value), **self._uid_kwargs())
                else:
                    raise
            self._ensure_ok(resp)

    async def read_reg(self, addr: int) -> int:
        async with self._io_lock("read_reg", addr=addr):
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_and_heartbeat()
            try:
                resp = await asyncio.to_thread(self._client.read_holding_registers, addr, count=1, **self._uid_kwargs())
            except Exception as e:
                if self._is_reset_err(e):
                    await asyncio.to_thread(self._close_sync)
                    await asyncio.to_thread(self._connect_sync)
                    await self._throttle_and_heartbeat()
                    resp = await asyncio.to_thread(self._client.read_holding_registers, addr, count=1, **self._uid_kwargs())
                else:
                    raise
            self._ensure_ok(resp)
            return int(resp.registers[0])

    async def write_reg(self, addr: int, value: int) -> None:
        async with self._io_lock("write_reg", addr=addr):
            await asyncio.to_thread(self._connect_sync)
            await self._throttle_and_heartbeat()
            try:
                resp = await asyncio.to_thread(self._client.write_register, addr, int(value), **self._uid_kwargs())
            except Exception as e:
                if self._is_reset_err(e):
                    await asyncio.to_thread(self._close_sync)
                    await asyncio.to_thread(self._connect_sync)
                    await self._throttle_and_heartbeat()
                    resp = await asyncio.to_thread(self._client.write_register, addr, int(value), **self._uid_kwargs())
                else:
                    raise
            self._ensure_ok(resp)

    # --------------------------------------------------
    # 공통 고수준: pulse / write_switch / read_bit
    # --------------------------------------------------

    async def pulse(self, addr: int, *, ms: Optional[int] = None) -> None:
        """coil을 True로 했다가 ms 후 False로 복귀(순간 입력이 필요한 PLC 로직에 사용)"""
        width = self.cfg.pulse_ms if ms is None else int(ms)
        await self.write_coil(addr, True)
        await asyncio.sleep(max(0.01, width / 1000.0))
        await self.write_coil(addr, False)

    async def write_switch(
        self,
        name_or_addr: Any,
        on: bool,
        *,
        momentary: bool = False,
        pulse_ms: Optional[int] = None,
    ) -> None:
        """
        coil 제어 공통 함수
        - momentary=False: 래치(set/reset)로 동작 (일반적인 ON/OFF)
        - momentary=True : pulse()로 짧게 눌렀다 떼는 형태
        """
        addr = self._addr(name_or_addr)

        # name_or_addr가 register면 여기로 오면 안 됨
        if self._is_reg_name(name_or_addr):
            raise TypeError(f"write_switch는 COIL 전용입니다. register로 보이는 입력: {name_or_addr}")

        if momentary:
            self.log("pulse %s (addr=%d, %sms)", name_or_addr, addr, str(pulse_ms or self.cfg.pulse_ms))
            await self.pulse(addr, ms=pulse_ms)
        else:
            self.log("set %s <- %s (addr=%d)", name_or_addr, on, addr)
            await self.write_coil(addr, bool(on))

    async def press_switch(self, name_or_addr: Any, pulse_ms: Optional[int] = None) -> None:
        """항상 momentary=True로 동작시키는 헬퍼"""
        await self.write_switch(name_or_addr, True, momentary=True, pulse_ms=pulse_ms)

    async def read_bit(self, name_or_addr: Any) -> bool:
        """coil 상태 읽기"""
        addr = self._addr(name_or_addr)
        if self._is_reg_name(name_or_addr):
            raise TypeError(f"read_bit은 COIL 전용입니다. register로 보이는 입력: {name_or_addr}")
        v = await self.read_coil(addr)
        self.log("read %s (addr=%d) -> %s", name_or_addr, addr, v)
        return bool(v)

    async def read_reg_name(self, name_or_addr: Any) -> int:
        """register 읽기"""
        addr = self._addr(name_or_addr)
        if not self._is_reg_name(name_or_addr):
            # 이름이 register 맵이 아니라도, addr가 D영역일 수는 있어서 그냥 읽어줌
            pass
        v = await self.read_reg(addr)
        self.log("read_reg %s (addr=%d) -> %d", name_or_addr, addr, v)
        return int(v)

    async def write_reg_name(self, name_or_addr: Any, value: int) -> None:
        """register 쓰기"""
        addr = self._addr(name_or_addr)
        if not self._is_reg_name(name_or_addr):
            # 이름이 register 맵이 아니라도 addr로 직접 쓰는 경우를 허용
            pass
        self.log("write_reg %s (addr=%d) <- %d", name_or_addr, addr, int(value))
        await self.write_reg(addr, int(value))

    # --------------------------------------------------
    # 4) 이번 장비용 "고수준" API (읽기 쉬운 함수들)
    # --------------------------------------------------

    # Vacuum / Valve / Pump
    async def rp(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("RP", on, momentary=momentary)

    async def rv(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("RV", on, momentary=momentary)

    async def fv(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("FV", on, momentary=momentary)

    async def mv(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("MV", on, momentary=momentary)

    async def vv(self, on: bool = True, *, momentary: bool = False) -> None:
        """V/V(보통 Vent Valve로 쓰는 경우가 많아서 vv/vent 둘 다 별칭 처리됨)"""
        await self.write_switch("V/V", on, momentary=momentary)

    async def tmp(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("TMP", on, momentary=momentary)

    # Utilities / Gas
    async def air(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("AIR", on, momentary=momentary)

    async def water(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("WATER", on, momentary=momentary)

    async def gas1(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("G1", on, momentary=momentary)

    async def gas2(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("G2", on, momentary=momentary)

    # Shutters / FTM
    async def shutter1(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("SHUTTER1", on, momentary=momentary)

    async def shutter2(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("SHUTTER2", on, momentary=momentary)

    async def main_shutter(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("MAINSHUTTER", on, momentary=momentary)

    async def ftm(self, on: bool = True, *, momentary: bool = False) -> None:
        """FTM enable/disable 용도로 가정(정확한 동작은 PLC 로직에 따라 다를 수 있음)"""
        await self.write_switch("FTM", on, momentary=momentary)

    # Power / Door
    async def power1(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("POWER1", on, momentary=momentary)

    async def power2(self, on: bool = True, *, momentary: bool = False) -> None:
        await self.write_switch("POWER2", on, momentary=momentary)

    async def door(self, on: bool = True, *, momentary: bool = False) -> None:
        """
        DOOR 코일이 '문 열기'인지 '도어락'인지 장비마다 다릅니다.
        - 래치형이면 momentary=False
        - 트리거형(버튼처럼)이라면 momentary=True로 사용
        """
        await self.write_switch("DOOR", on, momentary=momentary)

    # DAC (Holding registers)
    async def set_dac_power(self, ch: int, code: int) -> None:
        """
        DAC POWER 설정(원시 코드값)
        - ch=1 → D00000
        - ch=2 → D00001
        """
        if int(ch) == 1:
            key = "DAC_POWER_1"
        elif int(ch) == 2:
            key = "DAC_POWER_2"
        else:
            raise ValueError("DAC channel must be 1 or 2")

        code_i = int(code)
        await self.write_reg_name(key, code_i)

    async def set_dac_voltage(self, ch: int, volt: float) -> int:
        """
        0~10V 가정으로 voltage를 DAC code로 변환 후 write.
        - 실제 장비 스케일(예: 0~5V, 0~20mA, full scale code 등)은 PLC/아날로그 모듈 설정에 따라 달라집니다.
        """
        v = float(volt)
        v = max(0.0, min(self.cfg.dac_full_scale_volt, v))

        fs_code = int(self.cfg.dac_full_scale_code)
        fs_volt = float(self.cfg.dac_full_scale_volt)
        offset = int(self.cfg.dac_offset_code)

        code = int(round((v / fs_volt) * fs_code)) + offset
        code = max(0, min(fs_code, code))

        await self.set_dac_power(ch, code)
        return code

    # --------------------------------------------------
    # 5) 범용 명령 엔드포인트 (UI/서버에서 문자열로 제어할 때 유용)
    # --------------------------------------------------

    async def cmd(self, name: str, on: bool = True, *, momentary: bool = False, pulse_ms: Optional[int] = None) -> None:
        """
        범용 coil 제어
        예)
          await plc.cmd("RP", True)
          await plc.cmd("V/V", False)
          await plc.cmd("Shutter1", True)
          await plc.cmd("M00020", True)   # 직접 주소도 가능
        """
        await self.write_switch(name, bool(on), momentary=momentary, pulse_ms=pulse_ms)


# ======================================================
# (선택) 간단 테스트용 메인
# ======================================================
if __name__ == "__main__":
    async def _demo():
        # 로거를 print로 대체(프로젝트에서는 기존 logger 함수/메서드 넘기면 됨)
        def _log(fmt, *args):
            try:
                msg = fmt % args
            except Exception:
                msg = f"{fmt} {args}"
            print(msg)

        plc = AsyncPLC(ip="192.168.1.2", unit=1, logger=_log)
        await plc.connect()

        # 예시: AIR ON -> 1초 -> OFF
        await plc.air(True)
        await asyncio.sleep(1.0)
        await plc.air(False)

        # 예시: DAC1을 5V로
        code = await plc.set_dac_voltage(1, 5.0)
        print("DAC1 code =", code)

        await plc.close()

    asyncio.run(_demo())
