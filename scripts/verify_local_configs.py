# -*- coding: utf-8 -*-
"""장비별 설정 파일 분리 검증: Git 관리 제외 + 예시 파일(.example) + 없을 때만 자동 생성."""
from __future__ import annotations

import configparser
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.local_files import LOCAL_CONFIG_FILES, ensure_local_configs  # noqa: E402

# 예시 파일 내용 확인용 Git blob 해시 (줄바꿈 LF 기준).
# 장비 노트북 진단(2026-10-15 16:35)에서 장비의 실제 설정 파일로 계산한 해시와 같은 값이어야 한다.
EXPECTED_EXAMPLE_BLOBS = {
    "config/devices.ini.example": "2aa9abc3233b0e6f133614c67d70bba479ba8b18",
    "config/process_config.json.example": "9af3145ce0f594e107864f0412aa0e84848a0c54",
    "config/material_catalog.json.example": "afd220e09d945014ab9cf00fc69fc49b27f9c556",
}

FAILS: list = []


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [OK] " if ok else "  [NG] ") + name + (f" | {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def git(*args: str):
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout.strip()


def test_auto_create() -> None:
    print("[1] 없을 때만 예시에서 생성 (임시 폴더에서 시험)")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg = base / "config"
        cfg.mkdir()
        for n in LOCAL_CONFIG_FILES:
            shutil.copyfile(ROOT / "config" / (n + ".example"), cfg / (n + ".example"))
        created = ensure_local_configs(base)
        check("없는 설정 3개를 예시에서 생성", sorted(created) == sorted(LOCAL_CONFIG_FILES), str(created))
        same = all((cfg / n).read_bytes() == (cfg / (n + ".example")).read_bytes() for n in LOCAL_CONFIG_FILES)
        check("생성된 파일 = 예시 파일 (바이트 동일)", same)
        (cfg / "process_config.json").write_text("LOCAL", encoding="utf-8")
        created2 = ensure_local_configs(base)
        kept = (cfg / "process_config.json").read_text(encoding="utf-8") == "LOCAL"
        check("이미 있는 파일은 덮어쓰지 않음", created2 == [] and kept, str(created2))
        (cfg / "devices.ini").unlink()
        (cfg / "devices.ini.example").unlink()
        created3 = ensure_local_configs(base)
        check("예시 파일도 없으면 아무것도 하지 않음", created3 == [] and not (cfg / "devices.ini").exists(), str(created3))


def test_repo_state() -> None:
    print("[2] 저장소 상태")
    _, out = git("ls-files", "config")
    tracked = set(out.splitlines())
    for n in LOCAL_CONFIG_FILES:
        rel = f"config/{n}"
        check(f"{rel} 는 Git 관리 안 함", rel not in tracked)
        check(f"{rel}.example 는 Git 관리", rel + ".example" in tracked)
        rc, _ = git("check-ignore", "-q", rel)
        check(f"{rel} 는 .gitignore 대상", rc == 0)
        check(f"{rel} 실제 파일은 디스크에 그대로 있음", (ROOT / rel).exists())
    for rel, want in EXPECTED_EXAMPLE_BLOBS.items():
        _, got = git("hash-object", rel)
        check(f"{rel} 내용", got == want, got)
    for n in ("process_config.json", "material_catalog.json"):
        try:
            json.loads((ROOT / "config" / (n + ".example")).read_text(encoding="utf-8"))
            ok = True
        except Exception:
            ok = False
        check(f"config/{n}.example JSON 읽기", ok)
    cp = configparser.ConfigParser()
    cp.read(ROOT / "config" / "devices.ini.example", encoding="utf-8")
    need = ("io_policy", "stm100", "acs2000", "turbovac", "plc")
    check("config/devices.ini.example 섹션", all(s in cp for s in need), str(cp.sections()))
    _, attr = git("check-attr", "text", "eol", "--", "main.py")
    check(".gitattributes 적용 (main.py 는 text, eol=lf)", "eol: lf" in attr, attr.replace("\n", " / "))


def test_main_hook() -> None:
    print("[3] main.py 연결")
    src = (ROOT / "main.py").read_text(encoding="utf-8-sig")
    i_def = src.find("def main():")
    i_call = src.find("ensure_local_configs(_BASE_DIR)")
    i_load = src.find("load_plc_settings(ini_path)")
    check("main() 첫 부분에서 설정 확인 (devices.ini 읽기보다 먼저)", 0 <= i_def < i_call < i_load, f"{i_def} < {i_call} < {i_load}")


def main() -> None:
    _utf8_stdout()
    test_auto_create()
    test_repo_state()
    test_main_hook()
    print()
    if FAILS:
        print("실패: " + ", ".join(FAILS))
        sys.exit(1)
    print("모든 시험 통과 (ALL PASS)")


if __name__ == "__main__":
    main()
