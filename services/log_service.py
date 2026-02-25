# -*- coding: utf-8 -*-
"""
services/log_service.py

LogService
- UI 로그 출력 + 파일 로그 저장 + 공정(run)별 CSV 텔레메트리 저장을 한 곳에서 관리
- UI/공정/장비 서비스가 동시에 로그를 남겨도 안전하게(단일 writer thread) 처리

파일 구조(기본)
- base_dir/
    YYYY-MM-DD/
        Evaporator.log                 (일자 통합 로그)
        runs/
            <run_id>_<recipe>/
                run.log                (해당 공정(run) 전용 로그)
                telemetry.csv          (압력/두께/레이트/step 등 시간 데이터)
                meta.json              (레시피/설정/옵션 등 메타)

폴백
- base_dir 접근 실패 시: <cwd>/_Logs_local_Evaporator/ 로 자동 저장
"""

from __future__ import annotations

import csv
import json
import queue
import re
import time
import threading
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Union

from PySide6.QtCore import QObject, QThread, Signal


# ============================================================
# Internal Commands (writer thread)
# ============================================================

@dataclass(frozen=True)
class _CmdBase:
    pass


@dataclass(frozen=True)
class CmdLog(_CmdBase):
    level: str
    msg: str
    tag: str = ""          # 예: "PLC", "STM", "ACS", "PROCESS"
    also_ui: bool = True   # UI에도 내보낼지
    ts: Optional[float] = None


@dataclass(frozen=True)
class CmdOpenRun(_CmdBase):
    run_id: str
    recipe_name: str = ""
    meta: Optional[Dict[str, Any]] = None
    ts: Optional[float] = None


@dataclass(frozen=True)
class CmdCloseRun(_CmdBase):
    ts: Optional[float] = None


@dataclass(frozen=True)
class CmdTelemetry(_CmdBase):
    row: Dict[str, Any]     # dict -> csv row
    ts: Optional[float] = None


@dataclass(frozen=True)
class CmdSetBaseDir(_CmdBase):
    base_dir: Path


@dataclass(frozen=True)
class CmdStop(_CmdBase):
    pass


# ============================================================
# Helper
# ============================================================

def _now_ts(ts: Optional[float] = None) -> float:
    return float(ts if ts is not None else time.time())


def _dt_str(ts: Optional[float] = None) -> str:
    # 로컬 시간 기준 (Windows 장비 운용에 자연스러움)
    dt = datetime.fromtimestamp(_now_ts(ts))
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _time_str(ts: Optional[float] = None) -> str:
    # ✅ CSV 텔레메트리용: 시간만 저장
    dt = datetime.fromtimestamp(_now_ts(ts))
    return dt.strftime("%H:%M:%S")


