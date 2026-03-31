# -*- coding: utf-8 -*-
"""
services/plc_service.py

PLCService
- PLC(Modbus RTU/RS-232) 통신을 '단일 스레드/단일 연결'로 독점 관리하는 서비스 계층
- UI(HMI)와 Process(공정)가 동시에 PLC를 건드려도 포트 충돌이 나지 않도록:
    -> 모든 I/O는 이 서비스의 워커 스레드에서만 수행
    -> 외부는 "명령 enqueue/submit"만 한다.

구성
- PLCService(QObject): 앱/컨트롤러가 사용하는 공개 API
- PlcServiceWorker(QThread): 내부에서 asyncio event loop + AsyncPLC를 구동
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

RegValue = Union[int, float]

from PySide6.QtCore import QObject, QThread, Signal

from devices.plc import AsyncPLC
from config.plc_config import PLCSettings


# ============================================================
# ============================================================
# PLC 코일/레지스터 한글 설명 매핑
# ============================================================

_COIL_LABEL: Dict[str, str] = {
    "R_P_SW":          "러핑 펌프",
    "R_V_SW":          "러핑 밸브",
    "F_V_SW":          "포어라인 밸브",
    "M_V_SW":          "메인 게이트 밸브",
    "V_V_SW":          "벤팅 밸브",
    "TMP_SW":          "터보 분자 펌프",
    "SHUTTER_1_SW":    "소스 셔터 1",
    "SHUTTER_2_SW":    "소스 셔터 2",
    "MAIN_SHUTTER_SW": "메인 셔터",
    "POWER_1_SW":      "전원 1",
    "POWER_2_SW":      "전원 2",
    "FTM_SW":          "FTM 전원",
    "DOOR_SW":         "도어 잠금",
    "AIR_SW":          "에어 공급",
    "WATER_SW":        "냉각수 공급",
    "GAUGE_1_SW":      "게이지 1",
    "GAUGE_2_SW":      "게이지 2",
}

_DAC_FULL_SCALE: int = 4095   # DAC 풀스케일 코드 (12-bit)

# PLC Future 블로킹 최대 대기 시간.
# 워커 스레드가 예기치 않게 종료되었을 때 영구 블로킹을 방지한다.
_PLC_FUTURE_TIMEOUT_S: float = 10.0


def _format_plc_cmd_trace(d: dict) -> str:
    """
    PLC cmd_trace dict → 사람이 읽기 쉬운 로그 문자열.

    CmdWriteCoil  : "[PLC] 메인 셔터(MAIN_SHUTTER_SW) → ON  (tag: EVAP_MAIN_SHUTTER_OPEN)"
    CmdWriteReg   : "[PLC] DAC CH1: 700 / 4095 (17.1%)"
    ADC_READBACK  : "[PLC] ADC CH1: raw=523 / filtered=518.4"
    """
    event  = str(d.get("event", ""))
    target = str(d.get("target", ""))
    value  = d.get("value", "")
    tag    = str(d.get("tag", "")).strip()
    ok     = bool(d.get("ok", True))
    # msg가 이미 있으면 재사용 (ADC_READBACK 등에서 미리 채운 것)
    if d.get("msg"):
        return str(d["msg"])

    status = "" if ok else " [FAIL]"
    tag_str = f"  (tag: {tag})" if tag else ""

    if event == "CmdWriteCoil":
        label = _COIL_LABEL.get(target, "")
        label_str = f"{label}({target})" if label else target
        val_str = "ON" if value else "OFF"
        detail = str(d.get("detail", "")).strip()
        extra = f" / {detail}" if detail else ""
        return f"[PLC] {label_str} → {val_str}{extra}{status}{tag_str}"

    if event == "CmdWriteReg":
        t_upper = target.upper()
        if t_upper in ("DAC_POWER_1", "DAC_POWER_2"):
            ch = "1" if t_upper.endswith("_1") else "2"
            try:
                v = int(value)
                pct = v / _DAC_FULL_SCALE * 100.0
                return f"[PLC] DAC {ch} = {v}{status}{tag_str}"
            except Exception:
                pass
        return f"[PLC] REG {target} = {value}{status}{tag_str}"

    if event == "CmdSetDacCurrent":
        return f"[PLC] DAC 전류 설정: CH{target.replace('DAC_CH','')} = {value} mA{status}{tag_str}"

    return f"[PLC] {event} target={target} value={value}{status}{tag_str}"


# ============================================================
# Command definitions
# ============================================================

@dataclass
class _CmdBase:
    tag: str = ""
    reply: Any = None
    retries_left: int = 0   # ✅ 디바이스(plc.py)의 io_policy에 맡김 (서비스 기본 재시도는 끔)


@dataclass
class CmdWakeup(_CmdBase):
    """stop() 시 sleep/queue wait를 깨우기 위한 명령."""
    pass


@dataclass
class CmdWriteCoil(_CmdBase):
    coil_name: str = ""
    on: bool = False
    momentary: bool = False
    pulse_ms: Optional[int] = None


@dataclass
class CmdWriteReg(_CmdBase):
    reg_name: str = ""
    value: int = 0


@dataclass
class CmdSetDacCurrent(_CmdBase):
    ch: int = 1
    ma: float = 4.0


PLCCommand = Union[CmdWakeup, CmdWriteCoil, CmdWriteReg, CmdSetDacCurrent]


class PLCCommandError(Exception):
    """PLC 명령 실행 실패(재연결 후 재시도 트리거용)."""
    pass


# ============================================================
# Snapshot (optional for UI/Process)
# ============================================================

@dataclass(frozen=True)
class PLCSnapshot:
    ts: float
    connected: bool
    coils: Dict[str, bool]
    regs: Dict[str, RegValue]


# ============================================================
# Worker Thread
# ============================================================

class PlcServiceWorker(QThread):
    """
    PLC 폴링 + 명령 처리 워커 스레드.

    - 내부에서 asyncio event loop를 생성하고 AsyncPLC를 사용
    - 외부는 enqueue/submit으로 명령만 넣는다.
    """

    sig_connected = Signal(bool)   # 연결 상태 변화
    sig_error = Signal(str)        # 에러 메시지(문자열 1개만!)
    sig_coils = Signal(object)     # Dict[str,bool]
    sig_regs = Signal(object)      # Dict[str,int]
    sig_snapshot = Signal(object)  # PLCSnapshot
    sig_cmd_trace = Signal(object) # ✅ {"ok":bool, "event":..., "target":..., "value":..., "tag":..., "detail":...}

    def __init__(self, settings: PLCSettings, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._settings = settings

        self._stop_evt = threading.Event()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._cmd_q: Optional[asyncio.Queue] = None

        # ✅ 실패한 명령을 “순서 보존”하면서 재시도하기 위한 슬롯
        self._retry_cmd: Optional[PLCCommand] = None

        # 마지막 스냅샷(서비스가 조회할 수 있게)
        self._last_snapshot: Optional[PLCSnapshot] = None
        self._connected: bool = False

        # 공정 실행 중일 때만 ADC readback trace를 emit
        self._process_logging: bool = False

    def _publish_disconnected_snapshot(self) -> None:
        """
        연결 해제 상태를 last_snapshot에도 반영해서
        get_last_snapshot()이 stale connected 상태를 반환하지 않게 한다.
        """
        snap = PLCSnapshot(
            ts=time.time(),
            connected=False,
            coils={},
            regs={},
        )
        self._last_snapshot = snap
        try:
            self.sig_snapshot.emit(snap)
        except Exception:
            pass

    def is_connected(self) -> bool:
        return bool(self._connected)

    # -------------------------------
    # Public API (다른 스레드에서 호출)
    # -------------------------------
    def stop(self) -> None:
        """워커 중지 요청."""
        self._stop_evt.set()

        loop = self._loop
        q = self._cmd_q
        if not loop or not q:
            return

        def _wakeup() -> None:
            try:
                q.put_nowait(CmdWakeup(tag="__WAKEUP__"))
            except Exception:
                pass

        try:
            loop.call_soon_threadsafe(_wakeup)
        except Exception:
            pass

    def enqueue(self, cmd: PLCCommand) -> None:
        """명령을 큐에 넣기(결과 기다리지 않음)."""
        loop = self._loop
        q = self._cmd_q
        if (not loop) or (not q):
            raise RuntimeError("PLCServiceWorker not started. Call PLCService.start() before enqueue().")

        # ✅ 가장 단순/가벼운 방식: loop 스레드에서 put_nowait 실행
        def _put() -> None:
            try:
                q.put_nowait(cmd)
            except Exception as e:
                # ✅ submit(reply)인 경우 future가 pending으로 남지 않게 예외 처리
                fut = getattr(cmd, "reply", None)
                if fut is not None:
                    try:
                        if not fut.done():
                            fut.set_exception(e)
                    except Exception:
                        pass

        loop.call_soon_threadsafe(_put)

    # -------------------------------
    # Thread entry
    # -------------------------------
    def run(self) -> None:
        # ✅ 재시작 가능하도록 stop flag / 내부 상태 초기화
        self._stop_evt.clear()
        self._retry_cmd = None
        self._connected = False

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._cmd_q = asyncio.Queue()

        # 이전 실행의 stale snapshot 제거
        self._publish_disconnected_snapshot()

        try:
            loop.run_until_complete(self._main())
        except Exception as e:
            try:
                self.sig_error.emit(f"[PLCService] worker crashed: {e!r}")
            except Exception:
                pass
        finally:
            try:
                loop.stop()
                loop.close()
            except Exception:
                pass

            # ✅ 종료 후 stale loop/queue 참조 제거
            self._loop = None
            self._cmd_q = None

    # -------------------------------
    # Internal async logic
    # -------------------------------
    async def _main(self) -> None:
        plc = AsyncPLC(
            settings=self._settings,   # ✅ 여기 추가 (io_policy 포함, ini 재로드 방지)

            port=self._settings.port,
            method=self._settings.method,  # ✅ 설정값 그대로 사용
            baudrate=self._settings.baudrate,
            bytesize=self._settings.bytesize,
            parity=self._settings.parity,
            stopbits=self._settings.stopbits,
            unit=self._settings.unit,
            timeout_s=self._settings.timeout_s,
            pulse_ms=self._settings.pulse_ms,
            # DAC (EV는 코드값 write_reg만 사용)
            dac_full_scale_code=self._settings.dac_full_scale_code,
            dac_offset_code=self._settings.dac_offset_code,
        )

        def _emit_connected(v: bool) -> None:
            v = bool(v)
            if self._connected == v:
                return

            self._connected = v
            self.sig_connected.emit(v)

            if not v:
                self._publish_disconnected_snapshot()

        async def connect_until_ok() -> None:
            # 지수 백오프 재연결: 실패할수록 대기 시간이 factor배씩 증가하여 max_s로 수렴
            base = float(getattr(self._settings, "reconnect_backoff_base_s", getattr(self._settings, "reconnect_interval_s", 0.6)) or 0.6)
            factor = float(getattr(self._settings, "reconnect_backoff_factor", 1.7) or 1.7)
            max_s = float(getattr(self._settings, "reconnect_backoff_max_s", 5.0) or 5.0)

            backoff = max(0.2, base)

            while not self._stop_evt.is_set():
                try:
                    await plc.connect()
                    _emit_connected(True)
                    return
                except Exception as e:
                    _emit_connected(False)
                    self.sig_error.emit(
                        f"[PLCService] connect failed: {e!r} "
                        f"(port={self._settings.port} baud={self._settings.baudrate} "
                        f"8{self._settings.parity}{self._settings.stopbits} unit={self._settings.unit} "
                        f"timeout={float(self._settings.timeout_s):.3f}s)"
                    )
                    try:
                        await plc.close()
                    except Exception:
                        pass

                    await asyncio.sleep(backoff)
                    backoff = min(backoff * factor, max_s)

        await connect_until_ok()

        consecutive_fail = 0
        max_consecutive_fail = 3  # 환경에 따라 2~5 추천

        poll_s = float(getattr(self._settings, "poll_interval_s", 1.0) or 1.0)
        # ✅ 0 허용하면 busy-loop 위험 → 최소 50ms 보장
        if poll_s < 0.05:
            poll_s = 0.05

        while not self._stop_evt.is_set():
            try:
                # 1) 명령 처리(먼저)
                await self._drain_commands(plc)

                # 2) 폴링(필요한 것만)
                coils = await self._read_coils(plc)
                regs = await self._read_regs(plc)

                # 3) 시그널 송출
                self.sig_coils.emit(coils)
                self.sig_regs.emit(regs)

                snap = PLCSnapshot(
                    ts=time.time(),
                    connected=True,
                    coils=coils,
                    regs=regs,
                )
                self._last_snapshot = snap
                self.sig_snapshot.emit(snap)

                consecutive_fail = 0

                # 4) 다음 tick까지 대기(대기 중 명령 들어오면 즉시 처리)
                await self._sleep_with_command_break(plc, poll_s)

            except asyncio.CancelledError:
                break

            except PLCCommandError as e:
                # ✅ 명령 실패는 너 정책대로 "즉시 재연결 후 재시도"가 맞음
                self.sig_error.emit(f"[PLCService] command error: {e!r}")
                _emit_connected(False)
                try:
                    await plc.close()
                except Exception:
                    pass
                await connect_until_ok()
                consecutive_fail = 0
                continue

            except Exception as e:
                consecutive_fail += 1
                self.sig_error.emit(f"[PLCService] loop error: {e!r}")

                if consecutive_fail < max_consecutive_fail:
                    await asyncio.sleep(0.05)
                    continue

                _emit_connected(False)
                try:
                    await plc.close()
                except Exception:
                    pass

                await connect_until_ok()
                consecutive_fail = 0

        # 종료 정리: pending reply future 정리 후 close
        try:
            await self._finalize_pending_replies("PLCService stopped")
        except Exception:
            pass

        try:
            await plc.close()
        except Exception:
            pass
        _emit_connected(False)

    async def _drain_commands(self, plc: AsyncPLC) -> None:
        """
        큐에 쌓인 명령을 가능한 빨리 처리.
        - 실패 시 PLCCommandError를 올려 상위에서 즉시 재연결하도록 함
        """
        q = self._cmd_q
        if not q:
            return

        # ✅ 재시도 중인 명령이 있으면 “순서 보존”을 위해 가장 먼저 처리
        # 큐에 새 명령이 쌓여도 retry 슬롯이 먼저 나가야 명령 순서가 보장된다
        if self._retry_cmd is not None:
            await self._execute_one_command(plc, self._retry_cmd)
            # 성공하면 _execute_one_command에서 self._retry_cmd를 None으로 정리함

        while not self._stop_evt.is_set():
            try:
                cmd: PLCCommand = q.get_nowait()
            except asyncio.QueueEmpty:
                return

            await self._execute_one_command(plc, cmd)

    async def _execute_one_command(self, plc: AsyncPLC, cmd: PLCCommand) -> Any:
        """
        PLCCommand 1개를 실행.
        - 성공 시 reply가 있으면 set_result
        - 실패 시:
          - retries_left > 0 : future는 아직 끝내지 않고, self._retry_cmd에 보관 후 PLCCommandError raise
          - retries_left == 0: future에 set_exception 후 PLCCommandError raise
        """
        if isinstance(cmd, CmdWakeup):
            return None

        try:
            if isinstance(cmd, CmdWriteCoil):
                await plc.write_switch(
                    cmd.coil_name,
                    bool(cmd.on),
                    momentary=bool(cmd.momentary),
                    pulse_ms=cmd.pulse_ms,
                )
                result: Any = True

            elif isinstance(cmd, CmdWriteReg):
                rn = str(cmd.reg_name).upper()
                if rn in ("DAC_POWER_1", "DAC_POWER_2"):
                    ch = 1 if rn.endswith("_1") else 2
                    await plc.set_dac_power(ch, int(cmd.value))
                else:
                    await plc.write_reg_name(rn, int(cmd.value))
                result = True

            elif isinstance(cmd, CmdSetDacCurrent):
                code = await plc.set_dac_current(int(cmd.ch), float(cmd.ma))
                result = int(code)

            else:
                raise RuntimeError(f"Unknown PLCCommand: {cmd!r}")

            # ✅ 성공: 재시도 슬롯에 있던 명령이면 해제
            if self._retry_cmd is cmd:
                self._retry_cmd = None

            if cmd.reply is not None:
                try:
                    cmd.reply.set_result(result)
                except Exception:
                    pass

            # ✅ 성공 trace
            try:
                target = getattr(cmd, "coil_name", getattr(cmd, "reg_name", getattr(cmd, "ch", "")))
                value  = getattr(cmd, "on", getattr(cmd, "value", getattr(cmd, "ma", "")))
                detail = ""

                # ✅ CmdWriteReg는 target을 대문자로 통일(로그 일관성)
                if isinstance(cmd, CmdWriteReg):
                    target = str(cmd.reg_name).upper()

                # ✅ (개선1) CmdSetDacCurrent: target을 숫자 ch가 아니라 DAC_CH{n} 형태로
                if isinstance(cmd, CmdSetDacCurrent):
                    target = f"DAC_CH{int(cmd.ch)}"

                # ✅ (개선2) 펄스 코일: pulse_ms를 detail에 표시
                if isinstance(cmd, CmdWriteCoil) and cmd.pulse_ms is not None:
                    # momentary 여부도 같이 남기면 더 알아보기 쉬움(원하면 제거 가능)
                    if cmd.momentary:
                        detail = f"momentary=1,pulse_ms={int(cmd.pulse_ms)}"
                    else:
                        detail = f"pulse_ms={int(cmd.pulse_ms)}"

                trace_d = {
                    "ok": True,
                    "event": type(cmd).__name__,
                    "target": target,
                    "value": value,
                    "tag": getattr(cmd, "tag", ""),
                    "detail": detail,
                    "result": result,
                }
                try:
                    trace_d["msg"] = _format_plc_cmd_trace(trace_d)
                except Exception:
                    pass
                self.sig_cmd_trace.emit(trace_d)
            except Exception:
                pass

            return result

        except Exception as e:
            # ✅ 실패한 명령이 retry 슬롯에 있었으면 계속 유지(성공해야만 해제)
            # ✅ 재시도 남아 있으면: future는 아직 끝내지 말고, 재연결 후 “같은 명령”을 먼저 재시도
            if getattr(cmd, "retries_left", 0) > 0 and (not self._stop_evt.is_set()):
                try:
                    cmd.retries_left -= 1
                except Exception:
                    pass

                # 순서 보존: 큐 뒤로 보내지 않고 retry 슬롯에 보관
                self._retry_cmd = cmd

                # 명령 실패는 “즉시 재연결” 트리거
                raise PLCCommandError(f"cmd failed, will retry after reconnect: {cmd!r} ({e!r})")

            # ✅ 최종 실패 trace (재시도 소진)
            try:
                target = getattr(cmd, "coil_name", getattr(cmd, "reg_name", getattr(cmd, "ch", "")))
                value  = getattr(cmd, "on", getattr(cmd, "value", getattr(cmd, "ma", "")))
                detail = ""

                # ✅ CmdWriteReg는 target을 대문자로 통일(로그 일관성)
                if isinstance(cmd, CmdWriteReg):
                    target = str(cmd.reg_name).upper()

                # CmdSetDacCurrent: target 보정
                if isinstance(cmd, CmdSetDacCurrent):
                    target = f"DAC_CH{int(cmd.ch)}"

                # 펄스 코일이면 pulse_ms 표시(성공 trace와 동일 규칙)
                if isinstance(cmd, CmdWriteCoil) and cmd.pulse_ms is not None:
                    if cmd.momentary:
                        detail = f"momentary=1,pulse_ms={int(cmd.pulse_ms)}"
                    else:
                        detail = f"pulse_ms={int(cmd.pulse_ms)}"

                # 실패 이유 추가
                if detail:
                    detail = f"{detail} | ERR={e!r}"
                else:
                    detail = f"ERR={e!r}"

                fail_trace_d = {
                    "ok": False,
                    "event": type(cmd).__name__,
                    "target": target,
                    "value": value,
                    "tag": getattr(cmd, "tag", ""),
                    "detail": detail,
                    "result": None,
                }
                try:
                    fail_trace_d["msg"] = _format_plc_cmd_trace(fail_trace_d)
                except Exception:
                    pass
                self.sig_cmd_trace.emit(fail_trace_d)
            except Exception:
                pass

            # ✅ (추가1) 최종 실패면 retry 슬롯 해제(남아있으면 다음 명령 흐름이 꼬일 수 있음)
            if self._retry_cmd is cmd:
                self._retry_cmd = None

            # ✅ (추가2) submit 계열(Future)이라면 예외 확정
            if cmd.reply is not None:
                try:
                    if hasattr(cmd.reply, "done") and cmd.reply.done():
                        pass
                    else:
                        cmd.reply.set_exception(e)
                except Exception:
                    pass

            # ✅ (추가3) 최종 실패는 상위로 전파(재연결/에러 처리 흐름이 정상 동작하게)
            raise PLCCommandError(f"cmd failed (no retries left): {cmd!r} ({e!r})")
        

    async def _sleep_with_command_break(self, plc: AsyncPLC, seconds: float) -> None:
        """
        seconds 동안 대기하되,
        대기 중 명령이 들어오면 즉시 처리하고 남은 명령도 연속 처리.
        """
        if seconds <= 0:
            await self._drain_commands(plc)
            return

        q = self._cmd_q
        if not q:
            await asyncio.sleep(max(0.0, seconds))
            return

        try:
            cmd: PLCCommand = await asyncio.wait_for(q.get(), timeout=seconds)
            # ✅ 받은 cmd 먼저 처리(단, wakeup이면 내부에서 스킵)
            await self._execute_one_command(plc, cmd)

            # 남은 명령도 처리
            await self._drain_commands(plc)

        except asyncio.TimeoutError:
            return

    async def _read_coils(self, plc: AsyncPLC) -> Dict[str, bool]:
        """
        HMI/공정에서 공통으로 쓰기 좋은 최소 코일 상태 폴링.
        - 0..12 / 32..33 영역을 block read로 읽어 요청 횟수 최소화
        """
        block0 = await plc.read_coils_block(0, 13)   # 0..12
        block1 = await plc.read_coils_block(32, 4)   # 32..35 (AIR/WATER + GAUGE1/2)

        out: Dict[str, bool] = {
            "R_P_SW": bool(block0[0]),
            "R_V_SW": bool(block0[1]),
            "F_V_SW": bool(block0[2]),
            "M_V_SW": bool(block0[3]),
            "V_V_SW": bool(block0[4]),
            "TMP_SW": bool(block0[5]),
            "SHUTTER_1_SW": bool(block0[6]),
            "SHUTTER_2_SW": bool(block0[7]),
            "MAIN_SHUTTER_SW": bool(block0[8]),
            "POWER_1_SW": bool(block0[9]),
            "POWER_2_SW": bool(block0[10]),
            "FTM_SW": bool(block0[11]),
            "DOOR_SW": bool(block0[12]),

            "AIR_SW": bool(block1[0]),
            "WATER_SW": bool(block1[1]),
            "GAUGE_1_SW": bool(block1[2]),
            "GAUGE_2_SW": bool(block1[3]),
        }
        return out
    
    @staticmethod
    def _u16_to_i16(v: int) -> int:
        """
        PLC에서 unsigned 16-bit로 읽은 값을 signed 16-bit로 해석.
        예:
            65525 -> -11
            11    -> 11
        """
        x = int(v) & 0xFFFF
        # MSB(0x8000)가 1이면 2의 보수 음수로 해석
        return x - 0x10000 if x >= 0x8000 else x

    @classmethod
    def _sanitize_and_scale_power_read(
        cls,
        raw: int,
        *,
        scale: float = 0.1,
    ) -> float:
        """
        ADC readback 표시용 보정.
        - unsigned -> signed 변환
        - 음수는 0 처리
        - 정상값은 scale 적용 후 소수점 1자리 표시

        예:
            65525 -> -11 -> 0.0
            11    -> 1.1
            1000  -> 100.0
        """
        # 유효 범위 밖 raw값(쓰레기값) → 0 처리
        # 정상 PLC ADC 값 범위: 0 ~ 4095 (12bit) 또는 0 ~ 65535 (u16)
        # signed 변환 후 음수이거나, 스케일 적용 시 물리적으로 불가능한 큰 값이면 0
        if raw < 0 or raw > 60000:  # 60000 이상은 쓰레기값으로 처리
            return 0.0

        signed = cls._u16_to_i16(raw)
        if signed < 0:
            return 0.0

        return round(float(signed) * float(scale), 1)

    # MODIFIED: ADC 1 노이즈 임계값 — 이 값 미만이면 0으로 처리 (PLC 노이즈 차단)
    ADC1_NOISE_THRESHOLD: float = 2.0

    async def _read_regs(self, plc: AsyncPLC) -> Dict[str, RegValue]:
        """
        EV에서 필요한 레지스터:
        - DAC set 값 2개 (정수 그대로 유지)
        - ADC readback 값 2개 (signed 보정 + 0.1 스케일 적용)
        - 실패를 조용히 숨기지 않고, 루프 예외로 올려 끊김 감지/재연결 흐름에 태운다.
        """
        out: Dict[str, RegValue] = {}

        # ✅ 현재 set 값은 기존 그대로 유지
        out["DAC_POWER_1"] = int(await plc.read_reg_name("DAC_POWER_1"))
        out["DAC_POWER_2"] = int(await plc.read_reg_name("DAC_POWER_2"))

        # ✅ 실제 readback 값은 sanitize + scale + EMA 필터
        raw1 = int(await plc.read_reg_name("POWER_READ_1"))
        raw2 = int(await plc.read_reg_name("POWER_READ_2"))

        adc1_scaled = self._sanitize_and_scale_power_read(raw1, scale=0.1)
        # ADC 1 노이즈 하한 필터 (PLC 노이즈로 인한 0.x ~ 1.x 값 차단)
        if adc1_scaled < self.ADC1_NOISE_THRESHOLD:
            adc1_scaled = 0.0
        out["POWER_READ_1"] = adc1_scaled
        out["POWER_READ_1_RAW"] = adc1_scaled

        adc2_scaled = self._sanitize_and_scale_power_read(raw2, scale=0.1)
        out["POWER_READ_2"] = adc2_scaled
        out["POWER_READ_2_RAW"] = adc2_scaled

        # ADC readback trace — 공정 실행 중(_process_logging=True)에만 emit (CH1+CH2 1줄로 합산)
        if self._process_logging:
            try:
                self.sig_cmd_trace.emit({
                    "ok": True,
                    "event": "ADC_READBACK",
                    "target": "ADC",
                    "value": adc1_scaled,
                    "tag": "POLL",
                    "detail": f"CH1={adc1_scaled:.1f}, CH2={adc2_scaled:.1f}",
                    "msg": f"[PLC] ADC 1 = {adc1_scaled:.1f}, ADC 2 = {adc2_scaled:.1f}",
                    "result": adc1_scaled,
                })
            except Exception:
                pass

        return out
    
    async def _finalize_pending_replies(self, reason: str) -> None:
        """
        워커 종료/stop 시 submit(reply Future)들이 pending으로 남지 않도록
        큐/리트라이 슬롯의 Future를 예외로 완료 처리.
        """
        exc = RuntimeError(reason)

        # retry 슬롯 먼저 정리
        if self._retry_cmd is not None:
            cmd = self._retry_cmd
            self._retry_cmd = None
            fut = getattr(cmd, "reply", None)
            if fut is not None:
                try:
                    if not fut.done():
                        fut.set_exception(exc)
                except Exception:
                    pass

        q = self._cmd_q
        if not q:
            return

        while True:
            try:
                cmd = q.get_nowait()
            except asyncio.QueueEmpty:
                break

            if isinstance(cmd, CmdWakeup):
                continue

            fut = getattr(cmd, "reply", None)
            if fut is not None:
                try:
                    if not fut.done():
                        fut.set_exception(exc)
                except Exception:
                    pass

    def set_process_logging(self, enabled: bool) -> None:
        """공정 실행 중 ADC readback trace emit 활성/비활성 전환."""
        self._process_logging = bool(enabled)

    # -------------------------------
    # Worker state access
    # -------------------------------
    def get_last_snapshot(self) -> Optional[PLCSnapshot]:
        return self._last_snapshot


# ============================================================
# PLCService (Public API)
# ============================================================

class PLCService(QObject):
    """
    앱에서 사용하는 PLC 서비스.

    - start/stop 관리
    - enqueue/submit API 제공
    - 워커 시그널을 그대로 외부로 전달
    """

    sig_connected = Signal(bool)
    sig_error = Signal(str)
    sig_coils = Signal(object)
    sig_regs = Signal(object)
    sig_snapshot = Signal(object)
    sig_cmd_trace = Signal(object)

    def __init__(self, settings: PLCSettings, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._settings = settings

        self._worker = PlcServiceWorker(settings=settings)
        self._worker.sig_connected.connect(self.sig_connected)
        self._worker.sig_error.connect(self.sig_error)
        self._worker.sig_coils.connect(self.sig_coils)
        self._worker.sig_regs.connect(self.sig_regs)
        self._worker.sig_snapshot.connect(self.sig_snapshot)
        self._worker.sig_cmd_trace.connect(self.sig_cmd_trace)

    # -------------------------------
    # lifecycle
    # -------------------------------
    def start(self) -> None:
        if not self._worker.isRunning():
            self._worker.start()

    def stop(self, wait_ms: int = 3000) -> bool:
        if not self._worker.isRunning():
            return True

        try:
            self._worker.stop()
        except Exception as e:
            try:
                self.sig_error.emit(f"[PLCService] stop request failed: {e!r}")
            except Exception:
                pass
            return False

        # asyncio loop를 강제 stop → 블로킹 중인 I/O 태스크 해제
        # PLC는 asyncio 기반이라 serial 직접 close 대신 loop.stop() 사용
        try:
            loop = getattr(self._worker, "_loop", None)
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        try:
            ok = bool(self._worker.wait(int(wait_ms)))
        except Exception as e:
            try:
                self.sig_error.emit(f"[PLCService] stop wait failed: {e!r}")
            except Exception:
                pass
            return False

        if not ok:
            try:
                self.sig_error.emit(f"[PLCService] stop timeout after {int(wait_ms)} ms")
            except Exception:
                pass

        return ok

    def is_running(self) -> bool:
        return bool(self._worker.isRunning())

    def is_connected(self) -> bool:
        return self._worker.is_connected()

    def set_process_logging(self, enabled: bool) -> None:
        """공정 실행 중 ADC readback trace emit 활성/비활성 전환. 워커에 전파."""
        self._worker.set_process_logging(enabled)

    def get_last_snapshot(self) -> Optional[PLCSnapshot]:
        return self._worker.get_last_snapshot()

    # -------------------------------
    # enqueue (fire-and-forget)
    # -------------------------------
    def enqueue_write_coil(self, coil_name: str, on: bool, *, momentary: bool = False, pulse_ms: Optional[int] = None, tag: str = "") -> None:
        self._worker.enqueue(CmdWriteCoil(coil_name=str(coil_name), on=bool(on), momentary=bool(momentary), pulse_ms=pulse_ms, tag=tag))

    def enqueue_write_reg(self, reg_name: str, value: int, *, tag: str = "") -> None:
        self._worker.enqueue(CmdWriteReg(reg_name=str(reg_name), value=int(value), tag=tag))

    def enqueue_set_dac_current(self, ch: int, ma: float, *, tag: str = "") -> None:
        self._worker.enqueue(CmdSetDacCurrent(ch=int(ch), ma=float(ma), tag=tag))

    # -------------------------------
    # submit (want completion)
    # - 반환 Future는 .result(timeout=...)로 대기 가능
    # - UI 스레드에서는 직접 blocking 하지 말고, 공정 워커(QThread)에서 사용 권장
    # -------------------------------
    def submit_write_coil(self, coil_name: str, on: bool, *, momentary: bool = False, pulse_ms: Optional[int] = None, tag: str = ""):
        fut = concurrent.futures.Future()
        self._worker.enqueue(CmdWriteCoil(coil_name=str(coil_name), on=bool(on), momentary=bool(momentary), pulse_ms=pulse_ms, tag=tag, reply=fut))
        return fut

    def submit_write_reg(self, reg_name: str, value: int, *, tag: str = ""):
        fut = concurrent.futures.Future()
        self._worker.enqueue(CmdWriteReg(reg_name=str(reg_name), value=int(value), tag=tag, reply=fut))
        return fut

    def submit_set_dac_current(self, ch: int, ma: float, *, tag: str = ""):
        fut = concurrent.futures.Future()
        self._worker.enqueue(CmdSetDacCurrent(ch=int(ch), ma=float(ma), tag=tag, reply=fut))
        return fut

    # -------------------------------
    # blocking (synchronous) write — 타임아웃 보호 포함
    # Future.result(timeout=_PLC_FUTURE_TIMEOUT_S) 로 대기하며
    # 워커가 응답 없으면 PLCCommandError 를 raise 한다.
    # 워커가 종료되어 _finalize_pending_replies() 가 RuntimeError 를
    # Future 에 set_exception 하면 그 예외가 그대로 상위로 전파된다.
    # -------------------------------
    def write_coil(self, coil_name: str, on: bool, *, momentary: bool = False, pulse_ms: Optional[int] = None, tag: str = "") -> bool:
        """동기 블로킹 코일 쓰기. 타임아웃 또는 워커 오류 시 예외 발생."""
        fut = self.submit_write_coil(coil_name, on, momentary=momentary, pulse_ms=pulse_ms, tag=tag)
        try:
            return fut.result(timeout=_PLC_FUTURE_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            raise PLCCommandError("PLC 응답 타임아웃 (워커 응답 없음)")
        except Exception:
            raise

    def write_reg(self, reg_name: str, value: int, *, tag: str = "") -> bool:
        """동기 블로킹 레지스터 쓰기. 타임아웃 또는 워커 오류 시 예외 발생."""
        fut = self.submit_write_reg(reg_name, value, tag=tag)
        try:
            return fut.result(timeout=_PLC_FUTURE_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            raise PLCCommandError("PLC 응답 타임아웃 (워커 응답 없음)")
        except Exception:
            raise

    def set_dac_current(self, ch: int, ma: float, *, tag: str = "") -> int:
        """동기 블로킹 DAC 전류 설정. 타임아웃 또는 워커 오류 시 예외 발생."""
        fut = self.submit_set_dac_current(ch, ma, tag=tag)
        try:
            return fut.result(timeout=_PLC_FUTURE_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            raise PLCCommandError("PLC 응답 타임아웃 (워커 응답 없음)")
        except Exception:
            raise
