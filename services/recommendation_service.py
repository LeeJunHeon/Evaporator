# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Optional


class RecommendationService:
    def __init__(self, *, history_store: Any) -> None:
        self._history_store = history_store

    def recommend(self, run_profile: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not run_profile or self._history_store is None:
            return None

        material_name = str(run_profile.get("material_name", "") or "").strip()
        if not material_name:
            return None

        # 같은 물질의 성공 공정을 모두 가져옴 (최신순)
        candidates = self._history_store.fetch_run_summaries(
            result_status="success",
            material_name=material_name,
            use_power1=bool(run_profile.get("use_power1", False)),
            use_power2=bool(run_profile.get("use_power2", False)),
        )

        if not candidates:
            return None

        # target_rate 필터: ±20% 범위를 벗어나는 이전 공정은 다른 증착 조건으로 간주하여 제외
        req_rate = float(run_profile.get("target_rate", 0.0) or 0.0)
        if req_rate > 0:
            tolerance = req_rate * 0.20
            candidates = [
                c for c in candidates
                if abs(float(c.get("target_rate", 0.0) or 0.0) - req_rate) <= tolerance
            ]

        if not candidates:
            return None

        # finished_ts 기준 최신순 정렬
        candidates.sort(key=lambda c: float(c.get("finished_ts") or c.get("started_ts") or 0), reverse=True)

        # 가장 최근 공정의 ramp_steps를 그대로 사용
        best = candidates[0]
        best_cfg = dict(best.get("process_config") or {})
        ramp_steps = list(best_cfg.get("ramp_steps") or [])

        # 표시용: 최근 5개 공정 목록
        representative_runs = [
            {
                "run_id": str(c.get("run_id", "") or ""),
                "timestamp": c.get("finished_ts") or c.get("started_ts"),
                "material_name": str(c.get("material_name", "") or ""),
                "target_rate": c.get("target_rate"),
                "target_thickness": c.get("target_thickness"),
                "stable_dac_mean": c.get("stable_dac_mean"),
                "score": 1.0,
            }
            for c in candidates[:5]
        ]

        return {
            "recommended_ramp_steps": ramp_steps,
            "recommended_process_config": best_cfg,
            "recommended_fine_step_dac": int(best_cfg.get("fine_step_dac", 10) or 10),
            "recommended_rate_stable_sec": float(best_cfg.get("rate_stable_sec", 3.0) or 3.0),
            "representative_runs": representative_runs,
            "basis_run_count": len(candidates),
            "confidence": 1.0,
        }
