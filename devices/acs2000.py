# devices/acs2000.py
from __future__ import annotations

import time
from typing import Optional

from utils.base_serial import BaseSerialDevice, SerialDeviceError


class ACS2000ProtocolError(SerialDeviceError):
    """ACS-2000 RS-232 통신/파싱 오류"""


def _eom_bytes(eom: Optional[str]) -> bytes:
    # ✅ ACS2000 프로토콜의 EOM은 CR. LF는 컨트롤러 설정에 따라 "응답에 추가"될 수 있음.
    if not eom:
        return b"\r"
    e = eom.strip().upper()
    if e in ("CR", "CRLF"):
        return b"\r"
    raise ValueError(f"Unsupported EOM={eom!r} (use CR or CRLF)")


# 매뉴얼의 List of commands (문서화 목적)
ACS2000_COMMANDS = [
    "BAU", "CON", "CPF", "DGS", "DGT", "ERR", "FDS", "FLT", "FSR", "GAS",
    "LOC", "OFS", "PRD", "PRT", "RMS", "RTY", "SPS", "SP1", "SP2", "TAS",
    "TID", "TPM", "TRS", "UNI", "VER",
]


CON_STATUS_MAP = {
    0: "OK",
    1: "Ur",
    2: "Or",
    3: "Err03/Err04",
    5: "NoGauge",
    6: "IdErr",
    7: "ErrHi/ErrLo/Err06/Err07",
}


