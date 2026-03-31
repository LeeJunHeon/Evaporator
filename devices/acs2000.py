# devices/acs2000.py
from __future__ import annotations

import time
from typing import Optional, Callable, Any, Dict

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


def _extract_err_code(reply: str) -> Optional[str]:
    s = (reply or "").strip()
    if s.startswith("$"):
        s = s[1:]
    su = s.upper()
    if not su.startswith("ERR"):
        return None
    # 예: "ERR_01000"
    if "_" in s:
        return s.split("_", 1)[1].strip() or "UNKNOWN"
    # 예외적 형식 대비
    if "," in s:
        return s.split(",", 1)[1].strip() or "UNKNOWN"
    return "UNKNOWN"


class ACS2000(BaseSerialDevice):
    """
    Adixen/Alcatel ACS-2000 RS-232 드라이버.

    - STX: '$'
    - Command: <STX><COMMAND>[,PARAM1]...[EOM]
    - Reply  : <STX><DATA><EOM>[LF]   ← 장비 설정에 따라 LF가 "추가"될 수 있음
    - EOM: CR(0x0D)
    """

    def __init__(
        self,
        *,
        eom: str = "CR",
        # ✅ IO 정책(PLC에서 만든 io_policy와 동일 키)
        io_err_allow: int = 5,
        io_retry_sleep_s: float = 0.05,
        io_reconnect_max: int = 2,
        reconnect_backoff_base_s: float = 0.6,
        reconnect_backoff_factor: float = 1.7,
        reconnect_backoff_max_s: float = 5.0,
        **kwargs,
    ):
        # ✅ (추가) 서비스가 주입하는 trace 콜백/옵션은 BaseSerialDevice로 넘기기 전에 pop
        io_trace_cb = kwargs.pop("io_trace_cb", None)

        super().__init__(**kwargs)
        self._eom = _eom_bytes(eom)

        # ✅ (추가) trace 콜백
        self._io_trace_cb: Optional[Callable[[Dict[str, Any]], None]] = io_trace_cb

        # ✅ (추가) “이전 로그값 대비 변화 시만” 찍기 위한 마지막 값
        self._last_logged_pressure: Optional[float] = None
        self._last_logged_con_status: Optional[int] = None

        # ACS-2000은 CR 뒤에 장비 설정에 따라 LF가 붙기도 함: 다음 프레임의 첫 바이트와 구분하기 위해 1바이트를 프리패치로 버퍼링
        self._rx_prefetch = bytearray()
        self._streaming = False  # ✅ CON 스트리밍 상태

        # ✅ 마지막으로 요청한 CON interval 저장(재연결 후 스트림 재시작에 필요)
        self._con_interval_a: Optional[int] = None

        # ✅ IO policy 저장(나중에 serial_config/acs_service에서 devices.ini [io_policy]로 주입)
        self._io_err_allow = max(0, int(io_err_allow))
        self._io_retry_sleep_s = max(0.0, float(io_retry_sleep_s))
        self._io_reconnect_max = max(0, int(io_reconnect_max))

        self._reconnect_backoff_base_s = max(0.1, float(reconnect_backoff_base_s))
        self._reconnect_backoff_factor = max(1.0, float(reconnect_backoff_factor))
        self._reconnect_backoff_max_s = max(self._reconnect_backoff_base_s, float(reconnect_backoff_max_s))

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

    def _emit_io_trace(self, *, ok: bool, token: str, tx: str, rx: str = "", detail: str = "") -> None:
        cb = getattr(self, "_io_trace_cb", None)
        if not cb:
            return
        try:
            cb({
                "dev": "ACS2000",
                "ok": bool(ok),
                "token": str(token),
                "tx": str(tx),
                "rx": str(rx),
                "detail": str(detail),
                "ts": time.time(),
            })
        except Exception:
            pass

    def _emit_pressure_if_changed(self, *, pressure: float, source: str, raw: str, tx: str) -> None:
        """
        ✅ ‘이전 로그에 찍힌 압력값’과 달라졌을 때만 로그(trace) emit
        - 요구사항 그대로: 20% 같은 임계치 없이 “값이 바뀌면”
        """
        try:
            p = float(pressure)
        except Exception:
            return

        last = getattr(self, "_last_logged_pressure", None)
        if last is not None and p == last:
            return

        self._last_logged_pressure = p
        self._emit_io_trace(ok=True, token=f"{source}_PRESSURE", tx=tx, rx=raw, detail=f"pressure={p}")

    # -------------------------
    # TX/RX
    # -------------------------
    def _txrx(self, payload_no_eom: str, rx_timeout_s: float = 0.8, *, allow_streaming: bool = False) -> str:
        """
        payload_no_eom 예: "$VER", "$PRD", "$CON,1"
        """
        if not payload_no_eom.startswith("$"):
            raise ValueError("ACS2000 payload must start with '$'")

        tx = payload_no_eom.encode("ascii", errors="replace") + self._eom

        token = payload_no_eom.strip()
        token = token[1:] if token.startswith("$") else token
        token = token.split(",", 1)[0].strip().upper()

        # ✅ 연결 보장(서비스가 connect 해도 안전하게 한번 더)
        if not self.is_connected:
            self.connect()

        with self._lock:
            if self._streaming and not allow_streaming:
                raise ACS2000ProtocolError(
                    "ACS2000 is in CON streaming mode; stop stream before issuing commands."
                )

            ser = self._require()

            # 이전에 남아있던 LF/잔여 바이트 제거
            self._rx_prefetch.clear()
            ser.reset_input_buffer()
            ser.reset_output_buffer()

            ser.write(tx)
            ser.flush()

            rx = self._read_until_cr_unlocked(timeout_s=rx_timeout_s)

        if not rx:
            self._emit_io_trace(ok=False, token=token, tx=payload_no_eom, rx="", detail="no response")
            raise ACS2000ProtocolError(f"no response for '{payload_no_eom}'")

        reply = rx.decode("ascii", errors="replace").strip()

        # ✅ ERR 응답은 정상으로 취급하지 않음
        err_code = _extract_err_code(reply)
        if err_code is not None:
            self._emit_io_trace(ok=False, token=token, tx=payload_no_eom, rx=reply, detail=f"ERR code={err_code}")
            raise ACS2000ProtocolError(f"ACS2000 error reply: {reply} (code={err_code})")
        
        if token != "PRD":
            self._emit_io_trace(ok=True, token=token, tx=payload_no_eom, rx=reply, detail="")

        return reply
    
    def _reconnect_with_backoff(self, backoff_s: float) -> float:
        # close → sleep → connect
        try:
            self.close()
        except Exception:
            pass

        time.sleep(backoff_s)

        # connect 재시도
        self.connect()

        # backoff 업데이트
        next_backoff = min(backoff_s * self._reconnect_backoff_factor, self._reconnect_backoff_max_s)
        return next_backoff


    def _call_with_policy(self, op: str, fn):
        """
        ✅ 정책:
        - 에러 1~io_err_allow회: 연결 유지 재시도
        - 그 다음: close→reconnect(backoff) 후 재시도
        - io_reconnect_max 번의 재연결까지 허용
        """
        err_count = 0
        reconnect_left = self._io_reconnect_max
        backoff_s = self._reconnect_backoff_base_s

        while True:
            try:
                return fn()

            except (ACS2000ProtocolError, SerialDeviceError) as e:
                err_count += 1

                # 1~5회: 연결 유지 재시도
                if err_count <= self._io_err_allow:
                    if self._io_retry_sleep_s > 0:
                        time.sleep(self._io_retry_sleep_s)
                    continue

                # 6회째부터: reconnect
                if reconnect_left <= 0:
                    raise

                reconnect_left -= 1
                err_count = 0  # reconnect 성공 후 연속 에러 카운터 리셋: 다시 io_err_allow 회의 유예를 부여

                backoff_s = self._reconnect_with_backoff(backoff_s)

                # ✅ 스트림 모드였다면 재연결 후 스트림 재시작
                if self._con_interval_a is not None:
                    try:
                        self._streaming = False
                        _ = self._txrx(f"$CON,{self._con_interval_a}", rx_timeout_s=0.8, allow_streaming=True)
                        self._streaming = True
                    except Exception:
                        # ✅ 스트림 재시작이 실패하면, 다음 while 루프에서 다시 정책대로 재시도/재연결
                        self._streaming = False
                        continue

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
            p = float(cand)
            # ✅ 변화 있을 때만 로그
            self._emit_pressure_if_changed(pressure=p, source="PRD", raw=raw, tx=payload)
            return p
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

        self._con_interval_a = int(interval_a)

        def _start_once():
            # 재시작을 허용하기 위해 streaming 내려놓고 CON 전송
            self._streaming = False
            reply = self._txrx(f"$CON,{interval_a}", rx_timeout_s=0.8, allow_streaming=True)
            # 여기까지 왔으면 ERR이 아니라는 뜻
            self._streaming = True
            return reply

        return self._call_with_policy(f"CON({interval_a})", _start_once)

    def read_stream_line(self, timeout_s: float = 2.0) -> str:
        """
        첫 stream 샘플을 기다려 읽는 public 함수.
        여기서는 timeout을 통신 실패로 보고 policy 재시도 대상에 포함한다.
        """
        def _read_once():
            line = self._read_stream_line_once(
                timeout_s=timeout_s,
                emit_timeout_trace=True,
            )
            if line is None:
                raise ACS2000ProtocolError("stream timeout/no data")
            return line

        return self._call_with_policy("CON_STREAM_READ", _read_once)

    def _read_stream_line_once(
        self,
        timeout_s: float = 2.0,
        *,
        emit_timeout_trace: bool = True,
    ) -> Optional[str]:
        with self._lock:
            rx = self._read_until_cr_unlocked(timeout_s=timeout_s)

        if not rx:
            if emit_timeout_trace:
                self._emit_io_trace(
                    ok=False,
                    token="CON_STREAM",
                    tx="$CON_STREAM",
                    rx="",
                    detail="timeout/no data",
                )
            return None

        line = rx.decode("ascii", errors="replace").strip()

        err_code = _extract_err_code(line)
        if err_code is not None:
            self._emit_io_trace(
                ok=False,
                token="CON_STREAM",
                tx="$CON_STREAM",
                rx=line,
                detail=f"ERR code={err_code}",
            )
            raise ACS2000ProtocolError(f"ACS2000 stream ERR: {line} (code={err_code})")

        try:
            b, p = self.parse_con_line(line)

            if b != 0:
                last_b = getattr(self, "_last_logged_con_status", None)
                if last_b is None or last_b != b:
                    self._last_logged_con_status = b
                    self._emit_io_trace(
                        ok=False,
                        token="CON_STATUS",
                        tx="$CON_STREAM",
                        rx=line,
                        detail=f"status={b} ({CON_STATUS_MAP.get(b, '?')})",
                    )
            else:
                self._last_logged_con_status = 0
                if p is not None:
                    self._emit_pressure_if_changed(
                        pressure=p,
                        source="CON",
                        raw=line,
                        tx="$CON_STREAM",
                    )
        except Exception:
            pass

        return line

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

    def read_stream_sample(self, timeout_s: float = 2.0) -> dict:
        """
        공정/텔레메트리용:
        - status, status_text, pressure(optional) 같이 반환
        - status!=0이어도 예외로 끊지 않고 기록 가능
        """
        line = self.read_stream_line(timeout_s=timeout_s)
        b, p = self.parse_con_line(line)
        return {
            "status": b,
            "status_text": CON_STATUS_MAP.get(b, "?"),
            "pressure": p,
            "raw": line,
            "ok": (b == 0 and p is not None),
        }
    
    def read_stream_sample_latest(
        self,
        timeout_s: float = 2.0,
        drain_timeout_s: float = 0.02,
        max_drain_lines: int = 100,
    ) -> dict:
        """
        CON stream에서 '가장 최신의 완전한 샘플'만 반환한다.

        - 첫 샘플은 blocking + policy로 기다린다.
        - drain 구간은 추가 데이터가 없으면 정상 종료한다.
        - drain timeout은 오류나 reconnect 사유로 취급하지 않는다.
        """
        line = self.read_stream_line(timeout_s=timeout_s)
        latest_line = line
        drained = 0

        # 짧은 drain_timeout으로 버퍼에 쌓인 잔여 샘플을 소비하여 가장 최근 값만 취함
        while drained < max_drain_lines:
            extra = self._read_stream_line_once(
                timeout_s=drain_timeout_s,
                emit_timeout_trace=False,   # drain 종료는 정상 상황
            )

            if extra is None:
                break

            latest_line = extra
            drained += 1

        b, p = self.parse_con_line(latest_line)
        return {
            "status": b,
            "status_text": CON_STATUS_MAP.get(b, "?"),
            "pressure": p,
            "raw": latest_line,
            "ok": (b == 0 and p is not None),
            "drained": drained,
        }

    def stop_stream_if_running(self) -> None:
        """
        연결 직후 장비가 이전 세션의 CON 스트리밍 상태일 수 있으므로
        유효한 명령 프레임($VER)을 보내 스트림을 중단시키고,
        실제로 읽어서 버퍼를 비운다.

        - b'\\r' 단독 전송은 ACS-2000이 무시할 수 있어 스트림이 계속됨
        - reset_input_buffer()는 CH340/CP2102 Windows 드라이버에서
          OS 레벨 버퍼를 완전히 비우지 못함
        → 유효한 명령 전송 + 직접 read drain 방식으로 대체
        """
        with self._lock:
            try:
                ser = self._require()
                self._rx_prefetch.clear()

                # $VER: 유효한 명령 → 장비가 현재 출력을 멈추고 응답 준비
                # ACS-2000 매뉴얼: "Press any button to stop transmission"
                ser.write(b'$VER\r')
                ser.flush()

                # 1.2초 동안 직접 read로 버퍼를 완전히 소진
                # (reset_input_buffer 대신 직접 drain)
                old_timeout = ser.timeout
                try:
                    ser.timeout = 0.05
                    t0 = time.time()
                    while time.time() - t0 < 1.2:
                        chunk = ser.read(256)
                        if not chunk:
                            time.sleep(0.05)
                finally:
                    ser.timeout = old_timeout

                self._rx_prefetch.clear()
            except Exception:
                pass

        self._streaming = False
        self._con_interval_a = None

    def stop_stream_safe(self) -> None:
        self._streaming = False
        self._con_interval_a = None  # ✅ 자동 스트림 재시작 방지
        self.close()

    def query_errors(self) -> str:
        return self.raw("ERR")

    # alias
    def get_version(self) -> str:
        return self.query_version()

    def get_pressure(self, channel: int = 1) -> float:
        return self.query_pressure(channel)
