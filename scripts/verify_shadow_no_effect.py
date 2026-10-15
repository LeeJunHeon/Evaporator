# -*- coding: utf-8 -*-
"""
scripts/verify_shadow_no_effect.py

그림자 모드가 공정 제어에 영향을 주지 않는지 확인하는 검증 도구. 장비/Qt 없이 실행된다.

- 가상 시계 + 가상 장비(DAC→ADC→증착 rate, STM 1초 폴링, HOLD 중 +4 nm 계단 튐 1회)를 만들고
  process/evap_runtime.py 의 run_evap_deposition_control 을 그대로 실행한다.
- 같은 입력으로 아래 조합을 실행해서 PLC 명령(셔터/DAC/전원), 상태 메시지, 이벤트, 종료 결과가
  한 글자도 다르지 않은지 비교한다.
    1) 정상 종료:  그림자 켬  vs  그림자 끔
    2) 사용자 정지(HOLD 150초 후): 그림자 켬  vs  그림자 끔
    3) 그림자 내부 오류(매번 예외):  vs  그림자 끔   → 3회 후 그림자만 꺼지고 공정은 동일해야 함
    0) (--baseline 지정 시) git에 커밋된 기존 evap_runtime.py 와 지금 코드의 결과도 비교
사용법:  python scripts/verify_shadow_no_effect.py [--baseline HEAD]
"""
from __future__ import annotations

import math
import os
import random
import subprocess
import sys
import types
from collections import deque

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# process.engine(Qt 의존)는 불러오지 않고, evap_runtime 이 지연 import 하는 예외 클래스만 가짜로 제공
import process  # noqa: E402,F401

_fake_engine = types.ModuleType("process.engine")


class EngineFailed(Exception):
    def __init__(self, step, detail=""):
        super().__init__(f"{step}: {detail}")


class EngineTimeout(Exception):
    pass


class EngineStopRequested(Exception):
    pass


_fake_engine.EngineFailed = EngineFailed
_fake_engine.EngineTimeout = EngineTimeout
_fake_engine.EngineStopRequested = EngineStopRequested
sys.modules["process.engine"] = _fake_engine

import process.evap_runtime as rt  # noqa: E402
from process.stm_shadow import StmShadowObserver  # noqa: E402


class _BrokenObserver(StmShadowObserver):
    def update(self, **kw):  # type: ignore[override]
        raise RuntimeError("시험용 그림자 오류")


class _Plant:
    """DAC→ADC(1차 지연 3초) → 20초 지연 + 8초 열지연 → rate. STM은 1초마다 폴링."""

    def __init__(self) -> None:
        self.t = 0.0
        self.dac = 0
        self.adc = 0.0
        self.temp = 0.0
        self.delay: deque = deque()
        self.s_true = 0.0
        self.s_off = 0.0
        self.hist: deque = deque(maxlen=24)
        self.rng = random.Random(7)
        self.next_poll = 1.0
        self.next_int = 0.25
        self.snap = None
        self.hold_t0 = None
        self.glitch_done = False
        self.glitch_tail = 0

    def true_rate(self) -> float:
        x = (self.temp - 140.0) / 25.0
        return 0.3 * x * x if x > 0 else 0.0

    def advance(self, dt: float) -> None:
        end = self.t + max(0.0, float(dt))
        while self.t < end - 1e-12:
            h = min(0.05, end - self.t)
            self.adc += (0.05 * self.dac - self.adc) * (h / 3.0)
            self.delay.append((self.t, self.adc))
            while self.delay and self.delay[0][0] < self.t - 20.0:
                self.delay.popleft()
            a_del = self.delay[0][1] if self.delay else self.adc
            self.temp += (a_del - self.temp) * (h / 8.0)
            self.s_true += self.true_rate() * h
            self.t += h
            if self.t >= self.next_int:
                self.next_int += 0.25
                self.hist.append(self.true_rate())
            if self.t >= self.next_poll:
                self.next_poll += 1.0
                self._poll()

    def _poll(self) -> None:
        if self.hold_t0 is not None and not self.glitch_done and self.t >= self.hold_t0 + 200.0:
            self.s_off += 40.0  # +4 nm 계단 튐(측정값만)
            self.glitch_done = True
            self.glitch_tail = 6
        r = (sum(self.hist) / len(self.hist)) if self.hist else 0.0
        r = round((r + self.rng.gauss(0.0, 0.02)) * 10.0) / 10.0
        if self.glitch_tail > 0:
            r = round(r + 8.0 * self.glitch_tail / 6.0, 1)
            self.glitch_tail -= 1
        s = math.floor(self.s_true + self.s_off)
        self.snap = types.SimpleNamespace(ts=1.7e9 + self.t, connected=True, thickness_angstrom=float(s),
                                          rate_angstrom_per_s=float(r), meta={})


