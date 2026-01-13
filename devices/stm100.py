# devices/stm100.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Tuple

from .base_serial import BaseSerialDevice, SerialDeviceError


class STM100ProtocolError(SerialDeviceError):
    """프레임/체크섬/응답코드 등 STM-100 프로토콜 레벨 오류"""


class STM100CommandError(SerialDeviceError):
    """STM-100이 '정상(A/B)'이 아닌 응답코드를 반환했을 때"""


STX = 0x02  # Start of Text

_OK_CODES = {"A", "B"}
_CODE_MEANING = {
    "A": "OK (No reset)",
    "B": "OK (Power lost flag set)",
    "F": "Illegal Command (No reset)",
    "G": "Illegal Command (Power lost flag set)",
    "H": "Illegal Data Value (No reset)",
    "I": "Illegal Data Value (Power lost flag set)",
    "J": "Illegal Command Modifier (No reset)",
    "K": "Illegal Command Modifier (Power lost flag set)",
}


def checksum_data_only(data: bytes) -> int:
    # Sycon STM-100: sum(DATA) & 0xFF
    return sum(data) & 0xFF


def build_frame(data_ascii: str) -> bytes:
    """
    STX(1) + LEN(1) + DATA(LEN) + CHK(1)
    DATA는 ASCII 1~10 bytes
    """
    data = data_ascii.encode("ascii", errors="replace")
    if not (1 <= len(data) <= 10):
        raise ValueError("STM-100 DATA는 1~10 bytes(ASCII) 이어야 합니다.")
    ln = len(data)
    chk = checksum_data_only(data)
    return bytes([STX, ln]) + data + bytes([chk])


def read_frame(ser, timeout_s: float) -> Tuple[str, bool]:
    """
    STX 찾기 -> LEN -> DATA -> CHK
    """
    t0 = time.time()

    # STX 찾기
    while time.time() - t0 < timeout_s:
        b = ser.read(1)
        if not b:
            continue
        if b[0] == STX:
            break
    else:
        raise TimeoutError("STM-100: STX timeout")

    # LEN
    ln_b = ser.read(1)
    if not ln_b:
        raise TimeoutError("STM-100: LEN timeout")
    ln = ln_b[0]
    if not (1 <= ln <= 10):
        raise STM100ProtocolError(f"STM-100: invalid LEN={ln}")

    # DATA
    data_b = ser.read(ln)
    if len(data_b) != ln:
        raise TimeoutError("STM-100: DATA timeout")

    # CHK
    chk_b = ser.read(1)
    if not chk_b:
        raise TimeoutError("STM-100: CHK timeout")
    chk = chk_b[0]

    ok = (checksum_data_only(data_b) == chk)
    payload = data_b.decode("ascii", errors="replace")
    return payload, ok


@dataclass
class STMReply:
    code: str
    body: str
    raw: str


class STM100(BaseSerialDevice):
    """
    STM-100 / MF (Sycon) RS-232 Sycon Protocol 드라이버.

    - 명령 문자열: "토큰 1글자" + "modifier(선택)" + "data(선택)"
      예)  "T", "A?", "A!", "E=1.23"
    """

    def exchange(self, cmd: str, timeout_s: float = 1.0) -> STMReply:
        cmd = cmd.strip()
        if not cmd:
            raise ValueError("cmd empty")

        tx = build_frame(cmd)

        with self._lock:
            ser = self._require()
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(tx)
            ser.flush()

            payload, chk_ok = read_frame(ser, timeout_s=timeout_s)

        if not chk_ok:
            raise STM100ProtocolError(f"STM-100 checksum mismatch. rx={payload!r}")

        code = payload[0] if payload else ""
        body = payload[1:] if len(payload) > 1 else ""
        return STMReply(code=code, body=body, raw=payload)

    # ✅ Table 5.2 전체를 커버하는 범용 API
    def command(
        self,
        token: str,
        modifier: Optional[str] = None,
        data: str = "",
        timeout_s: float = 1.0,
        strip_body: bool = True,
    ) -> str:
        if not token or len(token) != 1:
            raise ValueError("token must be 1 char")
        if modifier is not None and len(modifier) != 1:
            raise ValueError("modifier must be 1 char or None")

        cmd = token + (modifier or "") + (data or "")
        rep = self.exchange(cmd, timeout_s=timeout_s)

        if rep.code not in _OK_CODES:
            meaning = _CODE_MEANING.get(rep.code, f"Unknown code={rep.code!r}")
            raise STM100CommandError(
                f"STM-100 command failed: {meaning} | sent={cmd!r} | body={rep.body!r}"
            )

        return rep.body.strip() if strip_body else rep.body

    # 기존 스타일 유지(호환)
    def query_text(self, cmd: str, timeout_s: float = 1.0) -> str:
        rep = self.exchange(cmd, timeout_s=timeout_s)
        if rep.code not in _OK_CODES:
            meaning = _CODE_MEANING.get(rep.code, f"Unknown code={rep.code!r}")
            raise STM100CommandError(
                f"STM-100 command failed: {meaning} | sent={cmd!r} | body={rep.body!r}"
            )
        return rep.body.strip()

    # 자주 쓰는 래퍼(필요한 것만)
    def get_version(self) -> str:
        return self.command("@")

    def get_thickness_angstrom(self) -> float:
        # S -> "-0001595" 같은 정수 문자열
        s = self.command("S")
        return float(int(s))

    def get_rate_angstrom_per_s(self) -> float:
        return float(self.command("T"))

    def shutter(self, on: bool) -> None:
        self.command("A", modifier="!" if on else "@")

    def shutter_state(self) -> str:
        return self.command("A", modifier="?")

    def acknowledge_reset_flag(self) -> None:
        # L
        self.command("L")

    # alias (이전 테스트/코드 호환)
    def get_firmware_version(self) -> str:
        return self.get_version()

    def get_thickness(self) -> float:
        return self.get_thickness_angstrom()

    def get_rate(self) -> float:
        return self.get_rate_angstrom_per_s()
