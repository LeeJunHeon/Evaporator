# -*- coding: utf-8 -*-
"""
process/stm_shadow.py

그림자 모드(읽기 전용) STM 신호 관찰기.

- 공정 제어(DAC/PID), 중단(abort), 종료 판정에는 전혀 관여하지 않는다.
- STM 새 샘플(약 1초 간격)마다 값을 받아 아래를 계산하고, 결과는 로그/CSV에만 남긴다.
  1) 정리된 두께: STM 두께에서 '두께 계단 튐'(측정 오류)만 뺀 값 (HOLD 구간에서만 표시)
  2) 정리된 rate: 최근 5개 rate의 중앙값 (1초짜리 튐 제거)
  3) 폭주 경보: 3초 단위 두께 증가량 중앙값 기울기 + rate 지속성 (목표 rate의 BURST_K배)
     - 계단 튐으로 판정된 구간은 기울기 계산에서 빼고, 계단 튐 모양의 급증은 판정이 끝날 때까지 경보를 보류한다.
  4) ADC 급등 경고: DAC는 그대로인데 ADC가 10% 이상 급등
  5) 1초짜리 rate 튐 횟수
- 모든 기준은 목표 rate의 배수 또는 그 런의 최근 rate 기준이라
  목표 rate/재료가 바뀌어도 같은 상수로 동작한다.
- 2026-03-30 ~ 2026-10-14 전체 공정 로그 재생으로 정한 상수이며 설정 파일에 넣지 않는다.

이 모듈은 Qt/엔진/장비를 import 하지 않는 순수 파이썬이다.
"""
from __future__ import annotations

import math
from collections import deque
from statistics import median
from typing import Any, Deque, Dict, List, Optional, Tuple

# ------------------------------------------------------------
# 상수
# ------------------------------------------------------------
BURST_K = 4.0                  # 폭주 기준: 목표 rate의 몇 배
BURST_WINDOW_S = 15.0          # 기울기 계산 창(초)
BURST_STEP_S = 3.0             # 3초 단위 두께 증가량으로 기울기 계산
BURST_MIN_SPAN_S = 10.0        # 창에 최소 이만큼 데이터가 있어야 판정
BURST_PERSIST_WINDOW_S = 10.0  # rate 지속성 확인 창(초)
BURST_PERSIST_FRAC = 0.7       # 창 안 샘플의 70% 이상이 기준 초과
BURST_DEBOUNCE_S = 2.0         # 위 두 조건이 연속으로 2초 이상(1초 샘플 3개) 만족해야 경보
BURST_REARM_RATIO = 2.0        # 기울기가 목표×2 아래로 내려오면 폭주 종료/재무장

HOLD_SKIP_S = 3.0              # HOLD 시작(STM ZERO) 직후 이 시간 동안은 무시
MIN_SAMPLE_DT_S = 0.5          # STM 폴링(1초) 기준: 이보다 촘촘한(중복) 샘플이나 시간 역행 샘플은 건너뜀
GAP_RESET_S = 10.0             # 샘플 간격이 이보다 크면 창을 비움(STM 끊김 등)

STEP_JUMP_K = 4.0              # 1초 증가율 > max(최근 rate, 목표)×4 이면 계단 튐 의심
STEP_JUMP_MIN_A = 3.0          # 또는 1초에 3 Å 이상(분해능 고려 하한)
STEP_ELEV_K = 2.0              # 의심 이후 '아직 높음' 판단 배수
STEP_ELEV_MIN_A = 1.0          # '아직 높음' 하한(1 Å/s)
STEP_NORMAL_N = 3              # 정상 증가 3번 연속이면 이벤트 종료 후 판정
BURST_MIN_ELEV_S = 6.0         # 6초 이상 높게 지속되면 진짜 증착으로 판정
STEP_SHARE = 0.6               # 큰 증가 2개가 전체의 60% 이상이고
PRESSURE_RISE = 1.10           # 압력이 10% 이상 오르지 않았으면 → 측정 튐
REF_RATE_N = 15                # 최근 정상 증가율 개수(중앙값: 판정 기준, 평균: 예상 증착량)
STEP_LOG_MIN_A = 5.0           # 제외량이 0.5 nm(5 Å) 이상일 때만 '계단 튐/급감'으로 집계/기록
STEP_NEG_SETTLE_S = 60.0       # 두께가 떨어진 뒤 60초 동안 회복이 없으면 '측정 기준 이동'으로 정리
LATE_ZERO_WINDOW_S = 10.0      # HOLD 기준점 이후 10초 안에 큰 음수 점프 → STM ZERO 지연 반영으로 보고 기준 재설정
LATE_ZERO_DROP_A = 20.0        # 2 nm 이상 떨어지면

