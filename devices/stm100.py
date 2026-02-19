# devices/stm100.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Tuple

from utils.base_serial import BaseSerialDevice, SerialDeviceError


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


def _fmt_compact_float(x: float, *, max_decimals: int = 3) -> str:
    """
    STM-100 DATA(최대 10 bytes) 제한을 고려한 compact float 포맷.
    예) 1.230 -> "1.23", 0.750 -> "0.75"
    """
    s = f"{float(x):.{int(max_decimals)}f}"
    s = s.rstrip("0").rstrip(".")
    if s == "-0":
        s = "0"
    return s

def _ensure_cmd_len(cmd: str) -> None:
    # build_frame() 제약: DATA 1~10 bytes(ASCII)
    b = cmd.encode("ascii", errors="replace")
    if not (1 <= len(b) <= 10):
        raise ValueError(f"STM-100 cmd too long: {cmd!r} (len={len(b)} bytes, must be 1~10)")
    

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
    예)  "T", "S", "@", "E=1.23", "F=1.234", "i5", "j5=0.75", "k5=0.50"
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
        """
        T : deposition rate (Å/s)
        메뉴얼 형식: leading space 또는 '-' + NNN.N 형태가 흔함.
        command()는 body를 strip하므로 float 변환만 안정적으로 처리.
        """
        s = self.command("T")  # body(str), strip됨
        if not s:
            raise STM100ProtocolError("STM-100: empty rate response")
        try:
            return float(s)
        except ValueError as e:
            raise STM100ProtocolError(f"STM-100: invalid rate response: {s!r}") from e

    # ------------------------------------------------------------
    # Film parameter / Zero helpers (필수: density, z-factor, zero)
    # ------------------------------------------------------------
    def zero_thickness(self) -> None:
        """
        C : Zeros thickness
        """
        self.command("C")

    def zero_timer_and_thickness(self) -> None:
        """
        B : Zeros timer and thickness
        """
        self.command("B")

    def set_density(self, density_g_cm3: float) -> None:
        """
        E= : Set current film density
        """
        v = _fmt_compact_float(density_g_cm3, max_decimals=3)
        cmd = f"E={v}"
        _ensure_cmd_len(cmd)
        # token/modifier로도 가능하지만, 길이 체크를 위해 raw를 씀
        self.query_text(cmd)

    def get_density(self) -> float:
        """
        E? : Query current film density
        """
        s = self.command("E", modifier="?")
        return float(s)

    def set_z_factor(self, z: float) -> None:
        """
        F= : Set current film Z-Factor
        """
        v = _fmt_compact_float(z, max_decimals=3)
        cmd = f"F={v}"
        _ensure_cmd_len(cmd)
        self.query_text(cmd)

    def get_z_factor(self) -> float:
        """
        F? : Query current film Z-Factor
        """
        s = self.command("F", modifier="?")
        return float(s)

    # ------------------------------------------------------------
    # Multi-Film (1~9) support (선택: 2개 material 저장/전환용)
    # ------------------------------------------------------------
    def set_current_film(self, film_no: int) -> None:
        """
        iN : Sets the current film to number N (1..9)
        예) i5
        """
        n = int(film_no)
        if not (1 <= n <= 9):
            raise ValueError("film_no must be 1..9")
        cmd = f"i{n}"
        _ensure_cmd_len(cmd)
        self.query_text(cmd)

    def set_film_density(self, film_no: int, density_g_cm3: float) -> None:
        """
        jN=val : Sets density for film N (1..9)
        예) j6=0.75
        """
        n = int(film_no)
        if not (1 <= n <= 9):
            raise ValueError("film_no must be 1..9")
        v = _fmt_compact_float(density_g_cm3, max_decimals=3)
        cmd = f"j{n}={v}"
        _ensure_cmd_len(cmd)
        self.query_text(cmd)

    def get_film_density(self, film_no: int) -> float:
        """
        jN? : Query density for film N (1..9)
        """
        n = int(film_no)
        if not (1 <= n <= 9):
            raise ValueError("film_no must be 1..9")
        cmd = f"j{n}?"
        _ensure_cmd_len(cmd)
        s = self.query_text(cmd)
        return float(s)

    def set_film_z_factor(self, film_no: int, z: float) -> None:
        """
        kN=val : Sets Z-Factor for film N (1..9)
        예) k5=0.5
        """
        n = int(film_no)
        if not (1 <= n <= 9):
            raise ValueError("film_no must be 1..9")
        v = _fmt_compact_float(z, max_decimals=3)
        cmd = f"k{n}={v}"
        _ensure_cmd_len(cmd)
        self.query_text(cmd)

    def get_film_z_factor(self, film_no: int) -> float:
        """
        kN? : Query Z-Factor for film N (1..9)
        """
        n = int(film_no)
        if not (1 <= n <= 9):
            raise ValueError("film_no must be 1..9")
        cmd = f"k{n}?"
        _ensure_cmd_len(cmd)
        s = self.query_text(cmd)
        return float(s)

    def apply_material_params(
        self,
        *,
        density_g_cm3: float,
        z_factor: float,
        film_no: int | None = None,
        do_zero_thickness: bool = False,
    ) -> None:
        """
        공정에서 쓰기 좋은 “한 방” API
        - film_no가 있으면 멀티필름(j/k)로 저장(또는 전환)
        - film_no가 없으면 현재 film(E/F)에 적용
        - 필요 시 thickness zero까지
        """
        if film_no is None:
            self.set_density(density_g_cm3)
            self.set_z_factor(z_factor)
        else:
            self.set_film_density(film_no, density_g_cm3)
            self.set_film_z_factor(film_no, z_factor)
            # 바로 해당 필름을 current로 쓰고 싶으면:
            self.set_current_film(film_no)

        if do_zero_thickness:
            self.zero_thickness()
