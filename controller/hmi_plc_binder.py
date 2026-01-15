# -*- coding: utf-8 -*-
"""hmi_plc_binder.py

HMI 페이지(UI)와 PLC(Modbus-TCP)를 연결하는 "바인더".

요구사항 정리(사용자 요청)
 - HMI 페이지의 Indicator(램프)와 버튼을 PLC 주소맵에 맞게 연결
 - PLC 상태를 "계속 읽어서" UI를 업데이트(폴링)
 - 버튼 클릭 시 래치 방식으로 PLC에 신호 전송(ON/OFF 유지)

구현 방식
 - UI 스레드가 멈추지 않도록 PLC 통신은 QThread에서 수행
 - QThread 내부에서 asyncio event loop를 돌려 AsyncPLC를 그대로 사용
   (추가 의존성(qasync) 없이 챔버1&2의 "비동기 PLC" 스타일을 최대한 유지)
"""

from __future__ import annotations

import time
import asyncio
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

from PySide6.QtCore import QObject, QThread, Signal, QSignalBlocker, QTimer
from PySide6.QtWidgets import QMessageBox

from devices.plc import AsyncPLC
from config.plc_config import PLCSettings


# ------------------------------------------------------------
# UI 위젯 <-> PLC 코일 매핑
#  - UI objectName은 ui/mainWindow.py 기준(당신이 디자이너로 만든 이름)
#  - PLC 코일 이름은 devices/plc.py의 PLC_COIL_MAP 키
# ------------------------------------------------------------


@dataclass(frozen=True)
class ButtonBinding:
    widget_name: str
    coil_name: str
    momentary: bool = False  # 기본은 래치. (필요시 버튼별로 True 설정 가능)


