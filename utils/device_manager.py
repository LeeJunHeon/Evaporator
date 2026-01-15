# utils/device_manager.py
from __future__ import annotations

from pathlib import Path
from typing import Dict

from config.serial_config import load_settings
from devices.stm100 import STM100
from devices.acs2000 import ACS2000


class DeviceManager:
    """
    STM100 / ACS2000 장비 객체를 들고 있다가,
    devices.ini 저장 후 즉시 close -> 재생성 -> connect 까지 처리한다.
    """

    def __init__(self, ini_path: str | Path, stm: STM100, acs: ACS2000):
        self.ini_path = Path(ini_path)
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
            rtscts=acs_s.rtscts,
            dsrdtr=acs_s.dsrdtr,
            eom=acs_s.eom or "CR",
        )

        return cls(ini_path=ini_path, stm=stm, acs=acs)

    def close_all(self) -> None:
        try:
            self.stm.close()
        except Exception:
            pass
        try:
            self.acs.close()
        except Exception:
            pass

    def connect_all(self) -> Dict[str, str]:
        """
        성공/실패를 dict로 반환(실패해도 프로그램이 죽지 않게)
        return: {"stm100": "error msg", "acs2000": "error msg"}  # 실패한 것만 담김
        """
        errors: Dict[str, str] = {}
        try:
            self.stm.connect()
        except Exception as e:
            errors["stm100"] = str(e)

        try:
            self.acs.connect()
        except Exception as e:
            errors["acs2000"] = str(e)

        return errors

    def reconnect_all(self) -> Dict[str, str]:
        """close -> connect"""
        self.close_all()
        return self.connect_all()

    def reload_from_ini(self, ini_path: str | Path | None = None, *, connect: bool = True) -> Dict[str, str]:
        """
        ✅ Config 저장 후 호출용:
        - ini 다시 읽어서 장비 객체를 새로 만들고
        - 원하면(connect=True) 즉시 connect까지
        """
        if ini_path is not None:
            self.ini_path = Path(ini_path)

        # 기존 연결 닫기
        self.close_all()

        # 새 설정으로 객체 재생성
        new_mgr = DeviceManager.from_ini(self.ini_path)
        self.stm = new_mgr.stm
        self.acs = new_mgr.acs

        if not connect:
            return {}
        return self.connect_all()
