# plc_config.py
from dataclasses import dataclass
from configparser import ConfigParser
from pathlib import Path
from typing import Optional

def _default_ini_path() -> Path:
    # <project_root>/config/devices.ini
    return Path(__file__).resolve().parent / "devices.ini"

def _normalize_parity(parity: str) -> str:
    p = str(parity).strip().upper()
    if p in ("", "NONE", "NO", "N", "0"):
        return "N"
    if p in ("EVEN", "E", "2"):
        return "E"
    if p in ("ODD", "O", "1"):
        return "O"
    if p in ("MARK", "M"):
        return "M"
    if p in ("SPACE", "S"):
        return "S"
    return "N"

def _normalize_unit(unit: int) -> int:
    # Modbus RTU에서 0은 브로드캐스트(읽기 응답 없음 가능) → 운용 안정상 1로 보정
    u = int(unit)
    return 1 if u <= 0 else u

@dataclass(frozen=True)
class PLCSettings:
    # ✅ 기본값은 devices.ini 템플릿과 동일하게
    port: str = "COM8"
    method: str = "rtu"
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    unit: int = 1

    timeout_s: float = 0.5
    poll_interval_s: float = 1.0
    reconnect_interval_s: float = 0.6

    # ✅ plc.py에 있던 값도 ini로 올려서 단일 소스화
    inter_cmd_gap_s: float = 0.05

    pulse_ms: int = 180
    door_move_time_s: float = 10.0

    # DAC (4~20mA)
    dac_full_scale_code: int = 4000
    dac_offset_code: int = 0
    dac_current_min_ma: float = 4.0
    dac_current_max_ma: float = 20.0

def load_plc_settings(ini_path: Optional[str | Path] = None, section: str = "plc") -> PLCSettings:
    path = Path(ini_path) if ini_path is not None else _default_ini_path()
    if not path.exists():
        return PLCSettings()

    cfg = ConfigParser()
    cfg.read(path, encoding="utf-8")
    if not cfg.has_section(section):
        return PLCSettings()

    # (생략) _get_str/_get_int/_get_float는 기존 그대로 사용 가능

    parity = _normalize_parity(cfg.get(section, "parity", fallback=PLCSettings.parity))
    unit = _normalize_unit(cfg.getint(section, "unit", fallback=PLCSettings.unit))

    return PLCSettings(
        port=cfg.get(section, "port", fallback=PLCSettings.port).strip() or PLCSettings.port,
        method=cfg.get(section, "method", fallback=PLCSettings.method).strip().lower() or PLCSettings.method,
        baudrate=cfg.getint(section, "baudrate", fallback=PLCSettings.baudrate),
        bytesize=cfg.getint(section, "bytesize", fallback=PLCSettings.bytesize),
        parity=parity,
        stopbits=cfg.getint(section, "stopbits", fallback=PLCSettings.stopbits),
        unit=unit,
        timeout_s=cfg.getfloat(section, "timeout_s", fallback=PLCSettings.timeout_s),
        poll_interval_s=cfg.getfloat(section, "poll_interval_s", fallback=PLCSettings.poll_interval_s),
        reconnect_interval_s=cfg.getfloat(section, "reconnect_interval_s", fallback=PLCSettings.reconnect_interval_s),
        inter_cmd_gap_s=cfg.getfloat(section, "inter_cmd_gap_s", fallback=PLCSettings.inter_cmd_gap_s),
        pulse_ms=cfg.getint(section, "pulse_ms", fallback=PLCSettings.pulse_ms),
        door_move_time_s=cfg.getfloat(section, "door_move_time_s", fallback=PLCSettings.door_move_time_s),
        dac_full_scale_code=cfg.getint(section, "dac_full_scale_code", fallback=PLCSettings.dac_full_scale_code),
        dac_offset_code=cfg.getint(section, "dac_offset_code", fallback=PLCSettings.dac_offset_code),
        dac_current_min_ma=cfg.getfloat(section, "dac_current_min_ma", fallback=PLCSettings.dac_current_min_ma),
        dac_current_max_ma=cfg.getfloat(section, "dac_current_max_ma", fallback=PLCSettings.dac_current_max_ma),
    )