class PlcWorker(QThread):
    """PLC 폴링 + 쓰기를 담당하는 워커 스레드."""

    sig_connected = Signal(bool)     # True/False
    sig_error = Signal(str)          # 에러 문자열
    sig_states = Signal(object)      # dict[str,bool] (coil_name -> state)

    def __init__(self, settings: PLCSettings, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._settings = settings

        # stop 플래그(스레드 종료 요청)
        self._stop_evt = threading.Event()

        # 스레드 내부 asyncio loop와 command queue
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._cmd_q: Optional[asyncio.Queue] = None

        # start() 이전에 enqueue가 들어오면 잠깐 보관
        self._pending_cmds: List[Tuple[str, bool, bool]] = []
        self._pending_lock = threading.Lock()

    # -------------------------------
    # public API (메인 스레드에서 호출)
    # -------------------------------
    def stop(self) -> None:
        """워커 중지 요청."""
        self._stop_evt.set()
        if self._loop:
            # sleep 중이어도 빨리 깨어나도록 빈 콜백 한 번
            try:
                self._loop.call_soon_threadsafe(lambda: None)
            except Exception:
                pass

    def enqueue_write(self, coil_name: str, on: bool, momentary: bool = False) -> None:
        """PLC에 coil write 요청을 큐에 넣는다."""
        if self._loop and self._cmd_q:
            asyncio.run_coroutine_threadsafe(
                self._cmd_q.put((coil_name, bool(on), bool(momentary))),
                self._loop,
            )
        else:
            # 아직 run() 시작 전이면 보관
            with self._pending_lock:
                self._pending_cmds.append((coil_name, bool(on), bool(momentary)))

    # -------------------------------
    # QThread entry
    # -------------------------------
    def run(self) -> None:
        """스레드 진입점: asyncio loop 생성 후 코루틴 실행."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._cmd_q = asyncio.Queue()

        # start 전에 들어온 커맨드 flush
        with self._pending_lock:
            for cmd in self._pending_cmds:
                loop.call_soon_threadsafe(self._cmd_q.put_nowait, cmd)
            self._pending_cmds.clear()

        try:
            loop.run_until_complete(self._main(loop))
        finally:
            try:
                loop.stop()
                loop.close()
            except Exception:
                pass

    # -------------------------------
    # internal async logic
    # -------------------------------
    async def _main(self, loop: asyncio.AbstractEventLoop) -> None:
        # AsyncPLC는 내부에서 ModbusTcpClient를 사용
        # (이 프로젝트의 devices/plc.py는 cfg 인자를 받지 않고, 생성자 파라미터로 설정을 받는다)
        plc = AsyncPLC(
            port=self._settings.port,
            method=self._settings.method,
            baudrate=self._settings.baudrate,
            bytesize=self._settings.bytesize,
            parity=self._settings.parity,
            stopbits=self._settings.stopbits,
            unit=self._settings.unit,
            timeout_s=self._settings.timeout_s,
            pulse_ms=self._settings.pulse_ms,
        )

        async def connect_until_ok() -> None:
            while not self._stop_evt.is_set():
                try:
                    await plc.connect()
                    self.sig_connected.emit(True)
                    return
                except Exception as e:
                    self.sig_connected.emit(False)
                    self.sig_error.emit(f"PLC connect failed: {e!r}")
                    await asyncio.sleep(max(0.2, self._settings.reconnect_interval_s))

        await connect_until_ok()

        # 폴링 루프
        while not self._stop_evt.is_set():
            try:
                # 1) 명령 처리 (큐에 있으면 즉시 처리)
                await self._drain_commands(plc)

                # 2) 상태 읽기
                states = await self._read_hmi_states(plc)
                self.sig_states.emit(states)

                # 3) 다음 폴링까지 대기(대기 중에도 커맨드가 오면 빨리 처리)
                await self._sleep_with_command_break(plc, self._settings.poll_interval_s)

            except Exception as e:
                # 통신 오류 → 연결 재시도
                self.sig_connected.emit(False)
                self.sig_error.emit(f"PLC polling error: {e!r}")
                try:
                    await plc.close()
                except Exception:
                    pass
                await connect_until_ok()

        # 종료
        try:
            await plc.close()
        except Exception:
            pass
        self.sig_connected.emit(False)

    async def _drain_commands(self, plc: AsyncPLC) -> None:
        if not self._cmd_q:
            return

        # 큐가 비면 빠르게 빠져나옴
        while not self._cmd_q.empty():
            coil_name, on, momentary = self._cmd_q.get_nowait()
            await plc.write_switch(coil_name, on, momentary=momentary)

    async def _sleep_with_command_break(self, plc: AsyncPLC, seconds: float) -> None:
        """seconds 동안 자되, 그 사이 커맨드가 들어오면 즉시 처리하고 계속 대기."""
        if not self._cmd_q or seconds <= 0:
            await asyncio.sleep(max(0.0, seconds))
            return

        try:
            # 커맨드 하나가 오거나, timeout이 되거나
            coil_name, on, momentary = await asyncio.wait_for(self._cmd_q.get(), timeout=seconds)
            await plc.write_switch(coil_name, on, momentary=momentary)
            # 추가로 쌓인 커맨드도 같이 처리
            await self._drain_commands(plc)
        except asyncio.TimeoutError:
            return

    async def _read_hmi_states(self, plc: AsyncPLC) -> Dict[str, bool]:
        """HMI에서 필요한 코일만 읽어서 dict로 반환."""

        # 현재 HMI가 쓰는 코일은 0~12, 32~35에 몰려있음.
        # => 블록으로 읽으면 Modbus 요청 횟수가 확 줄어듭니다.
        block0 = await plc.read_coils_block(0, 13)   # 0~12
        block1 = await plc.read_coils_block(32, 4)   # 32~35

        out: Dict[str, bool] = {}

        # 0~12
        out.update(
            {
                "RP_SW": bool(block0[0]),
                "RV_SW": bool(block0[1]),
                "FV_SW": bool(block0[2]),
                "MV_SW": bool(block0[3]),
                "VV_SW": bool(block0[4]),
                "TMP_SW": bool(block0[5]),
                "SHUTTER_1_SW": bool(block0[6]),
                "SHUTTER_2_SW": bool(block0[7]),
                "MAIN_SHUTTER_SW": bool(block0[8]),
                "POWER_1_SW": bool(block0[9]),
                "POWER_2_SW": bool(block0[10]),
                "FTM_SW": bool(block0[11]),
                "DOOR_SW": bool(block0[12]),
            }
        )

        # 32~35
        out.update(
            {
                "AIR_SW": bool(block1[0]),
                "WATER_SW": bool(block1[1]),
                "GAS_1_SW": bool(block1[2]),
                "GAS_2_SW": bool(block1[3]),
            }
        )
        return out


class HmiPlcBinder(QObject):
    """UI(HMI 페이지) ↔ PLC 연결을 담당."""

    # 버튼(래치 출력)
    BUTTONS: Tuple[ButtonBinding, ...] = (
        ButtonBinding("rpBtn", "RP_SW"),
        ButtonBinding("rvBtn", "RV_SW"),
        ButtonBinding("fvBtn", "FV_SW"),
        ButtonBinding("mvBtn", "MV_SW"),
        ButtonBinding("vvBtn", "VV_SW"),
        ButtonBinding("pushButton_13", "TMP_SW"),
        ButtonBinding("doorBtn", "DOOR_SW"),
        ButtonBinding("ftmBtn", "FTM_SW"),
        ButtonBinding("mainshutterBtn", "MAIN_SHUTTER_SW"),
        ButtonBinding("ms1shutterBtn", "SHUTTER_1_SW"),
        ButtonBinding("ms2shutterBtn", "SHUTTER_2_SW"),
        ButtonBinding("ms1powerBtn", "POWER_1_SW"),
        ButtonBinding("ms2powerBtn", "POWER_2_SW"),
    )

    # indicator(램프) — ui.mainWindow.Ui_Form.set_indicator_state(name,on)을 사용
    INDICATORS: Dict[str, str] = {
        "g1": "GAS_1_SW",
        "g2": "GAS_2_SW",
        "air": "AIR_SW",
        "water": "WATER_SW",
    }

    def __init__(self, ui: object, settings: PLCSettings, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.ui = ui
        self.settings = settings

        self._last_states: Dict[str, bool] = {}

        # 연결 상태(인터락 메시지용)
        self._connected: bool = False

        # Door 이동(열림/닫힘) 중 인터락용 busy window
        self._door_busy_until: float = 0.0
        self._door_busy_timer = QTimer(self)
        self._door_busy_timer.setSingleShot(True)
        self._door_busy_timer.timeout.connect(self._end_door_busy)

        # PLC worker
        self._worker = PlcWorker(settings=settings)
        self._worker.sig_connected.connect(self._on_connected)
        self._worker.sig_error.connect(self._on_error)
        self._worker.sig_states.connect(self._apply_states)

        self._wire_ui()

    def start(self) -> None:
        if not self._worker.isRunning():
            self._worker.start()

    def stop(self) -> None:
        self._worker.stop()
        # 너무 오래 기다리면 UI 종료가 느려지니 2초 정도만
        self._worker.wait(2000)

    # --------------------------------------------------
    # UI wiring
    # --------------------------------------------------
    def _wire_ui(self) -> None:
        # 1) 각 버튼: toggled -> PLC write
        for b in self.BUTTONS:
            w = getattr(self.ui, b.widget_name, None)
            if w is None:
                continue

            # checkable이 아니면 강제로(디자이너에서 이미 checkable로 설정해둔 상태)
            try:
                w.setCheckable(True)
            except Exception:
                pass

            # late-binding 이슈 방지: default arg로 캡처
            w.toggled.connect(lambda on, bb=b: self._on_button_toggled(bb, on))

        # 2) All Stop
        all_stop = getattr(self.ui, "allstopBtn", None)
        if all_stop is not None:
            all_stop.clicked.connect(self._on_all_stop_clicked)

    def _on_button_toggled(self, binding: ButtonBinding, on: bool) -> None:
        """사용자 클릭으로 토글되면 PLC로 래치 출력.

        요구사항(근본 해결: '경고 + PLC 전송 차단')
        1) Door ON/OFF(열기/닫기) 시도 시, Main Shutter가 OFF면:
            - 경고창 표시
            - Door 명령은 PLC에 전송하지 않음
        2) Door가 열리거나 닫히는 중(설정 시간)에는 Main Shutter를 닫을 수 없음:
            - 경고창 표시
            - Main Shutter OFF 명령은 PLC에 전송하지 않음
        """

        # PLC 미연결 상태에서 조작이 들어오면 → 전송 금지 + UI 원복
        if not self._connected:
            self._popup_warn("PLC 미연결", "PLC가 연결되지 않아 명령을 전송할 수 없습니다.")
            self._revert_button_to_plc(binding, fallback=not bool(on))
            return

        # 1) DOOR 특수 처리
        if binding.coil_name == "DOOR_SW":
            # Door 이동 중에는 중복 조작 금지
            if self._is_door_busy():
                self._popup_warn("인터락", "Door가 열리거나 닫히는 중입니다.\n완료 후 다시 시도하세요.")
                self._revert_button_to_plc(binding, fallback=not bool(on))
                return

            # PLC 상태를 아직 못 읽은 경우(초기 폴링 전) → 안전하게 막음
            if "MAIN_SHUTTER_SW" not in self._last_states:
                self._popup_warn("인터락", "PLC 상태를 아직 읽지 못했습니다.\n잠시 후 다시 시도하세요.")
                self._revert_button_to_plc(binding, fallback=not bool(on))
                return

            # Main Shutter 닫힘이면 Door 조작 금지(자동으로 열어주지 않음)
            if not bool(self._last_states.get("MAIN_SHUTTER_SW", False)):
                self._popup_warn("인터락", "Main Shutter가 닫혀 있습니다.\nMain Shutter를 먼저 열어주세요.")
                self._revert_button_to_plc(binding, fallback=not bool(on))
                return

            # 조건 만족 → Door만 PLC에 전송 + busy window 시작
            self._worker.enqueue_write("DOOR_SW", bool(on), momentary=False)
            self._begin_door_busy()
            self._set_hmi_status(f"DOOR_SW <- {int(bool(on))} (moving)")
            return

        # 2) MAIN SHUTTER 특수 처리
        if binding.coil_name == "MAIN_SHUTTER_SW":
            # Door 이동 중에는 Main Shutter OFF 금지
            if self._is_door_busy() and (not bool(on)):
                self._popup_warn("인터락", "Door가 열리거나 닫히는 중에는\nMain Shutter를 닫을 수 없습니다.")
                self._revert_button_to_plc(binding, fallback=True)
                return

        # 3) 일반 버튼: 기존 동작 유지
        self._worker.enqueue_write(binding.coil_name, bool(on), momentary=binding.momentary)
        self._set_hmi_status(f"{binding.coil_name} <- {int(bool(on))}")

    def _on_all_stop_clicked(self) -> None:
        """현재 주소맵 기준: HMI가 다루는 모든 출력 코일을 OFF로."""
        self._set_hmi_status("ALL STOP: set all HMI coils OFF")

        # 안전하게: 버튼/램프/펌프/밸브/파워/셔터 모두 OFF
        for b in self.BUTTONS:
            self._worker.enqueue_write(b.coil_name, False, momentary=False)

    # --------------------------------------------------
    # PLC -> UI update
    # --------------------------------------------------
    def _apply_states(self, states_obj: object) -> None:
        states: Dict[str, bool] = dict(states_obj or {})
        self._last_states = states

        # 1) indicators
        try:
            for name, coil in self.INDICATORS.items():
                if hasattr(self.ui, "set_indicator_state"):
                    self.ui.set_indicator_state(name, bool(states.get(coil, False)))
        except Exception:
            pass

        # 2) 버튼 체크 상태(PLC 상태를 UI에 반영)
        for b in self.BUTTONS:
            w = getattr(self.ui, b.widget_name, None)
            if w is None:
                continue
            target = bool(states.get(b.coil_name, False))

            # programmatic setChecked가 toggled를 발생시키지 않게 SignalBlocker로 막음
            try:
                with QSignalBlocker(w):
                    w.setChecked(target)
            except Exception:
                pass

    def _on_connected(self, ok: bool) -> None:
        self._connected = bool(ok)
        self._set_hmi_status("PLC CONNECTED" if ok else "PLC DISCONNECTED")

        # 연결이 끊기면 버튼 조작 자체를 막는 것도 안전함
        for b in self.BUTTONS:
            w = getattr(self.ui, b.widget_name, None)
            if w is None:
                continue
            try:
                w.setEnabled(bool(ok))
            except Exception:
                pass

    def _on_error(self, msg: str) -> None:
        # 너무 길어지지 않게 마지막 한 줄로
        self._set_hmi_log(msg)

    # --------------------------------------------------
    # UI helper
    # --------------------------------------------------
    def _popup_warn(self, title: str, message: str) -> None:
        """경고창 표시 (요구사항: 경고 후 PLC 전송은 하지 않음)."""
        parent = None
        try:
            btn = getattr(self.ui, "processBtn", None)
            parent = btn.window() if btn is not None else None
        except Exception:
            parent = None
        QMessageBox.warning(parent, title, message)

    def _revert_button_to_plc(self, binding: ButtonBinding, fallback: Optional[bool] = None) -> None:
        """사용자 클릭으로 토글된 버튼을 마지막 PLC 상태로 되돌린다."""
        w = getattr(self.ui, binding.widget_name, None)
        if w is None:
            return

        if binding.coil_name in self._last_states:
            target = bool(self._last_states.get(binding.coil_name, False))
        else:
            target = bool(fallback) if fallback is not None else False

        try:
            with QSignalBlocker(w):
                w.setChecked(target)
        except Exception:
            pass

    def _is_door_busy(self) -> bool:
        return time.monotonic() < float(self._door_busy_until or 0.0)

    def _begin_door_busy(self) -> None:
        """Door 명령 전송 시점부터 door_move_time_s 동안 busy로 간주."""
        move_s = float(getattr(self.settings, "door_move_time_s", 10.0) or 10.0)
        move_s = max(0.1, move_s)

        self._door_busy_until = time.monotonic() + move_s
        try:
            self._door_busy_timer.stop()
            self._door_busy_timer.start(int(move_s * 1000))
        except Exception:
            pass

    def _end_door_busy(self) -> None:
        self._door_busy_until = 0.0
        self._set_hmi_status("DOOR: move done")

    def _set_hmi_status(self, text: str) -> None:
        w = getattr(self.ui, "processMonitor_HMI", None)
        if w is not None:
            try:
                w.setText(text)
            except Exception:
                pass

    def _set_hmi_log(self, text: str) -> None:
        w = getattr(self.ui, "hmiLogWindow", None)
        if w is not None:
            try:
                w.setText(text)
            except Exception:
                pass
