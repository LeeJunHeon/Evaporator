# devices/stm100.py
from __future__ import annotations

import time
import re
from dataclasses import dataclass
from typing import Optional, Tuple, Callable, Set, Any

from utils.base_serial import BaseSerialDevice, SerialDeviceError


class STM100ProtocolError(SerialDeviceError):
    """프레임/체크섬/응답코드 등 STM-100 프로토콜 레벨 오류"""


class STM100ValueUnavailableError(STM100ProtocolError):
    """측정 불가/빈값/-------- 등 '값 자체가 없는' 케이스(통신 단절과 구분)"""


class STM100CommandError(SerialDeviceError):
    """STM-100이 '정상(A/B)'이 아닌 응답코드를 반환했을 때"""


STX = 0x02  # Start of Text

_DENSITY_MIN = 0.500
_DENSITY_MAX = 99.99
_Z_MIN = 0.100
_Z_MAX = 9.999
_Z_FILM_MAX = 99.99

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

_UNAVAILABLE_MARKERS = {"--------", "---------", "N/A", "NA"}
_INT_RE = re.compile(r"^[+-]?\d+$")


def _strip_echo_token(s: str, token: str) -> str:
    """
    STM-100 응답이 'U 5319234', 'V 012.4', 'M !' 처럼 토큰을 포함해 올 수도 있어
    토큰이 앞에 붙어있으면 제거해 값만 남긴다.

    - 예) "U 5319234" -> "5319234"
    - 예) "V012.4"    -> "012.4"
    - 예) "M !"       -> "!"
    """
    ss = (s or "").strip()
    if not ss:
        return ""

    t = (token or "").strip().upper()
    if not t or len(t) != 1:
        return ss

    # 응답 첫 글자가 토큰이면 제거
    if ss[:1].upper() == t:
        rest = ss[1:].strip()
        return rest

    return ss


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
    