GLITCH_ABS = 1.0               # 1초 튐: |rate - 이웃 4개 중앙값| > max(1.0, 3×목표)
GLITCH_K = 3.0
GLITCH_THK_A = 5.0             # 그리고 앞뒤 두께 변화 0.5 nm(5 Å) 미만

ADC_JUMP_RATIO = 1.10          # ADC 급등: 최근 3개 중앙값 ≥ 직전(−10~−3) 중앙값×1.10
ADC_JUMP_NOISE_K = 1.5         # 그리고 상승폭 > 직전 구간 흔들림 폭(최대−최소)×1.5 (노이즈 적응)
ADC_JUMP_MIN = 20.0            # 기준 ADC가 이 값 이상일 때만
ADC_JUMP_DAC_TOL = 10          # 최근 5샘플 동안 DAC 변화 ≤ 10
ADC_JUMP_COOLDOWN_S = 30.0

LABEL_STEP_FRAC = 0.05         # HOLD에서 제외된 계단 튐 합계가 목표 두께의 5% 초과면 '확인 필요'
LABEL_GLITCH_PER_H = 30.0      # 1초 튐이 시간당 30회 초과면 '확인 필요'


def _fmt(v: Optional[float], nd: int = 2) -> str:
    return "---" if v is None else f"{v:.{nd}f}"


def _num(v: Any) -> Optional[float]:
    """숫자로 바꿀 수 없거나 NaN/무한대면 None."""
    try:
        if v is None:
            return None
        f = float(v)
    except Exception:
        return None
    return f if math.isfinite(f) else None


class _Sample:
    """STM 샘플 1개. sc는 계단 튐을 뺀 '기울기 계산용' 두께(Å)."""

    __slots__ = ("t", "rate", "s", "sc", "p", "adc", "dac")

    def __init__(self, t: float, rate: float, s: float, sc: float,
                 p: Optional[float], adc: Optional[float], dac: Optional[float]) -> None:
        self.t = t
        self.rate = rate
        self.s = s
        self.sc = sc
        self.p = p
        self.adc = adc
        self.dac = dac


