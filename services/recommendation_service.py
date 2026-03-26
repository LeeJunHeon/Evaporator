# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Optional


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


class RecommendationService:
    def __init__(self, *, history_store: Any) -> None:
        self._history_store = history_store

    def recommend(self, run_profile: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not run_profile or self._history_store is None:
            return None

        material_name = str(run_profile.get("material_name", "") or "").strip()
        if not material_name:
            return None

        candidates = self._history_store.fetch_run_summaries(
            result_status="success",
            material_name=material_name,
            use_power1=bool(run_profile.get("use_power1", False)),
            use_power2=bool(run_profile.get("use_power2", False)),
        )

        hw_mapping = dict(run_profile.get("hw_mapping") or {})
        filtered = [
            cand
            for cand in candidates
            if dict(cand.get("hw_mapping") or {}) == hw_mapping
        ]
        if not filtered:
            return None

        scored: list[tuple[float, dict[str, Any]]] = []
        for candidate in filtered:
            score = self._candidate_score(run_profile, candidate)
            if score > 0.0:
                scored.append((score, candidate))

        if not scored:
            return None

        scored.sort(key=lambda item: item[0], reverse=True)
        basis = [(score, cand) for score, cand in scored if score >= 0.55][:5]
        if not basis:
            basis = [(score, cand) for score, cand in scored if score >= 0.35][:3]
        if not basis:
            return None

        return self._build_recommendation(run_profile, basis)

    def _candidate_score(self, run_profile: dict[str, Any], candidate: dict[str, Any]) -> float:
        process_cfg_score = self._process_config_similarity(
            dict(run_profile.get("process_config") or {}),
            dict(candidate.get("process_config") or {}),
            str(run_profile.get("process_config_hash", "") or ""),
            str(candidate.get("process_config_hash", "") or ""),
        )

        score = 0.0
        score += 0.25 * self._scaled_similarity(
            run_profile.get("target_rate"),
            candidate.get("target_rate"),
            tolerance=max(abs(_to_float(run_profile.get("target_rate"))) * 0.20, 0.10),
        )
        score += 0.20 * self._scaled_similarity(
            run_profile.get("target_thickness"),
            candidate.get("target_thickness"),
            tolerance=max(abs(_to_float(run_profile.get("target_thickness"))) * 0.25, 50.0),
        )
        score += 0.10 * self._scaled_similarity(
            run_profile.get("delay_min"),
            candidate.get("delay_min"),
            tolerance=max(abs(_to_float(run_profile.get("delay_min"))) * 0.50, 1.0),
        )
        score += 0.10 * self._scaled_similarity(
            run_profile.get("density"),
            candidate.get("density"),
            tolerance=max(abs(_to_float(run_profile.get("density"))) * 0.10, 0.10),
        )
        score += 0.10 * self._scaled_similarity(
            run_profile.get("z_factor"),
            candidate.get("z_factor"),
            tolerance=max(abs(_to_float(run_profile.get("z_factor"))) * 0.15, 0.02),
        )
        score += 0.25 * process_cfg_score
        return max(0.0, min(1.0, score))

    def _scaled_similarity(self, lhs: Any, rhs: Any, *, tolerance: float) -> float:
        left = _to_float(lhs)
        right = _to_float(rhs)
        if tolerance <= 0:
            return 1.0 if abs(left - right) <= 1e-9 else 0.0
        delta = abs(left - right)
        return max(0.0, 1.0 - (delta / float(tolerance)))

    def _process_config_similarity(
        self,
        current_cfg: dict[str, Any],
        candidate_cfg: dict[str, Any],
        current_hash: str,
        candidate_hash: str,
    ) -> float:
        if current_hash and candidate_hash and current_hash == candidate_hash:
            return 1.0

        current_steps = list(current_cfg.get("ramp_steps") or [])
        candidate_steps = list(candidate_cfg.get("ramp_steps") or [])
        if not current_steps or not candidate_steps:
            return 0.0

        step_count_score = self._scaled_similarity(
            current_cfg.get("step_count", len(current_steps)),
            candidate_cfg.get("step_count", len(candidate_steps)),
            tolerance=max(len(current_steps), len(candidate_steps), 1),
        )

        aligned = min(len(current_steps), len(candidate_steps))
        per_step_scores: list[float] = []
        for idx in range(aligned):
            lhs = dict(current_steps[idx] or {})
            rhs = dict(candidate_steps[idx] or {})
            per_step_scores.append(
                (
                    self._scaled_similarity(lhs.get("target_adc"), rhs.get("target_adc"), tolerance=max(abs(_to_float(lhs.get("target_adc"))) * 0.20, 20.0))
                    + self._scaled_similarity(lhs.get("dac_step"), rhs.get("dac_step"), tolerance=max(abs(_to_float(lhs.get("dac_step"))) * 0.50, 5.0))
                    + self._scaled_similarity(lhs.get("dac_interval_sec"), rhs.get("dac_interval_sec"), tolerance=max(abs(_to_float(lhs.get("dac_interval_sec"))) * 0.50, 2.0))
                    + self._scaled_similarity(lhs.get("hold_sec"), rhs.get("hold_sec"), tolerance=max(abs(_to_float(lhs.get("hold_sec"))) * 0.50, 1.0))
                ) / 4.0
            )

        ramp_steps_score = sum(per_step_scores) / len(per_step_scores) if per_step_scores else 0.0
        fine_step_score = self._scaled_similarity(
            current_cfg.get("fine_step_dac"),
            candidate_cfg.get("fine_step_dac"),
            tolerance=max(abs(_to_float(current_cfg.get("fine_step_dac"))) * 0.50, 5.0),
        )
        stable_sec_score = self._scaled_similarity(
            current_cfg.get("rate_stable_sec"),
            candidate_cfg.get("rate_stable_sec"),
            tolerance=max(abs(_to_float(current_cfg.get("rate_stable_sec"))) * 0.50, 1.0),
        )

        return (
            0.20 * step_count_score
            + 0.55 * ramp_steps_score
            + 0.15 * fine_step_score
            + 0.10 * stable_sec_score
        )

    def _build_recommendation(
        self,
        run_profile: dict[str, Any],
        basis: list[tuple[float, dict[str, Any]]],
    ) -> dict[str, Any]:
        basis_weights = [score for score, _ in basis]
        representative_run_ids = [str(cand.get("run_id", "") or "").strip() for _, cand in basis[:3] if cand.get("run_id")]
        representative_runs = [
            self._representative_run_payload(score, cand)
            for score, cand in basis[:5]
            if cand.get("run_id")
        ]

        step_count = self._weighted_mode(
            basis,
            lambda cand: int((cand.get("process_config") or {}).get("step_count") or len((cand.get("process_config") or {}).get("ramp_steps") or [])),
        )
        step_count = max(1, int(step_count or 1))

        ramp_steps = self._aggregate_ramp_steps(basis, step_count=step_count)
        recommended_fine_step_dac = round(self._weighted_mean(
            basis,
            lambda cand: _to_float((cand.get("process_config") or {}).get("fine_step_dac"), 10.0),
        ))
        recommended_rate_stable_sec = self._weighted_mean(
            basis,
            lambda cand: _to_float((cand.get("process_config") or {}).get("rate_stable_sec"), 3.0),
        )
        current_cfg = dict(run_profile.get("process_config") or {})
        recommended_cfg = dict(current_cfg)
        recommended_cfg["step_count"] = len(ramp_steps)
        recommended_cfg["ramp_steps"] = ramp_steps
        recommended_cfg["fine_step_dac"] = max(1, int(recommended_fine_step_dac))
        recommended_cfg["rate_stable_sec"] = max(0.0, float(recommended_rate_stable_sec))

        mean_score = sum(basis_weights) / len(basis_weights)
        confidence = max(0.0, min(1.0, mean_score * (0.70 + 0.30 * min(1.0, len(basis) / 5.0))))

        return {
            "recommended_ramp_steps": ramp_steps,
            "recommended_fine_step_dac": max(1, int(recommended_fine_step_dac)),
            "recommended_rate_stable_sec": max(0.0, float(recommended_rate_stable_sec)),
            "confidence": round(confidence, 3),
            "basis_run_count": len(basis),
            "representative_run_ids": representative_run_ids,
            "representative_runs": representative_runs,
            "recommended_process_config": recommended_cfg,
        }

    def _representative_run_payload(self, score: float, candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": str(candidate.get("run_id", "") or "").strip(),
            "timestamp": candidate.get("finished_ts") or candidate.get("started_ts"),
            "material_name": str(candidate.get("material_name", "") or "").strip(),
            "target_rate": candidate.get("target_rate"),
            "target_thickness": candidate.get("target_thickness"),
            "result_status": str(candidate.get("result_status", "") or "").strip(),
            "stable_rate_mean": candidate.get("stable_rate_mean"),
            "stable_dac_mean": candidate.get("stable_dac_mean"),
            "overshoot_ratio_peak": candidate.get("overshoot_ratio_peak"),
            "spike_count": candidate.get("spike_count"),
            "final_thickness_A": candidate.get("final_thickness_A"),
            "thickness_error_A": candidate.get("thickness_error_A"),
            "thickness_error_ratio": candidate.get("thickness_error_ratio"),
            "score": round(float(score), 3),
        }

    def _weighted_mode(self, basis: list[tuple[float, dict[str, Any]]], value_fn: Any) -> Optional[int]:
        scores: dict[int, float] = {}
        for weight, candidate in basis:
            value = int(value_fn(candidate))
            scores[value] = scores.get(value, 0.0) + float(weight)
        if not scores:
            return None
        return max(scores.items(), key=lambda item: item[1])[0]

    def _weighted_mean(self, basis: list[tuple[float, dict[str, Any]]], value_fn: Any) -> float:
        numerator = 0.0
        denominator = 0.0
        for weight, candidate in basis:
            value = float(value_fn(candidate))
            numerator += float(weight) * value
            denominator += float(weight)
        if denominator <= 0:
            return 0.0
        return numerator / denominator

    def _aggregate_ramp_steps(self, basis: list[tuple[float, dict[str, Any]]], *, step_count: int) -> list[dict[str, Any]]:
        aggregated: list[dict[str, Any]] = []
        for idx in range(step_count):
            rows = [
                (weight, dict((candidate.get("process_config") or {}).get("ramp_steps", [])[idx] or {}))
                for weight, candidate in basis
                if len((candidate.get("process_config") or {}).get("ramp_steps") or []) > idx
            ]
            if not rows:
                continue

            def _wm(field: str, default: float) -> float:
                numerator = 0.0
                denominator = 0.0
                for weight, row in rows:
                    numerator += float(weight) * _to_float(row.get(field), default)
                    denominator += float(weight)
                if denominator <= 0:
                    return float(default)
                return numerator / denominator

            aggregated.append(
                {
                    "target_adc": round(_wm("target_adc", 0.0), 3),
                    "dac_step": max(1, int(round(_wm("dac_step", 10.0)))),
                    "dac_interval_sec": round(max(0.1, _wm("dac_interval_sec", 30.0)), 3),
                    "hold_sec": round(max(0.0, _wm("hold_sec", 0.0)), 3),
                }
            )

        return aggregated
