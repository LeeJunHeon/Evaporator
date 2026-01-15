# -*- coding: utf-8 -*-
"""plc_config.py

Evaporator 프로젝트용 PLC(Modbus-TCP) 설정 로더.

목표
 - config/devices.ini 에서 PLC 접속정보를 읽는다.
 - 섹션이 없거나 값이 비어있어도, 프로그램이 "바로 죽지" 않게 기본값으로 동작한다.
   (처음 장비 세팅 전 UI 개발 단계에서 특히 유용)

추후 확장
 - 사용자가 UI의 Config 창에서 값을 수정하고 ini로 저장하는 기능을 추가할 때
   여기 파일의 dataclass 구조를 그대로 재사용하면 된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from configparser import ConfigParser
from pathlib import Path


@dataclass(frozen=True)
class PLCSettings:
    # Modbus-RTU (RS-232)
    port: str = "COM5"
    method: str = "rtu"
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    unit: int = 1

    # Door 열림/닫힘(기구 이동) 시간(초) — UI 인터락 기준
    door_move_time_s: float = 10.0

    # 타임아웃/주기
    timeout_s: float = 2.0
    poll_interval_s: float = 0.25
    reconnect_interval_s: float = 1.0

    # momentary(pulse) 동작 시 펄스폭(ms) — 현재는 HMI를 래치로 쓰므로 기본값만 둠
    pulse_ms: int = 180


def _get_str(cfg: ConfigParser, section: str, key: str, default: str) -> str:
    if cfg.has_option(section, key):
        return cfg.get(section, key).strip()
    return default


def _get_int(cfg: ConfigParser, section: str, key: str, default: int) -> int:
    if cfg.has_option(section, key):
        return cfg.getint(section, key)
    return default


def _get_float(cfg: ConfigParser, section: str, key: str, default: float) -> float:
    if cfg.has_option(section, key):
        return cfg.getfloat(section, key)
    return default


def load_plc_settings(ini_path: str | Path, section: str = "plc") -> PLCSettings:
    """devices.ini 에서 PLC 설정을 읽는다.

    - [plc] 섹션이 없으면 기본값(PLCSettings 기본값)으로 반환
    - ip가 비어있어도 기본값 사용
    """
    ini_path = Path(ini_path)
    if not ini_path.exists():
        # ini 자체가 없으면 개발 단계에서는 기본값으로라도 뜨게
        return PLCSettings()

    cfg = ConfigParser()
    cfg.read(ini_path, encoding="utf-8")

    if not cfg.has_section(section):
        return PLCSettings()

    port = _get_str(cfg, section, "port", PLCSettings.port) or PLCSettings.port
    method = (_get_str(cfg, section, "method", PLCSettings.method) or PLCSettings.method).lower()
    parity = (_get_str(cfg, section, "parity", PLCSettings.parity) or PLCSettings.parity).upper()

    return PLCSettings(
        port=port,
        method=method,
        baudrate=_get_int(cfg, section, "baudrate", PLCSettings.baudrate),
        bytesize=_get_int(cfg, section, "bytesize", PLCSettings.bytesize),
        parity=parity,
        stopbits=_get_int(cfg, section, "stopbits", PLCSettings.stopbits),
        unit=_get_int(cfg, section, "unit", PLCSettings.unit),
        timeout_s=_get_float(cfg, section, "timeout_s", PLCSettings.timeout_s),
        poll_interval_s=_get_float(cfg, section, "poll_interval_s", PLCSettings.poll_interval_s),
        reconnect_interval_s=_get_float(cfg, section, "reconnect_interval_s", PLCSettings.reconnect_interval_s),
        pulse_ms=_get_int(cfg, section, "pulse_ms", PLCSettings.pulse_ms),
        door_move_time_s=_get_float(cfg, section, "door_move_time_s", PLCSettings.door_move_time_s),
    )
