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


# MODIFIED: 컬럼 정의 확장 (key=None 인 항목은 구분선 컬럼으로 편집 불가)
MATERIAL_PARAMS = [
    ("Material",         "material",      "str",     "물질 이름 (예: Al, Au, SiO2)"),
    ("Density (g/cm³)", "density_g_cm3", "float>0", "STM density. 0보다 커야 합니다."),
    ("Z factor",         "z_factor",      "float>0", "STM Z factor. 0보다 커야 합니다."),
]


class MaterialCatalogDialog(QDialog):
    """
    JSON 기반 Material Catalog
    - config/material_catalog.json 만 사용
    - 더블클릭 편집 가능
    - Save(즉시 저장) + Apply(저장+선택 반환)

    목적:
    - Source 버튼에서는 material / density / z_factor 만 관리
    - note는 UI에 표시하지 않지만 기존 데이터 호환용으로 보존 가능
    - 공정 관련 ramp/control 파라미터는 Config dialog에서 관리
    """

    def __init__(self, *, base_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Material Catalog")
        self.setModal(True)
        self.resize(620, 520)
        self.setMinimumWidth(560)

        self._base_dir = Path(base_dir)
        self._json_path = self._base_dir / "config" / "material_catalog.json"
        self._selected: Optional[MaterialRow] = None

        # ---------- UI ----------
        root = QVBoxLayout(self)

        self.infoLabel = QLabel(
            "Double-click to edit. (Material / Density / Z factor)\n"
            "※ Source 버튼에서는 재료 상수만 관리합니다."
        )
        self.infoLabel.setWordWrap(True)
        self.infoLabel.setText(
            "Double-click to edit. (Material / Density / Z factor)\n"
            "Source 버튼에서는 재료 상수만 관리합니다."
        )
        self.infoLabel.setText(
            "Double-click to edit. (Material / Density / Z factor)\n"
            "Source only manages material constants."
        )
        root.addWidget(self.infoLabel)

        self.table = QTableWidget(self)
        self.table.setColumnCount(len(MATERIAL_PARAMS))
        self.table.setHorizontalHeaderLabels([c[0] for c in MATERIAL_PARAMS])

        # MODIFIED: 구분선 컬럼 헤더도 회색으로 표시 (toolTip 포함)
        hdr = self.table.horizontalHeader()
        for c in range(len(MATERIAL_PARAMS)):
            item = self.table.horizontalHeaderItem(c)
            if item and len(MATERIAL_PARAMS[c]) >= 4:
                item.setToolTip(MATERIAL_PARAMS[c][3])
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)

        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
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
        self._adjust_dialog_width_to_contents()

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
                        note=_to_str(it.get("note")),
                    )
                )

            return mats

        except Exception as e:
            QMessageBox.warning(self, "Load error", f"material_catalog.json 로드 실패:\n{e!r}")
            return []

    def _save_json_items(self, mats: list[MaterialRow]) -> None:
        self._json_path.parent.mkdir(parents=True, exist_ok=True)

        def _item_dict(m: MaterialRow) -> dict:
            return {
                "material": m.material,
                "density_g_cm3": float(m.density_g_cm3),
                "z_factor": float(m.z_factor),
                "note": m.note or "",
            }

        obj = {
            "version": 2,
            "items": [_item_dict(m) for m in mats],
        }
        self._json_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    def _populate(self, mats: list[MaterialRow]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(mats))

        for r, m in enumerate(mats):
            for c, col_def in enumerate(MATERIAL_PARAMS):
                col_key = col_def[1]
                val = getattr(m, col_key, None)

                if c == 0:
                    text = str(val) if val is not None else ""
                else:
                    text = f"{float(val):g}" if val is not None else ""

                item = QTableWidgetItem(text)
                self.table.setItem(r, c, item)
                if c == 0:
                    item.setData(Qt.UserRole, m.note or "")

        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        if self.table.rowCount() > 0:
            self.table.selectRow(0)

    def _adjust_dialog_width_to_contents(self) -> None:
        self.table.resizeColumnsToContents()

        table_width = int(self.table.frameWidth()) * 2
        if self.table.verticalHeader().isVisible():
            table_width += int(self.table.verticalHeader().width())
        for col in range(self.table.columnCount()):
            table_width += int(self.table.columnWidth(col))

        table_width += int(self.table.verticalScrollBar().sizeHint().width()) + 12

        button_width = (
            int(self.saveBtn.sizeHint().width())
            + int(self.applyBtn.sizeHint().width())
            + int(self.cancelBtn.sizeHint().width())
            + 48
        )

        layout = self.layout()
        margins = layout.contentsMargins() if layout is not None else None
        side_margins = (int(margins.left()) + int(margins.right())) if margins is not None else 24

        target_width = max(table_width + side_margins + 24, button_width + side_margins + 24, 560)
        target_width = min(target_width, 700)
        self.resize(int(target_width), max(520, int(self.height())))

    def _collect_rows(self) -> Optional[list[MaterialRow]]:
        mats: list[MaterialRow] = []

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

        for r in range(self.table.rowCount()):
            try:
                material = _to_str(cell(r, 0))

                # 필수값: 빈칸이면 막기
                density = _parse_float_cell(r, 1, "Density", 0.0, allow_blank_default=False)
                zfac = _parse_float_cell(r, 2, "Z factor", 0.0, allow_blank_default=False)

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

            mats.append(
                MaterialRow(
                    material=material,
                    density_g_cm3=density,
                    z_factor=zfac,
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
