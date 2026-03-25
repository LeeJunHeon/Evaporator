# -*- coding: utf-8 -*-
from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import re
import statistics
from pathlib import Path
from typing import Any, Optional


def _to_float_or_none(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RunSummaryService:
    def __init__(self, *, history_store: Any, log_service: Any = None) -> None:
        self._history_store = history_store
        self._log_service = log_service

    def store_finished_run(
        self,
        *,
        run_profile: dict[str, Any],
        result: Any,
    ) -> Optional[dict[str, Any]]:
        if result is None or not run_profile:
            return None

        run_id = str(getattr(result, "run_id", "") or "").strip()
        recipe_name = str(getattr(result, "recipe_name", "") or run_profile.get("recipe_name", "") or "").strip()
        if not run_id:
            return None

        artifacts = self._find_run_artifacts(run_id=run_id, recipe_name=recipe_name)
        csv_path = artifacts.get("csv")
        if csv_path is None:
            self._log_warn(f"summary skipped: csv not found (run_id={run_id}, recipe={recipe_name})")
            return None

        summary = self.build_summary(
            csv_path=csv_path,
            log_path=artifacts.get("log"),
            run_profile=run_profile,
            result=result,
        )

        if not summary:
            return None

        try:
            if self._history_store is not None and hasattr(self._history_store, "upsert_run_summary"):
                ok = bool(self._history_store.upsert_run_summary(summary))
                if ok:
                    self._log_info(f"run summary saved: {run_id}")
                else:
                    self._log_warn(f"run summary store failed: {run_id}")
            return summary
        except Exception as exc:
            self._log_warn(f"run summary exception: {exc!r}")
            return None

    def build_summary(
        self,
        *,
        csv_path: Path,
        log_path: Optional[Path],
        run_profile: dict[str, Any],
        result: Any,
    ) -> dict[str, Any]:
        meta, rows = self._load_telemetry_csv(Path(csv_path))
        merged = self._merge_profile(meta=meta, run_profile=run_profile, result=result)
        enriched_rows = self._enrich_rows(rows, merged)

        target_rate = _to_float_or_none(merged.get("target_rate"))
        target_thickness = _to_float_or_none(merged.get("target_thickness"))
        process_config = dict(merged.get("process_config") or {})
        hw_mapping = dict(merged.get("hw_mapping") or {})

        time_to_target_s = self._first_time_rate_geq(enriched_rows, target_rate)
        time_to_stable_rate_s, stable_row = self._find_stable_rate(
            enriched_rows,
            target_rate=target_rate,
            tol_ratio=_to_float_or_none(process_config.get("rate_tol_ratio")),
            stable_sec=_to_float_or_none(process_config.get("rate_stable_sec")),
        )
        time_to_main_shutter_open_s, shutter_row = self._find_main_shutter_open(enriched_rows)

        stable_window_rows = self._stable_window_rows(
            enriched_rows,
            stable_time=time_to_stable_rate_s,
            shutter_time=time_to_main_shutter_open_s,
            stable_row=stable_row,
        )

        stable_rate_mean, stable_rate_std = self._series_stats(stable_window_rows, "rate")
        stable_dac_mean, stable_dac_std = self._series_stats(stable_window_rows, "selected_dac")
        stable_adc_mean, stable_adc_std = self._series_stats(stable_window_rows, "selected_adc")

        overshoot_peak, overshoot_ratio_peak = self._overshoot_metrics(
            enriched_rows,
            target_rate=target_rate,
            start_time=(time_to_main_shutter_open_s or time_to_stable_rate_s),
        )
        spike_count, spike_max_abs = self._spike_metrics(
            enriched_rows,
            target_rate=target_rate,
            tol_ratio=_to_float_or_none(process_config.get("rate_tol_ratio")),
            start_time=(time_to_stable_rate_s or 0.0),
        )

        final_thickness = self._last_value(enriched_rows, "thickness")
        thickness_error = None
        thickness_error_ratio = None
        if final_thickness is not None and target_thickness and target_thickness > 0:
            thickness_error = final_thickness - target_thickness
            thickness_error_ratio = thickness_error / target_thickness

        result_status, fail_reason = self._result_status_and_reason(
            result=result,
            log_path=log_path,
        )

        started_ts = _to_float_or_none(getattr(result, "started_ts", None))
        finished_ts = _to_float_or_none(getattr(result, "finished_ts", None))
        total_run_time_s = None
        if started_ts is not None and finished_ts is not None and finished_ts >= started_ts:
            total_run_time_s = finished_ts - started_ts
        if total_run_time_s is None:
            total_run_time_s = self._last_value(enriched_rows, "elapsed_sec")

        summary = {
            "run_id": str(getattr(result, "run_id", "") or merged.get("run_id", "") or "").strip(),
            "recipe_name": str(getattr(result, "recipe_name", "") or merged.get("recipe_name", "") or "").strip(),
            "process_name": str(merged.get("process_name", "") or getattr(result, "recipe_name", "") or "").strip(),
            "material_name": str(merged.get("material_name", "") or "").strip(),
            "density": _to_float_or_none(merged.get("density")),
            "z_factor": _to_float_or_none(merged.get("z_factor")),
            "target_rate": target_rate,
            "target_thickness": target_thickness,
            "delay_min": _to_float_or_none(merged.get("delay_min")),
            "use_power1": 1 if bool(merged.get("use_power1", False)) else 0,
            "use_power2": 1 if bool(merged.get("use_power2", False)) else 0,
            "hw_mapping_json": hw_mapping,
            "process_config_json": process_config,
            "process_config_hash": str(merged.get("process_config_hash", "") or self._process_config_hash(process_config)),
            "started_ts": started_ts,
            "finished_ts": finished_ts,
            "result_status": result_status,
            "fail_reason": fail_reason,
            "time_to_target_s": time_to_target_s,
            "time_to_stable_rate_s": time_to_stable_rate_s,
            "time_to_main_shutter_open_s": time_to_main_shutter_open_s,
            "total_run_time_s": total_run_time_s,
            "stable_rate_mean": stable_rate_mean,
            "stable_rate_std": stable_rate_std,
            "stable_dac_mean": stable_dac_mean,
            "stable_dac_std": stable_dac_std,
            "stable_adc_mean": stable_adc_mean,
            "stable_adc_std": stable_adc_std,
            "dac_at_stable_reached": self._row_value(stable_row, "selected_dac"),
            "adc_at_stable_reached": self._row_value(stable_row, "selected_adc"),
            "dac_at_shutter_open": self._row_value(shutter_row, "selected_dac"),
            "adc_at_shutter_open": self._row_value(shutter_row, "selected_adc"),
            "overshoot_peak": overshoot_peak,
            "overshoot_ratio_peak": overshoot_ratio_peak,
            "spike_count": spike_count,
            "spike_max_abs": spike_max_abs,
            "final_thickness_A": final_thickness,
            "thickness_error_A": thickness_error,
            "thickness_error_ratio": thickness_error_ratio,
            "sensor_none_duration_s": self._missing_duration(
                enriched_rows,
                lambda row: row.get("rate") is None or row.get("thickness") is None,
            ),
            "adc_none_duration_s": self._missing_duration(
                enriched_rows,
                lambda row: row.get("selected_adc") is None,
            ),
            "ramp_step_count_used": self._ramp_step_count_used(enriched_rows),
            "stable_reached_in_step_index": self._stable_step_index(
                enriched_rows,
                stable_time=time_to_stable_rate_s,
            ),
            "dac_first_nonzero": self._first_positive_value(enriched_rows, "selected_dac"),
            "adc_first_nonzero": self._first_positive_value(enriched_rows, "selected_adc"),
        }

        return summary

    def _find_run_artifacts(self, *, run_id: str, recipe_name: str) -> dict[str, Optional[Path]]:
        if self._log_service is not None and hasattr(self._log_service, "find_run_artifacts"):
            with contextlib.suppress(Exception):
                return dict(self._log_service.find_run_artifacts(run_id, recipe_name) or {})
        return {"csv": None, "log": None}

    def _load_telemetry_csv(self, path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        meta: dict[str, Any] = {}
        rows: list[dict[str, Any]] = []

        with open(path, "r", encoding="utf-8-sig", newline="") as fp:
            raw_lines = fp.readlines()

        header_idx = None
        for idx, line in enumerate(raw_lines):
            stripped = line.strip()
            if stripped.startswith("# "):
                key, sep, value = stripped[2:].partition("=")
                if sep:
                    meta[key.strip()] = self._decode_meta_value(value.strip())
                continue
            if stripped.startswith("time,elapsed_sec,"):
                header_idx = idx
                break

        if header_idx is None:
            return meta, rows

        reader = csv.DictReader(raw_lines[header_idx:])
        for row in reader:
            rows.append(dict(row or {}))

        return meta, rows

    def _decode_meta_value(self, value: str) -> Any:
        if not value:
            return ""
        with contextlib.suppress(Exception):
            return json.loads(value)
        return value

    def _merge_profile(self, *, meta: dict[str, Any], run_profile: dict[str, Any], result: Any) -> dict[str, Any]:
        merged = dict(meta or {})
        merged.update(dict(run_profile or {}))

        merged.setdefault("run_id", str(getattr(result, "run_id", "") or ""))
        merged.setdefault("recipe_name", str(getattr(result, "recipe_name", "") or merged.get("process_name", "") or ""))
        merged.setdefault("process_name", str(merged.get("process_name", "") or merged.get("recipe_name", "") or ""))
        merged.setdefault("hw_mapping", dict(merged.get("hw_mapping") or {}))
        merged.setdefault("process_config", dict(merged.get("process_config") or {}))
        merged.setdefault("process_config_hash", self._process_config_hash(merged.get("process_config") or {}))
        return merged

    def _enrich_rows(self, rows: list[dict[str, Any]], merged: dict[str, Any]) -> list[dict[str, Any]]:
        use_power1 = bool(merged.get("use_power1", False))
        use_power2 = bool(merged.get("use_power2", False))
        power1_feedback_adc2 = bool((merged.get("hw_mapping") or {}).get("power1_feedback_adc2", False))

        enriched: list[dict[str, Any]] = []

        for row in rows:
            item = {
                "elapsed_sec": _to_float_or_none(row.get("elapsed_sec")),
                "pressure": _to_float_or_none(row.get("pressure_torr")),
                "dac1": _to_float_or_none(row.get("dac1")),
                "adc1": _to_float_or_none(row.get("adc1")),
                "dac2": _to_float_or_none(row.get("dac2")),
                "adc2": _to_float_or_none(row.get("adc2")),
                "rate": _to_float_or_none(row.get("dep.rate")),
                "thickness": _to_float_or_none(row.get("thickness_A")),
                "step": str(row.get("step", "") or "").strip(),
                "detail": str(row.get("detail", "") or "").strip(),
            }

            item["selected_dac"] = self._selected_dac_total(item, use_power1=use_power1, use_power2=use_power2)
            item["selected_adc"] = self._selected_adc_total(
                item,
                use_power1=use_power1,
                use_power2=use_power2,
                power1_feedback_adc2=power1_feedback_adc2,
            )
            enriched.append(item)

        return [row for row in enriched if row.get("elapsed_sec") is not None]

    def _selected_dac_total(self, row: dict[str, Any], *, use_power1: bool, use_power2: bool) -> Optional[float]:
        values: list[float] = []
        if use_power1 and row.get("dac1") is not None:
            values.append(float(row["dac1"]))
        if use_power2 and row.get("dac2") is not None:
            values.append(float(row["dac2"]))
        if not values:
            return None
        return sum(values)

    def _selected_adc_total(
        self,
        row: dict[str, Any],
        *,
        use_power1: bool,
        use_power2: bool,
        power1_feedback_adc2: bool,
    ) -> Optional[float]:
        if power1_feedback_adc2 and use_power1 and (not use_power2):
            return _to_float_or_none(row.get("adc2"))

        values: list[float] = []
        if use_power1 and row.get("adc1") is not None:
            values.append(float(row["adc1"]))
        if use_power2 and row.get("adc2") is not None:
            values.append(float(row["adc2"]))
        if not values:
            return None
        return sum(values)

    def _first_time_rate_geq(self, rows: list[dict[str, Any]], target_rate: Optional[float]) -> Optional[float]:
        if target_rate is None:
            return None
        for row in rows:
            rate = row.get("rate")
            if rate is not None and float(rate) >= float(target_rate):
                return _to_float_or_none(row.get("elapsed_sec"))
        return None

    def _find_stable_rate(
        self,
        rows: list[dict[str, Any]],
        *,
        target_rate: Optional[float],
        tol_ratio: Optional[float],
        stable_sec: Optional[float],
    ) -> tuple[Optional[float], Optional[dict[str, Any]]]:
        if target_rate is None or tol_ratio is None:
            return None, None

        tolerance = abs(float(target_rate)) * float(tol_ratio)
        required_sec = max(0.0, float(stable_sec or 0.0))
        start_elapsed: Optional[float] = None

        for row in rows:
            elapsed = _to_float_or_none(row.get("elapsed_sec"))
            rate = row.get("rate")
            if elapsed is None or rate is None:
                start_elapsed = None
                continue

            if abs(float(rate) - float(target_rate)) <= tolerance:
                if start_elapsed is None:
                    start_elapsed = elapsed
                if required_sec <= 0.0 or (elapsed - start_elapsed) >= required_sec:
                    return elapsed, row
            else:
                start_elapsed = None

        return None, None

    def _find_main_shutter_open(self, rows: list[dict[str, Any]]) -> tuple[Optional[float], Optional[dict[str, Any]]]:
        for row in rows:
            detail = str(row.get("detail", "") or "")
            if "WRITE_COIL MAIN_SHUTTER_SW=1" in detail or "tag=EVAP_MAIN_SHUTTER_OPEN" in detail:
                return _to_float_or_none(row.get("elapsed_sec")), row
        return None, None

    def _stable_window_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        stable_time: Optional[float],
        shutter_time: Optional[float],
        stable_row: Optional[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if stable_time is None:
            return []

        selected = [
            row
            for row in rows
            if _to_float_or_none(row.get("elapsed_sec")) is not None
            and float(row["elapsed_sec"]) >= float(stable_time)
            and (shutter_time is None or float(row["elapsed_sec"]) <= float(shutter_time))
        ]
        if selected:
            return selected
        return [stable_row] if stable_row is not None else []

    def _series_stats(self, rows: list[dict[str, Any]], key: str) -> tuple[Optional[float], Optional[float]]:
        values = [float(row[key]) for row in rows if row.get(key) is not None]
        if not values:
            return None, None
        if len(values) == 1:
            return values[0], 0.0
        return statistics.mean(values), statistics.pstdev(values)

    def _overshoot_metrics(
        self,
        rows: list[dict[str, Any]],
        *,
        target_rate: Optional[float],
        start_time: Optional[float],
    ) -> tuple[Optional[float], Optional[float]]:
        if target_rate is None or start_time is None or target_rate <= 0:
            return None, None

        overshoots = [
            max(0.0, float(row["rate"]) - float(target_rate))
            for row in rows
            if row.get("rate") is not None
            and row.get("elapsed_sec") is not None
            and float(row["elapsed_sec"]) >= float(start_time)
        ]
        if not overshoots:
            return None, None

        peak = max(overshoots)
        return peak, (peak / float(target_rate)) if target_rate else None

    def _spike_metrics(
        self,
        rows: list[dict[str, Any]],
        *,
        target_rate: Optional[float],
        tol_ratio: Optional[float],
        start_time: float,
    ) -> tuple[Optional[int], Optional[float]]:
        series = [
            float(row["rate"])
            for row in rows
            if row.get("rate") is not None
            and row.get("elapsed_sec") is not None
            and float(row["elapsed_sec"]) >= float(start_time)
        ]
        if len(series) < 2:
            return None, None

        threshold = max(abs(float(target_rate or 0.0)) * float(tol_ratio or 0.05) * 2.0, 0.05)
        deltas = [abs(curr - prev) for prev, curr in zip(series, series[1:])]
        if not deltas:
            return None, None

        spike_count = sum(1 for delta in deltas if delta >= threshold)
        return spike_count, max(deltas)

    def _missing_duration(self, rows: list[dict[str, Any]], predicate: Any) -> Optional[float]:
        if not rows:
            return None

        intervals = [
            float(curr["elapsed_sec"]) - float(prev["elapsed_sec"])
            for prev, curr in zip(rows, rows[1:])
            if prev.get("elapsed_sec") is not None
            and curr.get("elapsed_sec") is not None
            and float(curr["elapsed_sec"]) > float(prev["elapsed_sec"])
        ]
        default_interval = statistics.median(intervals) if intervals else 1.0
        total = 0.0

        for idx, row in enumerate(rows):
            if not predicate(row):
                continue

            elapsed = _to_float_or_none(row.get("elapsed_sec"))
            if elapsed is None:
                continue

            if idx + 1 < len(rows) and rows[idx + 1].get("elapsed_sec") is not None:
                delta = float(rows[idx + 1]["elapsed_sec"]) - elapsed
            else:
                delta = default_interval

            if delta <= 0:
                delta = default_interval
            total += delta

        return total

    def _ramp_step_count_used(self, rows: list[dict[str, Any]]) -> Optional[int]:
        max_step = 0
        for row in rows:
            for text in (row.get("step"), row.get("detail")):
                step_no = self._extract_ramp_step_no(text)
                if step_no is not None:
                    max_step = max(max_step, step_no)
        return max_step or None

    def _stable_step_index(self, rows: list[dict[str, Any]], *, stable_time: Optional[float]) -> Optional[int]:
        if stable_time is None:
            return None

        last_step_no: Optional[int] = None
        for row in rows:
            elapsed = _to_float_or_none(row.get("elapsed_sec"))
            if elapsed is None or elapsed > stable_time:
                break

            for text in (row.get("step"), row.get("detail")):
                step_no = self._extract_ramp_step_no(text)
                if step_no is not None:
                    last_step_no = step_no

        if last_step_no is None:
            return None
        return max(0, int(last_step_no) - 1)

    def _extract_ramp_step_no(self, text: Any) -> Optional[int]:
        s = str(text or "").strip()
        match = re.search(r"RAMP STEP\s+(\d+)\s*/", s)
        if match:
            return int(match.group(1))

        match = re.search(r"EVAP_RAMP_STEP(\d+)", s)
        if match:
            return int(match.group(1))

        return None

    def _first_positive_value(self, rows: list[dict[str, Any]], key: str) -> Optional[float]:
        for row in rows:
            value = row.get(key)
            if value is not None and float(value) > 0:
                return float(value)
        return None

    def _last_value(self, rows: list[dict[str, Any]], key: str) -> Optional[float]:
        for row in reversed(rows):
            value = row.get(key)
            if value is not None:
                return float(value)
        return None

    def _row_value(self, row: Optional[dict[str, Any]], key: str) -> Optional[float]:
        if row is None:
            return None
        return _to_float_or_none(row.get(key))

    def _result_status_and_reason(self, *, result: Any, log_path: Optional[Path]) -> tuple[str, str]:
        log_text = ""
        if log_path is not None and Path(log_path).exists():
            with contextlib.suppress(Exception):
                log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")

        stop_match = re.search(r"RUN STOPPED .* mode=([A-Z]+)", log_text)
        if stop_match:
            return "stopped", stop_match.group(1)

        if "RUN FINISHED OK" in log_text or bool(getattr(result, "ok", False)):
            return "success", ""

        err = getattr(result, "error", None)
        if err is not None:
            where = str(getattr(err, "where", "") or "").strip()
            message = str(getattr(err, "message", "") or "").strip()
            if where and message:
                return "fail", f"{where}: {message}"
            return "fail", where or message

        if "RUN ERROR" in log_text:
            match = re.search(r"RUN ERROR .* error=(.*)$", log_text, re.MULTILINE)
            return "fail", str(match.group(1)).strip() if match else ""

        return "abnormal_end", ""

    def _process_config_hash(self, process_config: dict[str, Any]) -> str:
        if not process_config:
            return ""
        payload = _canonical_json(process_config).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _log_info(self, message: str) -> None:
        if self._log_service is not None and hasattr(self._log_service, "info"):
            with contextlib.suppress(Exception):
                self._log_service.info(message, tag="HISTORY", also_ui=False)

    def _log_warn(self, message: str) -> None:
        if self._log_service is not None and hasattr(self._log_service, "warn"):
            with contextlib.suppress(Exception):
                self._log_service.warn(message, tag="HISTORY", also_ui=False)
