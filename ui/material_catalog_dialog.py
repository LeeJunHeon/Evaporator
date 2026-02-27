# ui/material_catalog_dialog.py
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QLabel,
    QMessageBox,
    QAbstractItemView,
    QHeaderView,
)


@dataclass
class MaterialRow:
    material: str
    density_g_cm3: float
    z_factor: float

    # ---- per-material ramp/control defaults (기존 하드코딩 값과 동일하게) ----
    ramp_step_dac: int = 100

    ramp_seg1_max_dac: int = 700
    ramp_interval_seg1_s: float = 10.0

    ramp_seg2_max_dac: int = 1500
    ramp_interval_seg2_s: float = 30.0

    ignite_dac: int = 1500
    ignite_rate_min: float = 0.1
    ignite_timeout_s: float = 300.0  # (원래 엔진이 timeout 없으면, process_controller 수정 시 반영)

    pre_rate: float = 0.4
    pre_hold_s: float = 120.0

    dac_adjust_interval_s: float = 10.0
    fine_step_dac: int = 10

    material_shortage_dac: int = 2000
    material_shortage_rate_max: float = 0.0
    material_shortage_time_s: float = 10.0

    # NOTE: UI에서는 제거하지만, 기존 json에 남아있을 수 있어 호환용으로 유지(원하면 나중에 완전 제거 가능)
    note: str = ""


