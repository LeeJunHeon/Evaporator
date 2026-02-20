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
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

from PySide6.QtCore import QObject, QThread, Signal

from devices.plc import AsyncPLC
from config.plc_config import PLCSettings


# ============================================================
# Command definitions
# ============================================================

@dataclass
class _CmdBase:
    tag: str = ""
    reply: Any = None
    retries_left: int = 2   # ✅ 기본 2회 재시도(총 3번: 최초+2)


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

PLCCommand = Union[CmdWakeup, CmdWriteCoil, CmdWriteReg]


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
    regs: Dict[str, int]


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
            except Exception:
                pass

        loop.call_soon_threadsafe(_put)

    # -------------------------------
    # Thread entry
    # -------------------------------
    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._cmd_q = asyncio.Queue()

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

    # -------------------------------
    # Internal async logic
    # -------------------------------
    async def _main(self) -> None:
        plc = AsyncPLC(
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

        connected_flag = False

        def _emit_connected(v: bool) -> None:
            nonlocal connected_flag
            v = bool(v)
            if connected_flag != v:
                connected_flag = v
                self.sig_connected.emit(v)

        async def connect_until_ok() -> None:
            """
            끊김/미응답 시 재시도 루프.
            - AsyncPLC.connect() 내부에서 ping 검증까지 수행
            """
            retry_s = max(0.2, float(getattr(self._settings, "reconnect_interval_s", 1.0) or 1.0))
            while not self._stop_evt.is_set():
                try:
                    await plc.connect()
                    _emit_connected(True)
                    return
                except Exception as e:
                    _emit_connected(False)
                    # ⚠️ sig_error는 반드시 str 1개만 emit
                    msg = (
                        f"[PLCService] connect failed: {e!r} "
                        f"(port={self._settings.port} baud={self._settings.baudrate} "
                        f"8{self._settings.parity}{self._settings.stopbits} unit={self._settings.unit} "
                        f"timeout={float(self._settings.timeout_s):.3f}s)"
                    )
                    self.sig_error.emit(msg)
                    try:
                        await plc.close()
                    except Exception:
                        pass
                    await asyncio.sleep(retry_s)

        await connect_until_ok()

        consecutive_fail = 0
        max_consecutive_fail = 3  # 환경에 따라 2~5 추천

        poll_s = float(getattr(self._settings, "poll_interval_s", 0.25) or 0.25)
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

        # 종료 정리
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
                # ✅ DAC는 clamp 포함된 set_dac_power로 통일
                if cmd.reg_name in ("DAC_POWER_1", "DAC_POWER_2"):
                    ch = 1 if cmd.reg_name.endswith("_1") else 2
                    await plc.set_dac_power(ch, int(cmd.value))
                else:
                    await plc.write_reg_name(cmd.reg_name, int(cmd.value))
                result = True

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

            # ✅ 재시도 소진: 그때만 future에 예외 확정
            if self._retry_cmd is cmd:
                self._retry_cmd = None

            if cmd.reply is not None:
                try:
                    cmd.reply.set_exception(e)
                except Exception:
                    pass

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
        block1 = await plc.read_coils_block(32, 2)   # 32..33 (AIR/WATER만)

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
        }
        return out

    async def _read_regs(self, plc: AsyncPLC) -> Dict[str, int]:
        """
        EV에서 필요한 레지스터: DAC 2개
        - 실패를 조용히 숨기지 않고, 루프 예외로 올려 끊김 감지/재연결 흐름에 태운다.
        """
        out: Dict[str, int] = {}
        out["DAC_POWER_1"] = int(await plc.read_reg_name("DAC_POWER_1"))
        out["DAC_POWER_2"] = int(await plc.read_reg_name("DAC_POWER_2"))
        return out

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

    def __init__(self, settings: PLCSettings, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._settings = settings

        self._worker = PlcServiceWorker(settings=settings)
        self._worker.sig_connected.connect(self.sig_connected)
        self._worker.sig_error.connect(self.sig_error)
        self._worker.sig_coils.connect(self.sig_coils)
        self._worker.sig_regs.connect(self.sig_regs)
        self._worker.sig_snapshot.connect(self.sig_snapshot)

    # -------------------------------
    # lifecycle
    # -------------------------------
    def start(self) -> None:
        if not self._worker.isRunning():
            self._worker.start()

    def stop(self, wait_ms: int = 3000) -> None:
        try:
            self._worker.stop()
        except Exception:
            pass
        try:
            self._worker.wait(int(wait_ms))
        except Exception:
            pass

    def is_running(self) -> bool:
        return bool(self._worker.isRunning())

    def get_last_snapshot(self) -> Optional[PLCSnapshot]:
        return self._worker.get_last_snapshot()

    # -------------------------------
    # enqueue (fire-and-forget)
    # -------------------------------
    def enqueue_write_coil(self, coil_name: str, on: bool, *, momentary: bool = False, pulse_ms: Optional[int] = None, tag: str = "") -> None:
        self._worker.enqueue(CmdWriteCoil(coil_name=str(coil_name), on=bool(on), momentary=bool(momentary), pulse_ms=pulse_ms, tag=tag))

    def enqueue_write_reg(self, reg_name: str, value: int, *, tag: str = "") -> None:
        self._worker.enqueue(CmdWriteReg(reg_name=str(reg_name), value=int(value), tag=tag))

    # -------------------------------
    # submit (want completion)
    # - 반환 Future는 .result(timeout=...)로 대기 가능
    # - UI 스레드에서는 직접 blocking 하지 말고, 공정 워커(QThread)에서 사용 권장
    # -------------------------------
    def submit_write_coil(self, coil_name: str, on: bool, *, momentary: bool = False, pulse_ms: Optional[int] = None, tag: str = ""):
        import concurrent.futures
        fut = concurrent.futures.Future()
        self._worker.enqueue(CmdWriteCoil(coil_name=str(coil_name), on=bool(on), momentary=bool(momentary), pulse_ms=pulse_ms, tag=tag, reply=fut))
        return fut

    def submit_write_reg(self, reg_name: str, value: int, *, tag: str = ""):
        import concurrent.futures
        fut = concurrent.futures.Future()
        self._worker.enqueue(CmdWriteReg(reg_name=str(reg_name), value=int(value), tag=tag, reply=fut))
        return fut
