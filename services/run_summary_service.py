# -*- coding: utf-8 -*-
from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import re
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from services.storage_paths import default_temp_log_root


def _to_float_or_none(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_timestamp(value: Any) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        with contextlib.suppress(Exception):
            return datetime.strptime(text, fmt).timestamp()
    with contextlib.suppress(Exception):
        return float(text)
    return None


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

    def backfill_history(self) -> dict[str, Any]:
        stats = {
            "scanned": 0,
            "unchanged_skipped": 0,
            "changed": 0,
            "stored": 0,
            "updated": 0,
            "failed": 0,
            "errors": [],
            "skipped": 0,
        }

        if self._history_store is None or not hasattr(self._history_store, "upsert_run_summary"):
            stats["failed"] = 1
            stats["errors"].append("history store is not available")
            return stats

        try:
            if self._log_service is not None and hasattr(self._log_service, "flush"):
                with contextlib.suppress(Exception):
                    self._log_service.flush(timeout_ms=4000)

            artifacts = self._scan_history_artifacts()
            stats["scanned"] = len(artifacts)

            for item in artifacts:
                try:
                    artifact_state = self._artifact_state_payload(item)
                    existing_state = None
                    if hasattr(self._history_store, "get_backfill_artifact"):
                        existing_state = self._history_store.get_backfill_artifact(str(item.get("stem", "") or ""))

                    existing_run_id = str((existing_state or {}).get("run_id", "") or "").strip()
                    if existing_state and self._artifact_state_matches(existing_state, artifact_state):
                        if not existing_run_id or self._history_store.get_run_summary(existing_run_id) is not None:
                            stats["unchanged_skipped"] += 1
                            continue

                    stats["changed"] += 1
                    summary = self._build_backfill_summary(item)
                    if not summary:
                        stats["failed"] += 1
                        stats["errors"].append(f"summary build failed: {item.get('stem', '')}")
                        continue

                    run_id = str(summary.get("run_id", "") or "").strip()
                    if not run_id:
                        stats["failed"] += 1
                        stats["errors"].append(f"missing run_id for {item.get('stem', '')}")
                        continue

                    existing = None
                    if self._history_store is not None and hasattr(self._history_store, "get_run_summary"):
                        existing = self._history_store.get_run_summary(run_id)

                    if existing is not None and self._summaries_equivalent(existing, summary):
                        artifact_state["run_id"] = run_id
                        artifact_state["processed_ts"] = time.time()
                        if hasattr(self._history_store, "upsert_backfill_artifact"):
                            self._history_store.upsert_backfill_artifact(artifact_state)
                        continue

                    ok = bool(self._history_store.upsert_run_summary(summary))
                    if not ok:
                        stats["failed"] += 1
                        stats["errors"].append(f"store failed: {run_id}")
                        continue

                    if existing is None:
                        stats["stored"] += 1
                    else:
                        stats["updated"] += 1

                    artifact_state["run_id"] = run_id
                    artifact_state["processed_ts"] = time.time()
                    if hasattr(self._history_store, "upsert_backfill_artifact"):
                        self._history_store.upsert_backfill_artifact(artifact_state)
                except Exception as exc:
                    stats["failed"] += 1
                    stats["errors"].append(f"{item.get('stem', '')}: {exc!r}")

            stats["skipped"] = stats["unchanged_skipped"]
            self._log_info(
                "history sync complete: "
                f"scanned={stats['scanned']} unchanged={stats['unchanged_skipped']} "
                f"changed={stats['changed']} stored={stats['stored']} "
                f"updated={stats['updated']} failed={stats['failed']}"
            )
            return stats
        except Exception as exc:
            stats["failed"] += 1
            stats["errors"].append(repr(exc))
            self._log_warn(f"history backfill failed: {exc!r}")
            return stats

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
        time_to_main_shutter_close_s = self._find_main_shutter_close(enriched_rows)

        # shutter_close가 shutter_open 이전이면 (초기화 close일 가능성) 무시
        if (
            time_to_main_shutter_close_s is not None
            and time_to_main_shutter_open_s is not None
            and time_to_main_shutter_close_s <= time_to_main_shutter_open_s
        ):
            time_to_main_shutter_close_s = None

        stable_window_rows = self._stable_window_rows(
            enriched_rows,
            stable_time=time_to_stable_rate_s,
            shutter_time=time_to_main_shutter_open_s,
            shutter_close_time=time_to_main_shutter_close_s,
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
        if started_ts is None:
            started_ts = _parse_timestamp(merged.get("started_ts"))
        if started_ts is None:
            started_ts = _parse_timestamp(merged.get("opened_at"))
        finished_ts = _to_float_or_none(getattr(result, "finished_ts", None))
        if finished_ts is None:
            finished_ts = _parse_timestamp(merged.get("finished_ts"))
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
            "configured_start_dac": _to_float_or_none(merged.get("configured_start_dac")),
            "initial_dac": _to_float_or_none(merged.get("initial_dac")),
            "initial_dac_source": str(merged.get("initial_dac_source", "") or "").strip(),
            "applied_recommended_start_dac": 1 if bool(merged.get("applied_recommended_start_dac", False)) else 0,
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
        merged.setdefault("configured_start_dac", _to_float_or_none(merged.get("configured_start_dac")))
        merged.setdefault("initial_dac", _to_float_or_none(merged.get("initial_dac")))
        merged.setdefault("initial_dac_source", str(merged.get("initial_dac_source", "") or "").strip())
        merged.setdefault("applied_recommended_start_dac", bool(merged.get("applied_recommended_start_dac", False)))
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
                "thickness": _to_float_or_none(
                    row.get("thickness_nm") if row.get("thickness_nm") not in (None, "")
                    else row.get("thickness_A")
                ),
                "step": str(row.get("step", "") or "").strip(),
                "detail": str(row.get("detail", "") or "").strip(),
            }
            item.update(self._parse_telemetry_detail(item["detail"]))

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

    def _parse_telemetry_detail(self, detail: str) -> dict[str, Any]:
        text = str(detail or "").strip()
        parsed: dict[str, Any] = {
            "event": "",
            "target": "",
            "event_value": "",
            "detail_extra": "",
            "tag": "",
        }
        if not text:
            return parsed

        match = re.match(r"^(?P<event>\S+)\s+(?P<target>[^=|]+)=(?P<value>[^|]*)(?:\|\s*(?P<extra>.*))?$", text)
        if match:
            parsed["event"] = str(match.group("event") or "").strip()
            parsed["target"] = str(match.group("target") or "").strip()
            parsed["event_value"] = str(match.group("value") or "").strip()
            parsed["detail_extra"] = str(match.group("extra") or "").strip()
        else:
            parsed["detail_extra"] = text

        extra = parsed["detail_extra"]
        tag_match = re.search(r"(?:^|[,|]\s*)tag=([^\s,|]+)", extra)
        if tag_match:
            parsed["tag"] = str(tag_match.group(1) or "").strip()
        return parsed

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
                # 값 없는 구간은 안정 구간 타이머 리셋
                start_elapsed = None
                continue

            if abs(float(rate) - float(target_rate)) <= tolerance:
                if start_elapsed is None:
                    start_elapsed = elapsed
                # required_sec 이상 연속으로 허용 범위 내에 있을 때 안정 도달로 판정
                if required_sec <= 0.0 or (elapsed - start_elapsed) >= required_sec:
                    return elapsed, row
            else:
                start_elapsed = None

        return None, None

    def _find_main_shutter_open(self, rows: list[dict[str, Any]]) -> tuple[Optional[float], Optional[dict[str, Any]]]:
        for row in rows:
            event = str(row.get("event", "") or "").upper()
            target = str(row.get("target", "") or "").strip().upper()
            event_value = str(row.get("event_value", "") or "").strip().lower()
            if event == "WRITE_COIL" and target == "MAIN_SHUTTER_SW" and event_value in ("1", "true"):
                return _to_float_or_none(row.get("elapsed_sec")), row
            if str(row.get("tag", "") or "").strip().upper() == "EVAP_MAIN_SHUTTER_OPEN":
                return _to_float_or_none(row.get("elapsed_sec")), row

            detail = str(row.get("detail", "") or "")
            if "WRITE_COIL MAIN_SHUTTER_SW=1" in detail or "tag=EVAP_MAIN_SHUTTER_OPEN" in detail:
                return _to_float_or_none(row.get("elapsed_sec")), row
        return None, None

    def _find_main_shutter_close(self, rows: list[dict[str, Any]]) -> Optional[float]:
        """
        EVAP_DONE_SHUTTER_CLOSE 태그 기준으로 메인 셔터가 닫힌 elapsed_sec을 반환.
        없으면 None.
        """
        for row in rows:
            tag = str(row.get("tag", "") or "").strip().upper()
            if tag == "EVAP_DONE_SHUTTER_CLOSE":
                return _to_float_or_none(row.get("elapsed_sec"))

            detail = str(row.get("detail", "") or "")
            if "tag=EVAP_DONE_SHUTTER_CLOSE" in detail:
                return _to_float_or_none(row.get("elapsed_sec"))

            # NOTE: WRITE_COIL MAIN_SHUTTER_SW=0 fallback은 의도적으로 제거.
            # 공정 초기화 시점에도 MAIN_SHUTTER_SW=0이 발생하므로
            # 태그 없는 WRITE_COIL 기반 fallback은 잘못된 행을 잡아버린다.

        return None

    def _stable_window_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        stable_time: Optional[float],
        shutter_time: Optional[float],
        shutter_close_time: Optional[float],
        stable_row: Optional[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        # 메인 셔터 오픈 ~ 클로즈 사이 구간만 사용
        # (ramp 구간과 rampdown 구간을 모두 제외)
        if shutter_time is not None:
            selected = [
                row
                for row in rows
                if _to_float_or_none(row.get("elapsed_sec")) is not None
                and float(row["elapsed_sec"]) >= float(shutter_time)
                and (
                    shutter_close_time is None
                    or float(row["elapsed_sec"]) < float(shutter_close_time)
                )
            ]
            if selected:
                return selected

        # shutter_time이 없으면 기존 방식: stable_time 이후 전체
        if stable_time is None:
            return []
        selected = [
            row
            for row in rows
            if _to_float_or_none(row.get("elapsed_sec")) is not None
            and float(row["elapsed_sec"]) >= float(stable_time)
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
        # 정렬된 JSON 직렬화 후 SHA-256 → 동일 설정이면 항상 같은 해시로 이력 검색 가능
        if not process_config:
            return ""
        payload = _canonical_json(process_config).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _scan_history_artifacts(self) -> list[dict[str, Any]]:
        roots: list[Path] = []
        if self._log_service is not None and hasattr(self._log_service, "get_storage_roots"):
            with contextlib.suppress(Exception):
                storage_roots = dict(self._log_service.get_storage_roots(force_resolve=True) or {})
                for key in ("resolved", "base", "fallback"):
                    root = storage_roots.get(key)
                    if root is not None and Path(root) not in roots:
                        roots.append(Path(root))

        if not roots:
            store = self._history_store
            if store is not None and hasattr(store, "storage_roots"):
                with contextlib.suppress(Exception):
                    for root in list(store.storage_roots() or []):
                        path = Path(root)
                        if path not in roots:
                            roots.append(path)

        if not roots:
            app_name = str(
                getattr(self._log_service, "_app_name", "")
                or getattr(self._log_service, "app_name", "")
                or "Evaporator"
            )
            roots.append(default_temp_log_root(app_name))

        discovered: dict[str, dict[str, Any]] = {}
        for root in roots:
            for subdir, key, pattern in (
                ("ProcessLog", "csv", "SUCCESS_*.csv"),
                ("ProcessWindowLog", "log", "SUCCESS_*.log"),
            ):
                folder = Path(root) / subdir
                if not folder.exists():
                    continue
                for path in sorted(folder.glob(pattern)):
                    stem = path.stem
                    entry = discovered.setdefault(
                        stem,
                        {
                            "stem": stem,
                            "csv": None,
                            "log": None,
                            "root": root,
                            "csv_mtime_ns": None,
                            "log_mtime_ns": None,
                            "csv_size": None,
                            "log_size": None,
                        },
                    )
                    if entry.get(key) is None:
                        entry[key] = path
                        stat_info = self._artifact_stat(path)
                        entry[f"{key}_mtime_ns"] = stat_info.get("mtime_ns")
                        entry[f"{key}_size"] = stat_info.get("size")

        return [discovered[stem] for stem in sorted(discovered.keys())]

    def _artifact_stat(self, path: Path) -> dict[str, Optional[int]]:
        try:
            stat = Path(path).stat()
        except Exception:
            return {"mtime_ns": None, "size": None}
        return {
            "mtime_ns": int(getattr(stat, "st_mtime_ns", 0) or 0),
            "size": int(getattr(stat, "st_size", 0) or 0),
        }

    def _artifact_state_payload(self, artifact: dict[str, Any]) -> dict[str, Any]:
        csv_path = artifact.get("csv")
        log_path = artifact.get("log")
        return {
            "stem": str(artifact.get("stem", "") or "").strip(),
            "run_id": "",
            "csv_path": str(Path(csv_path)) if csv_path is not None else "",
            "log_path": str(Path(log_path)) if log_path is not None else "",
            "csv_mtime_ns": artifact.get("csv_mtime_ns"),
            "log_mtime_ns": artifact.get("log_mtime_ns"),
            "csv_size": artifact.get("csv_size"),
            "log_size": artifact.get("log_size"),
            "processed_ts": 0.0,
        }

    def _artifact_state_matches(self, existing: dict[str, Any], current: dict[str, Any]) -> bool:
        for key in ("csv_path", "log_path"):
            if str(existing.get(key, "") or "").strip() != str(current.get(key, "") or "").strip():
                return False
        for key in ("csv_mtime_ns", "log_mtime_ns", "csv_size", "log_size"):
            left = existing.get(key)
            right = current.get(key)
            if left in ("", None):
                left = None
            else:
                try:
                    left = int(left)
                except Exception:
                    left = None
            if right in ("", None):
                right = None
            else:
                try:
                    right = int(right)
                except Exception:
                    right = None
            if left != right:
                return False
        return True

    def _build_backfill_summary(self, artifact: dict[str, Any]) -> Optional[dict[str, Any]]:
        csv_path = artifact.get("csv")
        log_path = artifact.get("log")
        stem = str(artifact.get("stem", "") or "").strip()
        if csv_path is None and log_path is None:
            return None

        csv_meta: dict[str, Any] = {}
        if csv_path is not None:
            csv_meta, _ = self._load_telemetry_csv(Path(csv_path))

        log_meta = self._load_log_metadata(log_path)
        inferred_run_id, inferred_recipe = self._parse_run_stem(stem)

        run_id = str(csv_meta.get("run_id", "") or log_meta.get("run_id", "") or "").strip()
        recipe_name = str(csv_meta.get("recipe_name", "") or log_meta.get("recipe_name", "") or "").strip()
        if not run_id:
            run_id = inferred_run_id
            if not recipe_name:
                recipe_name = inferred_recipe
        elif not recipe_name:
            recipe_name = inferred_recipe

        if not run_id:
            return None

        started_ts = (
            _parse_timestamp(csv_meta.get("started_ts"))
            or _parse_timestamp(csv_meta.get("opened_at"))
            or log_meta.get("started_ts")
            or self._path_timestamp(csv_path or log_path)
        )
        finished_ts = log_meta.get("finished_ts") or self._path_timestamp(log_path or csv_path)

        result = type(
            "BackfillResult",
            (),
            {
                "run_id": run_id,
                "recipe_name": recipe_name,
                "started_ts": started_ts,
                "finished_ts": finished_ts,
                "ok": bool(log_meta.get("result_status") == "success"),
                "error": None,
            },
        )()

        run_profile = {
            "run_id": run_id,
            "recipe_name": recipe_name,
            "process_name": str(
                csv_meta.get("process_name", "")
                or csv_meta.get("recipe_name", "")
                or recipe_name
                or inferred_recipe
            ).strip(),
            "material_name": str(csv_meta.get("material_name", "") or "").strip(),
            "density": _to_float_or_none(csv_meta.get("density")),
            "z_factor": _to_float_or_none(csv_meta.get("z_factor")),
            "target_rate": _to_float_or_none(csv_meta.get("target_rate")),
            "target_thickness": _to_float_or_none(csv_meta.get("target_thickness")),
            "delay_min": _to_float_or_none(csv_meta.get("delay_min")),
            "use_power1": bool(csv_meta.get("use_power1", False)),
            "use_power2": bool(csv_meta.get("use_power2", False)),
            "hw_mapping": dict(csv_meta.get("hw_mapping") or {}),
            "process_config": dict(csv_meta.get("process_config") or {}),
            "process_config_hash": str(csv_meta.get("process_config_hash", "") or ""),
            "configured_start_dac": _to_float_or_none(csv_meta.get("configured_start_dac")),
            "initial_dac": _to_float_or_none(csv_meta.get("initial_dac")),
            "initial_dac_source": str(csv_meta.get("initial_dac_source", "") or "").strip(),
            "applied_recommended_start_dac": bool(csv_meta.get("applied_recommended_start_dac", False)),
            "started_ts": started_ts,
            "finished_ts": finished_ts,
        }

        if csv_path is not None:
            summary = self.build_summary(
                csv_path=Path(csv_path),
                log_path=(Path(log_path) if log_path is not None else None),
                run_profile=run_profile,
                result=result,
            )
            if summary and not summary.get("recipe_name"):
                summary["recipe_name"] = recipe_name
            if summary and not summary.get("process_name"):
                summary["process_name"] = run_profile.get("process_name", "")
            return summary

        return self._build_log_only_summary(
            run_id=run_id,
            recipe_name=recipe_name,
            run_profile=run_profile,
            log_meta=log_meta,
        )

    def _build_log_only_summary(
        self,
        *,
        run_id: str,
        recipe_name: str,
        run_profile: dict[str, Any],
        log_meta: dict[str, Any],
    ) -> dict[str, Any]:
        started_ts = log_meta.get("started_ts")
        finished_ts = log_meta.get("finished_ts")
        total_run_time_s = None
        if started_ts is not None and finished_ts is not None and finished_ts >= started_ts:
            total_run_time_s = finished_ts - started_ts

        process_config = dict(run_profile.get("process_config") or {})
        hw_mapping = dict(run_profile.get("hw_mapping") or {})
        return {
            "run_id": run_id,
            "recipe_name": recipe_name,
            "process_name": str(run_profile.get("process_name", "") or recipe_name).strip(),
            "material_name": str(run_profile.get("material_name", "") or "").strip(),
            "density": _to_float_or_none(run_profile.get("density")),
            "z_factor": _to_float_or_none(run_profile.get("z_factor")),
            "target_rate": _to_float_or_none(run_profile.get("target_rate")),
            "target_thickness": _to_float_or_none(run_profile.get("target_thickness")),
            "delay_min": _to_float_or_none(run_profile.get("delay_min")),
            "use_power1": 1 if bool(run_profile.get("use_power1", False)) else 0,
            "use_power2": 1 if bool(run_profile.get("use_power2", False)) else 0,
            "hw_mapping_json": hw_mapping,
            "process_config_json": process_config,
            "process_config_hash": str(run_profile.get("process_config_hash", "") or self._process_config_hash(process_config)),
            "started_ts": started_ts,
            "finished_ts": finished_ts,
            "result_status": str(log_meta.get("result_status", "abnormal_end") or "abnormal_end"),
            "fail_reason": str(log_meta.get("fail_reason", "") or "").strip(),
            "time_to_target_s": None,
            "time_to_stable_rate_s": None,
            "time_to_main_shutter_open_s": None,
            "total_run_time_s": total_run_time_s,
            "stable_rate_mean": None,
            "stable_rate_std": None,
            "stable_dac_mean": None,
            "stable_dac_std": None,
            "stable_adc_mean": None,
            "stable_adc_std": None,
            "dac_at_stable_reached": None,
            "adc_at_stable_reached": None,
            "dac_at_shutter_open": None,
            "adc_at_shutter_open": None,
            "overshoot_peak": None,
            "overshoot_ratio_peak": None,
            "spike_count": None,
            "spike_max_abs": None,
            "final_thickness_A": None,
            "thickness_error_A": None,
            "thickness_error_ratio": None,
            "sensor_none_duration_s": None,
            "adc_none_duration_s": None,
            "ramp_step_count_used": None,
            "stable_reached_in_step_index": None,
            "dac_first_nonzero": None,
            "adc_first_nonzero": None,
            "configured_start_dac": _to_float_or_none(run_profile.get("configured_start_dac")),
            "initial_dac": _to_float_or_none(run_profile.get("initial_dac")),
            "initial_dac_source": str(run_profile.get("initial_dac_source", "") or "").strip(),
            "applied_recommended_start_dac": 1 if bool(run_profile.get("applied_recommended_start_dac", False)) else 0,
        }

    def _load_log_metadata(self, log_path: Optional[Path]) -> dict[str, Any]:
        meta = {
            "run_id": "",
            "recipe_name": "",
            "started_ts": None,
            "finished_ts": None,
            "result_status": "abnormal_end",
            "fail_reason": "",
        }
        if log_path is None or not Path(log_path).exists():
            return meta

        ts_values: list[float] = []
        try:
            for raw_line in Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines():
                line = str(raw_line or "").strip()
                if not line:
                    continue

                ts_match = re.match(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
                if ts_match:
                    parsed_ts = _parse_timestamp(ts_match.group(1))
                    if parsed_ts is not None:
                        ts_values.append(parsed_ts)

                open_match = re.search(r"\[RUN\]\[OPEN\]\s+run_id=([^\s]+)\s+recipe=(.*)$", line)
                if open_match:
                    meta["run_id"] = str(open_match.group(1) or "").strip()
                    meta["recipe_name"] = str(open_match.group(2) or "").strip()

                stop_match = re.search(r"RUN STOPPED .* mode=([A-Z_]+)", line)
                if stop_match:
                    meta["result_status"] = "stopped"
                    meta["fail_reason"] = stop_match.group(1)

                if "RUN FINISHED OK" in line:
                    meta["result_status"] = "success"
                    meta["fail_reason"] = ""

                if "RUN ERROR" in line and meta["result_status"] != "success":
                    meta["result_status"] = "fail"
                    error_match = re.search(r"RUN ERROR .* error=(.*)$", line)
                    meta["fail_reason"] = str(error_match.group(1) or "").strip() if error_match else meta["fail_reason"]
        except Exception:
            return meta

        if ts_values:
            meta["started_ts"] = min(ts_values)
            meta["finished_ts"] = max(ts_values)
        return meta

    def _parse_run_stem(self, stem: str) -> tuple[str, str]:
        text = str(stem or "").strip()
        match = re.match(r"^(\d{8}_\d{6})(?:_(.*))?$", text)
        if match:
            return str(match.group(1) or "").strip(), str(match.group(2) or "").strip()
        return text, ""

    def _path_timestamp(self, path: Optional[Path]) -> Optional[float]:
        if path is None:
            return None
        with contextlib.suppress(Exception):
            return float(Path(path).stat().st_mtime)
        return None

    def _summaries_equivalent(self, existing: dict[str, Any], candidate: dict[str, Any]) -> bool:
        ignore_keys = {"created_ts"}
        keys = set(candidate.keys()) - ignore_keys
        for key in keys:
            if self._normalize_compare_value(existing.get(key), key=key, row=existing) != self._normalize_compare_value(candidate.get(key), key=key, row=candidate):
                return False
        return True

    def _normalize_compare_value(self, value: Any, *, key: str, row: dict[str, Any]) -> Any:
        if key == "hw_mapping":
            value = row.get("hw_mapping", value)
        elif key == "process_config":
            value = row.get("process_config", value)
        elif key == "hw_mapping_json":
            value = row.get("hw_mapping", value)
        elif key == "process_config_json":
            value = row.get("process_config", value)

        if isinstance(value, (dict, list, tuple)):
            return _canonical_json(value)
        if isinstance(value, float):
            return round(value, 9)
        if key in ("applied_recommended_start_dac", "use_power1", "use_power2"):
            return 1 if bool(value) else 0
        if value in ("", None):
            return None
        return value

    def _log_info(self, message: str) -> None:
        if self._log_service is not None and hasattr(self._log_service, "info"):
            with contextlib.suppress(Exception):
                self._log_service.info(message, tag="HISTORY", also_ui=False)

    def _log_warn(self, message: str) -> None:
        if self._log_service is not None and hasattr(self._log_service, "warn"):
            with contextlib.suppress(Exception):
                self._log_service.warn(message, tag="HISTORY", also_ui=False)
