"""장비별 설정 파일 준비.

devices.ini / process_config.json / material_catalog.json 은 장비 화면에서 값이 바뀌는 파일이라
Git 으로 관리하지 않는다(.gitignore). Git 에는 예시 파일(<이름>.example)만 있다.
새로 clone 한 폴더처럼 설정 파일이 없을 때만 예시 파일을 복사해서 만든다.
이미 있는 설정 파일은 어떤 경우에도 덮어쓰지 않는다.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

LOCAL_CONFIG_FILES = ("devices.ini", "process_config.json", "material_catalog.json")


def ensure_local_configs(base_dir) -> List[str]:
    """config/<이름> 이 없고 config/<이름>.example 이 있으면 복사한다. 새로 만든 파일 이름 목록을 돌려준다."""
    cfg_dir = Path(base_dir) / "config"
    created: List[str] = []
    for name in LOCAL_CONFIG_FILES:
        dst = cfg_dir / name
        src = cfg_dir / (name + ".example")
        if dst.exists() or not src.exists():
            continue
        data = src.read_bytes()
        try:
            with open(dst, "xb") as f:  # "x": 파일이 이미 있으면 실패 -> 덮어쓰기 불가
                f.write(data)
        except FileExistsError:
            continue
        created.append(name)
    return created