def _ensure_range(name: str, v: float, lo: float, hi: float) -> None:
    fv = float(v)
    if not (lo <= fv <= hi):
        raise ValueError(f"{name} out of range: {fv} (allowed {lo}..{hi})")

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

    def __init__(
        self,
        *args,
        # ✅ PLC/ACS와 동일 키(나중에 devices.ini [io_policy]에서 주입)
        io_err_allow: int = 5,
        io_retry_sleep_s: float = 0.05,
        io_reconnect_max: int = 2,
        reconnect_backoff_base_s: float = 0.6,
        reconnect_backoff_factor: float = 1.7,
        reconnect_backoff_max_s: float = 5.0,
        **kwargs,
    ):
        io_trace_cb = kwargs.pop("io_trace_cb", None)
        io_trace_skip_tokens = kwargs.pop("io_trace_skip_tokens", {"S", "T"})

        # ✅ debug: 공정 중 U(주파수)/V(LIFE)도 주기적으로 읽어서 io_trace로 로그에 남길지
        uv_trace_enable = kwargs.pop("uv_trace_enable", True)
        uv_trace_interval_s = kwargs.pop("uv_trace_interval_s", 2.0)

        super().__init__(*args, **kwargs)

        # ✅ pop 다시 하지 말고, 위에서 뽑은 값을 저장
        self._io_trace_cb: Optional[Callable[[dict], None]] = io_trace_cb
        self._io_trace_skip_tokens: Set[str] = set(io_trace_skip_tokens or set())

        # ✅ U/V 디버그 로깅
        self._uv_trace_enable: bool = bool(uv_trace_enable)
        self._uv_trace_interval_s: float = max(0.0, float(uv_trace_interval_s))
        if self._uv_trace_interval_s <= 0:
            self._uv_trace_enable = False
        self._next_uv_trace_ts: float = 0.0

        self._io_err_allow = max(0, int(io_err_allow))
        self._io_retry_sleep_s = max(0.0, float(io_retry_sleep_s))
        self._io_reconnect_max = max(0, int(io_reconnect_max))

        self._reconnect_backoff_base_s = max(0.1, float(reconnect_backoff_base_s))
        self._reconnect_backoff_factor = max(1.0, float(reconnect_backoff_factor))
        self._reconnect_backoff_max_s = max(self._reconnect_backoff_base_s, float(reconnect_backoff_max_s))

    def _emit_io_trace(self, *, tx_cmd: str, rx: str | None, ok: bool, detail: str = "") -> None:
        cb = getattr(self, "_io_trace_cb", None)
        if not cb:
            return

        token = (tx_cmd[:1] if tx_cmd else "").upper()
        if token and token in getattr(self, "_io_trace_skip_tokens", set()):
            return

        try:
            cb({
                "dev": "STM100",
                "ok": bool(ok),
                "tx": tx_cmd,
                "rx": rx if rx is not None else "",
                "detail": detail,
            })
        except Exception:
            pass

    def _maybe_trace_uv(self) -> None:
        """
        ✅ 디버그 목적:
        - 서비스 레이어가 S/T(두께/레이트)만 주기적으로 읽더라도,
        여기서 U(센서 주파수) / V(크리스탈 LIFE)도 주기적으로 읽어서
        ProcessWindow 하단 로그(io_trace 경로)로 흘려보낸다.
        - 실패/빈값은 공정 폴링을 망치지 않도록 삼킴(best-effort)
        """
        if not getattr(self, "_uv_trace_enable", False):
            return
        # io_trace_cb가 없으면 UI 로그로 전달될 길이 없으므로 생략
        if not getattr(self, "_io_trace_cb", None):
            return

        now = time.time()
        next_ts = float(getattr(self, "_next_uv_trace_ts", 0.0) or 0.0)
        if now < next_ts:
            return

        interval = float(getattr(self, "_uv_trace_interval_s", 0.0) or 0.0)
        self._next_uv_trace_ts = now + (interval if interval > 0 else 0.0)

        # ✅ U/V는 값이 공백일 수도 있어 파싱보다 "원문 trace"만 남기는 게 안전
        #    (exchange() 단계에서 TX/RX trace는 이미 emit됨)
        try:
            _ = self.command("U", timeout_s=0.5)  # sensor frequency (Hz)
        except Exception:
            pass
        try:
            _ = self.command("V", timeout_s=0.5)  # crystal life (%)
        except Exception:
            pass

    def _ensure_connected(self) -> None:
        if not self.is_connected:
            self.connect()

    def _reconnect_with_backoff(self, backoff_s: float) -> float:
        try:
            self.close()
        except Exception:
            pass

        time.sleep(backoff_s)

        # 재연결
        self.connect()

        # 다음 backoff
        return min(backoff_s * self._reconnect_backoff_factor, self._reconnect_backoff_max_s)

    def exchange(self, cmd: str, timeout_s: float = 1.0, *, retries: int | None = None) -> STMReply:
        """
        ✅ 정책:
        - 실패 1~5회(기본): 연결 유지 재시도
        - 6회째부터: close → reconnect(backoff) 후 재시도
        - reconnect는 io_reconnect_max 만큼만 허용
        """
        cmd = cmd.strip()
        if not cmd:
            raise ValueError("cmd empty")

        tx = build_frame(cmd)

        # retries 인자를 주면 그 값을 우선(호환). None이면 io_policy 값 사용
        allow = self._io_err_allow if retries is None else max(0, int(retries))

        err_count = 0
        reconnect_left = int(self._io_reconnect_max)
        backoff_s = float(self._reconnect_backoff_base_s)

        last_err: Exception | None = None

        while True:
            try:
                with self._lock:
                    self._ensure_connected()
                    ser = self._require()
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                    ser.write(tx)
                    ser.flush()

                    payload, chk_ok = read_frame(ser, timeout_s=timeout_s)

                # checksum 불일치(노이즈)는 “복구 가능 오류”로 처리
                if not chk_ok:
                    raise STM100ProtocolError(f"STM-100 checksum mismatch. rx={payload!r}")

                code = payload[0] if payload else ""
                body = payload[1:] if len(payload) > 1 else ""

                ok = (code in _OK_CODES)
                meaning = _CODE_MEANING.get(code, f"Unknown code={code!r}")
                detail = f"code={code} ({meaning})"

                self._emit_io_trace(tx_cmd=cmd, rx=payload, ok=ok, detail=detail)

                return STMReply(code=code, body=body, raw=payload)

            except (TimeoutError, STM100ProtocolError, SerialDeviceError) as e:
                last_err = e
                err_count += 1

                # 1~allow회: 연결 유지 재시도
                if err_count <= allow:
                    if self._io_retry_sleep_s > 0:
                        time.sleep(self._io_retry_sleep_s)
                    continue

                # allow 초과 → reconnect(backoff)
                if reconnect_left <= 0:
                    # ✅ 최종 실패 trace 1회
                    self._emit_io_trace(tx_cmd=cmd, rx=None, ok=False, detail=f"final_fail: {last_err!r}")

                    # 마지막은 안전하게 닫아둠
                    try:
                        self.close()
                    except Exception:
                        pass
                    raise TimeoutError(f"STM-100 exchange failed after policy retries. last={last_err!r}") from last_err

                reconnect_left -= 1
                err_count = 0  # reconnect 후 카운트 리셋
                backoff_s = self._reconnect_with_backoff(backoff_s)
                continue

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
        """
        S : thickness value (Å)
        - 정상 예: "-0001595" 같은 정수 문자열
        - 간헐적으로 '' 또는 '--------' 같은 “측정 불가/응답 불완전”이 올 수 있어 방어 처리
        """
        s = self.command("S")
        ss = (s or "").strip()

        if not ss:
            raise STM100ValueUnavailableError("STM-100: empty thickness response")
        if ss in _UNAVAILABLE_MARKERS or set(ss) == {"-"}:
            raise STM100ValueUnavailableError(f"STM-100: thickness unavailable: {s!r}")
        if not _INT_RE.fullmatch(ss):
            raise STM100ProtocolError(f"STM-100: invalid thickness response: {s!r}")

        try:
            return float(int(ss))
        except ValueError as e:
            raise STM100ProtocolError(f"STM-100: invalid thickness response: {s!r}") from e
    
    def get_rate_angstrom_per_s(self) -> float:
        s = self.command("T")
        ss = (s or "").strip()
        if not ss:
            raise STM100ValueUnavailableError("STM-100: empty rate response")
        if ss in _UNAVAILABLE_MARKERS or set(ss) == {"-"}:
            raise STM100ValueUnavailableError(f"STM-100: rate unavailable: {s!r}")

        try:
            v = float(ss)
        except ValueError as e:
            raise STM100ProtocolError(f"STM-100: invalid rate response: {s!r}") from e

        # ✅ 디버그(U/V) 로그: 공정 중 주파수/LIFE 변화를 ProcessWindow 로그에서 확인
        try:
            self._maybe_trace_uv()
        except Exception:
            pass

        return v
            
    def ack_power_failure_flag(self) -> None:
        """
        L : Acknowledge "B" response (power lost flag reset)
        - 응답 코드가 B로 계속 오는 상황을 정리하기 위한 용도
        """
        self.command("L")

    def get_crystal_fail_status(self) -> bool:
        """
        M : Get the Crystal Fail Status

        ⚠️ 주의(호환 유지):
        - 함수명이 fail_status 이지만, 기존 코드 호환을 위해 반환 의미는 그대로 유지:
        True  = crystal GOOD(정상)
        False = crystal FAIL(불량/Fail)
        """
        s = self.command("M")
        ss = _strip_echo_token(s, "M")

        if ss == "@":
            return True   # 정상(GOOD)
        if ss == "!":
            return False  # FAIL

        raise STM100ProtocolError(f"STM-100: invalid crystal status: {s!r}")
    
    def is_crystal_fail(self) -> bool:
        """
        True=FAIL, False=GOOD 형태가 필요할 때 쓰는 별칭(혼동 방지용)
        """
        return not self.get_crystal_fail_status()


    def get_sensor_frequency_hz(self) -> int:
        """
        U : Return sensor freq. (Hz)
        - 매뉴얼 예시: U 5319234 (Hz)
        """
        s = self.command("U")
        ss = _strip_echo_token(s, "U")

        if not ss:
            raise STM100ValueUnavailableError("STM-100: empty frequency response")
        if ss in _UNAVAILABLE_MARKERS or set(ss) == {"-"}:
            raise STM100ValueUnavailableError(f"STM-100: frequency unavailable: {s!r}")

        # 보통 정수 Hz
        if not _INT_RE.fullmatch(ss):
            raise STM100ProtocolError(f"STM-100: invalid frequency response: {s!r}")

        try:
            hz = int(ss)
        except ValueError as e:
            raise STM100ProtocolError(f"STM-100: invalid frequency response: {s!r}") from e

        if hz <= 0:
            raise STM100ProtocolError(f"STM-100: invalid frequency value: {hz} (raw={s!r})")

        return hz


    def get_sensor_frequency_mhz(self) -> float:
        """
        공정 시작 전 sanity check 편의용(MHz)
        """
        return self.get_sensor_frequency_hz() / 1_000_000.0


    def get_crystal_life_percent(self) -> float:
        """
        V : Return crystal life (%)
        - 매뉴얼 예시: V 012.4  => 12.4%
        """
        s = self.command("V")
        ss = _strip_echo_token(s, "V")

        if not ss:
            raise STM100ValueUnavailableError("STM-100: empty crystal life response")
        if ss in _UNAVAILABLE_MARKERS or set(ss) == {"-"}:
            raise STM100ValueUnavailableError(f"STM-100: crystal life unavailable: {s!r}")

        try:
            life = float(ss)
        except ValueError as e:
            raise STM100ProtocolError(f"STM-100: invalid crystal life response: {s!r}") from e

        # 정상 범위 방어
        if not (0.0 <= life <= 100.0):
            raise STM100ProtocolError(f"STM-100: crystal life out of range: {life} (raw={s!r})")

        return life


    def get_crystal_health_snapshot(self) -> dict[str, Any]:
        """
        공정 시작 전 '한 번에' 읽기 편의용 스냅샷.
        (Start 로직에서 이 dict만 받아서 조건판단하면 됨)
        """
        crystal_ok = self.get_crystal_fail_status()  # True=GOOD, False=FAIL
        freq_hz = self.get_sensor_frequency_hz()
        life = self.get_crystal_life_percent()
        return {
            "crystal_ok": crystal_ok,
            "crystal_fail": (not crystal_ok),
            "freq_hz": freq_hz,
            "freq_mhz": freq_hz / 1_000_000.0,
            "life_percent": life,
        }

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
        _ensure_range("density_g_cm3", density_g_cm3, _DENSITY_MIN, _DENSITY_MAX)
        v = _fmt_compact_float(density_g_cm3, max_decimals=3)
        cmd = f"E={v}"
        _ensure_cmd_len(cmd)
        # token/modifier로도 가능하지만, 길이 체크를 위해 raw를 씀
        self.query_text(cmd)

    def get_density(self) -> float:
        s = self.command("E", modifier="?")
        ss = (s or "").strip()

        if not ss:
            raise STM100ValueUnavailableError("STM-100: empty density response")
        if ss in _UNAVAILABLE_MARKERS or set(ss) == {"-"}:
            raise STM100ValueUnavailableError(f"STM-100: density unavailable: {s!r}")

        try:
            return float(ss)
        except ValueError as e:
            raise STM100ProtocolError(f"STM-100: invalid density response: {s!r}") from e

    def set_z_factor(self, z: float) -> None:
        """
        F= : Set current film Z-Factor
        """
        _ensure_range("z_factor", z, _Z_MIN, _Z_MAX)
        v = _fmt_compact_float(z, max_decimals=3)
        cmd = f"F={v}"
        _ensure_cmd_len(cmd)
        self.query_text(cmd)

    def get_z_factor(self) -> float:
        s = self.command("F", modifier="?")
        ss = (s or "").strip()

        if not ss:
            raise STM100ValueUnavailableError("STM-100: empty z-factor response")
        if ss in _UNAVAILABLE_MARKERS or set(ss) == {"-"}:
            raise STM100ValueUnavailableError(f"STM-100: z-factor unavailable: {s!r}")

        try:
            return float(ss)
        except ValueError as e:
            raise STM100ProtocolError(f"STM-100: invalid z-factor response: {s!r}") from e

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
        
        _ensure_range("density_g_cm3", density_g_cm3, _DENSITY_MIN, _DENSITY_MAX)

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
        ss = (s or "").strip()
        if not ss:
            raise STM100ValueUnavailableError("STM-100: empty film density response")
        if ss in _UNAVAILABLE_MARKERS or set(ss) == {"-"}:
            raise STM100ValueUnavailableError(f"STM-100: film density unavailable: {s!r}")
        try:
            return float(ss)
        except ValueError as e:
            raise STM100ProtocolError(f"STM-100: invalid film density response: {s!r}") from e

    def set_film_z_factor(self, film_no: int, z: float) -> None:
        """
        kN=val : Sets Z-Factor for film N (1..9)
        예) k5=0.5
        """
        n = int(film_no)
        if not (1 <= n <= 9):
            raise ValueError("film_no must be 1..9")
        
        _ensure_range("z_factor", z, _Z_MIN, _Z_FILM_MAX)

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
        ss = (s or "").strip()
        if not ss:
            raise STM100ValueUnavailableError("STM-100: empty film z-factor response")
        if ss in _UNAVAILABLE_MARKERS or set(ss) == {"-"}:
            raise STM100ValueUnavailableError(f"STM-100: film z-factor unavailable: {s!r}")
        try:
            return float(ss)
        except ValueError as e:
            raise STM100ProtocolError(f"STM-100: invalid film z-factor response: {s!r}") from e

    def apply_material_params(
        self,
        *,
        density_g_cm3: float,
        z_factor: float,
        film_no: int | None = None,
        do_zero_thickness: bool = False,
        # ✅ 추가: 입력 후 READ-BACK 검증
        verify: bool = True,
        tol_density: float = 0.01,   # density는 장비 표시/반올림 영향 고려(기본 0.01)
        tol_z: float = 0.002,        # z-factor는 보통 0.001 단위 → 약간 여유
        verify_retry: int = 1,       # 불일치 시 재시도 횟수(기본 1회)
        settle_delay_s: float = 0.05 # SET 직후 내부 반영 딜레이(짧게)
    ) -> None:
        """
        공정에서 쓰기 좋은 “한 방” API
        - film_no가 없으면 현재 film(E/F)에 적용 (✅ 네 운영 방식)
        - verify=True면 E?/F? (또는 jN?/kN?)로 read-back 검증
        """

        # ✅ STM-100 전송 포맷과 동일하게 “보내는 값”을 정규화해서 오탐 줄임
        den_sent_str = _fmt_compact_float(float(density_g_cm3), max_decimals=3)
        z_sent_str   = _fmt_compact_float(float(z_factor),     max_decimals=3)
        den_sent = float(den_sent_str)
        z_sent   = float(z_sent_str)

        retries = max(0, int(verify_retry))

        for attempt in range(retries + 1):
            # -----------------------
            # 1) SET
            # -----------------------
            if film_no is None:
                # ✅ 네 공정 흐름은 여기로 들어옴(필름 번호 신경 X)
                self.set_density(den_sent)
                self.set_z_factor(z_sent)
            else:
                # (호환 유지: 외부에서 film_no를 쓰는 경우도 대비)
                self.set_film_density(film_no, den_sent)
                self.set_film_z_factor(film_no, z_sent)
                self.set_current_film(film_no)

            if settle_delay_s and settle_delay_s > 0:
                time.sleep(float(settle_delay_s))

            # -----------------------
            # 2) VERIFY (READ-BACK)
            # -----------------------
            if verify:
                if film_no is None:
                    den_got = float(self.get_density())   # E?
                    z_got   = float(self.get_z_factor())  # F?
                else:
                    den_got = float(self.get_film_density(film_no))  # jN?
                    z_got   = float(self.get_film_z_factor(film_no)) # kN?

                ok_den = abs(den_got - den_sent) <= float(tol_density)
                ok_z   = abs(z_got   - z_sent)   <= float(tol_z)

                if not (ok_den and ok_z):
                    # 불일치 → 재시도 기회가 남아있으면 1회 더 SET→VERIFY
                    if attempt < retries:
                        continue

                    raise STM100CommandError(
                        "STM-100 density/z-factor verify failed | "
                        f"film_no={film_no} | "
                        f"density got={den_got} sent={den_sent} tol={tol_density} | "
                        f"z got={z_got} sent={z_sent} tol={tol_z}"
                    )

            # 성공
            break

        # -----------------------
        # 3) 옵션: thickness zero
        # -----------------------
        if do_zero_thickness:
            self.zero_thickness()
