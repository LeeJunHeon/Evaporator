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
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Material", "Density (g/cm³)", "Z factor", "Note"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
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
            if not (isinstance(obj, dict) and obj.get("version") == 1 and isinstance(obj.get("items"), list)):
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
