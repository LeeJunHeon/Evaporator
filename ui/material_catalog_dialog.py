# ui/material_catalog_dialog.py
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QMessageBox, QAbstractItemView, QHeaderView
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
        # "1.000*" 같은 경우 방어
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
    ("Material", "material", "str"),
    ("Density (g/cm³)", "density_g_cm3", "float>0"),
    ("Z factor", "z_factor", "float>0"),

    ("ramp_step_dac", "ramp_step_dac", "int>0"),
    ("seg1_max_dac", "ramp_seg1_max_dac", "int>0"),
    ("seg1_interval_s", "ramp_interval_seg1_s", "float>0"),
    ("seg2_max_dac", "ramp_seg2_max_dac", "int>0"),
    ("seg2_interval_s", "ramp_interval_seg2_s", "float>0"),

    ("ignite_dac", "ignite_dac", "int>=0"),
    ("ignite_rate_min", "ignite_rate_min", "float>=0"),
    ("ignite_timeout_s", "ignite_timeout_s", "float>0"),

    ("pre_rate", "pre_rate", "float>=0"),
    ("pre_hold_s", "pre_hold_s", "float>=0"),

    ("dac_adjust_interval_s", "dac_adjust_interval_s", "float>0"),
    ("fine_step_dac", "fine_step_dac", "int>0"),

    ("shortage_dac", "material_shortage_dac", "int>=0"),
    ("shortage_rate_max", "material_shortage_rate_max", "float>=0"),
    ("shortage_time_s", "material_shortage_time_s", "float>=0"),
]


class MaterialCatalogDialog(QDialog):
    """
    JSON 기반 Material Catalog
    - config/material_catalog.json 만 사용 (엑셀 로딩 제거)
    - 더블클릭 편집 가능
    - Save(즉시 저장) + Apply(저장+선택 반환)
    """

    def __init__(self, *, base_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Material Catalog")
        self.setModal(True)
        self.resize(720, 520)

        self._base_dir = Path(base_dir)
        self._json_path = self._base_dir / "config" / "material_catalog.json"
        self._selected: Optional[MaterialRow] = None
        self._loading = True

        # ---------- UI ----------
        root = QVBoxLayout(self)

        self.infoLabel = QLabel("Double-click to edit. (Material / Density / Z-Factor / Note)")
        self.infoLabel.setWordWrap(True)
        root.addWidget(self.infoLabel)

        self.table = QTableWidget(self)

        self.table.setColumnCount(len(_COLS))
        self.table.setHorizontalHeaderLabels([c[0] for c in _COLS])

        hdr = self.table.horizontalHeader()
        for c in range(len(_COLS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
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
        self._loading = False

    @classmethod
    def pick(cls, *, base_dir: Path, parent=None) -> Optional[MaterialRow]:
        dlg = cls(base_dir=base_dir, parent=parent)
        if dlg.exec() != QDialog.Accepted:
            return None
        return dlg.selected()

    def selected(self) -> Optional[MaterialRow]:
        return self._selected

    # ------------------------ req: JSON load/save
    def _load_json_items(self) -> list[MaterialRow]:
        if not self._json_path.exists():
            QMessageBox.warning(self, "Missing file", f"파일이 없습니다:\n{self._json_path}")
            return []

        try:
            obj = json.loads(self._json_path.read_text(encoding="utf-8"))

            # ✅ 포맷 고정: {"version":1, "items":[...]}만 허용
            ver = int(obj.get("version", 0) or 0)
            if not (isinstance(obj, dict) and isinstance(obj.get("items"), list) and ver in (1, 2)):
                QMessageBox.warning(self, "Invalid file",
                                    f"material_catalog.json 포맷이 올바르지 않습니다.\n"
                                    f"기대 포맷: {{'version':1,'items':[...]}} \n\n경로:\n{self._json_path}")
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
                note = _to_str(it.get("note"))
                mats.append(MaterialRow(material=m, density_g_cm3=d, z_factor=z, note=note))

            # 키가 없으면 default로
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
            "version": 1,
            "items": [
                {
                    "material": m.material,
                    "density_g_cm3": float(m.density_g_cm3),
                    "z_factor": float(m.z_factor),
                    "note": m.note or "",
                }
                for m in mats
            ],
        }
        self._json_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------------- table helpers
    def _populate(self, mats: list[MaterialRow]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(mats))
        for r, m in enumerate(mats):
            self.table.setItem(r, 0, QTableWidgetItem(m.material))
            self.table.setItem(r, 1, QTableWidgetItem(f"{m.density_g_cm3:g}"))
            self.table.setItem(r, 2, QTableWidgetItem(f"{m.z_factor:g}"))
            self.table.setItem(r, 3, QTableWidgetItem(m.note or ""))
        self.table.blockSignals(False)

        if self.table.rowCount() > 0:
            self.table.selectRow(0)

    def _collect_rows(self) -> Optional[list[MaterialRow]]:
        mats: list[MaterialRow] = []

        for r in range(self.table.rowCount()):
            material = _to_str(self.table.item(r, 0).text() if self.table.item(r, 0) else "")
            density = _to_float(self.table.item(r, 1).text() if self.table.item(r, 1) else "", default=0.0)
            zfac = _to_float(self.table.item(r, 2).text() if self.table.item(r, 2) else "", default=0.0)
            note = _to_str(self.table.item(r, 3).text() if self.table.item(r, 3) else "")

            if not material:
                QMessageBox.warning(self, "Invalid", f"{r+1}행: Material이 비어있습니다.")
                return None
            if density <= 0:
                QMessageBox.warning(self, "Invalid", f"{r+1}행: Density는 0보다 커야 합니다.")
                return None
            if zfac <= 0:
                QMessageBox.warning(self, "Invalid", f"{r+1}행: Z factor는 0보다 커야 합니다.")
                return None

            mats.append(MaterialRow(material=material, density_g_cm3=density, z_factor=zfac, note=note))

        return mats

    # ---------------- signals
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

        material = _to_str(self.table.item(row, 0).text())
        density = _to_float(self.table.item(row, 1).text(), default=0.0)
        zfac = _to_float(self.table.item(row, 2).text(), default=0.0)
        note = _to_str(self.table.item(row, 3).text() if self.table.item(row, 3) else "")

        self._selected = MaterialRow(material=material, density_g_cm3=density, z_factor=zfac, note=note)
        self.accept()
