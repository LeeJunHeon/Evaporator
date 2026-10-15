# -*- coding: utf-8 -*-
"""
scripts/verify_stm_shadow.py

그림자 모드 관찰기(process/stm_shadow.py) 검증 도구. 장비/Qt 없이 실행된다.

사용법
  1) 자체 시험(가짜 데이터 + 실측 재현):  python scripts/verify_stm_shadow.py
  2) 과거 공정 CSV 재생:                  python scripts/verify_stm_shadow.py --replay <CSV 또는 폴더> [...]
     - NAS의 ProcessLog 폴더(또는 그 안의 CSV)를 그대로 넣으면 된다(구버전 포맷 포함).
     - 셔터 대기 구간과 HOLD 구간을 실제 공정처럼 1초 간격으로 다시 넣어 보고,
       이벤트 로그와 요약 한 줄을 출력한다.
"""
from __future__ import annotations

import csv
import glob
import io
import json
import math
import os
import random
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from process.stm_shadow import StmShadowObserver  # noqa: E402


# ------------------------------------------------------------
# 가짜 데이터 생성기
# ------------------------------------------------------------
class _Sim:
    """1초 간격 STM 샘플 생성: 두께는 1 Å 단위 내림, rate는 6초 이동평균 + 잡음 + 0.1 반올림."""

    def __init__(self, seed: int = 1, noise: float = 0.05) -> None:
        self.rng = random.Random(seed)
        self.noise = noise
        self.s_true = 0.0
        self.hist = []
        self.t = 0.0
        self.s_offset = 0.0

    def step(self, true_rate: float) -> tuple:
        self.t += 1.0
        self.s_true += true_rate
        self.hist.append(true_rate)
        self.hist = self.hist[-6:]
        r = sum(self.hist) / len(self.hist) + self.rng.gauss(0.0, self.noise)
        rate = round(r * 10.0) / 10.0
        s = float(math.floor(self.s_true + self.s_offset))
        return self.t, rate, s


def _run(obs: StmShadowObserver, rows, phase: str = "hold"):
    msgs = list(obs.start_phase(phase, rows[0][0] - 1.0 - 3.5 if phase == "hold" else rows[0][0] - 1.0))
    for (t, rate, s, p, adc, dac) in rows:
        msgs.extend(obs.update(t=t, rate=rate, thickness_a=s, pressure=p, adc=adc, dac=dac))
    msgs.extend(obs.finish())
    return msgs


def _rows_normal(tr: float, n: int, seed: int = 1, p0: float = 1e-5, adc0: float = 167.0, dac0: int = 3000):
    sim = _Sim(seed)
    rng = random.Random(seed + 100)
    rows = []
    for _ in range(n):
        t, rate, s = sim.step(tr)
        rows.append((t, rate, s, p0 * (1 + rng.gauss(0, 0.005)), adc0 + rng.uniform(-2, 2), dac0))
    return rows


