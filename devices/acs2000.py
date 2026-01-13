# devices/acs2000.py
from __future__ import annotations

import time
from typing import Optional, Tuple

from .base_serial import BaseSerialDevice, SerialDeviceError


class ACS2000ProtocolError(SerialDeviceError):
    pass


def _eom_bytes(eom: Optional[str]) -> bytes:
    # config: "CR" or "CRLF"
    if not eom or eom.upper() == "CR":
        return b"\r"
    if eom.upper() == "CRLF":
        return b"\r\n"
    raise ValueError(f"invalid eom: {eom} (use CR or CRLF)")


class ACS2000(BaseSerialDevice):
    """
    ACS2000 ASCII 프로토콜:
      TX: b"$VER\\r" 또는 b"$PRD,1\\r"
      RX: b"$...\\r" (+ optional LF)
    """

    def __init__(self, *, eom: str = "CR", **kwargs):
        super().__init__(**kwargs)
        self._eom = _eom_bytes(eom)

    def _read_until_cr(self, timeout_s: float) -> bytes:
        """
        매뉴얼/테스트 코드 방식: CR까지 읽고, 직후 LF가 있으면 버린다:contentReference[oaicite:11]{index=11}.
        """
        import serial  # local import for type/attr

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
                    nxt = ser.read(1)
                    if nxt == b"\n":
                        pass
                    else:
                        if nxt:
                            buf += nxt
                    ser.timeout = old_to
                    break

            return bytes(buf)

    def _txrx(self, payload_no_eom: str, rx_timeout_s: float = 0.8) -> str:
        payload_no_eom = payload_no_eom.strip()
        if not payload_no_eom.startswith("$"):
            payload_no_eom = "$" + payload_no_eom

        tx = payload_no_eom.encode("ascii", errors="replace") + self._eom

        # reset_input=True로 “이전 찌꺼기” 제거
        self._write(tx, flush=True, reset_input=True)
        rx = self._read_until_cr(timeout_s=rx_timeout_s)

        if not rx:
            raise ACS2000ProtocolError(f"no response for '{payload_no_eom}'")

        s = rx.decode("ascii", errors="replace").strip()
        return s

    @staticmethod
    def _split_status_data(reply: str) -> Tuple[str, str]:
        """
        응답 해석은 장비 설정/펌웨어에 따라 달라질 수 있어, 최대한 보수적으로 파싱:
        - ERR_XXXXX가 포함되면 에러로 간주:contentReference[oaicite:12]{index=12}
        """
        if "ERR_" in reply:
            return "ERR", reply
        if reply.startswith("OK"):
            return "OK", reply
        # 보통 '$'로 시작하는 ASCII 데이터
        return "DATA", reply

    def query_version(self) -> str:
        return self._txrx("$VER", rx_timeout_s=0.8)

    def query_pressure(self, channel: int = 1) -> float:
        """
        매뉴얼 command list에 PRD(채널 압력 값 조회) 존재:contentReference[oaicite:13]{index=13}.
        테스트 코드 스타일에 맞춰 '$PRD,<ch>' 형태로 보냄(사용자가 이 형태로 통신 테스트를 통과했기 때문).
        """
        if channel not in (1, 2):
            raise ValueError("ACS2000 channel must be 1 or 2")

        reply = self._txrx(f"$PRD,{channel}", rx_timeout_s=0.8)
        status, raw = self._split_status_data(reply)

        if status == "ERR":
            raise ACS2000ProtocolError(raw)

        # 숫자만 오는 경우 / '$PRD,...' 형태 / 'OK,...,<value>' 형태 등 변형 대비:
        tokens = raw.replace("$", "").replace("OK", "").split(",")
        tokens = [t.strip() for t in tokens if t.strip()]

        # 뒤에서부터 float 변환 가능한 값을 찾음
        for t in reversed(tokens):
            try:
                return float(t)
            except ValueError:
                continue

        # 마지막 fallback: 공백 split
        for t in reversed(raw.replace("$", "").split()):
            try:
                return float(t)
            except ValueError:
                continue

        raise ACS2000ProtocolError(f"cannot parse pressure reply: {raw}")

    def start_pressure_stream(self, interval_a: int = 1) -> str:
        """
        CON: 연속 수신 요청. 테스트 코드에서도 '$CON,<interval>'로 실행:contentReference[oaicite:14]{index=14}.
        interval_a 의미는 장비 설정에 따름(테스트 코드에서 사용자 입력으로 받는 구조).
        """
        return self._txrx(f"$CON,{interval_a}", rx_timeout_s=0.8)

    def read_stream_line(self, timeout_s: float = 2.0) -> str:
        """
        CON 이후 장비가 계속 송신하는 라인 1개를 읽음.
        """
        rx = self._read_until_cr(timeout_s=timeout_s)
        if not rx:
            raise ACS2000ProtocolError("stream timeout/no data")
        return rx.decode("ascii", errors="replace").strip()

    def stop_stream_safe(self) -> None:
        """
        매뉴얼/테스트 코드 코멘트: CON은 장비가 계속 송신할 수 있고, 장비에서 버튼으로 중지하는 방법도 고려:contentReference[oaicite:15]{index=15}.
        “확실한” stop 명령이 환경마다 다를 수 있어, 안전한 stop은 포트를 닫아 스트림을 끊는 것.
        """
        self.close()
