# devices/serial_config.py
from __future__ import annotations

from dataclasses import dataclass
from configparser import ConfigParser
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class SerialSettings:
    port: str
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    timeout_s: float = 0.5
    write_timeout_s: float = 0.5
    rtscts: bool = False
    dsrdtr: bool = False

    # ACS2000 전용
    eom: Optional[str] = None  # "CR" or "CRLF"


def _get_bool(cfg: ConfigParser, section: str, key: str, default: bool) -> bool:
    if cfg.has_option(section, key):
        return cfg.getboolean(section, key)
    return default


def _get_int(cfg: ConfigParser, section: str, key: str, default: int) -> int:
    if cfg.has_option(section, key):
        return cfg.getint(section, key)
    return default


def _get_float(cfg: ConfigParser, section: str, key: str, default: float) -> float:
    if cfg.has_option(section, key):
        return cfg.getfloat(section, key)
    return default


def _get_str(cfg: ConfigParser, section: str, key: str, default: str) -> str:
    if cfg.has_option(section, key):
        return cfg.get(section, key).strip()
    return default


def load_settings(ini_path: str | Path, section: str) -> SerialSettings:
    ini_path = Path(ini_path)
    if not ini_path.exists():
        raise FileNotFoundError(f"devices ini not found: {ini_path}")

    cfg = ConfigParser()
    cfg.read(ini_path, encoding="utf-8")

    if not cfg.has_section(section):
        raise KeyError(f"missing section in ini: [{section}]")

    port = _get_str(cfg, section, "port", "")
    if not port:
        raise ValueError(f"[{section}] port is empty")

    settings = SerialSettings(
        port=port,
        baudrate=_get_int(cfg, section, "baudrate", 9600),
        bytesize=_get_int(cfg, section, "bytesize", 8),
        parity=_get_str(cfg, section, "parity", "N").upper(),
        stopbits=_get_int(cfg, section, "stopbits", 1),
        timeout_s=_get_float(cfg, section, "timeout_s", 0.5),
        write_timeout_s=_get_float(cfg, section, "write_timeout_s", 0.5),
        rtscts=_get_bool(cfg, section, "rtscts", False),
        dsrdtr=_get_bool(cfg, section, "dsrdtr", False),
        eom=_get_str(cfg, section, "eom", "").upper() or None,
    )
    return settings
