# devices/device_manager.py
from __future__ import annotations

from pathlib import Path

from .serial_config import load_settings
from .stm100 import STM100
from .acs2000 import ACS2000


class DeviceManager:
    def __init__(self, stm: STM100, acs: ACS2000):
        self.stm = stm
        self.acs = acs

    @classmethod
    def from_ini(cls, ini_path: str | Path) -> "DeviceManager":
        ini_path = Path(ini_path)

        stm_s = load_settings(ini_path, "stm100")
        acs_s = load_settings(ini_path, "acs2000")

        stm = STM100(
            port=stm_s.port,
            baudrate=stm_s.baudrate,
            bytesize=stm_s.bytesize,
            parity=stm_s.parity,
            stopbits=stm_s.stopbits,
            timeout_s=stm_s.timeout_s,
            write_timeout_s=stm_s.write_timeout_s,
            rtscts=stm_s.rtscts,
            dsrdtr=stm_s.dsrdtr,
        )

        acs = ACS2000(
            port=acs_s.port,
            baudrate=acs_s.baudrate,
            bytesize=acs_s.bytesize,
            parity=acs_s.parity,
            stopbits=acs_s.stopbits,
            timeout_s=acs_s.timeout_s,
            write_timeout_s=acs_s.write_timeout_s,
            eom=acs_s.eom or "CR",
        )

        return cls(stm=stm, acs=acs)

    def connect_all(self) -> None:
        self.stm.connect()
        self.acs.connect()

    def close_all(self) -> None:
        self.stm.close()
        self.acs.close()