def _load_baseline(rev: str):
    """git에 커밋된 process/evap_runtime.py(rev)를 메모리에서 별도 모듈로 불러온다."""
    out = subprocess.run(["git", "show", f"{rev}:process/evap_runtime.py"], cwd=_ROOT,
                         capture_output=True, check=True)
    mod = types.ModuleType("evap_runtime_baseline")
    mod.__file__ = f"{rev}:process/evap_runtime.py"
    exec(compile(out.stdout.decode("utf-8-sig"), mod.__file__, "exec"), mod.__dict__)
    return mod


def _simulate(observer_cls, *, stop_after_hold_s=None, module=None) -> dict:
    rtm = rt if module is None else module
    plant = _Plant()
    tr = {"plc": [], "status": [], "tele": [], "shutdown": [], "shadow": [], "warn": []}

    class VTime:
        @staticmethod
        def monotonic():
            return 5000.0 + plant.t

        @staticmethod
        def time():
            return 1.7e9 + plant.t

        @staticmethod
        def sleep(dt):
            plant.advance(dt)

    class STM:
        device = None

        def get_last_snapshot(self):
            return plant.snap

        def submit_zero_thickness(self, mode="B"):
            plant.s_off = -math.floor(plant.s_true)
            plant.hold_t0 = plant.t
            if stop_after_hold_s is not None:
                eng.stop_at = plant.t + float(stop_after_hold_s)
            return None

        def submit_apply_material_params(self, **kw):
            return None

        def stop(self):
            return None

    class PLC:
        def get_last_snapshot(self):
            a = round(plant.adc, 1)
            return types.SimpleNamespace(regs={"POWER_READ_1": 0.0, "POWER_READ_2": a,
                                               "POWER_READ_1_RAW": 0.0, "POWER_READ_2_RAW": a})

    class Engine:
        def __init__(self):
            self.stm = STM()
            self.plc = PLC()
            self._graph_frozen = False
            self._last_dac_power_1 = None
            self._last_dac_power_2 = None
            self._last_sw_thickness_nm = None
            self._shadow_tele = {}
            self.stop_at = None

        def _check_stop(self, recipe, step):
            if self.stop_at is not None and plant.t >= self.stop_at:
                raise EngineStopRequested("stop")

        def _tick_emit(self, recipe, step):
            return None

        def _emit_status(self, message="", force=False, **kw):
            tr["status"].append((round(plant.t, 2), message))

        def _tele_event(self, *, event, target, value="", detail=""):
            tr["tele"].append((round(plant.t, 2), event, target, str(value), detail))

        @staticmethod
        def _fmt_hms(sec, *, ceil=False):
            s = int(math.ceil(sec)) if ceil else int(sec)
            return f"{s // 60:02d}:{s % 60:02d}"

        def _wait_future(self, fut, timeout_s=5.0, where="", msg=""):
            return None

        def _safe_shutdown_sequence(self, tag=""):
            tr["shutdown"].append((round(plant.t, 2), tag))

        def _run_line(self, text):
            return None

        def _log_info(self, msg, *, tag="ENGINE", also_ui=True):
            if tag == "SHADOW":
                tr["shadow"].append(msg)

        def _log_warn(self, msg, *, tag="ENGINE", also_ui=True):
            tr["warn"].append(msg)

        def _plc_write_coil(self, name, value, tag="", **kw):
            tr["plc"].append((round(plant.t, 2), "coil", name, bool(value)))

        def _plc_write_reg(self, name, value, tag="", silent=False, **kw):
            tr["plc"].append((round(plant.t, 2), "reg", name, int(value)))
            if name in ("DAC_POWER_1", "DAC_POWER_2"):
                plant.dac = int(value)

        def _get_stm_snapshot(self):
            return plant.snap

        def _get_rate(self):
            return None if plant.snap is None else plant.snap.rate_angstrom_per_s

        def _get_thickness(self):
            return None if plant.snap is None else plant.snap.thickness_angstrom

        def _get_pressure(self):
            return 1.0e-5 * (1.0 + 0.02 * math.sin(plant.t / 50.0))

    pcfg = {"step_count": 3,
            "ramp_steps": [{"target_adc": 100.0, "dac_step": 200, "dac_interval_sec": 2.0, "hold_sec": 0.0},
                           {"target_adc": 160.0, "dac_step": 20, "dac_interval_sec": 5.0, "hold_sec": 0.0},
                           {"target_adc": 200.0, "dac_step": 10, "dac_interval_sec": 10.0, "hold_sec": 30.0}],
            "dac_max": 4000, "rate_tol_ratio": 0.1, "rate_stable_sec": 3.0, "hold_control_interval_s": 3.0,
            "fine_step_dac": 10, "hold_control_mode": "PID", "hold_pi_kp": 15.0, "hold_pi_ki": 2.0,
            "hold_integral_limit": 2.5, "rate_abort_ratio": 0.3, "rate_abort_sec": 60.0, "sensor_none_abort_s": 10.0,
            "adc_none_abort_s": 10.0, "hold_max_dac_delta": 20, "pre_hold_timeout_sec": 600.0}
    meta = {"use_power1": False, "use_power2": True, "power1_feedback_adc2": False, "target_rate": 0.3,
            "target_thickness": 150.0, "delay_min": 1.0, "density": 2.7, "z_factor": 1.08, "zero_mode": "B",
            "process_config": pcfg}
    step = types.SimpleNamespace(name="EVAP", meta=meta)
    recipe = types.SimpleNamespace(recipe_name="SIM")

    plant.advance(1.5)
    eng = Engine()
    old_time = rtm.time
    had_obs = hasattr(rtm, "_StmShadowObserver")
    old_obs = getattr(rtm, "_StmShadowObserver", None)
    rtm.time = VTime
    if had_obs:
        rtm._StmShadowObserver = observer_cls
    try:
        rtm.run_evap_deposition_control(eng, recipe, step)
        tr["result"] = "정상 종료"
    except Exception as e:  # noqa: BLE001
        tr["result"] = f"{type(e).__name__}: {e}"
    finally:
        rtm.time = old_time
        if had_obs:
            rtm._StmShadowObserver = old_obs
    tr["end_t"] = round(plant.t, 2)
    return tr


