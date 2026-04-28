#!/usr/bin/env python3
"""
scripts/set_tmp_watchdog.py

TURBOVAC USS 통신 watchdog 영구 비활성 스크립트 (1회용)

목적:
- P182 (USS watchdog) 100 → 0  : 통신 끊겨도 watchdog 발동 안함
- P179 (Fallback CW) 0 → 1025  : 0x401 = bit10+bit0 (이중 안전장치)

연결 파라미터는 devices.ini의 [turbovac] 섹션을 직접 읽어와
TurbovacService와 100% 동일한 설정으로 연결합니다.

⚠️ 사용 전 필수 조건:
  1. Evaporator 프로그램 종료 (시리얼 포트 점유 해제)
  2. 펌프가 정상 운전 중일 것 (freq ≥ 800Hz)
"""
from __future__ import annotations

import configparser
import sys
import time
from pathlib import Path
from typing import Dict, Any

# 레포 루트
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from devices.turbovac import (
    Turbovac,
    AK_WRITE_16,
    RESP_16,
    _make_pke,
)

# =========================================================
# 설정
# =========================================================
INI_PATH = ROOT / "config" / "devices.ini"

NEW_P182 = 0       # USS watchdog: 0 = Indefinite (비활성)
NEW_P179 = 1025    # Fallback CW: 0x401 = (1<<10) | (1<<0)

SAFE_FREQ_THRESHOLD_HZ = 800
SAVE_WAIT_S = 35


# =========================================================
# 헬퍼
# =========================================================
def load_turbovac_cfg(ini_path: Path) -> Dict[str, Any]:
    """
    services/turbovac_service.py의 _load_config()와 동일한 방식으로
    devices.ini [turbovac] 섹션 로드.
    """
    if not ini_path.exists():
        raise FileNotFoundError(f"devices.ini not found: {ini_path}")
    cp = configparser.ConfigParser()
    cp.read(ini_path, encoding="utf-8")
    if "turbovac" not in cp:
        raise KeyError(f"[turbovac] section not found in {ini_path}")
    sec = cp["turbovac"]
    cfg = {
        "port": sec.get("port", "").strip(),
        "baudrate": sec.getint("baudrate", fallback=19200),
        "bytesize": sec.getint("bytesize", fallback=8),
        "parity": sec.get("parity", "E").strip().upper(),
        "stopbits": sec.getint("stopbits", fallback=1),
        "timeout_s": sec.getfloat("timeout_s", fallback=0.5),
        "write_timeout_s": sec.getfloat("write_timeout_s", fallback=2.0),
        "rtscts": sec.getboolean("rtscts", fallback=False),
        "dsrdtr": sec.getboolean("dsrdtr", fallback=False),
    }
    if not cfg["port"]:
        raise ValueError("[turbovac] port is empty in devices.ini")
    return cfg


def write_u16(dev: Turbovac, pnu: int, value: int) -> int:
    """파라미터 P{pnu}에 16-bit 값 쓰기. echo 값 반환."""
    pke = _make_pke(AK_WRITE_16, pnu)
    resp = dev._txrx(pke=pke, ind=0, pwe=int(value) & 0xFFFF)
    if resp["ak_resp"] != RESP_16:
        raise RuntimeError(
            f"P{pnu} write failed: AK_resp=0x{resp['ak_resp']:X}, "
            f"PWE={resp['pwe_u32']}"
        )
    return resp["pwe_u32"] & 0xFFFF


def read_u16_with_retry(
    dev: Turbovac,
    pnu: int,
    retries: int = 5,
    delay_s: float = 2.0,
) -> int:
    """P8 저장 직후 일시적 응답 불가에 대비한 재시도 read."""
    last_e: Exception | None = None
    for _ in range(retries):
        try:
            return dev.read_parameter_u16(pnu)
        except Exception as e:
            last_e = e
            time.sleep(delay_s)
    raise RuntimeError(f"P{pnu} read failed after {retries} retries: {last_e!r}")


