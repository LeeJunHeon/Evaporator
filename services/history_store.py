# -*- coding: utf-8 -*-
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import tempfile
from pathlib import Path
from typing import Any, Optional


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _default_history_root(name: str) -> Path:
    return Path(tempfile.gettempdir()) / name


class HistoryStore:
    _SUMMARY_COLUMNS: tuple[tuple[str, str], ...] = (
        ("run_id", "TEXT PRIMARY KEY"),
        ("recipe_name", "TEXT"),
        ("process_name", "TEXT"),
        ("material_name", "TEXT"),
        ("density", "REAL"),
        ("z_factor", "REAL"),
        ("target_rate", "REAL"),
        ("target_thickness", "REAL"),
        ("delay_min", "REAL"),
        ("use_power1", "INTEGER"),
        ("use_power2", "INTEGER"),
        ("hw_mapping_json", "TEXT"),
        ("process_config_json", "TEXT"),
        ("process_config_hash", "TEXT"),
        ("started_ts", "REAL"),
        ("finished_ts", "REAL"),
        ("result_status", "TEXT"),
        ("fail_reason", "TEXT"),
        ("time_to_target_s", "REAL"),
        ("time_to_stable_rate_s", "REAL"),
        ("time_to_main_shutter_open_s", "REAL"),
        ("total_run_time_s", "REAL"),
        ("stable_rate_mean", "REAL"),
        ("stable_rate_std", "REAL"),
        ("stable_dac_mean", "REAL"),
        ("stable_dac_std", "REAL"),
        ("stable_adc_mean", "REAL"),
        ("stable_adc_std", "REAL"),
        ("dac_at_stable_reached", "REAL"),
        ("adc_at_stable_reached", "REAL"),
        ("dac_at_shutter_open", "REAL"),
        ("adc_at_shutter_open", "REAL"),
        ("overshoot_peak", "REAL"),
        ("overshoot_ratio_peak", "REAL"),
        ("spike_count", "INTEGER"),
        ("spike_max_abs", "REAL"),
        ("final_thickness_A", "REAL"),
        ("thickness_error_A", "REAL"),
        ("thickness_error_ratio", "REAL"),
        ("sensor_none_duration_s", "REAL"),
        ("adc_none_duration_s", "REAL"),
        ("ramp_step_count_used", "INTEGER"),
        ("stable_reached_in_step_index", "INTEGER"),
        ("dac_first_nonzero", "REAL"),
        ("adc_first_nonzero", "REAL"),
        ("configured_start_dac", "REAL"),
        ("initial_dac", "REAL"),
        ("initial_dac_source", "TEXT"),
        ("applied_recommended_start_dac", "INTEGER"),
        ("created_ts", "REAL"),
    )

    def __init__(
        self,
        *,
        primary_root: Path,
        fallback_root: Optional[Path] = None,
        subdir: str = "ProcessHistory",
        db_name: str = "run_history.sqlite",
    ) -> None:
        self._primary_root = Path(primary_root)
        self._fallback_root = Path(fallback_root) if fallback_root is not None else None
        self._subdir = str(subdir)
        self._db_name = str(db_name)

    @classmethod
    def from_log_service(
        cls,
        log_service: Any,
        *,
        subdir: str = "ProcessHistory",
        db_name: str = "run_history.sqlite",
    ) -> "HistoryStore":
        roots = {}
        if log_service is not None and hasattr(log_service, "get_storage_roots"):
            with contextlib.suppress(Exception):
                roots = dict(log_service.get_storage_roots(force_resolve=False) or {})

        primary_root = Path(roots.get("base") or _default_history_root("Evaporator_Logs"))
        fallback_root = Path(roots.get("fallback") or _default_history_root("Evaporator_Logs_local"))
        return cls(
            primary_root=primary_root,
            fallback_root=fallback_root,
            subdir=subdir,
            db_name=db_name,
        )

    def storage_roots(self) -> list[Path]:
        return list(self._roots_for_query())

    def _db_path_for_root(self, root: Path) -> Path:
        return Path(root) / self._subdir / self._db_name

    def _write_db_path(self) -> Optional[Path]:
        for root in self._roots_for_query():
            path = self._db_path_for_root(root)
            try:
                _ensure_dir(path.parent)
                with sqlite3.connect(path, timeout=2.0) as conn:
                    self._ensure_schema(conn)
                return path
            except Exception:
                continue
        return None

    def _roots_for_query(self) -> list[Path]:
        roots: list[Path] = []
        for root in (self._primary_root, self._fallback_root):
            if root is None:
                continue
            root = Path(root)
            if root not in roots:
                roots.append(root)
        return roots

    def _db_paths_for_query(self) -> list[Path]:
        paths: list[Path] = []
        for root in self._roots_for_query():
            path = self._db_path_for_root(root)
            if path not in paths:
                paths.append(path)
        return paths

    def _connect(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        columns_sql = ",\n                ".join(
            f"{name} {type_sql}" for name, type_sql in self._SUMMARY_COLUMNS
        )
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS run_summaries (
                {columns_sql}
            );

            CREATE INDEX IF NOT EXISTS idx_run_summaries_result_status
            ON run_summaries(result_status);

            CREATE INDEX IF NOT EXISTS idx_run_summaries_material
            ON run_summaries(material_name, result_status);

            CREATE INDEX IF NOT EXISTS idx_run_summaries_finished_ts
            ON run_summaries(finished_ts);

            CREATE INDEX IF NOT EXISTS idx_run_summaries_cfg_hash
            ON run_summaries(process_config_hash);
            """
        )
        self._ensure_columns(conn)
        conn.commit()

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        existing = {
            str((row["name"] if isinstance(row, sqlite3.Row) else row[1]) or "").strip()
            for row in conn.execute("PRAGMA table_info(run_summaries)").fetchall()
        }
        for name, type_sql in self._SUMMARY_COLUMNS:
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE run_summaries ADD COLUMN {name} {type_sql}")

    def upsert_run_summary(self, summary: dict[str, Any]) -> bool:
        db_path = self._write_db_path()
        if db_path is None:
            return False

        payload = self._normalize_summary(summary)
        columns = list(payload.keys())
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT OR REPLACE INTO run_summaries ({', '.join(columns)}) VALUES ({placeholders})"

        with self._connect(db_path) as conn:
            conn.execute(sql, [payload[col] for col in columns])
            conn.commit()
        return True

    def get_run_summary(self, run_id: str) -> Optional[dict[str, Any]]:
        key = str(run_id or "").strip()
        if not key:
            return None

        merged: Optional[dict[str, Any]] = None
        for db_path in self._db_paths_for_query():
            if not db_path.exists():
                continue
            try:
                with self._connect(db_path) as conn:
                    row = conn.execute(
                        "SELECT * FROM run_summaries WHERE run_id = ?",
                        [key],
                    ).fetchone()
                if row is None:
                    continue
                data = self._decode_row(dict(row))
                if merged is None or float(data.get("finished_ts") or 0.0) >= float(merged.get("finished_ts") or 0.0):
                    merged = data
            except Exception:
                continue
        return merged

    def fetch_run_summaries(
        self,
        *,
        result_status: Optional[str] = None,
        material_name: Optional[str] = None,
        use_power1: Optional[bool] = None,
        use_power2: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}

        for db_path in self._db_paths_for_query():
            if not db_path.exists():
                continue

            try:
                with self._connect(db_path) as conn:
                    sql = "SELECT * FROM run_summaries WHERE 1=1"
                    params: list[Any] = []

                    if result_status is not None:
                        sql += " AND result_status = ?"
                        params.append(str(result_status))
                    if material_name is not None:
                        sql += " AND material_name = ?"
                        params.append(str(material_name))
                    if use_power1 is not None:
                        sql += " AND use_power1 = ?"
                        params.append(1 if use_power1 else 0)
                    if use_power2 is not None:
                        sql += " AND use_power2 = ?"
                        params.append(1 if use_power2 else 0)

                    sql += " ORDER BY finished_ts DESC"

                    for row in conn.execute(sql, params).fetchall():
                        data = self._decode_row(dict(row))
                        run_id = str(data.get("run_id") or "").strip()
                        if not run_id:
                            continue

                        prev = merged.get(run_id)
                        if prev is None or float(data.get("finished_ts") or 0.0) >= float(prev.get("finished_ts") or 0.0):
                            merged[run_id] = data
            except Exception:
                continue

        return list(merged.values())

    def _normalize_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        payload = dict(summary or {})
        payload.setdefault("created_ts", time.time())

        for key in ("hw_mapping_json", "process_config_json"):
            value = payload.get(key)
            if isinstance(value, (dict, list, tuple)):
                payload[key] = _json_text(value)
            elif value is None:
                payload[key] = ""

        if not payload.get("run_id"):
            raise ValueError("run_id is required for history summary")

        return payload

    def _decode_row(self, row: dict[str, Any]) -> dict[str, Any]:
        for key in ("hw_mapping_json", "process_config_json"):
            text = row.get(key)
            if isinstance(text, str) and text.strip():
                with contextlib.suppress(Exception):
                    row[key[:-5]] = json.loads(text)
        return row