FAILS = []


def _same_control(name: str, a: dict, b: dict) -> None:
    keys = ("plc", "status", "tele", "shutdown", "result", "end_t")
    diff = [k for k in keys if a[k] != b[k]]
    ok = not diff
    print(f"  [{'OK' if ok else 'FAIL'}] {name}: PLC 명령 {len(a['plc'])}개 / 상태 메시지 {len(a['status'])}개 / "
          f"이벤트 {len(a['tele'])}개 / 결과 '{a['result']}'" + ("" if ok else f" | 다른 항목: {diff}"))
    if not ok:
        FAILS.append(name)


def _check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}{(' | ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def _utf8_stdout() -> None:
    # Windows(cp949) 파이프 출력에서 Å 등 문자로 멈추지 않도록
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv) -> int:
    _utf8_stdout()
    if not hasattr(rt, "_StmShadowObserver"):
        print("evap_runtime 에 그림자 모드 연결(_StmShadowObserver)이 없습니다.")
        return 1
    on = _simulate(StmShadowObserver)
    off = _simulate(None)
    if len(argv) >= 3 and argv[1] == "--baseline":
        rev = argv[2]
        print(f"S0 기존 코드({rev}:process/evap_runtime.py) vs 지금 코드(그림자 켬)")
        try:
            base = _load_baseline(rev)
        except Exception as e:  # noqa: BLE001
            base = None
            _check("기존 코드 불러오기", False, repr(e))
        if base is not None:
            _same_control("정상 종료 결과 동일", _simulate(None, module=base), on)
            _same_control("사용자 정지 결과 동일", _simulate(None, stop_after_hold_s=150.0, module=base),
                          _simulate(StmShadowObserver, stop_after_hold_s=150.0))
    print("S1 정상 종료: 그림자 켬 vs 끔")
    _same_control("제어 동작 동일", on, off)
    _check("그림자 시작 로그", any(m.startswith("그림자 모드 시작") for m in on["shadow"]))
    _check("계단 튐 판정 로그", any("두께 계단 튐 판정(HOLD)" in m for m in on["shadow"]))
    _check("요약 1줄(정상 종료)", any(m.startswith("요약") and "정상 종료" in m for m in on["shadow"]))
    _check("그림자 끔이면 로그 없음", not off["shadow"])
    for m in on["shadow"]:
        print("     ", m)

    print("S2 사용자 정지(HOLD 150초 후): 그림자 켬 vs 끔")
    on2 = _simulate(StmShadowObserver, stop_after_hold_s=150.0)
    off2 = _simulate(None, stop_after_hold_s=150.0)
    _same_control("제어 동작 동일", on2, off2)
    _check("요약 1줄(사용자 정지)", any(m.startswith("요약") and "사용자 정지" in m for m in on2["shadow"]))

    print("S3 그림자 내부 오류(매번 예외) vs 그림자 끔")
    broken = _simulate(_BrokenObserver)
    _same_control("제어 동작 동일", broken, off)
    _check("오류 3회 후 그림자만 중지 경고", any("그림자 모드 중지" in m for m in broken["warn"]),
           " / ".join(broken["warn"]))
    print()
    if FAILS:
        print(f"실패 {len(FAILS)}건: {FAILS}")
        return 1
    print("제어 영향 없음 확인 완료 (ALL PASS)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