# =========================================================
# 메인
# =========================================================
def main() -> int:
    print("=" * 64)
    print(" TURBOVAC USS Watchdog 영구 비활성 스크립트 (1회용)")
    print("=" * 64)

    # devices.ini 로드 (TurbovacService와 동일한 방식)
    try:
        cfg = load_turbovac_cfg(INI_PATH)
    except Exception as e:
        print(f" ❌ ini 로드 실패: {e}")
        return 1

    print()
    print(" devices.ini [turbovac]에서 로드한 연결 설정:")
    print(f"   port            = {cfg['port']}")
    print(f"   baudrate        = {cfg['baudrate']}")
    print(f"   bytesize        = {cfg['bytesize']}")
    print(f"   parity          = {cfg['parity']}")
    print(f"   stopbits        = {cfg['stopbits']}")
    print(f"   timeout_s       = {cfg['timeout_s']}")
    print(f"   write_timeout_s = {cfg['write_timeout_s']}")
    print(f"   rtscts          = {cfg['rtscts']}")
    print(f"   dsrdtr          = {cfg['dsrdtr']}")
    print()
    print(" 변경할 파라미터:")
    print(f"   P182 (USS watchdog): default(100) → {NEW_P182} (비활성)")
    print(f"   P179 (Fallback CW):  default(0)   → {NEW_P179} (0x{NEW_P179:04X})")
    print()
    print(" ⚠️ 필수 사전 조건:")
    print("    1) Evaporator 프로그램 종료 (시리얼 포트 점유 해제)")
    print("    2) 펌프가 정상 운전 중 (freq ≥ 800Hz)")
    print()

    confirm = input(" 위 조건이 모두 만족되면 'yes' 입력 (취소: Ctrl+C): ").strip().lower()
    if confirm != "yes":
        print(" 취소됨.")
        return 1

    print()
    print("[1/7] 시리얼 포트 연결 + 상태 확인...")
    # ✅ ini에서 로드한 cfg를 그대로 unpack → service와 동일한 파라미터
    dev = Turbovac(**cfg)

    try:
        try:
            fast = dev.connect_and_probe(assume_running=True)
        except Exception as e:
            print(f"  ❌ 연결 실패: {e!r}")
            print("     → 시리얼 포트 점유, USB 케이블, 전원 확인하세요.")
            return 2

        freq_hz = int(fast.get("freq_hz", 0) or 0)
        state_text = fast.get("state_text", "?")
        print(f"  ✓ 연결됨 | freq={freq_hz}Hz | state={state_text}")

        if freq_hz < SAFE_FREQ_THRESHOLD_HZ:
            print()
            print(f" ❌ 펌프 freq({freq_hz}Hz)가 {SAFE_FREQ_THRESHOLD_HZ}Hz 미만입니다.")
            print("    이 스크립트는 운전 중인 펌프에서만 실행해야 합니다.")
            print("    Evaporator 프로그램으로 펌프 시작 후 다시 실행하세요.")
            return 3

        # ---- 현재 값 ----
        print()
        print("[2/7] 현재 파라미터 값 읽기...")
        cur_p179 = dev.read_parameter_u16(179)
        cur_p182 = dev.read_parameter_u16(182)
        print(f"  P179 (현재) = {cur_p179} (0x{cur_p179:04X})")
        print(f"  P182 (현재) = {cur_p182} ({cur_p182/10:.1f}초)")

        if cur_p179 == NEW_P179 and cur_p182 == NEW_P182:
            print()
            print(" ℹ️  이미 원하는 값으로 설정되어 있습니다. 작업 불필요.")
            return 0

        # ---- 새 값 RAM에 쓰기 ----
        print()
        print("[3/7] 새 값을 RAM에 쓰기...")
        if cur_p182 != NEW_P182:
            echo = write_u16(dev, 182, NEW_P182)
            print(f"  ✓ P182 ← {NEW_P182} (echo: {echo})")
            time.sleep(0.1)
        if cur_p179 != NEW_P179:
            echo = write_u16(dev, 179, NEW_P179)
            print(f"  ✓ P179 ← {NEW_P179} (echo: {echo})")
            time.sleep(0.1)

        # ---- RAM 즉시 검증 ----
        print()
        print("[4/7] RAM 즉시 검증...")
        ram_p182 = dev.read_parameter_u16(182)
        ram_p179 = dev.read_parameter_u16(179)
        print(f"  P182 (RAM) = {ram_p182}  (목표: {NEW_P182})")
        print(f"  P179 (RAM) = {ram_p179}  (목표: {NEW_P179})")
        if ram_p182 != NEW_P182 or ram_p179 != NEW_P179:
            print(" ❌ RAM 검증 실패. 영구 저장하지 않고 중단합니다.")
            print("    펌프 재부팅하면 원상 복구됩니다.")
            return 4

        # ---- P8 = 1 영구 저장 ----
        print()
        print("[5/7] P8 = 1 영구 저장 명령 전송...")
        print(f"    ⚠️ 저장 약 30초 소요. 이 동안 USB/전원 절대 끊지 마세요.")
        write_u16(dev, 8, 1)
        print("  ✓ P8 = 1 명령 전송 (저장 진행 중)")

        # ---- 저장 완료 대기 ----
        # 저장 중 parameter read/write 불가, PZD만 전달됨.
        # RAM의 P182=0 덕분에 watchdog은 이미 비활성이므로 telegram 미발신해도 안전.
        print()
        print(f"[6/7] 저장 완료 대기 ({SAVE_WAIT_S}초)...")
        for i in range(SAVE_WAIT_S, 0, -1):
            print(f"     남은 시간 {i:2d}초 ", end="\r", flush=True)
            time.sleep(1)
        print("     대기 완료              ")

        # ---- 영구 저장 검증 ----
        print()
        print("[7/7] 영구 저장 검증 (재읽기, 재시도 포함)...")
        try:
            final_p182 = read_u16_with_retry(dev, 182)
            final_p179 = read_u16_with_retry(dev, 179)
        except Exception as e:
            print(f" ⚠️ 재읽기 실패: {e!r}")
            print("    저장은 진행됐을 수 있으나 검증 실패.")
            print("    Evaporator 프로그램 시작 후 재읽기로 확인하세요.")
            return 5

        print(f"  P182 (최종) = {final_p182}")
        print(f"  P179 (최종) = {final_p179}")

        if final_p182 == NEW_P182 and final_p179 == NEW_P179:
            print()
            print("=" * 64)
            print(" ✅ 모든 파라미터가 영구 저장되었습니다.")
            print("=" * 64)
            print()
            print(" 다음 단계:")
            print("   1. 이 스크립트 종료 (자동)")
            print("   2. Evaporator 프로그램 정상 시작")
            print("   3. 검증 테스트:")
            print("      - 프로그램 종료 → 30초 이상 대기 → 다시 시작")
            print("      - freq가 1000Hz 그대로 유지되는지 확인")
            return 0
        else:
            print(" ❌ 영구 저장 검증 실패.")
            print("    LEYASSIST로 수동 확인 권장.")
            return 6

    finally:
        try:
            dev.close()
        except Exception:
            pass
        print()
        print(" (시리얼 포트 닫힘)")


if __name__ == "__main__":
    sys.exit(main())