# devices/turbovac.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from utils.base_serial import BaseSerialDevice, SerialDeviceError


class TurbovacProtocolError(SerialDeviceError):
    """Leybold TURBOVAC USS/USB 프로토콜 오류"""


class TurbovacCommandError(TurbovacProtocolError):
    """파라미터 접근/제어 명령 실패"""


# ---------------------------------------------------------
# USB/USS 기본 사양
# ---------------------------------------------------------
# 메뉴얼:
# - USB = CDC-Data(COM port emulation)
# - 19200 baud fixed
# - address 0 fixed
# - protocol = VDI/VDE 3689 (USS style)
USB_FIXED_ADDRESS = 0
USS_STX = 0x02
USS_LGE = 22           # bytes 3..22 payload + 2 = 22
USS_TOTAL_LEN = 24     # STX..BCC = 24 bytes total

# Parameter access query designator (AK / high nibble of PKE)
AK_NO_ACCESS = 0x0
AK_READ_VALUE = 0x1          # non-indexed 16/32-bit parameter read
AK_WRITE_16 = 0x2
AK_WRITE_32 = 0x3
AK_READ_FIELD = 0x6          # indexed(field) parameter read
AK_WRITE_FIELD_16 = 0x7
AK_WRITE_FIELD_32 = 0x8

# Response designator
RESP_16 = 0x1
RESP_32 = 0x2
RESP_FIELD_16 = 0x4
RESP_FIELD_32 = 0x5
RESP_CANNOT_RUN = 0x7
RESP_NO_WRITE_PERMISSION = 0x8

# USS Control Word bits
CW_START_STOP = 0
CW_X201 = 5
CW_ENABLE_SETPOINT = 6
CW_RESET_ERROR = 7
CW_STANDBY = 8
CW_ENABLE_PROCESS = 10
CW_X202 = 14
CW_X203 = 15

# USS Status Word bits
SW_READY = 0
SW_OPERATION_ENABLED = 2
SW_ERROR = 3
SW_ACCEL = 4
SW_DECEL = 5
SW_SWITCH_ON_LOCK = 6
SW_TEMP_WARNING = 7
SW_PARAM_CHANNEL_ENABLED = 9
SW_NORMAL_OPERATION = 10
SW_PUMP_TURNING = 11
SW_OVERLOAD_WARNING = 13
SW_COLLECTIVE_WARNING = 14
SW_PROCESS_CHANNEL_ENABLED = 15


# ---------------------------------------------------------
# UI/상태용 dataclass
# ---------------------------------------------------------
@dataclass(frozen=True)
class TurbovacSnapshot:
    ts: float
    connected: bool

    # UI 직접 매핑용
    state_text: str
    freq_hz: int
    current_a: float
    motor_temp_c: Optional[int]
    converter_temp_c: Optional[int]
    bearing_temp_c: Optional[int]
    dc_bus_v: Optional[int]

    # 에러/경고
    warning_bits: int
    last_error_code: int
    last_error_freq_hz: Optional[int]
    last_error_hours: Optional[float]
    alarm_text: str

    # raw / flags
    status_word: int
    control_word: int
    ready: bool
    operation_enabled: bool
    pump_turning: bool
    normal_operation: bool
    accelerating: bool
    decelerating: bool
    switch_on_lock: bool
    temp_warning: bool
    overload_warning: bool
    collective_warning: bool

    meta: Dict[str, Any]


# ---------------------------------------------------------
# 설명용 매핑
# ---------------------------------------------------------
_WARNING_BIT_MAP = {
    0: "PumpTemp1High",
    1: "PumpTemp2High",
    2: "PumpTemp3High",
    3: "AmbientTooLow",
    6: "OverspeedWarn",
    7: "PumpTemp4High",
    11: "OverloadWarn",
    12: "PumpTemp5High",
    13: "PumpTemp6High",
    14: "SupplyVoltageWarn",
}

