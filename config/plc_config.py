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
    # Modbus-TCP
    ip: str = "192.168.1.2"
    port: int = 502
    unit: int = 1

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

    ip = _get_str(cfg, section, "ip", PLCSettings.ip) or PLCSettings.ip
    return PLCSettings(
        ip=ip,
        port=_get_int(cfg, section, "port", PLCSettings.port),
        unit=_get_int(cfg, section, "unit", PLCSettings.unit),
        timeout_s=_get_float(cfg, section, "timeout_s", PLCSettings.timeout_s),
        poll_interval_s=_get_float(cfg, section, "poll_interval_s", PLCSettings.poll_interval_s),
        reconnect_interval_s=_get_float(cfg, section, "reconnect_interval_s", PLCSettings.reconnect_interval_s),
        pulse_ms=_get_int(cfg, section, "pulse_ms", PLCSettings.pulse_ms),
    )
