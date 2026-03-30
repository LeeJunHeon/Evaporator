# -*- coding: utf-8 -*-
"""
process/recipe_io.py

레시피(Recipe) 로드/저장 모듈.
- JSON / CSV 지원
- CSV는 사람이 엑셀/메모장으로 편집하기 쉬운 스키마 제공
- 로드 시 타입/범위/필수 필드 검증(models.py의 validate 호출)

권장
- 개발/테스트 단계: JSON이 가장 안정적(구조 유지)
- 운영/현장 편집: CSV(엑셀로 수정 쉬움) + strict 검증

CSV 스키마(헤더)
    idx,name,type,
    coil,on,pulse_ms,
    reg,value,
    dac_ch,dac_ma,
    seconds,
    pressure_target,thickness_target_a,rate_min_a_s,rate_max_a_s,
    expected,
    timeout_s,stable_s,poll_s,on_timeout,
    message,
    meta

- meta: JSON 문자열(예: {"note":"xxx","foo":123})
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .models import (
    ProcessRecipe,
    ProcessStep,
    StepType,
    OnTimeout,
)


# ============================================================
# Exceptions
# ============================================================

class RecipeIOError(Exception):
    pass


# ============================================================
# CSV Schema
# ============================================================

CSV_COLUMNS: List[str] = [
    "idx",
    "name",
    "type",

    "coil",
    "on",
    "pulse_ms",

    "reg",
    "value",

    "dac_ch",
    "dac_ma",

    "seconds",

    "pressure_target",
    "thickness_target_a",
    "rate_min_a_s",
    "rate_max_a_s",

    "expected",

    "timeout_s",
    "stable_s",
    "poll_s",
    "on_timeout",

    "message",
    "meta",
]


# ============================================================
# Utility parse helpers
# ============================================================

def _strip(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _none_if_empty(v: Any) -> Optional[str]:
    s = _strip(v)
    return None if s == "" else s


def _parse_bool(v: Any) -> Optional[bool]:
    """
    CSV에서 bool 파싱:
    - true/false, t/f, yes/no, y/n, on/off, 1/0
    - 빈 값 -> None
    """
    s = _strip(v).lower()
    if s == "":
        return None
    if s in ("1", "true", "t", "yes", "y", "on"):
        return True
    if s in ("0", "false", "f", "no", "n", "off"):
        return False
    raise ValueError(f"invalid bool: {v!r}")


def _parse_int(v: Any) -> Optional[int]:
    s = _strip(v)
    if s == "":
        return None
    # "10.0" 같은 값이 들어오면 의도치 않으니 에러로 처리
    if any(ch in s for ch in (".", "e", "E")):
        # 소수점·지수 표기 포함된 문자열은 int 필드에서 거부: 레지스터 값/펄스 ms는 정수만 허용
        # 단, 사용자가 "1000e0" 같은 걸 쓸 수도 있는데 int로 쓰는 필드는
        # 보통 레지스터 값/펄스 ms라서 안전하게 막는 편이 낫다.
        raise ValueError(f"invalid int: {v!r}")
    return int(s)


def _parse_float(v: Any) -> Optional[float]:
    s = _strip(v)
    if s == "":
        return None
    return float(s)  # 1e-6 같은 표현도 허용


def _parse_json_meta(v: Any) -> Dict[str, Any]:
    s = _strip(v)
    if s == "":
        return {}
    try:
        obj = json.loads(s)
        if not isinstance(obj, dict):
            raise ValueError("meta must be JSON object")
        return obj
    except Exception as e:
        raise ValueError(f"invalid meta JSON: {e!r} (value={s!r})")


def _parse_enum_step_type(v: Any) -> StepType:
    s = _strip(v)
    if s == "":
        raise ValueError("type is required")
    try:
        return StepType(s)
    except Exception:
        # 사용자가 소문자/공백 넣어도 최대한 보정
        # 소문자·공백 허용을 위해 대문자+언더스코어로 정규화 후 재시도
        s2 = s.strip().upper().replace(" ", "_")
        try:
            return StepType(s2)
        except Exception:
            raise ValueError(f"invalid StepType: {v!r}")


def _parse_enum_on_timeout(v: Any) -> OnTimeout:
    s = _strip(v)
    if s == "":
        return OnTimeout.ABORT
    try:
        return OnTimeout(s)
    except Exception:
        s2 = s.strip().upper()
        try:
            return OnTimeout(s2)
        except Exception:
            return OnTimeout.ABORT


# ============================================================
# Public API
# ============================================================

def load_recipe(path: Union[str, Path], *, strict: bool = True) -> ProcessRecipe:
    """
    확장자에 따라 자동 로드:
      - .json -> JSON
      - .csv  -> CSV

    strict=True:
      - models.ProcessStep.validate(strict=True) 호출
      - coil/reg 이름 오타 즉시 검출
    """
    p = Path(path)
    if not p.exists():
        raise RecipeIOError(f"recipe file not found: {p}")

    ext = p.suffix.lower()
    if ext == ".json":
        recipe = load_recipe_json(p, strict=strict)
    elif ext == ".csv":
        recipe = load_recipe_csv(p, strict=strict)
    else:
        raise RecipeIOError(f"unsupported recipe file extension: {ext} (use .json or .csv)")

    return recipe


def save_recipe(path: Union[str, Path], recipe: ProcessRecipe, *, fmt: Optional[str] = None) -> None:
    """
    확장자 또는 fmt 지정에 따라 저장:
      - fmt: "json" | "csv" (옵션)
      - fmt 미지정 시 파일 확장자로 결정
    """
    p = Path(path)
    if fmt is None:
        ext = p.suffix.lower()
        if ext == ".json":
            fmt = "json"
        elif ext == ".csv":
            fmt = "csv"
        else:
            raise RecipeIOError(f"unsupported save extension: {ext} (use .json or .csv)")

    fmt = fmt.lower().strip()
    if fmt == "json":
        save_recipe_json(p, recipe)
    elif fmt == "csv":
        save_recipe_csv(p, recipe)
    else:
        raise RecipeIOError(f"unsupported format: {fmt!r}")


# ============================================================
# JSON I/O
# ============================================================

def load_recipe_json(path: Union[str, Path], *, strict: bool = True) -> ProcessRecipe:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise RecipeIOError(f"failed to read json: {p} -> {e!r}")

    try:
        recipe = ProcessRecipe.from_dict(data)
        recipe.validate(strict=strict)
        return recipe
    except Exception as e:
        raise RecipeIOError(f"invalid recipe json: {p} -> {e!r}")


def save_recipe_json(path: Union[str, Path], recipe: ProcessRecipe) -> None:
    p = Path(path)
    try:
        # 저장 시 strict=False: 알 수 없는 코일 이름이 있어도 파일 저장은 허용하고 운영 로드 시 strict=True로 검출
        recipe.validate(strict=False)  # 저장 자체는 가능하도록(오타는 운영에서 strict로 잡기)
    except Exception:
        # validate 실패해도 저장은 허용하고 싶으면 여기서 pass 가능.
        # 하지만 지금은 안전하게 유지.
        raise

    payload = recipe.to_dict()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# CSV I/O
# ============================================================

def load_recipe_csv(path: Union[str, Path], *, strict: bool = True) -> ProcessRecipe:
    """
    CSV 로드 규칙:
    - 첫 줄은 header 필수(권장 스키마 CSV_COLUMNS)
    - idx는 선택(정렬용). 없으면 파일 순서대로 적용
    - 빈 줄/주석 줄(# ...) 무시
    - type 은 StepType 문자열
    - meta 는 JSON object 문자열
    """
    p = Path(path)
    try:
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise RecipeIOError("CSV header is missing")

            rows: List[Dict[str, Any]] = []
            for raw in reader:
                # raw는 dict인데, 빈 줄이면 모든 값이 None일 수 있음
                if raw is None:
                    continue
                # 주석/빈 줄 처리: name/type 둘 다 비면 스킵
                if _strip(raw.get("name")) == "" and _strip(raw.get("type")) == "":
                    continue
                # '#'로 시작하는 name이면 주석으로 간주
                if _strip(raw.get("name")).startswith("#"):
                    continue
                rows.append(raw)

    except RecipeIOError:
        raise
    except Exception as e:
        raise RecipeIOError(f"failed to read csv: {p} -> {e!r}")

    # idx 정렬 (있으면 idx 기준, 없으면 입력 순서)
    parsed: List[Tuple[int, ProcessStep]] = []
    for i, raw in enumerate(rows):
        try:
            idx = _parse_int(raw.get("idx"))
            idx2 = idx if idx is not None else i
            step = _step_from_csv_row(raw)
            parsed.append((idx2, step))
        except Exception as e:
            raise RecipeIOError(f"CSV row parse error at line~{i+2} (header=1): {e!r} | row={raw}")

    # idx 컬럼이 존재하면 파일 내 행 순서가 아닌 idx 기준으로 스텝 순서 재정렬
    parsed.sort(key=lambda x: x[0])
    steps = [s for _, s in parsed]

    # recipe_name은 파일명 기본값
    recipe = ProcessRecipe(recipe_name=p.stem, steps=steps)

    # 검증
    try:
        recipe.validate(strict=strict)
    except Exception as e:
        raise RecipeIOError(f"invalid recipe(csv) after validate: {p} -> {e!r}")

    return recipe


def save_recipe_csv(path: Union[str, Path], recipe: ProcessRecipe) -> None:
    """
    CSV 저장:
    - CSV_COLUMNS 헤더로 저장
    - step별로 관련 없는 값은 공백으로 저장
    - meta는 JSON 문자열로 저장
    """
    p = Path(path)
    recipe.validate(strict=False)

    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for idx, st in enumerate(recipe.steps):
            writer.writerow(_step_to_csv_row(idx, st))


def write_recipe_csv_template(path: Union[str, Path]) -> None:
    """
    빈 템플릿 CSV 생성(헤더만).
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()


# ============================================================
# CSV row <-> Step convert
# ============================================================

def _step_from_csv_row(row: Dict[str, Any]) -> ProcessStep:
    """
    row(dict) -> ProcessStep
    """
    name = _strip(row.get("name"))
    stype = _parse_enum_step_type(row.get("type"))
    on_timeout = _parse_enum_on_timeout(row.get("on_timeout"))

    step = ProcessStep(
        name=name,
        type=stype,

        coil=_none_if_empty(row.get("coil")),
        on=_parse_bool(row.get("on")),
        pulse_ms=_parse_int(row.get("pulse_ms")),

        reg=_none_if_empty(row.get("reg")),
        value=_parse_int(row.get("value")),

        dac_ch=_parse_int(row.get("dac_ch")),
        dac_ma=_parse_float(row.get("dac_ma")),

        seconds=_parse_float(row.get("seconds")),

        pressure_target=_parse_float(row.get("pressure_target")),
        thickness_target_a=_parse_float(row.get("thickness_target_a")),
        rate_min_a_s=_parse_float(row.get("rate_min_a_s")),
        rate_max_a_s=_parse_float(row.get("rate_max_a_s")),

        expected=_parse_bool(row.get("expected")),

        timeout_s=_parse_float(row.get("timeout_s")),
        stable_s=_parse_float(row.get("stable_s")),
        poll_s=_parse_float(row.get("poll_s")),

        on_timeout=on_timeout,

        message=_strip(row.get("message")),
        meta=_parse_json_meta(row.get("meta")),
    )

    return step


def _step_to_csv_row(idx: int, st: ProcessStep) -> Dict[str, Any]:
    """
    ProcessStep -> csv row(dict)
    - 관련 없는 필드는 ""로 저장
    """
    def b(v: Optional[bool]) -> str:
        if v is None:
            return ""
        return "1" if v else "0"

    def i(v: Optional[int]) -> str:
        return "" if v is None else str(int(v))

    def f(v: Optional[float]) -> str:
        return "" if v is None else str(float(v))

    meta_s = ""
    try:
        if st.meta:
            meta_s = json.dumps(st.meta, ensure_ascii=False)
    except Exception:
        meta_s = ""

    return {
        "idx": str(idx),
        "name": st.name,
        "type": st.type.value,

        "coil": st.coil or "",
        "on": b(st.on),
        "pulse_ms": i(st.pulse_ms),

        "reg": st.reg or "",
        "value": i(st.value),

        "dac_ch": i(st.dac_ch),
        "dac_ma": f(st.dac_ma),

        "seconds": f(st.seconds),

        "pressure_target": f(st.pressure_target),
        "thickness_target_a": f(st.thickness_target_a),
        "rate_min_a_s": f(st.rate_min_a_s),
        "rate_max_a_s": f(st.rate_max_a_s),

        "expected": b(st.expected),

        "timeout_s": f(st.timeout_s),
        "stable_s": f(st.stable_s),
        "poll_s": f(st.poll_s),
        "on_timeout": st.on_timeout.value,

        "message": st.message or "",
        "meta": meta_s,
    }


# ============================================================
# Sample recipe builder (optional helper)
# ============================================================

def make_sample_recipe() -> ProcessRecipe:
    """
    빠른 테스트용 샘플 레시피 생성.
    (엔진 붙인 뒤 smoke test에 유용)
    """
    steps = [
        ProcessStep(name="LOG_START", type=StepType.LOG, message="Process start"),
        ProcessStep(name="WATER_ON", type=StepType.PLC_WRITE_COIL, coil="WATER_SW", on=True),
        ProcessStep(name="WAIT_2S", type=StepType.WAIT_SECONDS, seconds=2.0),
        ProcessStep(name="WAIT_PRESSURE", type=StepType.WAIT_PRESSURE_LEQ, pressure_target=1e-5, timeout_s=60.0, poll_s=0.5),
        ProcessStep(name="LOG_DONE", type=StepType.LOG, message="Process end"),
    ]
    r = ProcessRecipe(recipe_name="sample", steps=steps, default_poll_s=0.2, telemetry_interval_s=0.5)
    # strict False로도 한번 검증
    r.validate(strict=False)
    return r