class StmShadowObserver:
    """STM 신호 그림자 관찰기 (제어 영향 없음)."""

    def __init__(self, target_rate_aps: float, target_thickness_a: float) -> None:
        self.tr = max(1e-6, float(target_rate_aps))
        self.target_a = max(0.0, float(target_thickness_a))
        self.phase: str = ""
        self.phase_t0: Optional[float] = None
        self._samples: Deque[_Sample] = deque()
        self._last_t: Optional[float] = None
        self._p_hist: Deque[Tuple[float, Optional[float]]] = deque()
        self._cur_p: Optional[float] = None
        self._rs_cache: Optional[float] = None
        # 폭주 감지 상태
        self._cond_since: Optional[float] = None
        self._armed = True
        self._burst_active = False
        self._burst_t0: Optional[float] = None
        self._burst_sc0: Optional[float] = None
        # 두께 증가량 추적(계단 튐/급증/급감) 상태 - 구간마다 초기화
        self._film_started = False
        self._film_t0: Optional[float] = None
        self._s_first: Optional[float] = None
        self._clean_inc_a = 0.0
        self._prev_s: Optional[float] = None
        self._prev_t: Optional[float] = None
        self._inc_hist: Deque[float] = deque(maxlen=REF_RATE_N)
        self._pend: Optional[Dict[str, Any]] = None
        self._s_off = 0.0
        # ADC 급등
        self._adc_cool_until: Optional[float] = None
        # 1초 튐 판정용 (2샘플 지연)
        self._g_buf: Deque[Tuple[float, float, float]] = deque(maxlen=5)
        # 집계
        self.n_samples = 0
        self.obs_s = 0.0
        self.n_glitch = 0
        self.n_step = 0                # HOLD 계단 튐
        self.step_excluded_a = 0.0     # HOLD 계단 튐 제외량(Å)
        self.n_step_sd = 0             # 셔터 대기 중 계단 튐
        self.n_drop = 0                # 두께 급감(측정 기준 이동 추정)
        self.drop_excluded_a = 0.0     # HOLD 두께 급감 제외량(Å, 음수)
        self.n_burst = 0
        self.burst_excess_a = 0.0
        self.n_adc_jump = 0
        self.max_robust_slope = 0.0
        self.last_sw_a: Optional[float] = None
        self.last_stm_a: Optional[float] = None
        self._snap: Dict[str, Any] = {}

    def start_line(self) -> str:
        """공정 시작 시 1회 남길 안내 문구."""
        return (
            f"그림자 모드 시작 (제어·종료 판정에는 영향 없음) | 목표 {self.tr:.2f} Å/s, {self.target_a / 10.0:.1f} nm"
            f" | 폭주 경보 기준: 목표의 {BURST_K:g}배"
        )

    # --------------------------------------------------------
    # 구간 전환
    # --------------------------------------------------------
    def start_phase(self, phase: str, t: float) -> List[str]:
        """phase: 'sdelay'(셔터 대기) 또는 'hold'. hold는 STM ZERO 직후 호출. 반환: 로그 메시지 목록."""
        msgs = self._close_open_events("구간 전환") if self.phase else []
        self.phase = str(phase)
        self.phase_t0 = float(t)
        self._samples.clear()
        self._g_buf.clear()
        self._cond_since = None
        self._armed = True
        self._burst_active = False
        self._last_t = None
        self._rs_cache = None
        self._film_started = False
        self._film_t0 = None
        self._s_first = None
        self._clean_inc_a = 0.0
        self._prev_s = None
        self._prev_t = None
        self._inc_hist.clear()
        self._pend = None
        self._s_off = 0.0
        return msgs

    # --------------------------------------------------------
    # 샘플 입력
    # --------------------------------------------------------
    def update(
        self,
        *,
        t: float,
        rate: Optional[float],
        thickness_a: Optional[float],
        pressure: Optional[float] = None,
        adc: Optional[float] = None,
        dac: Optional[float] = None,
        sw_thickness_a: Optional[float] = None,
    ) -> List[str]:
        """STM 새 샘플 1개를 넣는다. 반환: 로그로 남길 메시지 목록(없으면 빈 목록)."""
        msgs: List[str] = []
        tt = _num(t)
        rate_v = _num(rate)
        s = _num(thickness_a)
        if not self.phase or tt is None or rate_v is None or s is None:
            return msgs
        t = tt
        if self._last_t is not None and (t - self._last_t) < MIN_SAMPLE_DT_S:
            return msgs
        if self.phase == "hold" and self.phase_t0 is not None and (t - self.phase_t0) < HOLD_SKIP_S:
            return msgs
        if self._last_t is not None:
            gap = t - self._last_t
            if gap > GAP_RESET_S:
                self._samples.clear()
                self._g_buf.clear()
                self._cond_since = None
            else:
                self.obs_s += gap
        self._last_t = t
        self.n_samples += 1
        sw = _num(sw_thickness_a)
        if sw is not None:
            self.last_sw_a = sw
        self.last_stm_a = s
        p = _num(pressure)
        self._cur_p = p

        self._samples.append(_Sample(t, rate_v, s, s - self._s_off, p, _num(adc), _num(dac)))
        while self._samples and self._samples[0].t < t - 120.0:
            self._samples.popleft()
        self._p_hist.append((t, p))
        while self._p_hist and self._p_hist[0][0] < t - 120.0:
            self._p_hist.popleft()

        msgs.extend(self._update_steps(t, s))
        msgs.extend(self._update_burst(t))
        msgs.extend(self._update_adc(t))
        self._update_glitch(t, rate_v, s)
        self._update_snapshot(t)
        return msgs

    # --------------------------------------------------------
    # 1) 두께 증가량 추적: 계단 튐 / 실제 급증 / 급감 (양 구간 공통, 막 두께 표시는 HOLD만)
    # --------------------------------------------------------
    def _ref_rate(self) -> float:
        """판정 기준용: 최근 정상 증가율의 중앙값(튀는 값에 강함)."""
        if len(self._inc_hist) < 5:
            return self.tr
        return max(0.0, median(self._inc_hist))

    def _ref_rate_mean(self) -> float:
        """예상 증착량 계산용: 최근 정상 증가율의 평균(1 Å 단위 끊김에도 치우치지 않음)."""
        if len(self._inc_hist) < 5:
            return self.tr
        return max(0.0, sum(self._inc_hist) / len(self._inc_hist))

    def _pressure_base(self, t0: float) -> Optional[float]:
        base = [p for (tt, p) in self._p_hist if p and t0 - 60.0 <= tt <= t0 - 5.0]
        if len(base) < 10:
            return None
        b = median(base)
        return b if b > 0 else None

    def _pressure_ratio(self, t0: float, t1: float) -> Optional[float]:
        b = self._pressure_base(t0)
        ev = [p for (tt, p) in self._p_hist if p and t0 - 2.0 <= tt <= t1 + 5.0]
        if b is None or not ev:
            return None
        return max(ev) / b

    def _update_steps(self, t: float, s: float) -> List[str]:
        msgs: List[str] = []
        if not self._film_started:
            self._film_started = True
            self._film_t0 = t
            self._s_first = s
            self._prev_s, self._prev_t = s, t
            return msgs
        dt = max(0.5, t - float(self._prev_t))
        d = s - float(self._prev_s)
        r = d / dt
        rr = self._ref_rate()
        thr = max(STEP_JUMP_MIN_A / dt, STEP_JUMP_K * max(rr, self.tr))
        elev = max(STEP_ELEV_MIN_A / dt, STEP_ELEV_K * max(rr, self.tr))
        p = self._pend
        if p is not None and self._cur_p:
            p["p_max"] = self._cur_p if p["p_max"] is None else max(p["p_max"], self._cur_p)
        if p is None:
            if r > thr or r < -max(STEP_JUMP_MIN_A / dt, STEP_JUMP_K * self.tr):
                if (self.phase == "hold" and r < 0 and -d >= LATE_ZERO_DROP_A and self._film_t0 is not None
                        and (t - self._film_t0) <= LATE_ZERO_WINDOW_S):
                    # STM ZERO가 늦게 반영된 경우: 기준점을 새 값으로 다시 잡는다
                    self._s_first = s
                    self._clean_inc_a = 0.0
                    self._inc_hist.clear()
                    self._prev_s, self._prev_t = s, t
                    self._s_off = 0.0
                    last = self._samples[-1] if self._samples else None
                    self._samples.clear()
                    if last is not None:
                        last.sc = s
                        self._samples.append(last)
                    self._cond_since = None
                    msgs.append(f"STM ZERO 지연 반영 감지 → 정리 두께 기준점 재설정 ({d / 10.0:+.2f} nm)")
                    return msgs
                self._pend = {
                    "t0": float(self._prev_t), "s0": float(self._prev_s), "rr": rr,
                    "rr_mean": self._ref_rate_mean(), "normal": 0,
                    "elev_s": (dt if r > 0 else 0.0), "neg": r < 0, "incs": [d], "s_min": s,
                    "p_base": self._pressure_base(float(self._prev_t)), "p_max": self._cur_p,
                }
            else:
                self._clean_inc_a += d
                self._inc_hist.append(r)
        else:
            settled = ((not p["neg"]) or s >= p["s0"] - STEP_JUMP_MIN_A
                       or (t - p["t0"]) >= STEP_NEG_SETTLE_S)
            if abs(r) <= elev and settled:
                p["normal"] += 1
            else:
                p["normal"] = 0
                if r > elev:
                    p["elev_s"] += dt
            p["incs"].append(d)
            p["s_min"] = min(p["s_min"], s)
            if p["normal"] >= STEP_NORMAL_N:
                msgs.extend(self._close_pending(t, s))
        self._prev_s, self._prev_t = s, t
        return msgs

    @staticmethod
    def _shape(p: Dict[str, Any], total: float) -> Tuple[float, Optional[float]]:
        """(큰 증가 2개의 비중, 압력 비)."""
        incs_sorted = sorted(p["incs"], reverse=True)
        share = (sum(incs_sorted[:2]) / total) if total > 0 else 1.0
        prat = (p["p_max"] / p["p_base"]) if (p.get("p_base") and p.get("p_max")) else None
        return share, prat

    @classmethod
    def _classify(cls, p: Dict[str, Any], total: float) -> Tuple[bool, Optional[float]]:
        """판정 대기 이벤트가 '실제 증착'처럼 보이는지 판단. 반환: (실제 여부, 압력 비)."""
        share, prat = cls._shape(p, total)
        low_p = (prat is None) or (prat < PRESSURE_RISE)
        is_real = p["elev_s"] >= BURST_MIN_ELEV_S and total > 0 and not (share >= STEP_SHARE and low_p)
        return is_real, prat

    def _pending_glitch_shaped(self) -> bool:
        """판정 대기 중인 (+) 급증이 '계단 튐 모양'(큰 증가 2개가 대부분, 압력 변화 없음)인지."""
        p = self._pend
        if p is None or p["neg"] or self._prev_s is None:
            return False
        total = float(self._prev_s) - p["s0"]
        if total <= 0:
            return False
        share, prat = self._shape(p, total)
        return share >= STEP_SHARE and (prat is None or prat < PRESSURE_RISE)

    def _correct_series(self, p: Dict[str, Any], t1: float, included: float, total: float) -> None:
        """계단 튐/급감으로 정리된 구간을 기울기 계산용 두께열에서 '포함분 직선'으로 바꾸고 이후 값을 보정."""
        t0 = float(p["t0"])
        base = float(p["s0"]) - self._s_off
        span = max(1e-6, t1 - t0)
        for x in self._samples:
            if t0 < x.t <= t1:
                x.sc = base + included * (x.t - t0) / span
        self._s_off += total - included

    def _close_pending(self, t: float, s: float, *, final: bool = False) -> List[str]:
        p = self._pend
        self._pend = None
        if p is None:
            return []
        dur = max(0.0, t - p["t0"])
        total = s - p["s0"]
        expected = max(0.0, p["rr_mean"] * dur)
        is_real, prat = self._classify(p, total)
        ptxt = "---" if prat is None else f"{prat:.2f}배"
        tail = " [구간 종료 시점 미완료 이벤트]" if final else ""
        ph = self._phase_ko()
        if is_real:
            self._clean_inc_a += total
            return [f"두께 급증 판정({ph}): 실제 증착으로 판단 +{total / 10.0:.2f} nm ({dur:.0f}초, 압력 {ptxt}){tail}"]
        dropped = p["neg"] and s < p["s0"]
        included = max(0.0, total) if (p["neg"] and not dropped) else expected
        self._clean_inc_a += included
        self._correct_series(p, t, included, total)
        excluded = total - included
        if dropped:
            if excluded <= -STEP_LOG_MIN_A and (p["s_min"] - p["s0"]) <= -STEP_LOG_MIN_A:
                self.n_drop += 1
                if self.phase == "hold":
                    self.drop_excluded_a += excluded
                return [f"두께 급감 판정({ph}): {excluded / 10.0:+.2f} nm 제외 대상 "
                        f"(측정 기준 이동 추정, {dur:.0f}초){tail}"]
            return []
        if excluded >= STEP_LOG_MIN_A:
            if self.phase == "hold":
                self.n_step += 1
                self.step_excluded_a += excluded
            else:
                self.n_step_sd += 1
            return [f"두께 계단 튐 판정({ph}): +{excluded / 10.0:.2f} nm 제외 대상 ({dur:.0f}초, 압력 {ptxt}){tail}"]
        return []

    def film_thickness_a(self) -> Optional[float]:
        """정리된 막 두께(Å, HOLD만). 판정 대기 중인 급증은 '실제 증착'처럼 보일 때만 잠정 포함."""
        if self.phase != "hold" or not self._film_started or self._s_first is None:
            return None
        v = float(self._s_first) + self._clean_inc_a
        p = self._pend
        if p is not None and self._prev_s is not None:
            total = float(self._prev_s) - p["s0"]
            is_real, _ = self._classify(p, total)
            if is_real:
                v += total
        return v

    # --------------------------------------------------------
    # 2) 폭주 경보
    # --------------------------------------------------------
    def _robust_slope(self, t: float) -> Optional[float]:
        win = [x for x in self._samples if x.t >= t - BURST_WINDOW_S]
        if len(win) < 6 or (t - win[0].t) < BURST_MIN_SPAN_S:
            return None
        incs: List[float] = []
        n = len(win)
        j = 0
        for i in range(n):
            if j <= i:
                j = i + 1
            while j < n and (win[j].t - win[i].t) < (BURST_STEP_S - 0.3):
                j += 1
            if j >= n:
                break
            incs.append((win[j].sc - win[i].sc) / (win[j].t - win[i].t))
        if len(incs) < 4:
            return None
        return median(incs)

    def _persistence(self, t: float) -> float:
        win = [x for x in self._samples if x.t >= t - BURST_PERSIST_WINDOW_S]
        if not win:
            return 0.0
        lim = BURST_K * self.tr
        return sum(1 for x in win if x.rate > lim) / len(win)

    def _update_burst(self, t: float) -> List[str]:
        msgs: List[str] = []
        rs = self._robust_slope(t)
        self._rs_cache = rs
        if rs is None:
            self._cond_since = None
            return msgs
        self.max_robust_slope = max(self.max_robust_slope, rs)
        pers = self._persistence(t)
        lim = BURST_K * self.tr
        cond = rs > lim and pers >= BURST_PERSIST_FRAC
        if cond:
            if self._cond_since is None:
                self._cond_since = t
        else:
            self._cond_since = None
        held = cond and self._cond_since is not None and (t - self._cond_since) >= BURST_DEBOUNCE_S
        if held and self._armed:
            if self._pending_glitch_shaped():
                return msgs  # 계단 튐 모양의 급증: 판정이 끝날 때까지 경보 보류
            self._armed = False
            self._burst_active = True
            self.n_burst += 1
            back = [x for x in self._samples if x.t >= t - 30.0]
            start = back[0]
            for x in back:
                if x.rate <= 1.5 * self.tr:
                    start = x
            self._burst_t0, self._burst_sc0 = start.t, start.sc
            prat = self._pressure_ratio(start.t, t)
            msgs.append(
                f"폭주 경보(K={BURST_K:g}, {self._phase_ko()}): 두께 기울기 {rs:.2f} Å/s "
                f"(목표 {self.tr:.2f}의 {rs / self.tr:.1f}배), 최근 {BURST_PERSIST_WINDOW_S:.0f}초 중 "
                f"{pers * 100:.0f}%가 {lim:.2f} Å/s 초과, 압력 {'---' if prat is None else f'{prat:.2f}배'}"
            )
        elif rs < BURST_REARM_RATIO * self.tr:
            if self._burst_active:
                msgs.extend(self._end_burst("폭주 종료"))
            self._burst_active = False
            self._armed = True
        return msgs

    def _end_burst(self, title: str) -> List[str]:
        active = self._burst_active
        self._burst_active = False
        if not active or self._burst_t0 is None or self._burst_sc0 is None or not self._samples:
            return []
        cur = self._samples[-1]
        dur = cur.t - self._burst_t0
        excess = max(0.0, (cur.sc - self._burst_sc0) - self.tr * dur)
        self.burst_excess_a += excess
        return [f"{title}({self._phase_ko()}): 지속 약 {dur:.0f}초, 목표 대비 초과 증착 약 {excess / 10.0:.1f} nm"]

    def _phase_ko(self) -> str:
        return "HOLD" if self.phase == "hold" else ("셔터 대기" if self.phase == "sdelay" else self.phase)

    # --------------------------------------------------------
    # 3) ADC 급등 경고
    # --------------------------------------------------------
    def _update_adc(self, t: float) -> List[str]:
        if len(self._samples) < 12:
            return []
        if self._adc_cool_until is not None and t < self._adc_cool_until:
            return []
        xs = list(self._samples)
        recent = [x.adc for x in xs[-3:] if x.adc is not None]
        base = [x.adc for x in xs[-10:-3] if x.adc is not None]
        dacs = [x.dac for x in xs[-5:] if x.dac is not None]
        if len(recent) < 2 or len(base) < 5 or len(dacs) < 2:
            return []
        mb = median(base)
        mr = median(recent)
        if mb < ADC_JUMP_MIN:
            return []
        noise = max(max(base) - min(base), 1.0)
        if (mr >= mb * ADC_JUMP_RATIO and (mr - mb) > ADC_JUMP_NOISE_K * noise
                and (max(dacs) - min(dacs)) <= ADC_JUMP_DAC_TOL):
            self.n_adc_jump += 1
            self._adc_cool_until = t + ADC_JUMP_COOLDOWN_S
            return [
                f"ADC 급등 경고({self._phase_ko()}): DAC 변화 {max(dacs) - min(dacs):.0f} 이내인데 "
                f"ADC {mb:.1f} → {mr:.1f} (+{(mr / mb - 1) * 100:.0f}%)"
            ]
        return []

    # --------------------------------------------------------
    # 4) 1초짜리 rate 튐 집계 (2샘플 지연 판정)
    # --------------------------------------------------------
    def _update_glitch(self, t: float, rate: float, s: float) -> None:
        self._g_buf.append((t, rate, s))
        if len(self._g_buf) < 5:
            return
        b = list(self._g_buf)
        x = b[2][1]
        nb = [b[0][1], b[1][1], b[3][1], b[4][1]]
        if abs(x - median(nb)) > max(GLITCH_ABS, GLITCH_K * self.tr) and abs(b[3][2] - b[1][2]) < GLITCH_THK_A:
            self.n_glitch += 1

    # --------------------------------------------------------
    # CSV용 스냅샷 / 요약
    # --------------------------------------------------------
    def _update_snapshot(self, t: float) -> None:
        rates = [x.rate for x in list(self._samples)[-5:]]
        rs = self._rs_cache
        flag = ""
        if self._burst_active:
            flag = "BURST"
        elif self._pend is not None:
            flag = "STEP?"
        elif self._adc_cool_until is not None and t < self._adc_cool_until:
            flag = "ADC_JUMP"
        film = self.film_thickness_a()
        self._snap = {
            "shadow_thk_nm": (round(film / 10.0, 3) if film is not None else None),
            "shadow_rate": (round(median(rates), 3) if rates else None),
            "shadow_slope": (round(rs, 3) if rs is not None else None),
            "shadow_flag": flag,
        }

    def snapshot(self) -> Dict[str, Any]:
        return dict(self._snap)

    def _close_open_events(self, why: str) -> List[str]:
        msgs: List[str] = []
        if self._pend is not None and self._prev_t is not None and self._prev_s is not None:
            msgs.extend(self._close_pending(float(self._prev_t), float(self._prev_s), final=True))
        if self._burst_active:
            msgs.extend(self._end_burst(f"폭주 진행 중 {why}"))
        return msgs

    def finish(self) -> List[str]:
        """공정 종료(정상/중단 모두) 시 1회 호출. 미완료 이벤트를 정리한다."""
        return self._close_open_events("공정 종료")

    def summary(self) -> Dict[str, Any]:
        film = self.film_thickness_a()
        reasons: List[str] = []
        if self.n_burst > 0:
            reasons.append(f"폭주 {self.n_burst}회")
        if self.target_a > 0 and self.step_excluded_a > LABEL_STEP_FRAC * self.target_a:
            reasons.append(f"계단 튐 {self.step_excluded_a / 10.0:.1f} nm")
        if self.n_drop > 0:
            reasons.append(f"두께 급감 {self.n_drop}회")
        obs_h = self.obs_s / 3600.0
        if obs_h > 0.05 and (self.n_glitch / obs_h) > LABEL_GLITCH_PER_H:
            reasons.append(f"1초 튐 시간당 {self.n_glitch / obs_h:.0f}회")
        minor_nm = None
        if film is not None and self.last_stm_a is not None:
            minor_nm = (self.last_stm_a - film - self.step_excluded_a - self.drop_excluded_a) / 10.0
        return {
            "minor_nm": minor_nm,
            "sw_nm": None if self.last_sw_a is None else self.last_sw_a / 10.0,
            "stm_nm": None if self.last_stm_a is None else self.last_stm_a / 10.0,
            "clean_nm": None if film is None else film / 10.0,
            "n_glitch": self.n_glitch,
            "n_step": self.n_step,
            "step_excluded_nm": self.step_excluded_a / 10.0,
            "n_step_sd": self.n_step_sd,
            "n_drop": self.n_drop,
            "n_burst": self.n_burst,
            "burst_excess_nm": self.burst_excess_a / 10.0,
            "n_adc_jump": self.n_adc_jump,
            "max_slope_ratio": self.max_robust_slope / self.tr,
            "label": "확인 필요" if reasons else "정상",
            "reasons": reasons,
        }

    def summary_line(self) -> str:
        sm = self.summary()
        minor = sm["minor_nm"]
        step_txt = f"계단 튐 {sm['n_step']}회(제외 {sm['step_excluded_nm']:.1f} nm"
        if sm["n_step_sd"]:
            step_txt += f", 셔터 대기 중 {sm['n_step_sd']}회"
        step_txt += ")"
        return (
            f"요약 | 종료 시점 SW {_fmt(sm['sw_nm'])} nm / STM {_fmt(sm['stm_nm'])} nm / 정리 {_fmt(sm['clean_nm'])} nm"
            f"{(' (기타 미세 보정 ' + format(minor, '+.1f') + ' nm 포함)') if (minor is not None and abs(minor) >= 0.1) else ''}"
            f" | 1초 튐 {sm['n_glitch']}회 | {step_txt}"
            f"{(' | 두께 급감 ' + str(sm['n_drop']) + '회') if sm['n_drop'] else ''}"
            f" | 폭주 경보 {sm['n_burst']}회(초과 약 {sm['burst_excess_nm']:.1f} nm) | ADC 급등 {sm['n_adc_jump']}회"
            f" | 최대 기울기 목표의 {sm['max_slope_ratio']:.1f}배"
            f" | 그림자 판정: {sm['label']}{(' (' + ', '.join(sm['reasons']) + ')') if sm['reasons'] else ''}"
        )