FAILS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}{(' | ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def test_normal() -> None:
    print("T1 정상 공정 (Al 0.3 Å/s, 30분)")
    tr = 0.3
    rows = _rows_normal(tr, 1800)
    obs = StmShadowObserver(tr, 700.0)
    _run(obs, rows)
    sm = obs.summary()
    check("폭주 경보 0", sm["n_burst"] == 0, str(sm["n_burst"]))
    check("계단 튐 0", sm["n_step"] == 0, str(sm["n_step"]))
    check("ADC 급등 0", sm["n_adc_jump"] == 0, str(sm["n_adc_jump"]))
    check("정리 두께 ≈ STM 두께", abs(sm["clean_nm"] - sm["stm_nm"]) <= 0.2, f"{sm['clean_nm']:.2f} vs {sm['stm_nm']:.2f}")
    check("판정 정상", sm["label"] == "정상", sm["label"])


def _step_rows(tr: float, seed: int = 2):
    sim = _Sim(seed)
    rows = []
    tail = [45.0, 30.0, 5.1, 4.4, 3.7, 2.9, 2.2, 1.4]
    k = 0
    for i in range(1200):
        t, rate, s = sim.step(tr)
        if i in (600, 601):
            sim.s_offset += 40.0
            s = float(math.floor(sim.s_true + sim.s_offset))
        if 600 <= i < 600 + len(tail):
            rate = tail[k]
            k += 1
        rows.append((t, rate, s, 1e-5, 120.0, 2500))
    return rows


def test_step_glitch() -> None:
    print("T2 두께 계단 튐 (+8 nm, 2초, 압력 변화 없음)")
    tr = 0.5
    rows = _step_rows(tr)
    obs = StmShadowObserver(tr, 600.0)
    msgs = _run(obs, rows)
    sm = obs.summary()
    check("계단 튐 1회", sm["n_step"] == 1, str(sm["n_step"]))
    check("제외량 7.5~8.5 nm", 7.5 <= sm["step_excluded_nm"] <= 8.5, f"{sm['step_excluded_nm']:.2f}")
    check("폭주 경보 0", sm["n_burst"] == 0, str(sm["n_burst"]))
    check("정리 두께 ≈ STM − 8 nm", abs((sm["stm_nm"] - sm["clean_nm"]) - 8.0) <= 0.5,
          f"STM {sm['stm_nm']:.2f} / 정리 {sm['clean_nm']:.2f}")
    for m in msgs:
        print("     ", m)


def _burst_rows(tr: float, seed: int = 3, phase_pressure: bool = True, n: int = 1500):
    sim = _Sim(seed)
    rows = []
    for i in range(n):
        if i < 600:
            r = tr
        elif i < 620:
            r = tr + (6.0 - tr) * (i - 600) / 20.0
        elif i < 680:
            r = 6.0
        elif i < 720:
            r = 6.0 - (6.0 - tr) * (i - 680) / 40.0
        else:
            r = tr
        t, rate, s = sim.step(r)
        p = 1.1e-5 * (1.3 if (phase_pressure and 602 <= i < 700) else 1.0)
        rows.append((t, rate, s, p, 167.0, 3279))
    return rows


def test_real_burst() -> None:
    print("T3 진짜 폭주 (0.3 → 6 Å/s, 약 2분, 압력 +30%)")
    tr = 0.3
    rows = _burst_rows(tr)
    obs = StmShadowObserver(tr, 700.0)
    msgs = _run(obs, rows)
    sm = obs.summary()
    alarm = [m for m in msgs if m.startswith("폭주 경보")]
    check("폭주 경보 1회", sm["n_burst"] == 1, str(sm["n_burst"]))
    check("정리 두께 ≈ STM 두께(실제 증착은 포함)", abs(sm["clean_nm"] - sm["stm_nm"]) <= 0.5,
          f"{sm['clean_nm']:.2f} vs {sm['stm_nm']:.2f}")
    check("초과 증착 30 nm 이상으로 집계", sm["burst_excess_nm"] >= 30.0, f"{sm['burst_excess_nm']:.1f}")
    check("판정 확인 필요", sm["label"] == "확인 필요", sm["label"])
    # 경보 시점: 폭주 시작(600 s 행 = t 601) 후 25초 이내
    obs2 = StmShadowObserver(tr, 700.0)
    obs2.start_phase("hold", rows[0][0] - 4.5)
    t_alarm = None
    for (t, rate, s, p, adc, dac) in rows:
        for m in obs2.update(t=t, rate=rate, thickness_a=s, pressure=p, adc=adc, dac=dac):
            if m.startswith("폭주 경보") and t_alarm is None:
                t_alarm = t
    check("경보가 폭주 시작 후 25초 이내", t_alarm is not None and t_alarm - 601.0 <= 25.0,
          f"{t_alarm - 601.0:.0f}초 후" if t_alarm is not None else "경보 없음")
    for m in alarm:
        print("     ", m)


def test_isolated_glitch() -> None:
    print("T4 1초짜리 rate 튐 10회 (두께 변화 없음)")
    tr = 0.3
    rows = _rows_normal(tr, 1200, seed=4)
    out = []
    n = 0
    for i, r in enumerate(rows):
        if 300 <= i and (i - 300) % 60 == 0 and n < 10:
            v = 10.3 if n % 2 == 0 else -8.9
            r = (r[0], v, r[2], r[3], r[4], r[5])
            n += 1
        out.append(r)
    obs = StmShadowObserver(tr, 700.0)
    _run(obs, out)
    sm = obs.summary()
    check("1초 튐 10회 집계", sm["n_glitch"] == 10, str(sm["n_glitch"]))
    check("폭주 경보 0", sm["n_burst"] == 0, str(sm["n_burst"]))
    check("계단 튐 0", sm["n_step"] == 0, str(sm["n_step"]))


def test_adc_jump() -> None:
    print("T5 ADC 급등 (DAC 그대로, 167 → 191)")
    tr = 0.3
    rows = _rows_normal(tr, 900, seed=5)
    jumped = [(r[0], r[1], r[2], r[3], (r[4] + 24.0) if i >= 600 else r[4], r[5]) for i, r in enumerate(rows)]
    obs = StmShadowObserver(tr, 700.0)
    msgs = _run(obs, jumped)
    check("ADC 급등 1회", obs.summary()["n_adc_jump"] == 1, str(obs.summary()["n_adc_jump"]))
    obs2 = StmShadowObserver(tr, 700.0)
    _run(obs2, rows)
    check("급등 없는 경우 0회", obs2.summary()["n_adc_jump"] == 0, str(obs2.summary()["n_adc_jump"]))
    for m in msgs:
        print("     ", m)


def test_late_zero() -> None:
    print("T6 STM ZERO 지연 반영 (시작 직후 두께가 6.5 nm → 0)")
    tr = 0.3
    rows = _rows_normal(tr, 600, seed=6)
    out = []
    for i, r in enumerate(rows):
        if i < 2:
            out.append((r[0], r[1], 65.0, r[3], r[4], r[5]))
        else:
            out.append(r)
    obs = StmShadowObserver(tr, 700.0)
    msgs = _run(obs, out)
    sm = obs.summary()
    check("기준점 재설정 메시지", any("기준점 재설정" in m for m in msgs), "")
    check("정리 두께 ≈ STM 두께", abs(sm["clean_nm"] - sm["stm_nm"]) <= 0.2, f"{sm['clean_nm']:.2f} vs {sm['stm_nm']:.2f}")
    check("폭주 경보 0", sm["n_burst"] == 0, str(sm["n_burst"]))


def test_low_rate() -> None:
    print("T7 저속 공정 (Cu 0.1 Å/s, 2시간) 오경보 없음")
    tr = 0.1
    rows = _rows_normal(tr, 7200, seed=7)
    obs = StmShadowObserver(tr, 700.0)
    _run(obs, rows)
    sm = obs.summary()
    check("폭주 경보 0", sm["n_burst"] == 0, str(sm["n_burst"]))
    check("계단 튐 0", sm["n_step"] == 0, str(sm["n_step"]))


def test_robustness() -> None:
    print("T8 None/NaN 값 / 15초 끊김 / 시간 역행")
    tr = 0.5
    rows = _rows_normal(tr, 600, seed=8)
    obs = StmShadowObserver(tr, 300.0)
    obs.start_phase("hold", rows[0][0] - 4.5)
    ok = True
    try:
        for i, (t, rate, s, p, adc, dac) in enumerate(rows):
            if 200 <= i < 215:
                continue
            if i % 37 == 0:
                obs.update(t=t, rate=None, thickness_a=s, pressure=p, adc=adc, dac=dac)
                continue
            if i % 41 == 0:
                obs.update(t=t, rate=float("nan"), thickness_a=s, pressure="x", adc=adc, dac=dac)
                continue
            if i % 53 == 0:
                obs.update(t=t - 5.0, rate=rate, thickness_a=s, pressure=None, adc=None, dac=None)
            obs.update(t=t, rate=rate, thickness_a=s, pressure=p, adc=adc, dac=dac)
            if i % 97 == 0:
                obs.snapshot()
        obs.finish()
        obs.summary_line()
    except Exception as e:  # pragma: no cover
        ok = False
        print("     예외:", repr(e))
    check("예외 없음", ok)


def test_shutter_delay_phase() -> None:
    print("T9 셔터 대기 구간: 폭주 경보는 동작, 막 두께는 계산 안 함")
    tr = 0.3
    rows = _burst_rows(tr, seed=9)
    obs = StmShadowObserver(tr, 700.0)
    _run(obs, rows, phase="sdelay")
    sm = obs.summary()
    check("폭주 경보 1회", sm["n_burst"] == 1, str(sm["n_burst"]))
    check("막 두께 없음(None)", sm["clean_nm"] is None, str(sm["clean_nm"]))


# 2026-05-15 Cu 0.1 Å/s 런 실측 일부(HOLD 시작 약 290초 후부터 74샘플):
# 두께가 2초 사이 +1.7 nm 튀고 rate가 9.0에서 서서히 내려온 '계단 튐'.
# 이전 관찰기(계단 튐을 모르는 폭주 감지)는 이 구간에서 폭주 경보를 잘못 냈다.
_R0515_T = [0.0, 1.02, 2.02, 3.03, 4.04, 5.05, 6.06, 7.07, 8.08, 9.09, 10.1, 11.11, 12.12, 13.13, 14.14, 15.14,
            16.97, 18.18, 20.0, 21.21, 23.03, 24.24, 26.06, 27.27, 29.1, 30.31, 31.32, 32.69, 33.7, 34.71, 35.72,
            36.73, 37.74, 38.75, 39.76, 40.77, 41.78, 42.79, 43.8, 44.81, 45.84, 46.85, 47.86, 48.87, 49.88, 50.89,
            51.9, 52.91, 53.92, 54.93, 55.94, 56.95, 57.96, 58.97, 59.98, 60.99, 62.0, 63.01, 64.04, 65.05, 66.06,
            67.14, 68.16, 69.47, 70.17, 71.18, 72.52, 73.23, 74.24, 75.55, 76.26, 77.27, 78.58, 79.29]
_R0515_RATE = [0.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1,
               0.1, 0.1, 0.1, 0.1, 0.2, 0.2, 0.2, 0.3, 0.4, 0.6, 9.0, 3.3, 2.8, 2.3, 1.8, 1.3, 0.7, 0.3, 0.2, 0.1,
               0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.0, 0.1, 0.1, 0.0, 0.1, 0.0, 0.1,
               0.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.0, 0.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1]
_R0515_S = [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 5, 7, 15, 24, 25, 25,
            25, 26, 26, 26, 26, 26, 26, 26, 26, 26, 26, 26, 26, 27, 27, 27, 27, 27, 27, 27, 27, 27, 27, 27, 27, 27,
            27, 27, 27, 27, 27, 27, 27, 27, 28, 28, 28, 28, 28, 28]
_R0515_P = [12.1, 12.4, 12.6, 12.7, 12.8, 12.8, 12.8, 12.8, 12.8, 12.8, 12.8, 12.8, 12.8, 12.9, 12.9, 12.9, 12.9,
            12.9, 12.9, 12.9, 13.0, 13.0, 13.1, 13.1, 13.1, 13.1, 13.2, 13.2, 13.3, 13.3, 13.3, 13.3, 13.3, 13.3,
            13.4, 13.4, 13.4, 13.4, 13.4, 13.4, 13.5, 13.5, 13.5, 13.5, 13.5, 13.5, 13.5, 13.5, 13.6, 13.6, 13.6,
            13.6, 13.6, 13.6, 13.6, 13.6, 13.6, 13.7, 13.7, 13.7, 13.7, 13.7, 13.7, 13.8, 13.8, 13.8, 13.8, 13.8,
            13.9, 13.9, 13.9, 14.0, 14.0, 14.0]


def test_real_0515_spit() -> None:
    print("T10 실측 재현: 05-15 Cu 0.1 Å/s 순간 튐(+1.7 nm) → 계단 튐 1회, 폭주 오경보 없음")
    obs = StmShadowObserver(0.1, 1000.0)
    obs.start_phase("hold", -4.0)
    msgs = []
    for k in range(len(_R0515_T)):
        msgs.extend(obs.update(t=_R0515_T[k], rate=_R0515_RATE[k], thickness_a=float(_R0515_S[k]),
                               pressure=_R0515_P[k] * 1e-6, adc=150.0, dac=2000))
    msgs.extend(obs.finish())
    sm = obs.summary()
    check("폭주 경보 0", sm["n_burst"] == 0, str(sm["n_burst"]))
    check("계단 튐 1회", sm["n_step"] == 1, str(sm["n_step"]))
    check("제외량 1.5~2.0 nm", 1.5 <= sm["step_excluded_nm"] <= 2.0, f"{sm['step_excluded_nm']:.2f}")
    for m in msgs:
        print("     ", m)


def test_step_in_shutter_delay() -> None:
    print("T11 셔터 대기 중 계단 튐: 별도 집계, 폭주 경보 없음")
    tr = 0.5
    rows = _step_rows(tr, seed=11)
    obs = StmShadowObserver(tr, 600.0)
    msgs = _run(obs, rows, phase="sdelay")
    sm = obs.summary()
    check("셔터 대기 계단 튐 1회", sm["n_step_sd"] == 1, str(sm["n_step_sd"]))
    check("HOLD 계단 튐 0", sm["n_step"] == 0, str(sm["n_step"]))
    check("폭주 경보 0", sm["n_burst"] == 0, str(sm["n_burst"]))
    for m in msgs:
        print("     ", m)


def test_permanent_drop() -> None:
    print("T12 두께 기준 이동(HOLD 중 −3 nm 후 회복 없음): 급감 1회, 정리 두께는 계속 증가")
    tr = 0.3
    sim = _Sim(12)
    rows = []
    for i in range(1200):
        t, rate, s = sim.step(tr)
        if i == 300:
            sim.s_offset -= 30.0
            s = float(math.floor(sim.s_true + sim.s_offset))
        rows.append((t, rate, s, 1e-5, 150.0, 2500))
    obs = StmShadowObserver(tr, 700.0)
    msgs = _run(obs, rows)
    sm = obs.summary()
    check("두께 급감 1회", sm["n_drop"] == 1, str(sm["n_drop"]))
    check("정리 두께 ≈ STM + 3 nm", abs((sm["clean_nm"] - sm["stm_nm"]) - 3.0) <= 0.5,
          f"STM {sm['stm_nm']:.2f} / 정리 {sm['clean_nm']:.2f}")
    check("폭주 경보 0", sm["n_burst"] == 0, str(sm["n_burst"]))
    check("판정 확인 필요", sm["label"] == "확인 필요", sm["label"])
    for m in msgs:
        print("     ", m)


def test_phase_switch_during_burst() -> None:
    print("T13 셔터 대기 중 폭주가 끝나기 전에 HOLD 진입: 구간 전환 메시지로 정리")
    tr = 0.3
    rows = _burst_rows(tr, seed=13, n=700)
    obs = StmShadowObserver(tr, 700.0)
    obs.start_phase("sdelay", rows[0][0] - 1.0)
    for (t, rate, s, p, adc, dac) in rows:
        obs.update(t=t, rate=rate, thickness_a=s, pressure=p, adc=adc, dac=dac)
    msgs = obs.start_phase("hold", rows[-1][0] + 1.0)
    check("구간 전환 메시지", any(m.startswith("폭주 진행 중 구간 전환") for m in msgs), " / ".join(msgs))
    check("폭주 경보 1회", obs.summary()["n_burst"] == 1, str(obs.summary()["n_burst"]))
    for m in msgs:
        print("     ", m)


# ------------------------------------------------------------
# 과거 CSV 재생
# ------------------------------------------------------------
_HOLD_SW_RE = re.compile(r"\( SW:(-?[\d.]+)/")


def _f(x):
    try:
        if x is None or x == "":
            return None
        v = float(x)
        return None if math.isnan(v) else v
    except Exception:
        return None


def replay_csv(path: str) -> None:
    with open(path, encoding="utf-8-sig") as fp:
        lines = fp.read().splitlines()
    meta = {}
    data = []
    for ln in lines:
        if ln.startswith("#"):
            m = re.match(r"#\s*([^=]+)=(.*)$", ln)
            if m:
                meta[m.group(1).strip()] = m.group(2)
        elif ln.strip():
            data.append(ln)
    rd = csv.DictReader(io.StringIO("\n".join(data)))
    cols = rd.fieldnames or []
    thk_col = "thickness_nm" if "thickness_nm" in cols else ("thickness_A" if "thickness_A" in cols else None)
    up1 = str(meta.get("use_power1", "")).lower() == "true"
    up2 = str(meta.get("use_power2", "")).lower() == "true"
    try:
        hw = json.loads(meta.get("hw_mapping") or "{}")
    except Exception:
        hw = {}
    tr = _f(meta.get("target_rate")) or 0.0
    tt = _f(meta.get("target_thickness")) or 0.0
    rows = []
    for r in rd:
        t = _f(r.get("elapsed_sec"))
        if t is None:
            continue
        thk = _f(r.get(thk_col)) if thk_col else None
        if thk is not None:
            thk = thk * (10.0 if thk_col == "thickness_nm" else 1.0)  # Å
        if up2 and not up1:
            dac, adc = _f(r.get("dac2")), _f(r.get("adc2"))
        elif up1 and not up2:
            dac = _f(r.get("dac1"))
            adc = _f(r.get("adc2")) if hw.get("power1_feedback_adc2") else _f(r.get("adc1"))
        else:
            d2 = _f(r.get("dac2"))
            dac = d2 if d2 is not None else _f(r.get("dac1"))
            adcs = [v for v in (_f(r.get("adc1")), _f(r.get("adc2"))) if v is not None]
            adc = max(adcs) if adcs else None
        step = r.get("step") or ""
        sw = _f(r.get("sw_thickness_nm"))
        if sw is None:
            m = _HOLD_SW_RE.search(step)
            sw = _f(m.group(1)) if m else None
        rows.append(dict(t=t, clock=r.get("time") or "", rate=_f(r.get("dep.rate")), thk=thk, p=_f(r.get("pressure_torr")),
                         dac=dac, adc=adc, step=step, detail=r.get("detail") or "", sw_a=(sw * 10.0 if sw is not None else None)))
    t_open = t_close = None
    for r in rows:
        if t_open is None and "MAIN_SHUTTER_SW=1" in r["detail"]:
            t_open = r["t"]
        elif t_open is not None and t_close is None and "MAIN_SHUTTER_SW=0" in r["detail"] and r["t"] > t_open:
            t_close = r["t"]

    def per_second(sel):
        out = {}
        for r in sel:
            if r["rate"] is None or r["thk"] is None:
                continue
            out[int(math.floor(r["t"]))] = r
        return [out[k] for k in sorted(out)]

    sd = per_second([r for r in rows if (t_open is None or r["t"] < t_open)
                     and ("셔터 대기" in r["step"] or "셔터 오픈 대기" in r["step"])])
    hold = per_second([r for r in rows if t_open is not None and r["t"] >= t_open
                       and (t_close is None or r["t"] < t_close)]) if t_open is not None else []
    print(f"=== {os.path.basename(path)} | 목표 {tr} Å/s, {tt / 10.0:.1f} nm | 셔터대기 {len(sd)}개, HOLD {len(hold)}개 샘플")
    if tr <= 0:
        print("  목표 rate 없음 → 건너뜀")
        return
    obs = StmShadowObserver(tr, tt)
    for phase, S, t0 in (("sdelay", sd, sd[0]["t"] if sd else None), ("hold", hold, t_open)):
        if not S:
            continue
        for m in obs.start_phase(phase, t0):
            print(f"  (구간 전환) {m}")
        for r in S:
            for m in obs.update(t=r["t"], rate=r["rate"], thickness_a=r["thk"], pressure=r["p"], adc=r["adc"],
                                dac=r["dac"], sw_thickness_a=r["sw_a"]):
                print(f"  {r['clock']} {m}")
    for m in obs.finish():
        print(f"  (종료) {m}")
    print("  " + obs.summary_line())


def _expand(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            out.extend(sorted(glob.glob(os.path.join(p, "*.csv"))))
        else:
            out.append(p)
    return out


def _utf8_stdout() -> None:
    # Windows(cp949) 파이프 출력에서 Å 등 문자로 멈추지 않도록
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv) -> int:
    _utf8_stdout()
    if len(argv) >= 2 and argv[1] == "--replay":
        for p in _expand(argv[2:]):
            try:
                replay_csv(p)
            except Exception as e:
                print(f"=== {p}: 재생 실패 {e!r}")
        return 0
    test_normal()
    test_step_glitch()
    test_real_burst()
    test_isolated_glitch()
    test_adc_jump()
    test_late_zero()
    test_low_rate()
    test_robustness()
    test_shutter_delay_phase()
    test_real_0515_spit()
    test_step_in_shutter_delay()
    test_permanent_drop()
    test_phase_switch_during_burst()
    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        return 1
    print("모든 시험 통과 (ALL PASS)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
