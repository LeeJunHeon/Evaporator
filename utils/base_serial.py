# devices/base_serial.py
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

try:
    import serial  # pyserial
except ImportError as e:
    raise ImportError("pyserial is required. run: pip install pyserial") from e


@dataclass
class TxRx:
    tx: bytes
    rx: bytes
    ts: float


class SerialDeviceError(RuntimeError):
    pass


class BaseSerialDevice:
    """
    - connect()/close()
    - thread-safe (RLock)
    - low-level write/read helpers
    """

    def __init__(self, *, port: str, baudrate: int, bytesize: int, parity: str, stopbits: int,
                 timeout_s: float, write_timeout_s: float, rtscts: bool = False, dsrdtr: bool = False):
        self._port = port
        self._baudrate = baudrate
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits
        self._timeout_s = timeout_s
        self._write_timeout_s = write_timeout_s
        self._rtscts = rtscts
        self._dsrdtr = dsrdtr

        self._lock = threading.RLock()
        self._ser: Optional[serial.Serial] = None

    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def connect(self) -> None:
        with self._lock:
            if self.is_connected:
                return
            try:
                self._ser = serial.Serial(
                    port=self._port,
                    baudrate=self._baudrate,
                    bytesize=self._bytesize,
                    parity=self._parity,
                    stopbits=self._stopbits,
                    timeout=self._timeout_s,
                    write_timeout=self._write_timeout_s,
                    rtscts=self._rtscts,
                    dsrdtr=self._dsrdtr,
                )
                # 깨끗한 시작
                self._ser.reset_input_buffer()
                self._ser.reset_output_buffer()
            except Exception as e:
                self._ser = None
                raise SerialDeviceError(f"connect failed: port={self._port}, baud={self._baudrate}, err={e}") from e

    def close(self) -> None:
        with self._lock:
            if self._ser is None:
                return
            try:
                self._ser.close()
            finally:
                self._ser = None

    def _require(self) -> "serial.Serial":
        if not self.is_connected or self._ser is None:
            raise SerialDeviceError("device not connected")
        return self._ser

    def _write(self, data: bytes, *, flush: bool = True, reset_input: bool = True) -> None:
        with self._lock:
            ser = self._require()
            if reset_input:
                ser.reset_input_buffer()
            ser.write(data)
            if flush:
                ser.flush()

    def _read_exact(self, n: int, timeout_s: float) -> bytes:
        """
        n 바이트를 timeout 안에 최대한 읽음.
        """
        with self._lock:
            ser = self._require()
            t0 = time.time()
            buf = bytearray()
            while len(buf) < n and (time.time() - t0) < timeout_s:
                chunk = ser.read(n - len(buf))
                if chunk:
                    buf += chunk
            return bytes(buf)