class ACS2000(BaseSerialDevice):
    """
    Adixen/Alcatel ACS-2000 RS-232 드라이버.

    - STX: '$'
    - Command: <STX><COMMAND>[,PARAM1]...[EOM]
    - Reply  : <STX><DATA><EOM>[LF]   ← 장비 설정에 따라 LF가 "추가"될 수 있음
    - EOM: CR(0x0D)
    """

    def __init__(self, *, eom: str = "CR", **kwargs):
        super().__init__(**kwargs)
        self._eom = _eom_bytes(eom)

        # CR 뒤에 따라붙은 LF(또는 CR-only 응답에서 다음 프레임 첫 바이트)를 안전하게 처리하기 위한 프리패치
        self._rx_prefetch = bytearray()
        self._streaming = False  # ✅ CON 스트리밍 상태

    # -------------------------
    # Low-level RX helpers
    # -------------------------
    def _read1_unlocked(self) -> bytes:
        """caller가 lock을 잡고 있어야 함."""
        if self._rx_prefetch:
            b = bytes(self._rx_prefetch[:1])
            del self._rx_prefetch[:1]
            return b
        ser = self._require()
        return ser.read(1)

    def _consume_optional_lf_unlocked(self) -> None:
        """
        CR 다음에 LF가 있으면 소비(버림).
        LF가 아니면(또는 timeout으로 빈 바이트면) 그대로 유지(프리패치로 되돌림).
        """
        ser = self._require()
        old_to = ser.timeout
        try:
            ser.timeout = 0.02  # 아주 짧게만 확인
            b = ser.read(1)
        finally:
            ser.timeout = old_to

        if not b:
            return
        if b == b"\n":
            return
        # LF가 아니면 다음 프레임의 첫 바이트일 가능성이 있으므로 프리패치로 되돌림
        self._rx_prefetch += b

    def _read_until_cr_unlocked(self, timeout_s: float) -> bytes:
        """
        CR(0x0D)까지 읽는다. (CR 뒤에 LF가 올 수 있음 → 있으면 제거)
        caller가 lock을 잡고 있어야 함.
        """
        ser = self._require()
        old_to = ser.timeout
        try:
            # 1바이트 read가 너무 오래 막히지 않도록 짧게 폴링
            ser.timeout = 0.05
            t0 = time.time()
            buf = bytearray()

            while time.time() - t0 < timeout_s:
                b = self._read1_unlocked()
                if not b:
                    continue
                buf += b
                if b == b"\r":
                    self._consume_optional_lf_unlocked()
                    break

            return bytes(buf)
        finally:
            ser.timeout = old_to

    # -------------------------
    # TX/RX
    # -------------------------
    def _txrx(self, payload_no_eom: str, rx_timeout_s: float = 0.8) -> str:
        """
        payload_no_eom 예: "$VER", "$PRD", "$CON,1"
        """
        if self._streaming:
            raise ACS2000ProtocolError("ACS2000 is in CON streaming mode; stop stream before issuing commands.")

        if not payload_no_eom.startswith("$"):
            raise ValueError("ACS2000 payload must start with '$'")

        tx = payload_no_eom.encode("ascii", errors="replace") + self._eom

        with self._lock:
            ser = self._require()

            # 이전에 남아있던 LF/잔여 바이트 제거
            self._rx_prefetch.clear()

            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(tx)
            ser.flush()

            rx = self._read_until_cr_unlocked(timeout_s=rx_timeout_s)

        if not rx:
            raise ACS2000ProtocolError(f"no response for '{payload_no_eom}'")

        return rx.decode("ascii", errors="replace").strip()

    # ✅ 매뉴얼의 "모든 명령"을 커버하는 범용 RAW
    def raw(self, command: str, *params: str, rx_timeout_s: float = 0.8) -> str:
        cmd = command.strip()
        if not cmd:
            raise ValueError("command empty")

        # "$VER" 같이 이미 $가 들어오면 그대로 사용
        if cmd.startswith("$"):
            payload = cmd
            if params:
                payload = ",".join([payload] + [str(p) for p in params])
        else:
            cmd_u = cmd.upper()
            parts = ["$" + cmd_u]
            if params:
                parts.extend(str(p) for p in params)
            payload = ",".join(parts)

        reply = self._txrx(payload, rx_timeout_s=rx_timeout_s)

        # reply도 '$'로 시작하므로 data만 반환
        if reply.startswith("$"):
            reply = reply[1:]
        return reply.strip()

    # -------------------------
    # Convenience wrappers
    # -------------------------
    def query_version(self) -> str:
        return self.raw("VER")

    def query_pressure(self, channel: int = 1) -> float:
        """
        PRD: 단일 채널 장비는 channel=1이 사실상 기본.
        - channel=1이면 "$PRD"로 보내고
        - channel!=1이면 "$PRD,<channel>"로 보냄
        """
        if channel < 1:
            raise ValueError("ACS2000 channel must be >= 1")

        payload = "$PRD" if channel == 1 else f"$PRD,{channel}"
        raw = self._txrx(payload, rx_timeout_s=0.8)
        s = raw.replace("$", "").strip()

        tokens = [t.strip() for t in s.split(",") if t.strip()]
        cand = tokens[-1] if tokens else s

        try:
            return float(cand)
        except ValueError:
            for t in reversed(s.replace(",", " ").split()):
                try:
                    return float(t)
                except ValueError:
                    continue
            raise ACS2000ProtocolError(f"cannot parse pressure reply: {raw!r}")

    def start_pressure_stream(self, interval_a: int = 1) -> str:
        if interval_a not in (0, 1, 2):
            raise ValueError("interval_a must be 0(100ms), 1(1s), or 2(1min)")
        reply = self._txrx(f"$CON,{interval_a}", rx_timeout_s=0.8)
        self._streaming = True
        return reply

    def read_stream_line(self, timeout_s: float = 2.0) -> str:
        with self._lock:
            rx = self._read_until_cr_unlocked(timeout_s=timeout_s)
        if not rx:
            raise ACS2000ProtocolError("stream timeout/no data")
        return rx.decode("ascii", errors="replace").strip()

    def parse_con_line(self, line: str) -> tuple[int, Optional[float]]:
        """
        CON 스트림 라인 파싱.
        매뉴얼: reply = $b,c,  (b=status, c=pressure)
        """
        s = (line or "").strip()
        if s.startswith("$"):
            s = s[1:]
        # trailing comma가 있을 수 있어서 빈 토큰 제거
        tokens = [t.strip() for t in s.split(",") if t.strip()]

        if len(tokens) < 2:
            raise ACS2000ProtocolError(f"invalid CON line: {line!r}")

        # b = status
        try:
            b = int(tokens[0])
        except ValueError:
            raise ACS2000ProtocolError(f"invalid gauge status in CON line: {line!r}")

        # c = pressure (no gauge면 0.00E+00 등)
        try:
            p = float(tokens[1])
        except ValueError:
            p = None

        return b, p

    def read_stream_pressure(self, timeout_s: float = 2.0) -> tuple[int, Optional[float]]:
        line = self.read_stream_line(timeout_s=timeout_s)
        return self.parse_con_line(line)

    def read_stream_pressure_value(self, timeout_s: float = 2.0) -> float:
        """
        UI 표시용: status가 OK(0)일 때만 pressure를 float로 반환.
        """
        b, p = self.read_stream_pressure(timeout_s=timeout_s)
        if b != 0:
            # Or/Ur/Err/NoGauge 등 → UI는 이전 값 유지하고 로그만 남기는 식으로 처리 권장
            raise ACS2000ProtocolError(f"CON gauge status not OK: {b} ({CON_STATUS_MAP.get(b,'?')})")
        if p is None:
            raise ACS2000ProtocolError("CON pressure parse failed")
        return p

    def stop_stream_safe(self) -> None:
        self._streaming = False
        self.close()

    def query_errors(self) -> str:
        return self.raw("ERR")

    # alias
    def get_version(self) -> str:
        return self.query_version()

    def get_pressure(self, channel: int = 1) -> float:
        return self.query_pressure(channel)
