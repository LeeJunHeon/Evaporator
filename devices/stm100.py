# devices/stm100.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Tuple

from .base_serial import BaseSerialDevice, SerialDeviceError


class STM100ProtocolError(SerialDeviceError):
    pass


STX = 0x02


def checksum_data_only(data: bytes) -> int:
    return sum(data) & 0xFF


def build_frame(data_ascii: str) -> bytes:
    data = data_ascii.encode("ascii", errors="replace")
    if not (1 <= len(data) <= 10):
        raise ValueError("STM-100 DATA는 1~10 bytes(ASCII) 이어야 합니다.")
    ln = len(data)
    chk = checksum_data_only(data)
    return bytes([STX, ln]) + data + bytes([chk])


def read_frame(ser, timeout_s: float) -> Tuple[str, bool]:
    """
    STX(1) + LEN(1) + DATA(LEN) + CHK(1)
    테스트 코드와 동일한 방식으로 STX를 찾고 읽는다:contentReference[oaicite:19]{index=19}.
    """
    t0 = time.time()

    # 1) STX 찾기
    while time.time() - t0 < timeout_s:
        b = ser.read(1)
        if not b:
            continue
        if b[0] == STX:
            break
    else:
        raise TimeoutError("STM-100: STX timeout")

    # 2) LEN
    ln_b = ser.read(1)
    if not ln_b:
        raise TimeoutError("STM-100: LEN timeout")
    ln = ln_b[0]
    if ln <= 0 or ln > 250:
        raise STM100ProtocolError(f"STM-100: invalid LEN={ln}")

    # 3) DATA
    data = ser.read(ln)
    if len(data) != ln:
        raise TimeoutError("STM-100: DATA timeout")

    # 4) CHK
    chk_b = ser.read(1)
    if len(chk_b) != 1:
        raise TimeoutError("STM-100: CHK timeout")

    chk_rx = chk_b[0]
    chk_calc = checksum_data_only(data)
    ok = (chk_rx == chk_calc)

    payload = data.decode("ascii", errors="replace")
    return payload, ok


# 테스트 코드의 응답 코드(첫 글자) 의미를 그대로 포함:contentReference[oaicite:20]{index=20}
RESP_CODE_MEANING = {
    "B": "OK / Power Lost (전원 꺼짐/브라운아웃 이력)",
    "C": "OK",
    "D": "OK / Defaulted params (파라미터 초기화 이력)",
    "E": "OK / EEPROM fail (NV fail 이력)",
    "F": "OK / EEPROM fail + Defaulted",
    "G": "Bad command / Busy / Not allowed",
    "H": "Data out of range",
    "I": "Checksum error (장비가 우리 TX를 거부)",
}

OK_CODES = {"B", "C", "D", "E", "F"}


@dataclass
class STMReply:
    code: str
    body: str
    checksum_ok: bool
    raw: str


class STM100(BaseSerialDevice):
    """
    STM-100 Sycon format.
    장비는 요청을 받았을 때만 응답합니다:contentReference[oaicite:21]{index=21}.
    """

    def exchange(self, cmd: str, timeout_s: float = 1.0) -> STMReply:
        cmd = cmd.strip()
        if not cmd:
            raise ValueError("cmd empty")
        if len(cmd.encode("ascii", errors="replace")) > 10:
            raise ValueError("STM-100 cmd too long (max 10 ASCII bytes)")

        tx = build_frame(cmd)

        with self._lock:
            ser = self._require()
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(tx)
            ser.flush()

            payload, chk_ok = read_frame(ser, timeout_s=timeout_s)

        code = payload[0] if payload else ""
        body = payload[1:] if len(payload) > 1 else ""
        return STMReply(code=code, body=body, checksum_ok=chk_ok, raw=payload)

    def query_text(self, cmd: str, timeout_s: float = 1.0) -> str:
        rep = self.exchange(cmd, timeout_s=timeout_s)

        if not rep.checksum_ok:
            raise STM100ProtocolError(f"STM-100 checksum mismatch: raw='{rep.raw}'")

        if rep.code not in OK_CODES:
            meaning = RESP_CODE_MEANING.get(rep.code, "Unknown")
            raise STM100ProtocolError(f"STM-100 error code={rep.code} ({meaning}) raw='{rep.raw}'")

        return rep.body

    # ---- 통합 프로그램에서 자주 쓰는 “안전 조회” API ----
    def get_version(self) -> str:
        # 테스트 코드 메뉴에도 '@'를 version/model로 사용:contentReference[oaicite:22]{index=22}
        return self.query_text("@", timeout_s=1.0)

    def get_thickness_angstrom(self) -> float:
        # 테스트 코드 메뉴: 'S' Thickness(Å):contentReference[oaicite:23]{index=23}
        s = self.query_text("S", timeout_s=1.0)
        return float(s)

    def get_rate_angstrom_per_s(self) -> float:
        # 테스트 코드 메뉴: 'T' Rate(Å/s):contentReference[oaicite:24]{index=24}
        s = self.query_text("T", timeout_s=1.0)
        return float(s)
