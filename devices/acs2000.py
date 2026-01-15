# devices/acs2000.py
from __future__ import annotations

import time
from typing import Optional, Tuple

from utils.base_serial import BaseSerialDevice, SerialDeviceError


class ACS2000ProtocolError(SerialDeviceError):
    """ACS-2000 RS-232 통신/파싱 오류"""


def _eom_bytes(eom: Optional[str]) -> bytes:
    """
    config: "CR" or "CRLF" (권장: CR)
    """
    if not eom:
        return b"\r"
    e = eom.strip().upper()
    if e == "CR":
        return b"\r"
    if e == "CRLF":
        return b"\r\n"
    raise ValueError(f"Unsupported EOM={eom!r} (use CR or CRLF)")


# 매뉴얼의 List of commands (문서화 목적)
ACS2000_COMMANDS = [
    "BAU", "CON", "CPF", "DGS", "DGT", "ERR", "FDS", "FLT", "FSR", "GAS",
    "LOC", "OFS", "PRD", "PRT", "RMS", "RTY", "SPS", "SP1", "SP2", "TAS",
    "TID", "TPM", "TRS", "UNI", "VER",
]


class ACS2000(BaseSerialDevice):
    """
    Adixen/Alcatel ACS-2000 RS-232 드라이버.

    - STX: '$'
    - Command: <STX><COMMAND>[,PARAM1]...[EOM]
    - Reply  : <STX><DATA><EOM>[LF]
    - EOM: 기본 CR
    """

    def __init__(self, *, eom: str = "CR", **kwargs):
        super().__init__(**kwargs)
        self._eom = _eom_bytes(eom)

    def _read_until_cr(self, timeout_s: float) -> bytes:
        """
        CR까지 읽는다. (CR 뒤에 LF가 올 수 있음)
        """
        with self._lock:
            ser = self._require()
            t0 = time.time()
            buf = bytearray()

            while time.time() - t0 < timeout_s:
                b = ser.read(1)
                if not b:
                    continue
                buf += b
                if b == b"\r":
                    # optional LF discard
                    old_to = ser.timeout
                    ser.timeout = 0.05
                    _ = ser.read(1)
                    ser.timeout = old_to
                    break

            return bytes(buf)

    def _txrx(self, payload_no_eom: str, rx_timeout_s: float = 0.8) -> str:
        """
        payload_no_eom 예: "$VER", "$PRD,1"
        """
        if not payload_no_eom.startswith("$"):
            raise ValueError("ACS2000 payload must start with '$'")

        tx = payload_no_eom.encode("ascii", errors="replace") + self._eom

        with self._lock:
            ser = self._require()
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(tx)
            ser.flush()
            rx = self._read_until_cr(timeout_s=rx_timeout_s)

        if not rx:
            raise ACS2000ProtocolError(f"no response for '{payload_no_eom}'")

        return rx.decode("ascii", errors="replace").strip()

    # ✅ 매뉴얼의 "모든 명령"을 커버하는 범용 RAW
    def raw(self, command: str, *params: str, rx_timeout_s: float = 0.8) -> str:
        cmd = command.strip().upper()
        if not cmd:
            raise ValueError("command empty")

        parts = ["$" + cmd]
        if params:
            parts.extend(str(p) for p in params)

        payload = ",".join(parts)
        reply = self._txrx(payload, rx_timeout_s=rx_timeout_s)

        # reply도 '$'로 시작하므로 data만 반환
        if reply.startswith("$"):
            reply = reply[1:]
        return reply.strip()

    # 기존 스타일 유지(통합 프로그램에서 자주 쓰는 최소 셋)
    def query_version(self) -> str:
        return self.raw("VER")

    def query_pressure(self, channel: int = 1) -> float:
        if channel not in (1, 2):
            raise ValueError("ACS2000 channel must be 1 or 2")

        raw = self._txrx(f"$PRD,{channel}", rx_timeout_s=0.8)
        s = raw.replace("$", "").strip()

        tokens = [t.strip() for t in s.split(",") if t.strip()]
        cand = tokens[-1] if tokens else s

        try:
            return float(cand)
        except ValueError:
            for t in reversed(s.split()):
                try:
                    return float(t)
                except ValueError:
                    continue
            raise ACS2000ProtocolError(f"cannot parse pressure reply: {raw!r}")

    def start_pressure_stream(self, interval_a: int = 1) -> str:
        return self._txrx(f"$CON,{interval_a}", rx_timeout_s=0.8)

    def read_stream_line(self, timeout_s: float = 2.0) -> str:
        rx = self._read_until_cr(timeout_s=timeout_s)
        if not rx:
            raise ACS2000ProtocolError("stream timeout/no data")
        return rx.decode("ascii", errors="replace").strip()

    def stop_stream_safe(self) -> None:
        self.close()

    # 자주 쓰는 추가 명령(필요할 때만 래핑)
    def query_errors(self) -> str:
        return self.raw("ERR")

    def set_continuous(self, interval_a: int = 1) -> str:
        return self.raw("CON", str(interval_a))

    def set_baudrate(self, baud: int) -> str:
        # 주의: 적용 즉시 통신 끊길 수 있음
        return self.raw("BAU", str(baud))

    # alias
    def get_version(self) -> str:
        return self.query_version()

    def get_pressure(self, channel: int = 1) -> float:
        return self.query_pressure(channel)