_ERROR_CODE_MAP = {
    0: "OK",
    1: "OverspeedWarning",
    2: "PassThroughTimeError",
    3: "BearingTempTooHigh",
    4: "ShortCircuitError",
    5: "ConverterTempError",
    6: "RunUpTimeError",
    7: "MotorTempError",
    61: "BearingTempWarning",
    83: "MotorUndertempWarning",
    84: "MotorTempWarning",
    85: "ConverterTempWarning",
    86: "PumpTemp6Warning",
    87: "PumpTemp6Failure",
    94: "PumpTemp4Warning",
    95: "PumpTemp4Failure",
    96: "PumpTemp5Warning",
    97: "PumpTemp5Failure",
    101: "OverloadWarning",
    103: "SupplyVoltageWarning",
    106: "OverloadFailure",
    111: "MotorUndertempError",
    116: "PermanentOverloadError",
    117: "MotorCurrentError",
    143: "OverspeedFailure",
    213: "SupplyVoltageErrorOver",
    221: "ChecksumError1",
    225: "BearingRunInActive",
    227: "FrequencyConverterError",
    228: "FrequencyConverterError",
    229: "FrequencyConverterError",
    230: "FrequencyConverterError",
    231: "SupplyVoltageErrorOver",
    232: "SupplyVoltageErrorUnder",
    233: "SupplyVoltageErrorOver",
    234: "SupplyVoltageErrorUnder",
    235: "FrequencyConverterError",
    236: "StartupFailure",
    237: "FrequencyConverterError",
    238: "FrequencyConverterError",
    239: "FrequencyConverterError",
    240: "ChecksumError2",
    241: "SupplyIsNot24V",
    242: "SupplyIsNot48V",
    252: "HardwarePlausibilityError",
    600: "GaugeSecondStageNotStarted",
    601: "GaugeLost",
    602: "NoPowerSupplyAtGauge",
    603: "NoVoltageAtGaugeOutput",
    608: "GaugeFilamentBreak",
    609: "GaugePiraniError",
    610: "ComElectronicsTempWarning",
    611: "ComElectronicsTempFailure",
    612: "IntermediateCircuitVoltageWarning",
    700: "FrequencyConverterError",
    701: "FrequencyConverterError",
    702: "FrequencyConverterError",
    703: "FrequencyConverterError",
    704: "FrequencyConverterError",
    705: "RTCBatteryLow",
    706: "FrequencyConverterError",
    707: "FrequencyConverterError",
}


def _u16(v: int) -> int:
    return v & 0xFFFF


def _u32(v: int) -> int:
    return v & 0xFFFFFFFF


def _i16_from_u16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _i32_from_u32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def _split_u16(v: int) -> tuple[int, int]:
    v &= 0xFFFF
    return (v >> 8) & 0xFF, v & 0xFF


def _split_u32(v: int) -> tuple[int, int, int, int]:
    v &= 0xFFFFFFFF
    return (
        (v >> 24) & 0xFF,
        (v >> 16) & 0xFF,
        (v >> 8) & 0xFF,
        v & 0xFF,
    )


def _join_u16(msb: int, lsb: int) -> int:
    return ((msb & 0xFF) << 8) | (lsb & 0xFF)


def _join_u32(b0: int, b1: int, b2: int, b3: int) -> int:
    return (
        ((b0 & 0xFF) << 24)
        | ((b1 & 0xFF) << 16)
        | ((b2 & 0xFF) << 8)
        | (b3 & 0xFF)
    )


def _bcc_xor(buf: bytes) -> int:
    c = 0
    for b in buf:
        c ^= b
    return c & 0xFF


def _make_pke(query_designator: int, pnu: int) -> int:
    return ((query_designator & 0xF) << 12) | (pnu & 0x07FF)


def _parse_pke(pke: int) -> tuple[int, int]:
    ak = (pke >> 12) & 0xF
    pnu = pke & 0x07FF
    return ak, pnu


def _has_bit(word: int, bit: int) -> bool:
    return bool(word & (1 << bit))


def _status_to_text(zsw: int) -> str:
    if _has_bit(zsw, SW_ERROR):
        return "ERROR"
    if _has_bit(zsw, SW_ACCEL):
        return "ACCEL"
    if _has_bit(zsw, SW_DECEL):
        return "DECEL"
    if _has_bit(zsw, SW_NORMAL_OPERATION):
        return "RUNNING"
    if _has_bit(zsw, SW_PUMP_TURNING):
        return "TURNING"
    if _has_bit(zsw, SW_READY):
        return "READY"
    return "IDLE"


def _warning_text(bits: int) -> str:
    if not bits:
        return "-"
    items: list[str] = []
    for bit, name in _WARNING_BIT_MAP.items():
        if bits & (1 << bit):
            items.append(name)
    return ",".join(items) if items else f"0x{bits:04X}"