def _to_str(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        s = _to_str(v)
        if not s:
            return float(default)
        s = s.replace("*", "").strip()
        return float(s)
    except Exception:
        return float(default)


def _to_int(v: Any, default: int = 0) -> int:
    try:
        s = _to_str(v)
        if not s:
            return int(default)
        s = s.replace("*", "").strip()
        return int(float(s))
    except Exception:
        return int(default)


_COLS = [
    ("Material", "material", "str", "물질 이름 (예: Al, Au, SiO2)"),
    ("Density (g/cm³)", "density_g_cm3", "float>0", "필수. 0보다 커야 함"),
    ("Z factor", "z_factor", "float>0", "필수. 0보다 커야 함"),

    ("DAC 증가폭", "ramp_step_dac", "int>0", "Ramp에서 DAC를 한 번에 올리는 값"),
    ("구간1 끝(DAC)", "ramp_seg1_max_dac", "int>0", "예: 700"),
    ("구간1 간격(s)", "ramp_interval_seg1_s", "float>0", "예: 10초"),
    ("구간2 끝(DAC)", "ramp_seg2_max_dac", "int>0", "예: 1500"),
    ("구간2 간격(s)", "ramp_interval_seg2_s", "float>0", "예: 30초"),

    ("Ignite DAC", "ignite_dac", "int>=0", "Ignite 판단을 시작하는 DAC"),
    ("Ignite 최소 Rate(Å/s)", "ignite_rate_min", "float>=0", "0.1 등"),
    ("Ignite 타임아웃(s)", "ignite_timeout_s", "float>0", "Ignite 대기 최대 시간"),

    ("Pre-rate(Å/s)", "pre_rate", "float>=0", "예: 0.4 도달 시 pre-hold 시작"),
    ("Pre-hold(s)", "pre_hold_s", "float>=0", "예: 120초 대기"),

    ("DAC 변경 텀(s)", "dac_adjust_interval_s", "float>0", "제어 중 DAC 변경 최소 간격"),
    ("Fine step(DAC)", "fine_step_dac", "int>0", "미세 조정용 DAC step"),

    ("Shortage DAC", "material_shortage_dac", "int>=0", "예: 2000 이상인데 rate가 낮으면"),
    ("Shortage Rate(Å/s)", "material_shortage_rate_max", "float>=0", "예: 0.0 이하"),
    ("Shortage time(s)", "material_shortage_time_s", "float>=0", "예: 10초 지속 시 중단"),
]


class MaterialCatalogDialog(QDialog):
    """
    JSON 기반 Material Catalog
    - config/material_catalog.json 만 사용 (엑셀 로딩 제거)
    - 더블클릭 편집 가능
    - Save(즉시 저장) + Apply(저장+선택 반환)

    변경 목표:
    - Note 컬럼은 UI에서 제거(기존 json에 남아있을 수 있어 데이터는 보존)
    - 물질별 power ramp/control 파라미터를 여기서 편집/저장
    - 사용자가 값을 수정하지 않으면 기본값(현재 하드코딩 값)을 그대로 사용
    """

    def __init__(self, *, base_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Material Catalog")
        self.setModal(True)
        self.resize(1200, 520)

        self._base_dir = Path(base_dir)
        self._json_path = self._base_dir / "config" / "material_catalog.json"
        self._selected: Optional[MaterialRow] = None

        # ---------- UI ----------
        root = QVBoxLayout(self)

        self.infoLabel = QLabel(
            "Double-click to edit. (Material / Density / Z factor / Ramp params...)\n"
            "※ 새 파라미터를 수정하지 않아도 기본값(현재 동작값)을 자동 적용합니다."
        )
        self.infoLabel.setWordWrap(True)
        root.addWidget(self.infoLabel)

        self.table = QTableWidget(self)
        self.table.setColumnCount(len(_COLS))
        self.table.setHorizontalHeaderLabels([c[0] for c in _COLS])

        for c in range(len(_COLS)):
            item = self.table.horizontalHeaderItem(c)
            if item and len(_COLS[c]) >= 4:
                item.setToolTip(_COLS[c][3])

        hdr = self.table.horizontalHeader()
        for c in range(len(_COLS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)

        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        root.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        self.saveBtn = QPushButton("Save", self)
        self.applyBtn = QPushButton("Apply", self)
        self.cancelBtn = QPushButton("Cancel", self)

        btn_row.addWidget(self.saveBtn)
        btn_row.addWidget(self.applyBtn)
        btn_row.addWidget(self.cancelBtn)
        root.addLayout(btn_row)

        # ---------- signals ----------
        self.saveBtn.clicked.connect(self._on_save)
        self.applyBtn.clicked.connect(self._on_apply)
        self.cancelBtn.clicked.connect(self.reject)

        # ---------- load ----------
        mats = self._load_json_items()
        self._populate(mats)

    @classmethod
    def pick(cls, *, base_dir: Path, parent=None) -> Optional[MaterialRow]:
        dlg = cls(base_dir=base_dir, parent=parent)
        if dlg.exec() != QDialog.Accepted:
            return None
        return dlg.selected()

    def selected(self) -> Optional[MaterialRow]:
        return self._selected

    def _load_json_items(self) -> list[MaterialRow]:
        if not self._json_path.exists():
            QMessageBox.warning(self, "Missing file", f"파일이 없습니다:\n{self._json_path}")
            return []

        try:
            obj = json.loads(self._json_path.read_text(encoding="utf-8"))
            ver = int(obj.get("version", 0) or 0)

            if not (isinstance(obj, dict) and isinstance(obj.get("items"), list) and ver in (1, 2)):
                QMessageBox.warning(
                    self,
                    "Invalid file",
                    f"material_catalog.json 포맷이 올바르지 않습니다.\n"
                    f"기대 포맷: {{'version':1|2,'items':[...]}}\n\n경로:\n{self._json_path}",
                )
                return []

            items = obj["items"]
            mats: list[MaterialRow] = []

            for it in items:
                if not isinstance(it, dict):
                    continue

                m = _to_str(it.get("material"))
                if not m:
                    continue

                d = _to_float(it.get("density_g_cm3"), default=0.0)
                z = _to_float(it.get("z_factor"), default=0.0)

                mats.append(
                    MaterialRow(
                        material=m,
                        density_g_cm3=d,
                        z_factor=z,
                        ramp_step_dac=_to_int(it.get("ramp_step_dac"), 100),
                        ramp_seg1_max_dac=_to_int(it.get("ramp_seg1_max_dac"), 700),
                        ramp_interval_seg1_s=_to_float(it.get("ramp_interval_seg1_s"), 10.0),
                        ramp_seg2_max_dac=_to_int(it.get("ramp_seg2_max_dac"), 1500),
                        ramp_interval_seg2_s=_to_float(it.get("ramp_interval_seg2_s"), 30.0),
                        ignite_dac=_to_int(it.get("ignite_dac"), 1500),
                        ignite_rate_min=_to_float(it.get("ignite_rate_min"), 0.1),
                        ignite_timeout_s=_to_float(it.get("ignite_timeout_s"), 300.0),
                        pre_rate=_to_float(it.get("pre_rate"), 0.4),
                        pre_hold_s=_to_float(it.get("pre_hold_s"), 120.0),
                        dac_adjust_interval_s=_to_float(it.get("dac_adjust_interval_s"), 10.0),
                        fine_step_dac=_to_int(it.get("fine_step_dac"), 10),
                        material_shortage_dac=_to_int(it.get("material_shortage_dac"), 2000),
                        material_shortage_rate_max=_to_float(it.get("material_shortage_rate_max"), 0.0),
                        material_shortage_time_s=_to_float(it.get("material_shortage_time_s"), 10.0),
                        note=_to_str(it.get("note")),
                    )
                )

            return mats

        except Exception as e:
            QMessageBox.warning(self, "Load error", f"material_catalog.json 로드 실패:\n{e!r}")
            return []

    def _save_json_items(self, mats: list[MaterialRow]) -> None:
        self._json_path.parent.mkdir(parents=True, exist_ok=True)

        obj = {
            "version": 2,
            "items": [
                {
                    "material": m.material,
                    "density_g_cm3": float(m.density_g_cm3),
                    "z_factor": float(m.z_factor),

                    "ramp_step_dac": int(m.ramp_step_dac),
                    "ramp_seg1_max_dac": int(m.ramp_seg1_max_dac),
                    "ramp_interval_seg1_s": float(m.ramp_interval_seg1_s),
                    "ramp_seg2_max_dac": int(m.ramp_seg2_max_dac),
                    "ramp_interval_seg2_s": float(m.ramp_interval_seg2_s),

                    "ignite_dac": int(m.ignite_dac),
                    "ignite_rate_min": float(m.ignite_rate_min),
                    "ignite_timeout_s": float(m.ignite_timeout_s),

                    "pre_rate": float(m.pre_rate),
                    "pre_hold_s": float(m.pre_hold_s),

                    "dac_adjust_interval_s": float(m.dac_adjust_interval_s),
                    "fine_step_dac": int(m.fine_step_dac),

                    "material_shortage_dac": int(m.material_shortage_dac),
                    "material_shortage_rate_max": float(m.material_shortage_rate_max),
                    "material_shortage_time_s": float(m.material_shortage_time_s),

                    "note": m.note or "",
                }
                for m in mats
            ],
        }
        self._json_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    def _populate(self, mats: list[MaterialRow]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(mats))

        for r, m in enumerate(mats):
            values = [
                m.material,
                f"{m.density_g_cm3:g}",
                f"{m.z_factor:g}",

                str(int(m.ramp_step_dac)),
                str(int(m.ramp_seg1_max_dac)),
                f"{m.ramp_interval_seg1_s:g}",
                str(int(m.ramp_seg2_max_dac)),
                f"{m.ramp_interval_seg2_s:g}",

                str(int(m.ignite_dac)),
                f"{m.ignite_rate_min:g}",
                f"{m.ignite_timeout_s:g}",

                f"{m.pre_rate:g}",
                f"{m.pre_hold_s:g}",

                f"{m.dac_adjust_interval_s:g}",
                str(int(m.fine_step_dac)),

                str(int(m.material_shortage_dac)),
                f"{m.material_shortage_rate_max:g}",
                f"{m.material_shortage_time_s:g}",
            ]

            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                self.table.setItem(r, c, item)
                if c == 0:
                    item.setData(Qt.UserRole, m.note or "")

        self.table.blockSignals(False)
        if self.table.rowCount() > 0:
            self.table.selectRow(0)

    def _collect_rows(self) -> Optional[list[MaterialRow]]:
        mats: list[MaterialRow] = []
        _DEF = MaterialRow(material="_", density_g_cm3=1.0, z_factor=1.0)

        def cell(r: int, c: int) -> str:
            it = self.table.item(r, c)
            return it.text() if it else ""

        def row_note(r: int) -> str:
            it = self.table.item(r, 0)
            if not it:
                return ""
            return _to_str(it.data(Qt.UserRole))
        
        def _fail(row_idx: int, col_idx: int, msg: str) -> None:
            # 문제 셀로 커서 이동 + 행 선택해서 수정하기 쉽게
            self.table.setCurrentCell(row_idx, col_idx)
            self.table.selectRow(row_idx)
            QMessageBox.warning(self, "Invalid", msg)

        def _parse_float_cell(row_idx: int, col_idx: int, field: str, default: float, *, allow_blank_default: bool) -> float:
            raw = _to_str(cell(row_idx, col_idx))
            if raw == "":
                if allow_blank_default:
                    return float(default)
                _fail(row_idx, col_idx, f"{row_idx+1}행({field}): 값이 비어있습니다.")
                raise ValueError

            s = raw.replace("*", "").strip()
            try:
                v = float(s)
            except Exception:
                _fail(row_idx, col_idx, f"{row_idx+1}행({field}): '{raw}' 는 숫자가 아닙니다.")
                raise ValueError

            if not math.isfinite(v):
                _fail(row_idx, col_idx, f"{row_idx+1}행({field}): nan/inf 는 허용되지 않습니다.")
                raise ValueError
            return v

        def _parse_int_cell(row_idx: int, col_idx: int, field: str, default: int, *, allow_blank_default: bool) -> int:
            raw = _to_str(cell(row_idx, col_idx))
            if raw == "":
                if allow_blank_default:
                    return int(default)
                _fail(row_idx, col_idx, f"{row_idx+1}행({field}): 값이 비어있습니다.")
                raise ValueError

            s = raw.replace("*", "").strip()
            try:
                fv = float(s)
            except Exception:
                _fail(row_idx, col_idx, f"{row_idx+1}행({field}): '{raw}' 는 정수가 아닙니다.")
                raise ValueError

            if not math.isfinite(fv):
                _fail(row_idx, col_idx, f"{row_idx+1}행({field}): nan/inf 는 허용되지 않습니다.")
                raise ValueError

            iv = int(round(fv))
            if abs(fv - iv) > 1e-9:
                _fail(row_idx, col_idx, f"{row_idx+1}행({field}): 정수만 입력 가능합니다. (입력값: '{raw}')")
                raise ValueError

            return iv

        for r in range(self.table.rowCount()):
            try:
                material = _to_str(cell(r, 0))

                # 필수값: 빈칸이면 막기
                density = _parse_float_cell(r, 1, "Density", 0.0, allow_blank_default=False)
                zfac = _parse_float_cell(r, 2, "Z factor", 0.0, allow_blank_default=False)

                # 나머지 값들: 빈칸이면 default 사용(=기존값 유지), 형식 오류는 막기
                ramp_step_dac = _parse_int_cell(r, 3, "ramp_step_dac", _DEF.ramp_step_dac, allow_blank_default=True)
                seg1_max = _parse_int_cell(r, 4, "seg1_max_dac", _DEF.ramp_seg1_max_dac, allow_blank_default=True)
                seg1_int = _parse_float_cell(r, 5, "seg1_interval_s", _DEF.ramp_interval_seg1_s, allow_blank_default=True)
                seg2_max = _parse_int_cell(r, 6, "seg2_max_dac", _DEF.ramp_seg2_max_dac, allow_blank_default=True)
                seg2_int = _parse_float_cell(r, 7, "seg2_interval_s", _DEF.ramp_interval_seg2_s, allow_blank_default=True)

                ignite_dac = _parse_int_cell(r, 8, "ignite_dac", _DEF.ignite_dac, allow_blank_default=True)
                ignite_rate_min = _parse_float_cell(r, 9, "ignite_rate_min", _DEF.ignite_rate_min, allow_blank_default=True)
                ignite_timeout_s = _parse_float_cell(r, 10, "ignite_timeout_s", _DEF.ignite_timeout_s, allow_blank_default=True)

                pre_rate = _parse_float_cell(r, 11, "pre_rate", _DEF.pre_rate, allow_blank_default=True)
                pre_hold_s = _parse_float_cell(r, 12, "pre_hold_s", _DEF.pre_hold_s, allow_blank_default=True)

                dac_adjust_interval_s = _parse_float_cell(r, 13, "dac_adjust_interval_s", _DEF.dac_adjust_interval_s, allow_blank_default=True)
                fine_step_dac = _parse_int_cell(r, 14, "fine_step_dac", _DEF.fine_step_dac, allow_blank_default=True)

                shortage_dac = _parse_int_cell(r, 15, "material_shortage_dac", _DEF.material_shortage_dac, allow_blank_default=True)
                shortage_rate_max = _parse_float_cell(r, 16, "material_shortage_rate_max", _DEF.material_shortage_rate_max, allow_blank_default=True)
                shortage_time_s = _parse_float_cell(r, 17, "material_shortage_time_s", _DEF.material_shortage_time_s, allow_blank_default=True)

            except ValueError:
                # _fail()에서 메시지/포커싱까지 했으므로 조용히 중단
                return None

            # ✅ 범위/조건 검증: 모두 _fail로 통일 (포커스 이동 포함)

            if not material:
                _fail(r, 0, f"{r+1}행(Material): 값이 비어있습니다.")
                return None

            if density <= 0:
                _fail(r, 1, f"{r+1}행(Density): 0보다 커야 합니다.")
                return None

            if zfac <= 0:
                _fail(r, 2, f"{r+1}행(Z factor): 0보다 커야 합니다.")
                return None

            if ramp_step_dac <= 0:
                _fail(r, 3, f"{r+1}행(ramp_step_dac): 1 이상이어야 합니다.")
                return None

            # seg max dac는 각각 따로 체크해서 정확한 셀로 이동
            if seg1_max <= 0:
                _fail(r, 4, f"{r+1}행(seg1_max_dac): 1 이상이어야 합니다.")
                return None

            if seg2_max <= 0:
                _fail(r, 6, f"{r+1}행(seg2_max_dac): 1 이상이어야 합니다.")
                return None

            if seg2_max < seg1_max:
                _fail(r, 6, f"{r+1}행(seg2_max_dac): seg1_max_dac 이상이어야 합니다.")
                return None

            # seg interval도 각각 따로 체크
            if seg1_int <= 0:
                _fail(r, 5, f"{r+1}행(seg1_interval_s): 0보다 커야 합니다.")
                return None

            if seg2_int <= 0:
                _fail(r, 7, f"{r+1}행(seg2_interval_s): 0보다 커야 합니다.")
                return None

            if ignite_timeout_s <= 0:
                _fail(r, 10, f"{r+1}행(ignite_timeout_s): 0보다 커야 합니다.")
                return None

            if dac_adjust_interval_s <= 0:
                _fail(r, 13, f"{r+1}행(dac_adjust_interval_s): 0보다 커야 합니다.")
                return None

            if fine_step_dac <= 0:
                _fail(r, 14, f"{r+1}행(fine_step_dac): 1 이상이어야 합니다.")
                return None

            # shortage는 어떤 값이 문제인지 분리해서 정확한 셀로 이동
            if shortage_dac < 0:
                _fail(r, 15, f"{r+1}행(material_shortage_dac): 0 이상이어야 합니다.")
                return None

            if shortage_rate_max < 0:
                _fail(r, 16, f"{r+1}행(material_shortage_rate_max): 0 이상이어야 합니다.")
                return None

            if shortage_time_s < 0:
                _fail(r, 17, f"{r+1}행(material_shortage_time_s): 0 이상이어야 합니다.")
                return None

            mats.append(
                MaterialRow(
                    material=material,
                    density_g_cm3=density,
                    z_factor=zfac,
                    ramp_step_dac=ramp_step_dac,
                    ramp_seg1_max_dac=seg1_max,
                    ramp_interval_seg1_s=seg1_int,
                    ramp_seg2_max_dac=seg2_max,
                    ramp_interval_seg2_s=seg2_int,
                    ignite_dac=ignite_dac,
                    ignite_rate_min=ignite_rate_min,
                    ignite_timeout_s=ignite_timeout_s,
                    pre_rate=pre_rate,
                    pre_hold_s=pre_hold_s,
                    dac_adjust_interval_s=dac_adjust_interval_s,
                    fine_step_dac=fine_step_dac,
                    material_shortage_dac=shortage_dac,
                    material_shortage_rate_max=shortage_rate_max,
                    material_shortage_time_s=shortage_time_s,
                    note=row_note(r),
                )
            )

        return mats

    def _on_save(self) -> None:
        mats = self._collect_rows()
        if mats is None:
            return
        self._save_json_items(mats)
        QMessageBox.information(self, "Saved", "material_catalog.json 저장 완료")

    def _on_apply(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Select", "적용할 행을 먼저 선택하세요.")
            return

        mats = self._collect_rows()
        if mats is None:
            return

        self._save_json_items(mats)
        self._selected = mats[row]
        self.accept()