def _date_dir(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(_now_ts(ts))
    return dt.strftime("%Y-%m-%d")


def _safe_name(s: str, max_len: int = 64) -> str:
    s = (s or "").strip()
    s = re.sub(r"[^\w\-. ]+", "_", s)  # 파일명에 위험한 문자 제거
    s = re.sub(r"\s+", "_", s)
    return s[:max_len] if len(s) > max_len else s


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


# ============================================================
# Writer Worker
# ============================================================

class LogWriterWorker(QThread):
    """
    단일 writer 스레드:
    - 큐로 들어온 로그/텔레메트리를 파일로 저장
    - UI 업데이트는 Signal로 전달
    """

    sig_line = Signal(str)     # UI에 출력할 최종 문자열(한 줄)
    sig_error = Signal(str)    # writer 자체 에러

    def __init__(
        self,
        app_name: str = "Evaporator",
        base_dir: Optional[Union[str, Path]] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)

        self._app_name = app_name

        self._cmd_q: "queue.Queue[_CmdBase]" = queue.Queue()
        self._stop_evt = threading.Event()

        self._base_dir = Path(base_dir) if base_dir else (Path.cwd() / "_Logs")
        self._fallback_dir = Path.cwd() / f"_Logs_local_{_safe_name(self._app_name)}"

        # ✅ base_dir 접근 테스트 캐시(매 로그마다 write_test 하지 않도록)
        self._resolved_base_cache: Optional[Path] = None
        self._resolved_base_checked_ts: float = 0.0

        # 파일 핸들
        self._day_log_fp = None             # YYYY-MM-DD/<app>.log
        self._day_log_path: Optional[Path] = None

        self._run_open = False
        self._run_dir: Optional[Path] = None
        self._run_log_fp = None
        self._run_log_path: Optional[Path] = None
        self._tele_fp = None
        self._tele_writer: Optional[csv.DictWriter] = None
        self._tele_path: Optional[Path] = None
        self._tele_header_written = False

        # 현재 open된 "날짜 디렉터리" 캐시
        self._current_date_dir: Optional[str] = None

        # ✅ run 메타 (telemetry 기본값 채우기용)
        self._run_id: str = ""
        self._run_recipe: str = ""
        self._run_open_ts: float = 0.0
        self._run_folder_name: str = ""

        # ✅ telemetry 컬럼 고정(Excel에서 항상 동일 컬럼)
        self._tele_fieldnames = [
            "time", "elapsed_sec", "step",
            "event", "target", "value", "detail",
            "pressure_torr",
            "dac1", "dac2",
            "rate_Aps", "thickness_A",
        ]

    # ---------- public (thread-safe) ----------
    def post(self, cmd: _CmdBase) -> None:
        try:
            self._cmd_q.put_nowait(cmd)
        except Exception:
            pass

    def request_stop(self) -> None:
        self._stop_evt.set()
        self.post(CmdStop())

    # ---------- thread entry ----------
    def run(self) -> None:
        try:
            while not self._stop_evt.is_set():
                try:
                    cmd = self._cmd_q.get(timeout=0.2)
                except queue.Empty:
                    # 날짜 변경 감지 → day log rotate
                    self._roll_day_log_if_needed()
                    continue

                if isinstance(cmd, CmdStop):
                    break

                try:
                    self._handle_cmd(cmd)
                except Exception as e:
                    try:
                        self.sig_error.emit(f"[LogService] writer error: {e!r}")
                    except Exception:
                        pass

                # 날짜 변경 감지
                self._roll_day_log_if_needed()

        finally:
            self._close_all()

    # ---------- internals ----------
    def _handle_cmd(self, cmd: _CmdBase) -> None:
        if isinstance(cmd, CmdSetBaseDir):
            self._base_dir = Path(cmd.base_dir)

            # ✅ base_dir 바뀌면 캐시 초기화
            self._resolved_base_cache = None
            self._resolved_base_checked_ts = 0.0

            self._close_day_log()
            return

        if isinstance(cmd, CmdOpenRun):
            self._open_run(cmd.run_id, cmd.recipe_name, cmd.meta or {}, cmd.ts)
            return

        if isinstance(cmd, CmdCloseRun):
            self._close_run()
            return

        if isinstance(cmd, CmdTelemetry):
            self._write_telemetry(cmd.row, cmd.ts)
            return

        if isinstance(cmd, CmdLog):
            self._write_log(cmd.level, cmd.msg, cmd.tag, cmd.also_ui, cmd.ts)
            return

        # 알 수 없는 cmd
        self._write_log("WARN", f"Unknown command: {cmd!r}", "LogService", True, None)

    def _roll_day_log_if_needed(self) -> None:
        today = _date_dir()
        if self._current_date_dir != today:
            self._current_date_dir = today
            self._close_day_log()
            # 다음 write 시 열도록 lazy open

    def _resolve_base_dir(self, *, force: bool = False) -> Path:
        """
        base_dir 접근 실패 가능성이 있으므로 폴더 생성/쓰기 테스트 후 폴백을 선택한다.
        ✅ 단, 매번 테스트하면 NAS에 부담 → 캐시(기본 10초) 적용
        """
        now = time.time()
        if (not force) and self._resolved_base_cache is not None and (now - self._resolved_base_checked_ts) < 10.0:
            return self._resolved_base_cache

        self._resolved_base_checked_ts = now

        # 1) base_dir 시도
        try:
            _ensure_dir(self._base_dir)
            # ✅ 파일로 write_test 하지 않음(불필요 파일 생성 방지)
            self._resolved_base_cache = self._base_dir
            return self._base_dir
        except Exception:
            pass

        # 2) fallback 시도
        try:
            _ensure_dir(self._fallback_dir)
            self._resolved_base_cache = self._fallback_dir
            return self._fallback_dir
        except Exception:
            self._resolved_base_cache = Path.cwd()
            return Path.cwd()

    def _ensure_day_log_open(self, ts: Optional[float] = None) -> None:
        """
        날짜별 통합 로그 파일을 연다.
        - base_dir/YYYY-MM-DD/<app>.log
        """
        date_dir = _date_dir(ts)
        base = self._resolve_base_dir()
        day_dir = base / date_dir
        _ensure_dir(day_dir)

        log_path = day_dir / f"{_safe_name(self._app_name)}.log"
        if self._day_log_fp is not None and self._day_log_path == log_path:
            return

        # 기존 핸들 닫고 새로 오픈
        self._close_day_log()
        self._day_log_path = log_path
        self._day_log_fp = open(log_path, "a", encoding="utf-8", newline="")

    def _close_day_log(self) -> None:
        try:
            if self._day_log_fp:
                self._day_log_fp.flush()
                self._day_log_fp.close()
        except Exception:
            pass
        self._day_log_fp = None
        self._day_log_path = None

    def _open_run(self, run_id: str, recipe_name: str, meta: Dict[str, Any], ts: Optional[float]) -> None:
        """
        ✅ 공정(run)당 파일 1개만 생성:
        - (NAS 우선) base_dir/<run_id>_<recipe>.log
        - NAS open 실패 시 로컬(_fallback_dir)로 저장
        """
        self._close_run()

        # ✅ 파일명/런 메타 세팅
        rid = _safe_name(run_id, 48) or _safe_name(datetime.now().strftime("%Y%m%d_%H%M%S"), 48)
        rcp = _safe_name(recipe_name, 48)
        file_stem = f"{rid}_{rcp}" if rcp else rid

        self._run_id = run_id
        self._run_recipe = recipe_name
        self._run_open_ts = _now_ts(ts)
        self._run_folder_name = file_stem  # 재오픈용 key

        last_err = None
        chosen_base = None
        chosen_path = None
        chosen_fp = None

        # ✅ “NAS 못 열면 로컬 저장”만 명확히: 후보를 NAS(base_dir) -> 로컬(fallback)로 고정
        candidates = [self._base_dir, self._fallback_dir]

        for b in candidates:
            try:
                _ensure_dir(b)
                p = b / f"{file_stem}.csv"
                fp = open(p, "a", encoding="utf-8-sig", newline="")  # 엑셀 호환(한글 깨짐 방지)
                chosen_base, chosen_path, chosen_fp = b, p, fp
                break
            except Exception as e:
                last_err = e

        if chosen_fp is None:
            try:
                self.sig_error.emit(f"[LogService] RUN OPEN failed (NAS+LOCAL): {last_err!r}")
            except Exception:
                pass
            self._run_open = False
            self._run_dir = None
            self._run_folder_name = ""
            return

        self._run_dir = chosen_base
        self._run_open = True
        self._tele_path = chosen_path
        self._tele_fp = chosen_fp
        self._tele_writer = None
        self._tele_header_written = False

        self._write_log("INFO", f"RUN OPEN -> {self._tele_path}", "LogService", True, ts)

    def _close_run(self) -> None:
        if self._run_open:
            self._write_log("INFO", "RUN CLOSE", "LogService", True, None)

        try:
            if self._tele_fp:
                self._tele_fp.flush()
                self._tele_fp.close()
        except Exception:
            pass

        self._tele_fp = None
        self._tele_writer = None
        self._tele_header_written = False
        self._tele_path = None

        self._run_open = False
        self._run_dir = None
        self._run_folder_name = ""

    def _close_all(self) -> None:
        self._close_run()
        self._close_day_log()

    def _format_line(self, level: str, msg: str, tag: str, ts: Optional[float]) -> str:
        # [YYYY-MM-DD HH:MM:SS] [LEVEL] [TAG] message
        level = (level or "INFO").upper()
        tag = (tag or "").strip()
        prefix = f"[{_dt_str(ts)}] [{level}]"
        if tag:
            prefix += f" [{tag}]"
        return f"{prefix} {msg}"
        
    def _invalidate_base_cache(self) -> None:
        self._resolved_base_cache = None
        self._resolved_base_checked_ts = 0.0

    def _reopen_run_files_in_resolved_base(self) -> None:
        """
        run이 열린 상태에서 파일 쓰기 실패 시:
        - base_dir 재판정(force)
        - 동일 file_stem(<run_id>_<recipe>)으로 base/<file_stem>.log 재오픈
        """
        if not self._run_open or not self._run_folder_name:
            return

        # 기존 핸들 닫기
        try:
            if self._tele_fp:
                self._tele_fp.flush()
                self._tele_fp.close()
        except Exception:
            pass
        self._tele_fp = None
        self._tele_writer = None
        self._tele_header_written = False

        last_err = None
        chosen_base = None
        chosen_path = None
        chosen_fp = None

        candidates = []
        b0 = self._resolve_base_dir(force=True)
        candidates.append(b0)
        if self._fallback_dir not in candidates:
            candidates.append(self._fallback_dir)
        if Path.cwd() not in candidates:
            candidates.append(Path.cwd())

        for b in candidates:
            try:
                _ensure_dir(b)
                p = b / f"{self._run_folder_name}.csv"
                fp = open(p, "a", encoding="utf-8-sig", newline="")
                chosen_base, chosen_path, chosen_fp = b, p, fp
                break
            except Exception as e:
                last_err = e

        if chosen_fp is None:
            try:
                self.sig_error.emit(f"[LogService] RUN reopen failed: {last_err!r}")
            except Exception:
                pass
            return

        self._run_dir = chosen_base
        self._tele_path = chosen_path
        self._tele_fp = chosen_fp
        self._tele_writer = None
        self._tele_header_written = False

    def _write_log(self, level: str, msg: str, tag: str, also_ui: bool, ts: Optional[float]) -> None:
        line = self._format_line(level, msg, tag, ts)

        # UI로만 전달 (파일 저장은 main.py의 화면 로그 writer가 담당)
        if also_ui:
            try:
                self.sig_line.emit(line)
            except Exception:
                pass

    def _write_telemetry(self, row: Dict[str, Any], ts: Optional[float]) -> None:
        if not self._run_open or not self._tele_fp:
            return

        row2 = dict(row or {})
        now_ts = _now_ts(ts)

        # 1) time: CSV에는 항상 "HH:MM:SS"만 기록 (외부에서 준 값은 무시)
        row2["time"] = _time_str(now_ts)

        # 2) elapsed_sec: 항상 여기서 계산 (외부에서 준 값은 무시)
        row2["elapsed_sec"] = round(now_ts - self._run_open_ts, 3) if self._run_open_ts > 0 else ""

        # 3) step (engine이 세분화 문자열을 넣으면 그대로 저장)
        row2.setdefault("step", "")

        # 4) event/target/value/detail (이벤트 행용)
        row2.setdefault("event", "")
        row2.setdefault("target", "")
        row2.setdefault("value", "")
        row2.setdefault("detail", "")

        # 5) pressure 키 보정
        if "pressure_torr" not in row2 and "pressure" in row2:
            row2["pressure_torr"] = row2.get("pressure")

        # ✅ writer 준비(고정 헤더)
        if self._tele_writer is None:
            self._tele_writer = csv.DictWriter(self._tele_fp, fieldnames=self._tele_fieldnames)
            try:
                if self._tele_fp.tell() == 0:
                    self._tele_writer.writeheader()
            except Exception:
                # tell() 실패하면 헤더 작성 시도
                try:
                    self._tele_writer.writeheader()
                except Exception:
                    pass

        def _norm_cell(v: Any) -> Any:
            if v is None:
                return ""
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return ""
            return v

        filtered = {k: _norm_cell(row2.get(k, "")) for k in self._tele_fieldnames}

        try:
            self._tele_writer.writerow(filtered)
            self._tele_fp.flush()
        except Exception as e:
            try:
                self.sig_error.emit(f"[LogService] telemetry write failed: {e!r}")
            except Exception:
                pass
            self._invalidate_base_cache()
            self._reopen_run_files_in_resolved_base()


# ============================================================
# Public Service API
# ============================================================

class LogService(QObject):
    """
    앱/컨트롤러가 사용하는 로그 서비스.

    사용 예:
        log = LogService(app_name="Evaporator", base_dir="D:/Logs")
        log.start()
        log.info("program started", "APP")

        log.open_run(run_id="RUN_001", recipe_name="Al_100nm", meta={"operator":"JH"})
        log.telemetry({"step":"PUMPDOWN","pressure":1e-3})
        log.close_run()
        log.stop()
    """

    sig_line = Signal(str)   # UI에 붙일 한 줄
    sig_error = Signal(str)  # 파일 쓰기/worker 에러

    def __init__(
        self,
        app_name: str = "Evaporator",
        base_dir: Optional[Union[str, Path]] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._worker = LogWriterWorker(app_name=app_name, base_dir=base_dir)
        self._worker.sig_line.connect(self.sig_line)
        self._worker.sig_error.connect(self.sig_error)

    # ---------- lifecycle ----------
    def start(self) -> None:
        if not self._worker.isRunning():
            self._worker.start()

    def stop(self, wait_ms: int = 3000) -> None:
        try:
            self._worker.request_stop()
        except Exception:
            pass
        try:
            self._worker.wait(int(wait_ms))
        except Exception:
            pass

    def is_running(self) -> bool:
        return bool(self._worker.isRunning())

    # ---------- config ----------
    def set_base_dir(self, base_dir: Union[str, Path]) -> None:
        self._worker.post(CmdSetBaseDir(Path(base_dir)))

    # ---------- run control ----------
    def open_run(self, run_id: str, recipe_name: str = "", meta: Optional[Dict[str, Any]] = None) -> None:
        self._worker.post(CmdOpenRun(run_id=run_id, recipe_name=recipe_name, meta=meta))

    def close_run(self) -> None:
        self._worker.post(CmdCloseRun())

    # ---------- telemetry ----------
    def telemetry(self, row: Dict[str, Any]) -> None:
        """
        공정 중 주기적으로 호출:
            log.telemetry({
                "step": "DEP",
                "pressure": 1.2e-6,
                "thickness_A": 230.5,
                "rate_Aps": 0.8
            })
        """
        self._worker.post(CmdTelemetry(row=row))

    # ---------- logging convenience ----------
    def log(self, level: str, msg: str, tag: str = "", also_ui: bool = True) -> None:
        self._worker.post(CmdLog(level=str(level), msg=str(msg), tag=str(tag), also_ui=bool(also_ui)))

    def info(self, msg: str, tag: str = "", also_ui: bool = True) -> None:
        self.log("INFO", msg, tag, also_ui)

    def warn(self, msg: str, tag: str = "", also_ui: bool = True) -> None:
        self.log("WARN", msg, tag, also_ui)

    def error(self, msg: str, tag: str = "", also_ui: bool = True) -> None:
        self.log("ERROR", msg, tag, also_ui)
