# -*- coding: utf-8 -*-
"""
controller/hmi_plc_binder.py

HMI(UI) ↔ PLC(Modbus RTU / RS-232) 연결 바인더

구조(중요)
1) UI 스레드: 버튼 클릭/램프 표시만 처리 (절대 블로킹 금지)
2) PlcWorker(QThread): PLC 폴링(read) + 명령(write)을 전담
3) PlcWorker 내부에서 asyncio loop를 돌려 devices.plc.AsyncPLC를 그대로 사용
   - qasync 같은 추가 의존성 없이, 기존 프로젝트 스타일 유지

동작 흐름
- HmiPlcBinder.start() → PlcWorker.start()
- PlcWorker:
  - (재)연결 시도 → connect + ping 성공하면 CONNECTED emit
  - poll_interval_s 주기로 필요한 코일 블록(0~12, 32~35)을 읽어서 UI 업데이트 emit
  - 버튼이 눌리면 enqueue_write로 들어온 명령을 큐에서 꺼내 PLC에 write

버튼은 "래치(toggle)"로 동작(ON/OFF 유지).

[추가(중요)]
- 공정(ProcessEngine)에서도 이 binder를 PLC 게이트웨이처럼 쓰기 위해
  public API(is_connected/get_states/read_coil/enqueue_write/pulse_coil)를 추가했다.
- 단, "팝업"은 UI 버튼 클릭 시에만 띄운다(공정 중 팝업 금지).
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QSignalBlocker, QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import QMessageBox

from config.plc_config import PLCSettings
from devices.plc import AsyncPLC


# ------------------------------------------------------------
# UI 위젯 <-> PLC 코일 매핑
#  - widget_name: Qt Designer에서 지정한 objectName
#  - coil_name  : devices/plc.py 의 PLC_COIL_MAP 키(=canonical)
# ------------------------------------------------------------

@dataclass(frozen=True)
class ButtonBinding:
    widget_name: str
    coil_name: str
    momentary: bool = False  # 기본: 래치(유지). 펄스가 필요하면 True


class PlcWorker(QThread):
    """PLC 폴링 + 쓰기를 담당하는 워커 스레드."""

    sig_connected = Signal(bool)    # True/False
    sig_error = Signal(str)         # 에러 문자열
    sig_states = Signal(object)     # dict[str,bool] (coil_name -> state)

    def __init__(self, settings: PLCSettings, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._settings = settings

        self._stop_evt = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._cmd_q: Optional[asyncio.Queue] = None

        # start() 이전에 enqueue된 명령은 임시 저장
        self._pending: List[Tuple[str, bool, bool]] = []
        self._pending_lock = threading.Lock()

    # -------------------------------
    # public API (UI thread)
    # -------------------------------
    def stop(self) -> None:
        self._stop_evt.set()
        if self._loop and self._cmd_q:
            # 큐 대기 중이면 깨우기
            def _poke():
                try:
                    self._cmd_q.put_nowait(("__STOP__", False, False))
                except Exception:
                    pass

            try:
                self._loop.call_soon_threadsafe(_poke)
            except Exception:
                pass

    def enqueue_write(self, coil_name: str, on: bool, momentary: bool = False) -> None:
        """PLC에 write 요청(스레드 안전)."""
        if self._loop and self._cmd_q:
            try:
                # (기존 방식 유지) asyncio queue put을 loop에 안전하게 전달
                asyncio.run_coroutine_threadsafe(
                    self._cmd_q.put((coil_name, bool(on), bool(momentary))),
                    self._loop,
                )
                return
            except Exception:
                # 아래 pending에 저장으로 fallback
                pass

        # 워커가 아직 run()에 들어가기 전이면 보관
        with self._pending_lock:
            self._pending.append((coil_name, bool(on), bool(momentary)))

    # -------------------------------
    # QThread entry
    # -------------------------------
    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._cmd_q = asyncio.Queue()

        # start 전에 들어온 명령 flush
        # - 여기(run)는 워커 스레드이므로, call_soon_threadsafe 대신 직접 put_nowait가 더 안전
        with self._pending_lock:
            for item in self._pending:
                try:
                    self._cmd_q.put_nowait(item)
                except Exception:
                    pass
            self._pending.clear()

        try:
            loop.run_until_complete(self._main())
        finally:
            try:
                loop.stop()
                loop.close()
            except Exception:
                pass

    # -------------------------------
    # internal async logic
    # -------------------------------
    async def _main(self) -> None:
        plc = AsyncPLC(
            port=self._settings.port,
            method=self._settings.method,  # ✅ ini 호환(AsyncPLC 내부에서는 무시하지만, 호출부는 그대로 둠)
            baudrate=self._settings.baudrate,
            bytesize=self._settings.bytesize,
            parity=self._settings.parity,
            stopbits=self._settings.stopbits,
            unit=self._settings.unit,
            timeout_s=self._settings.timeout_s,
            pulse_ms=self._settings.pulse_ms,

            # DAC
            dac_full_scale_code=self._settings.dac_full_scale_code,
            dac_offset_code=self._settings.dac_offset_code,
            dac_current_min_ma=self._settings.dac_current_min_ma,
            dac_current_max_ma=self._settings.dac_current_max_ma,
        )

        connected = False

        def emit_connected(v: bool) -> None:
            nonlocal connected
            v = bool(v)
            if connected != v:
                connected = v
                self.sig_connected.emit(v)

        async def connect_loop() -> None:
            """끊겨도 계속 재시도."""
            while not self._stop_evt.is_set():
                try:
                    await plc.connect()
                    await plc.ping()
                    emit_connected(True)
                    return
                except Exception as e:
                    emit_connected(False)
                    self.sig_error.emit(f"PLC connect failed: {e!r}")
                    try:
                        await plc.close()
                    except Exception:
                        pass
                    await asyncio.sleep(max(0.2, float(self._settings.reconnect_interval_s)))

        await connect_loop()

        consecutive_fail = 0
        max_consecutive_fail = 3  # 연속 실패 N회면 재연결

        while not self._stop_evt.is_set():
            try:
                # 1) 큐에 쌓인 명령 먼저 처리(버튼 반응성을 올림)
                await self._drain_commands(plc)

                # 2) 상태 읽기
                states = await self._read_hmi_states(plc)
                self.sig_states.emit(states)

                consecutive_fail = 0

                # 3) poll_interval 동안 "명령이 오면 즉시 처리" (없으면 타임아웃)
                await self._wait_or_handle_one_command(plc, float(self._settings.poll_interval_s))

            except Exception as e:
                consecutive_fail += 1
                self.sig_error.emit(f"PLC polling error: {e!r}")

                if consecutive_fail < max_consecutive_fail:
                    # 일시적 타임아웃/노이즈로 보고 짧게 텀
                    await asyncio.sleep(0.05)
                    continue

                # N회 연속 실패 → 재연결
                emit_connected(False)
                try:
                    await plc.close()
                except Exception:
                    pass
                await connect_loop()
                consecutive_fail = 0

        # 종료
        try:
            await plc.close()
        except Exception:
            pass
        emit_connected(False)

    async def _drain_commands(self, plc: AsyncPLC) -> None:
        if not self._cmd_q:
            return
        while not self._cmd_q.empty():
            coil_name, on, momentary = self._cmd_q.get_nowait()
            if coil_name in ("__STOP__",):
                continue
            await plc.write_switch(coil_name, on, momentary=momentary)

    async def _wait_or_handle_one_command(self, plc: AsyncPLC, seconds: float) -> None:
        """poll interval 동안 대기하되, 명령이 오면 즉시 1개 처리."""
        if not self._cmd_q or seconds <= 0:
            await asyncio.sleep(max(0.0, seconds))
            return
        try:
            coil_name, on, momentary = await asyncio.wait_for(self._cmd_q.get(), timeout=seconds)
            if coil_name not in ("__STOP__",):
                await plc.write_switch(coil_name, on, momentary=momentary)
                await self._drain_commands(plc)
        except asyncio.TimeoutError:
            return

    async def _read_hmi_states(self, plc: AsyncPLC) -> Dict[str, bool]:
        """HMI에서 필요한 코일만 읽어서 dict로 반환."""
        block0 = await plc.read_coils_block(0, 13)  # 0..12
        block1 = await plc.read_coils_block(32, 4)  # 32..35

        return {
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
            "GAS_1_SW": bool(block1[2]),
            "GAS_2_SW": bool(block1[3]),
        }


class HmiPlcBinder(QObject):
    """UI(HMI) ↔ PLC 연결을 담당."""

    # ✅ 여기 코일명이 plc.py의 PLC_COIL_MAP key와 1:1로 맞아야 함
    BUTTONS: Tuple[ButtonBinding, ...] = (
        ButtonBinding("rpBtn", "R_P_SW"),
        ButtonBinding("rvBtn", "R_V_SW"),
        ButtonBinding("fvBtn", "F_V_SW"),
        ButtonBinding("mvBtn", "M_V_SW"),
        ButtonBinding("vvBtn", "V_V_SW"),
        ButtonBinding("pushButton_13", "TMP_SW"),
        ButtonBinding("doorBtn", "DOOR_SW"),
        ButtonBinding("ftmBtn", "FTM_SW"),
        ButtonBinding("mainshutterBtn", "MAIN_SHUTTER_SW"),
        ButtonBinding("ms1shutterBtn", "SHUTTER_1_SW"),
        ButtonBinding("ms2shutterBtn", "SHUTTER_2_SW"),
        ButtonBinding("ms1powerBtn", "POWER_1_SW"),
        ButtonBinding("ms2powerBtn", "POWER_2_SW"),
    )

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

        # thread-safe state (공정/다른 스레드에서 읽을 수 있음)
        self._state_lock = threading.Lock()
        self._last_states: Dict[str, bool] = {}
        self._connected: bool = False

        # Door 이동 중 인터락(시간 기반)
        self._door_busy_until: float = 0.0
        self._door_busy_timer = QTimer(self)
        self._door_busy_timer.setSingleShot(True)
        self._door_busy_timer.timeout.connect(self._end_door_busy)

        # PLC worker
        self._worker: Optional[PlcWorker] = None
        self._reset_worker(settings)

        self._wire_ui()

    # ============================================================
    # Public API (공정에서도 사용 가능)
    # - 팝업은 절대 띄우지 않는다(공정 중 팝업 금지)
    # ============================================================
    def is_connected(self) -> bool:
        with self._state_lock:
            return bool(self._connected)

    def get_states(self) -> Dict[str, bool]:
        with self._state_lock:
            return dict(self._last_states)

    def read_coil(self, coil_name: str, default: bool = False) -> bool:
        with self._state_lock:
            return bool(self._last_states.get(str(coil_name), bool(default)))

    def enqueue_write(self, coil_name: str, on: bool, momentary: bool = False) -> None:
        """
        공정/외부 코드에서 쓰는 write API.
        - 연결이 안 되어 있으면 예외로 처리(공정 엔진이 판단하도록)
        """
        if not self.is_connected():
            raise RuntimeError("PLC not connected")
        if not self._worker:
            raise RuntimeError("PLC worker not initialized")
        self._worker.enqueue_write(str(coil_name), bool(on), momentary=bool(momentary))

    def pulse_coil(self, coil_name: str) -> None:
        self.enqueue_write(str(coil_name), True, momentary=True)

    # ============================================================
    # internal
    # ============================================================
    def _reset_worker(self, settings: PLCSettings) -> None:
        if self._worker is not None:
            try:
                self._worker.stop()
                wait_ms = int(max(3000, (float(settings.timeout_s) + 0.5) * 2000))
                if not self._worker.wait(wait_ms):
                    try:
                        self._worker.terminate()
                        self._worker.wait(1000)
                    except Exception:
                        pass
            except Exception:
                pass

        self._worker = PlcWorker(settings=settings)
        self._worker.sig_connected.connect(self._on_connected)
        self._worker.sig_error.connect(self._on_error)
        self._worker.sig_states.connect(self._apply_states)

    def reload_settings(self, new_settings: PLCSettings) -> None:
        self.settings = new_settings
        self._reset_worker(new_settings)
        self.start()

    def start(self) -> None:
        if self._worker and (not self._worker.isRunning()):
            self._worker.start()

    def stop(self) -> None:
        if not self._worker:
            return
        self._worker.stop()
        # settings.timeout_s 반영해서 종료 대기(기존 기능 유지 + 안정성 개선)
        wait_ms = int(max(2000, (float(self.settings.timeout_s) + 0.5) * 2000))
        self._worker.wait(wait_ms)

    def _wire_ui(self) -> None:
        for b in self.BUTTONS:
            w = getattr(self.ui, b.widget_name, None)
            if w is None:
                continue
            try:
                w.setCheckable(True)
            except Exception:
                pass
            w.toggled.connect(lambda on, bb=b: self._on_button_toggled(bb, on))

        all_stop = getattr(self.ui, "allstopBtn", None)
        if all_stop is not None:
            all_stop.clicked.connect(self._on_all_stop_clicked)

    def _get_state_locked(self, coil: str, default: bool = False) -> bool:
        with self._state_lock:
            return bool(self._last_states.get(coil, default))

    def _on_button_toggled(self, binding: ButtonBinding, on: bool) -> None:
        # ✅ 기존 기능 유지: 미연결이면 팝업 + 버튼 원복
        if not self.is_connected():
            self._popup_warn("PLC 미연결", "PLC가 연결되지 않아 명령을 전송할 수 없습니다.")
            self._revert_button_to_plc(binding, fallback=not bool(on))
            return

        # Door 인터락(기존 유지)
        if binding.coil_name == "DOOR_SW":
            if self._is_door_busy():
                self._popup_warn("인터락", "Door가 열리거나 닫히는 중입니다.\n완료 후 다시 시도하세요.")
                self._revert_button_to_plc(binding, fallback=not bool(on))
                return

            # PLC 상태가 아직 없으면 차단(기존 유지)
            if not self.get_states():
                self._popup_warn("인터락", "PLC 상태를 아직 읽지 못했습니다.\n잠시 후 다시 시도하세요.")
                self._revert_button_to_plc(binding, fallback=not bool(on))
                return

            # Main shutter가 닫혀있으면 door 금지(기존 유지)
            if not self._get_state_locked("MAIN_SHUTTER_SW", False):
                self._popup_warn("인터락", "Main Shutter가 닫혀 있습니다.\nMain Shutter를 먼저 열어주세요.")
                self._revert_button_to_plc(binding, fallback=not bool(on))
                return

            # ✅ write는 worker로 (기존과 동일하게 래치)
            self._worker.enqueue_write("DOOR_SW", bool(on), momentary=False)
            self._begin_door_busy()
            self._set_hmi_status(f"DOOR_SW <- {int(bool(on))} (moving)")
            return

        # Main shutter 인터락(door 이동중 닫기 금지) - 기존 유지
        if binding.coil_name == "MAIN_SHUTTER_SW":
            if self._is_door_busy() and (not bool(on)):
                self._popup_warn("인터락", "Door가 열리거나 닫히는 중에는\nMain Shutter를 닫을 수 없습니다.")
                self._revert_button_to_plc(binding, fallback=True)
                return

        # ✅ 기존 기능 유지: 일반 토글은 바로 write
        self._worker.enqueue_write(binding.coil_name, bool(on), momentary=binding.momentary)
        self._set_hmi_status(f"{binding.coil_name} <- {int(bool(on))}")

    def _on_all_stop_clicked(self) -> None:
        # 기존 동작 유지(연결 체크/팝업 추가하지 않음)
        self._set_hmi_status("ALL STOP: set all HMI coils OFF")
        if not self._worker:
            return
        for b in self.BUTTONS:
            self._worker.enqueue_write(b.coil_name, False, momentary=False)

    def _apply_states(self, states_obj: object) -> None:
        states: Dict[str, bool] = dict(states_obj or {})

        # thread-safe 저장
        with self._state_lock:
            self._last_states = states

        # indicators
        try:
            if hasattr(self.ui, "set_indicator_state"):
                for name, coil in self.INDICATORS.items():
                    self.ui.set_indicator_state(name, bool(states.get(coil, False)))
        except Exception:
            pass

        # buttons (PLC 상태로 UI를 "정답"으로 동기화)
        for b in self.BUTTONS:
            w = getattr(self.ui, b.widget_name, None)
            if w is None:
                continue
            target = bool(states.get(b.coil_name, False))
            try:
                with QSignalBlocker(w):
                    w.setChecked(target)
            except Exception:
                pass

    def _on_connected(self, ok: bool) -> None:
        with self._state_lock:
            prev = self._connected
            self._connected = bool(ok)

        status = "PLC CONNECTED" if self.is_connected() else "PLC DISCONNECTED"
        self._set_hmi_status(status)

        if prev != self.is_connected():
            self._set_hmi_log(status)

    def _on_error(self, msg: str) -> None:
        self._set_hmi_log(msg)

    # --------------------------
    # UI helpers
    # --------------------------
    def _popup_warn(self, title: str, message: str) -> None:
        parent = None
        try:
            btn = getattr(self.ui, "processBtn", None)
            parent = btn.window() if btn is not None else None
        except Exception:
            parent = None

        box = QMessageBox(QMessageBox.Warning, title, message, QMessageBox.Ok, parent)
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        box.exec()

    def _revert_button_to_plc(self, binding: ButtonBinding, fallback: Optional[bool] = None) -> None:
        w = getattr(self.ui, binding.widget_name, None)
        if w is None:
            return

        # PLC에 이미 상태가 있으면 그 상태로, 없으면 fallback로 원복
        with self._state_lock:
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
        if w is None:
            return

        line = f"[{time.strftime('%H:%M:%S')}] {text}"

        try:
            stick_to_bottom = True
            try:
                sb = w.verticalScrollBar()
                stick_to_bottom = (sb.value() >= (sb.maximum() - 2))
            except Exception:
                stick_to_bottom = True

            if hasattr(w, "appendPlainText"):
                w.appendPlainText(line)
            elif hasattr(w, "append"):
                w.append(line)
            else:
                prev = w.text() if hasattr(w, "text") else ""
                joined = (prev + "\n" + line).strip()
                if hasattr(w, "setText"):
                    w.setText(joined)

            if stick_to_bottom:
                try:
                    sb = w.verticalScrollBar()
                    sb.setValue(sb.maximum())
                except Exception:
                    pass
        except Exception:
            pass
