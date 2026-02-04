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
            # 다음 기록부터 새 base_dir로 기록되도록 파일 핸들 닫기
            self._close_day_log()
            # run은 유지하되, run_dir은 다음 open_run에서 새로 열도록 권장
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

    def _resolve_base_dir(self) -> Path:
        """
        base_dir 접근 실패 가능성이 있으므로 폴더 생성 테스트 후 폴백을 선택한다.
        """
        try:
            _ensure_dir(self._base_dir)
            # 쓰기 테스트(폴더 권한/네트워크 문제)
            test_file = self._base_dir / ".write_test"
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("ok")
            try:
                test_file.unlink(missing_ok=True)  # py3.8+ compatible in 3.11/3.13
            except Exception:
                pass
            return self._base_dir
        except Exception:
            try:
                _ensure_dir(self._fallback_dir)
            except Exception:
                # 최후: 현재 작업 디렉터리
                return Path.cwd()
            return self._fallback_dir

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
        run별 폴더/파일 오픈:
        - base_dir/YYYY-MM-DD/runs/<run_id>_<recipe>/
            run.log
            telemetry.csv
            meta.json
        """
        # 이미 열려 있으면 먼저 닫고 새로 연다(중복 방지)
        self._close_run()

        base = self._resolve_base_dir()
        date_dir = _date_dir(ts)
        runs_dir = base / date_dir / "runs"
        _ensure_dir(runs_dir)

        rid = _safe_name(run_id, 48) or _safe_name(datetime.now().strftime("%H%M%S"), 48)
        rcp = _safe_name(recipe_name, 48)
        folder_name = f"{rid}_{rcp}" if rcp else rid
        self._run_dir = runs_dir / folder_name
        _ensure_dir(self._run_dir)

        # meta.json 저장
        meta_path = self._run_dir / "meta.json"
        try:
            meta_out = dict(meta or {})
            meta_out.setdefault("run_id", run_id)
            meta_out.setdefault("recipe_name", recipe_name)
            meta_out.setdefault("opened_at", _dt_str(ts))
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta_out, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.sig_error.emit(f"[LogService] meta.json write failed: {e!r}")

        # run.log 오픈
        self._run_log_path = self._run_dir / "run.log"
        self._run_log_fp = open(self._run_log_path, "a", encoding="utf-8", newline="")
        self._run_open = True

        # telemetry.csv 오픈 (header는 첫 write에서)
        self._tele_path = self._run_dir / "telemetry.csv"
        self._tele_fp = open(self._tele_path, "a", encoding="utf-8", newline="")
        self._tele_writer = None
        self._tele_header_written = False

        # 안내 로그
        self._write_log("INFO", f"RUN OPEN -> {self._run_dir}", "LogService", True, ts)

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

        try:
            if self._run_log_fp:
                self._run_log_fp.flush()
                self._run_log_fp.close()
        except Exception:
            pass
        self._run_log_fp = None
        self._run_log_path = None

        self._run_open = False
        self._run_dir = None

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

    def _write_log(self, level: str, msg: str, tag: str, also_ui: bool, ts: Optional[float]) -> None:
        line = self._format_line(level, msg, tag, ts)

        # UI로도 전달
        if also_ui:
            try:
                self.sig_line.emit(line)
            except Exception:
                pass

        # day log
        try:
            self._ensure_day_log_open(ts)
            if self._day_log_fp:
                self._day_log_fp.write(line + "\n")
                self._day_log_fp.flush()
        except Exception as e:
            try:
                self.sig_error.emit(f"[LogService] day log write failed: {e!r}")
            except Exception:
                pass

        # run log (열려있을 때만)
        if self._run_open and self._run_log_fp:
            try:
                self._run_log_fp.write(line + "\n")
                self._run_log_fp.flush()
            except Exception as e:
                try:
                    self.sig_error.emit(f"[LogService] run log write failed: {e!r}")
                except Exception:
                    pass

    def _write_telemetry(self, row: Dict[str, Any], ts: Optional[float]) -> None:
        """
        run이 열려 있을 때만 telemetry.csv에 기록한다.
        row 예시:
          {
            "t": "2026-02-04 12:00:01",
            "step": "PUMPDOWN",
            "pressure": 1.2e-6,
            "thickness_A": 120.0,
            "rate_Aps": 0.8
          }
        """
        if not self._run_open or not self._tele_fp:
            # run 미오픈 상태에서는 텔레메트리 저장하지 않음
            return

        # timestamp column 보장
        row2 = dict(row or {})
        row2.setdefault("t", _dt_str(ts))

        # writer 준비(헤더는 최초 row의 key로 고정)
        if self._tele_writer is None:
            fieldnames = list(row2.keys())
            self._tele_writer = csv.DictWriter(self._tele_fp, fieldnames=fieldnames)
            # 파일이 비었으면 헤더 작성
            try:
                if self._tele_fp.tell() == 0:
                    self._tele_writer.writeheader()
                    self._tele_header_written = True
            except Exception:
                # tell() 실패하면 그냥 헤더 작성 시도
                try:
                    self._tele_writer.writeheader()
                    self._tele_header_written = True
                except Exception:
                    pass

        # 만약 새 row에 새로운 key가 들어오면? (운영 중 컬럼이 바뀌면 CSV가 깨짐)
        # -> 안전하게 "기존 필드만" 기록하고, 추가키는 버린다.
        filtered = {k: row2.get(k, "") for k in self._tele_writer.fieldnames}

        try:
            self._tele_writer.writerow(filtered)
            self._tele_fp.flush()
        except Exception as e:
            try:
                self.sig_error.emit(f"[LogService] telemetry write failed: {e!r}")
            except Exception:
                pass


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