def _error_text(code: int) -> str:
    return _ERROR_CODE_MAP.get(code, f"Error{code}")


def _safe_call(default, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return default


class Turbovac(BaseSerialDevice):
    """
    Leybold TURBOVAC i/iX USB(COM port emulation) 드라이버

    핵심:
    - USB = COM port emulation
    - baud = 19200 fixed
    - address = 0 fixed
    - telegram = USS/VDI-VDE 3689 style
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # polling 때도 start bit가 날아가지 않도록 현재 제어 상태 유지
        self._address = USB_FIXED_ADDRESS
        self._control_word: int = 0
        self._setpoint_hz: int = 0

        self._last_snapshot: Optional[TurbovacSnapshot] = None

    # ---------------------------------------------------------
    # Low-level telegram
    # ---------------------------------------------------------
    def _build_frame(
        self,
        *,
        pke: int = 0,
        ind: int = 0,
        pwe: int = 0,
        stw: Optional[int] = None,
        hsw: Optional[int] = None,
    ) -> bytes:
        if stw is None:
            stw = self._control_word
        if hsw is None:
            hsw = self._setpoint_hz

        payload = bytearray()
        payload.append(USS_STX)
        payload.append(USS_LGE)
        payload.append(self._address)

        pke_hi, pke_lo = _split_u16(pke)
        payload.append(pke_hi)
        payload.append(pke_lo)

        payload.append(0x00)  # reserved
        payload.append(ind & 0xFF)

        payload.extend(_split_u32(pwe))

        stw_hi, stw_lo = _split_u16(stw)
        payload.append(stw_hi)
        payload.append(stw_lo)

        hsw_hi, hsw_lo = _split_u16(hsw)
        payload.append(hsw_hi)
        payload.append(hsw_lo)

        payload.extend(b"\x00" * 8)

        if len(payload) != 23:
            raise AssertionError(f"invalid frame body length: {len(payload)}")

        bcc = _bcc_xor(bytes(payload))
        payload.append(bcc)

        if len(payload) != USS_TOTAL_LEN:
            raise AssertionError(f"invalid total frame length: {len(payload)}")

        return bytes(payload)

    def _read_frame(self, timeout_s: float = 0.8) -> bytes:
        rx = self._read_exact(USS_TOTAL_LEN, timeout_s=timeout_s)
        if len(rx) != USS_TOTAL_LEN:
            raise TurbovacProtocolError(
                f"incomplete response: expected {USS_TOTAL_LEN} bytes, got {len(rx)}"
            )

        if rx[0] != USS_STX:
            raise TurbovacProtocolError(f"invalid STX: 0x{rx[0]:02X}")

        if rx[1] != USS_LGE:
            raise TurbovacProtocolError(f"invalid LGE: {rx[1]}")

        if rx[2] != self._address:
            raise TurbovacProtocolError(f"invalid address: {rx[2]}")

        calc = _bcc_xor(rx[:-1])
        if calc != rx[-1]:
            raise TurbovacProtocolError(
                f"BCC mismatch: calc=0x{calc:02X}, rx=0x{rx[-1]:02X}"
            )

        return rx

    def _txrx(
        self,
        *,
        pke: int = 0,
        ind: int = 0,
        pwe: int = 0,
        stw: Optional[int] = None,
        hsw: Optional[int] = None,
        timeout_s: float = 0.8,
    ) -> Dict[str, Any]:
        if not self.is_connected:
            self.connect()

        frame = self._build_frame(pke=pke, ind=ind, pwe=pwe, stw=stw, hsw=hsw)

        with self._lock:
            ser = self._require()
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(frame)
            ser.flush()
            rx = self._read_frame(timeout_s=timeout_s)

        pke_rx = _join_u16(rx[3], rx[4])
        ak_resp, pnu_resp = _parse_pke(pke_rx)

        pwe_rx = _join_u32(rx[7], rx[8], rx[9], rx[10])
        zsw = _join_u16(rx[11], rx[12])
        hiw = _join_u16(rx[13], rx[14])
        p11 = _join_u16(rx[15], rx[16])      # converter temperature
        p5 = _join_u16(rx[17], rx[18])       # motor current (0.1 A)
        p125 = _join_u16(rx[19], rx[20])     # bearing temperature
        p4 = _join_u16(rx[21], rx[22])       # DC bus voltage

        if ak_resp == RESP_CANNOT_RUN:
            raise TurbovacCommandError(
                f"command cannot run: pnu={pnu_resp}, error={pwe_rx}"
            )
        if ak_resp == RESP_NO_WRITE_PERMISSION:
            raise TurbovacCommandError(
                f"write permission denied: pnu={pnu_resp}, error={pwe_rx}"
            )

        return {
            "raw": rx,
            "pke": pke_rx,
            "ak_resp": ak_resp,
            "pnu_resp": pnu_resp,
            "ind": rx[6],
            "pwe_u32": pwe_rx,
            "zsw": zsw,
            "hiw_hz": hiw,
            "p11_converter_temp_c": _i16_from_u16(p11),
            "p5_motor_current_a": _u16(p5) / 10.0,
            "p125_bearing_temp_c": _i16_from_u16(p125),
            "p4_dc_bus_v": _u16(p4),
        }

    # ---------------------------------------------------------
    # connect / control state
    # ---------------------------------------------------------
    def connect(self) -> None:
        super().connect()
        try:
            self.read_fast_status()
        except Exception as e:
            self.close()
            raise TurbovacProtocolError(f"Turbovac USB connect/ping failed: {e}") from e

    def relinquish_control(self) -> None:
        """
        완전히 제어권을 내려놓을 때 사용.
        stop 이후에도 polling은 가능하지만, 정말 손을 떼고 싶을 때만 호출.
        """
        self._control_word = 0
        self._setpoint_hz = 0
        self._txrx(stw=0, hsw=0)

    def start_pump(self, *, setpoint_hz: Optional[int] = None) -> None:
        """
        기본 start:
            bit10 + bit0
        setpoint active start:
            bit10 + bit6 + bit0 + HSW
        """
        cw = (1 << CW_ENABLE_PROCESS) | (1 << CW_START_STOP)

        hsw = 0
        if setpoint_hz is not None:
            if int(setpoint_hz) < 0:
                raise ValueError("setpoint_hz must be >= 0")
            hsw = int(setpoint_hz)
            cw |= (1 << CW_ENABLE_SETPOINT)

        self._control_word = cw
        self._setpoint_hz = hsw
        self._txrx(stw=self._control_word, hsw=self._setpoint_hz)

    def stop_pump(self) -> None:
        """
        stop은 start bit(bit0)와 setpoint bit(bit6)만 내린다.
        bit10은 유지해서 통신/상태 polling은 계속 가능하게 둔다.
        """
        self._control_word &= ~(1 << CW_START_STOP)
        self._control_word &= ~(1 << CW_ENABLE_SETPOINT)
        self._setpoint_hz = 0
        self._txrx(stw=self._control_word, hsw=self._setpoint_hz)

    def reset_error(self) -> None:
        """
        메뉴얼상 reset은 bit7.
        단, reset은 bit0(start)가 살아있으면 불가하므로
        잠깐 start를 내리고 reset 후 기존 제어 상태를 복원한다.
        """
        prev_cw = self._control_word
        prev_hsw = self._setpoint_hz

        cw_reset = (prev_cw | (1 << CW_ENABLE_PROCESS) | (1 << CW_RESET_ERROR))
        cw_reset &= ~(1 << CW_START_STOP)
        self._txrx(stw=cw_reset, hsw=0)

        time.sleep(0.05)

        self._txrx(stw=prev_cw, hsw=prev_hsw)

    # ---------------------------------------------------------
    # parameter access
    # ---------------------------------------------------------
    def read_parameter_u16(self, pnu: int, index: Optional[int] = None) -> int:
        indexed = index is not None
        pke = _make_pke(AK_READ_FIELD if indexed else AK_READ_VALUE, int(pnu))
        resp = self._txrx(pke=pke, ind=int(index or 0), pwe=0)
        ak = resp["ak_resp"]
        pwe = resp["pwe_u32"]

        if indexed:
            if ak != RESP_FIELD_16:
                raise TurbovacProtocolError(
                    f"unexpected response AK={ak} for field u16 read pnu={pnu}, idx={index}"
                )
        else:
            if ak != RESP_16:
                raise TurbovacProtocolError(
                    f"unexpected response AK={ak} for u16 read pnu={pnu}"
                )

        return pwe & 0xFFFF

    def read_parameter_i16(self, pnu: int, index: Optional[int] = None) -> int:
        return _i16_from_u16(self.read_parameter_u16(pnu, index=index))

    def read_parameter_u32(self, pnu: int, index: Optional[int] = None) -> int:
        indexed = index is not None
        pke = _make_pke(AK_READ_FIELD if indexed else AK_READ_VALUE, int(pnu))
        resp = self._txrx(pke=pke, ind=int(index or 0), pwe=0)
        ak = resp["ak_resp"]
        pwe = resp["pwe_u32"]

        if indexed:
            if ak != RESP_FIELD_32:
                raise TurbovacProtocolError(
                    f"unexpected response AK={ak} for field u32 read pnu={pnu}, idx={index}"
                )
        else:
            if ak != RESP_32:
                raise TurbovacProtocolError(
                    f"unexpected response AK={ak} for u32 read pnu={pnu}"
                )

        return _u32(pwe)

    def read_parameter_i32(self, pnu: int, index: Optional[int] = None) -> int:
        return _i32_from_u32(self.read_parameter_u32(pnu, index=index))

    def read_parameter_ascii(self, pnu: int, length: int) -> str:
        chars: list[str] = []
        for idx in range(length):
            v = self.read_parameter_u16(pnu, index=idx)
            if v == 0:
                break
            chars.append(chr(v & 0xFF))
        return "".join(chars).strip()

    # ---------------------------------------------------------
    # fast status / snapshot
    # ---------------------------------------------------------
    def read_fast_status(self) -> Dict[str, Any]:
        """
        파라미터 채널 없이 빠르게 읽는 현재 상태.
        PZD2~PZD6를 그대로 활용한다.
        """
        resp = self._txrx(pke=0, ind=0, pwe=0)
        zsw = int(resp["zsw"])

        return {
            "status_word": zsw,
            "state_text": _status_to_text(zsw),
            "freq_hz": int(resp["hiw_hz"]),
            "converter_temp_c": int(resp["p11_converter_temp_c"]),
            "current_a": float(resp["p5_motor_current_a"]),
            "bearing_temp_c": int(resp["p125_bearing_temp_c"]),
            "dc_bus_v": int(resp["p4_dc_bus_v"]),
            "ready": _has_bit(zsw, SW_READY),
            "operation_enabled": _has_bit(zsw, SW_OPERATION_ENABLED),
            "pump_turning": _has_bit(zsw, SW_PUMP_TURNING),
            "normal_operation": _has_bit(zsw, SW_NORMAL_OPERATION),
            "accelerating": _has_bit(zsw, SW_ACCEL),
            "decelerating": _has_bit(zsw, SW_DECEL),
            "switch_on_lock": _has_bit(zsw, SW_SWITCH_ON_LOCK),
            "temp_warning": _has_bit(zsw, SW_TEMP_WARNING),
            "overload_warning": _has_bit(zsw, SW_OVERLOAD_WARNING),
            "collective_warning": _has_bit(zsw, SW_COLLECTIVE_WARNING),
            "param_channel_enabled": _has_bit(zsw, SW_PARAM_CHANNEL_ENABLED),
            "process_channel_enabled": _has_bit(zsw, SW_PROCESS_CHANNEL_ENABLED),
        }

    def read_snapshot(self, *, include_extended: bool = True) -> TurbovacSnapshot:
        """
        UI/로그용 종합 상태 읽기

        - fast status는 최대한 항상 유지
        - extended 값(P7/P171/P174/P176/P227)은 실패해도 전체 snapshot을 죽이지 않음
        """
        ts = time.time()
        fast = self.read_fast_status()
        prev = self._last_snapshot

        if include_extended:
            motor_temp_c = _safe_call(None, self.read_parameter_i16, 7)

            last_error_code = _safe_call(None, self.read_parameter_u16, 171, index=0)
            if last_error_code is None:
                last_error_code = _safe_call(0, self.read_parameter_u16, 171)

            last_error_freq_hz = _safe_call(None, self.read_parameter_u16, 174, index=0)
            if last_error_freq_hz is None:
                last_error_freq_hz = _safe_call(None, self.read_parameter_u16, 174)

            last_error_hours_raw = _safe_call(None, self.read_parameter_i32, 176, index=0)
            if last_error_hours_raw is None:
                last_error_hours_raw = _safe_call(None, self.read_parameter_i32, 176)

            warning_bits = _safe_call(None, self.read_parameter_u16, 227)
            if warning_bits is None:
                warning_bits = _safe_call(0, self.read_parameter_u16, 227, index=0)
        else:
            motor_temp_c = prev.motor_temp_c if prev else None
            last_error_code = prev.last_error_code if prev else 0
            last_error_freq_hz = prev.last_error_freq_hz if prev else None
            last_error_hours_raw = (
                int(prev.last_error_hours * 100)
                if (prev and prev.last_error_hours is not None)
                else None
            )
            warning_bits = prev.warning_bits if prev else 0

        if motor_temp_c is None and prev is not None:
            motor_temp_c = prev.motor_temp_c

        if last_error_code is None:
            last_error_code = prev.last_error_code if prev else 0

        if last_error_freq_hz is None and prev is not None:
            last_error_freq_hz = prev.last_error_freq_hz

        if warning_bits is None:
            warning_bits = prev.warning_bits if prev else 0

        last_error_hours = (
            float(last_error_hours_raw) / 100.0
            if last_error_hours_raw is not None
            else (prev.last_error_hours if prev else None)
        )

        last_error_code = int(last_error_code or 0)
        warning_bits = int(warning_bits or 0)

        if last_error_code:
            alarm_text = f"E{last_error_code}:{_error_text(last_error_code)}"
        elif warning_bits:
            alarm_text = _warning_text(warning_bits)
        elif fast["collective_warning"] or fast["temp_warning"] or fast["overload_warning"]:
            alarm_text = "Warning"
        else:
            alarm_text = "-"

        snap = TurbovacSnapshot(
            ts=ts,
            connected=True,

            state_text=str(fast["state_text"]),
            freq_hz=int(fast["freq_hz"]),
            current_a=float(fast["current_a"]),
            motor_temp_c=int(motor_temp_c) if motor_temp_c is not None else None,
            converter_temp_c=int(fast["converter_temp_c"]),
            bearing_temp_c=int(fast["bearing_temp_c"]),
            dc_bus_v=int(fast["dc_bus_v"]),

            warning_bits=int(warning_bits),
            last_error_code=int(last_error_code),
            last_error_freq_hz=int(last_error_freq_hz) if last_error_freq_hz is not None else None,
            last_error_hours=float(last_error_hours) if last_error_hours is not None else None,
            alarm_text=alarm_text,

            status_word=int(fast["status_word"]),
            control_word=int(self._control_word),
            ready=bool(fast["ready"]),
            operation_enabled=bool(fast["operation_enabled"]),
            pump_turning=bool(fast["pump_turning"]),
            normal_operation=bool(fast["normal_operation"]),
            accelerating=bool(fast["accelerating"]),
            decelerating=bool(fast["decelerating"]),
            switch_on_lock=bool(fast["switch_on_lock"]),
            temp_warning=bool(fast["temp_warning"]),
            overload_warning=bool(fast["overload_warning"]),
            collective_warning=bool(fast["collective_warning"]),

            meta={
                "param_channel_enabled": bool(fast["param_channel_enabled"]),
                "process_channel_enabled": bool(fast["process_channel_enabled"]),
                "warning_text": _warning_text(int(warning_bits or 0)),
                "error_text": _error_text(int(last_error_code or 0)),
                "extended_included": bool(include_extended),
            },
        )
        self._last_snapshot = snap
        return snap

    def get_last_snapshot(self) -> Optional[TurbovacSnapshot]:
        return self._last_snapshot

    # ---------------------------------------------------------
    # convenience / identity
    # ---------------------------------------------------------
    def read_identity(self) -> Dict[str, str]:
        return {
            "product_name": self.read_parameter_ascii(313, 18),
            "catalog_no": self.read_parameter_ascii(350, 18),
            "serial_no": self.read_parameter_ascii(355, 18),
        }

    def snapshot_to_ui_dict(self, snap: Optional[TurbovacSnapshot] = None) -> Dict[str, str]:
        s = snap or self._last_snapshot
        if s is None:
            return {
                "conn": "Disconnected",
                "state": "-",
                "freq": "- Hz",
                "current": "- A",
                "temp": "- °C",
                "alarm": "-",
            }

        return {
            "conn": "Connected" if s.connected else "Disconnected",
            "state": s.state_text,
            "freq": f"{s.freq_hz} Hz",
            "current": f"{s.current_a:.1f} A",
            "temp": f"{s.motor_temp_c} °C" if s.motor_temp_c is not None else "- °C",
            "alarm": s.alarm_text or "-",
        }