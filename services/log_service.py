# -*- coding: utf-8 -*-
r"""
services/log_service.py

✅ 목표(사용자 요구 3종 로그만 깔끔히 생성)
1) \\...\Evaporator\\HMIWindowLog\\YYYY-MM-DD.log           (일별 HMI 로그)
2) \\...\Evaporator\\ProcessWindowLog\\<run_id>_<recipe>.log (공정 1개당 ProcessWindow 로그)
3) \\...\Evaporator\\ProcessLog\\<run_id>_<recipe>.csv       (공정 1개당 세부값 CSV)

- UI/공정/장비 서비스가 동시에 로그를 남겨도 안전하게(단일 writer thread) 처리
- NAS 장애 시 자동 fallback(_Logs_local_*)에 저장 후, NAS 복구 시 파일을 NAS로 “합쳐서” 복구(간단/확실 방식)
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
    tag: str = ""
    also_ui: bool = True
    ts: Optional[float] = None

@dataclass(frozen=True)
class CmdUiLine(_CmdBase):
    """
    UI 위젯(appendPlainText/append)에서 올라온 “원문 그대로” 저장용.
    - channel="HMI"     -> 일별 로그에 저장
    - channel="PROCESS" -> run이 열려있으면 run 로그에 저장
    """
    channel: str
    text: str
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
    row: Dict[str, Any]
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
    dt = datetime.fromtimestamp(_now_ts(ts))
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def _time_str(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(_now_ts(ts))
    return dt.strftime("%H:%M:%S")

def _date_str(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(_now_ts(ts))
    return dt.strftime("%Y-%m-%d")

def _safe_name(s: str, max_len: int = 64) -> str:
    s = (s or "").strip()
    s = re.sub(r"[^\w\-. ]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    return s[:max_len] if len(s) > max_len else s

def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


# ============================================================
# Writer Worker
# ============================================================

class LogWriterWorker(QThread):
    sig_line = Signal(str)
    sig_error = Signal(str)

    # ✅ NAS 하위 폴더 고정
    _HMI_SUBDIR = "HMIWindowLog"
    _PW_SUBDIR = "ProcessWindowLog"
    _PROC_SUBDIR = "ProcessLog"

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

        # ✅ base_dir는 “루트” (\\...\Evaporator)
        self._base_dir = Path(base_dir) if base_dir else (Path.cwd() / "_Logs")
        self._fallback_dir = Path.cwd() / f"_Logs_local_{_safe_name(self._app_name)}"

        # base_dir 접근 테스트 캐시
        self._resolved_base_cache: Optional[Path] = None
        self._resolved_base_checked_ts: float = 0.0

        # day log (HMI 일별)
        self._day_log_fp = None
        self._day_log_path: Optional[Path] = None
        self._current_day: Optional[str] = None

        # run files
        self._run_open = False
        self._run_dir: Optional[Path] = None
        self._run_folder_name: str = ""  # <run_id>_<recipe>
        self._run_log_fp = None
        self._run_log_path: Optional[Path] = None

        self._tele_fp = None
        self._tele_writer: Optional[csv.DictWriter] = None
        self._tele_path: Optional[Path] = None

        # run meta
        self._run_id: str = ""
        self._run_recipe: str = ""
        self._run_open_ts: float = 0.0

        self._tele_fieldnames = [
            "time",
            "elapsed_sec",
            "pressure_torr",
            "dac1",
            "dac2",
            "dep.rate",
            "thickness_A",
            "step",
            "detail",
        ]

        # ✅ NAS 복구 감지 주기(초)
        self._last_migrate_check_ts = 0.0
        self._migrate_interval_s = 5.0

    # ---------- public ----------
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
                    self._roll_day_if_needed()
                    self._maybe_migrate_back_to_nas()
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

                self._roll_day_if_needed()
                self._maybe_migrate_back_to_nas()

        finally:
            self._close_all()

    # ---------- internals ----------
    def _handle_cmd(self, cmd: _CmdBase) -> None:
        if isinstance(cmd, CmdSetBaseDir):
            self._base_dir = Path(cmd.base_dir)
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

        if isinstance(cmd, CmdUiLine):
            self._write_ui_line(cmd.channel, cmd.text, cmd.ts)
            return

        if isinstance(cmd, CmdLog):
            self._write_log(cmd.level, cmd.msg, cmd.tag, cmd.also_ui, cmd.ts)
            return

        self._write_log("WARN", f"Unknown command: {cmd!r}", "LogService", True, None)

    def _resolve_base_dir(self, *, force: bool = False) -> Path:
        """
        base_dir 접근 실패 가능성이 있으므로 폴더 생성 가능 여부로 폴백 선택.
        캐시(10초)로 NAS 부담 감소.
        """
        now = time.time()
        if (not force) and self._resolved_base_cache is not None and (now - self._resolved_base_checked_ts) < 10.0:
            return self._resolved_base_cache

        self._resolved_base_checked_ts = now

        try:
            _ensure_dir(self._base_dir)
            self._resolved_base_cache = self._base_dir
            return self._base_dir
        except Exception:
            pass

        try:
            _ensure_dir(self._fallback_dir)
            self._resolved_base_cache = self._fallback_dir
            return self._fallback_dir
        except Exception:
            self._resolved_base_cache = Path.cwd()
            return Path.cwd()

    def _hmi_dir(self, root: Path) -> Path:
        return root / self._HMI_SUBDIR

    def _pw_dir(self, root: Path) -> Path:
        return root / self._PW_SUBDIR

    def _proc_dir(self, root: Path) -> Path:
        return root / self._PROC_SUBDIR

    def _roll_day_if_needed(self) -> None:
        today = _date_str()
        if self._current_day != today:
            self._current_day = today
            self._close_day_log()

    def _ensure_day_log_open(self, ts: Optional[float] = None) -> None:
        """
        ✅ HMI 일별 로그
        - <root>/HMIWindowLog/YYYY-MM-DD.log
        """
        day = _date_str(ts)
        root = self._resolve_base_dir()
        d = self._hmi_dir(root)
        _ensure_dir(d)

        log_path = d / f"{day}.log"
        if self._day_log_fp is not None and self._day_log_path == log_path:
            return

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
        ✅ 공정(run) 파일 2종 생성 (NAS 우선, 실패 시 fallback)
        - <root>/ProcessWindowLog/<run_id>_<recipe>.log
        - <root>/ProcessLog/<run_id>_<recipe>.csv  (+ optional meta.json)
        """
        self._close_run()

        rid = _safe_name(run_id, 48) or _safe_name(datetime.now().strftime("%Y%m%d_%H%M%S"), 48)
        rcp = _safe_name(recipe_name, 48)
        file_stem = f"{rid}_{rcp}" if rcp else rid

        self._run_id = run_id
        self._run_recipe = recipe_name
        self._run_open_ts = _now_ts(ts)
        self._run_folder_name = file_stem

        last_err = None
        chosen_root: Optional[Path] = None

        for root in (self._base_dir, self._fallback_dir):
            try:
                _ensure_dir(root)
                _ensure_dir(self._pw_dir(root))
                _ensure_dir(self._proc_dir(root))

                run_p = self._pw_dir(root) / f"{file_stem}.log"
                run_fp = open(run_p, "a", encoding="utf-8", newline="")

                tele_p = self._proc_dir(root) / f"{file_stem}.csv"
                tele_fp = open(tele_p, "a", encoding="utf-8-sig", newline="")  # Excel 호환

                self._run_dir = root
                self._run_log_path = run_p
                self._run_log_fp = run_fp

                self._tele_path = tele_p
                self._tele_fp = tele_fp
                self._tele_writer = None

                chosen_root = root
                break
            except Exception as e:
                last_err = e
                try:
                    if self._run_log_fp:
                        self._run_log_fp.close()
                except Exception:
                    pass
                try:
                    if self._tele_fp:
                        self._tele_fp.close()
                except Exception:
                    pass
                self._run_log_fp = None
                self._tele_fp = None

        if chosen_root is None:
            try:
                self.sig_error.emit(f"[LogService] RUN OPEN failed (NAS+LOCAL): {last_err!r}")
            except Exception:
                pass
            self._run_open = False
            self._run_dir = None
            self._run_folder_name = ""
            return

        self._run_open = True

        # (선택) meta 저장 (ProcessLog 폴더에 같이)
        if meta:
            try:
                meta_p = self._proc_dir(chosen_root) / f"{file_stem}.meta.json"
                with open(meta_p, "w", encoding="utf-8") as wf:
                    json.dump(meta, wf, ensure_ascii=False, indent=2)
            except Exception:
                pass

        # RUN OPEN은 “일별 로그”에 기록(디버깅용)
        self._write_log("INFO", f"RUN OPEN -> {file_stem}", "LogService", True, ts)

    def _close_run(self) -> None:
        if self._run_open:
            self._write_log("INFO", "RUN CLOSE", "LogService", True, None)

        try:
            if self._run_log_fp:
                self._run_log_fp.flush()
                self._run_log_fp.close()
        except Exception:
            pass
        self._run_log_fp = None
        self._run_log_path = None

        try:
            if self._tele_fp:
                self._tele_fp.flush()
                self._tele_fp.close()
        except Exception:
            pass
        self._tele_fp = None
        self._tele_writer = None
        self._tele_path = None

        self._run_open = False
        self._run_dir = None
        self._run_folder_name = ""

    def _close_all(self) -> None:
        self._close_run()
        self._close_day_log()

    def _format_line(self, level: str, msg: str, tag: str, ts: Optional[float]) -> str:
        level = (level or "INFO").upper()
        tag = (tag or "").strip()
        prefix = f"[{_dt_str(ts)}] [{level}]"
        if tag:
            prefix += f" [{tag}]"
        return f"{prefix} {msg}"

    def _invalidate_base_cache(self) -> None:
        self._resolved_base_cache = None
        self._resolved_base_checked_ts = 0.0

    def _write_log(self, level: str, msg: str, tag: str, also_ui: bool, ts: Optional[float]) -> None:
        """
        ✅ 포맷 로그는 “일별(HMI) 로그”에만 저장
        - run 로그는 ProcessWindow UI 로그(원문)만 저장하도록 분리
        """
        line = self._format_line(level, msg, tag, ts)

        if also_ui:
            try:
                self.sig_line.emit(line)
            except Exception:
                pass

        try:
            self._ensure_day_log_open(ts)
            if self._day_log_fp:
                self._day_log_fp.write(line + "\n")
                self._day_log_fp.flush()
        except Exception:
            try:
                self._close_day_log()
                self._invalidate_base_cache()
                self._ensure_day_log_open(ts)
                if self._day_log_fp:
                    self._day_log_fp.write(line + "\n")
                    self._day_log_fp.flush()
            except Exception:
                pass

    def _write_ui_line(self, channel: str, text: str, ts: Optional[float]) -> None:
        """
        UI 위젯에서 올라온 원문(text)을 그대로 저장
        - HMI     -> 일별 로그
        - PROCESS -> run이 열려있으면 run 로그, 아니면 일별 로그(유실 방지)
        """
        s = str(text).rstrip("\n")
        if not s:
            return

        ch = (channel or "").upper().strip()

        if ch == "HMI":
            try:
                self._ensure_day_log_open(ts)
                if self._day_log_fp:
                    self._day_log_fp.write(s + "\n")
                    self._day_log_fp.flush()
            except Exception:
                # fallback 재시도
                try:
                    self._close_day_log()
                    self._invalidate_base_cache()
                    self._ensure_day_log_open(ts)
                    if self._day_log_fp:
                        self._day_log_fp.write(s + "\n")
                        self._day_log_fp.flush()
                except Exception:
                    pass
            return

        # PROCESS
        if self._run_open and self._run_folder_name:
            try:
                if self._run_log_fp is None:
                    root = self._run_dir or self._resolve_base_dir(force=False)
                    _ensure_dir(self._pw_dir(root))
                    self._run_log_path = self._pw_dir(root) / f"{self._run_folder_name}.log"
                    self._run_log_fp = open(self._run_log_path, "a", encoding="utf-8", newline="")
                if self._run_log_fp:
                    self._run_log_fp.write(s + "\n")
                    self._run_log_fp.flush()
                return
            except Exception:
                # NAS ↔ LOCAL 전환 포함 재오픈 1회 시도
                try:
                    self._invalidate_base_cache()
                    self._reopen_run_files_in_resolved_base()
                    if self._run_log_fp:
                        self._run_log_fp.write(s + "\n")
                        self._run_log_fp.flush()
                        return
                except Exception:
                    pass

        # run이 아직 없으면(또는 run 로그 실패) 일별 로그로 유실 방지
        self._write_ui_line("HMI", f"[PROCESS][NO-RUN] {s}", ts)

    def _reopen_run_files_in_resolved_base(self) -> None:
        """
        run이 열린 상태에서 파일 쓰기 실패 시,
        동일 file_stem으로 NAS/LOCAL 중 가능한 쪽으로 재오픈
        """
        if not self._run_open or not self._run_folder_name:
            return

        try:
            if self._run_log_fp:
                self._run_log_fp.flush()
                self._run_log_fp.close()
        except Exception:
            pass
        self._run_log_fp = None
        self._run_log_path = None

        try:
            if self._tele_fp:
                self._tele_fp.flush()
                self._tele_fp.close()
        except Exception:
            pass
        self._tele_fp = None
        self._tele_writer = None
        self._tele_path = None

        candidates: list[Path] = []
        b0 = self._resolve_base_dir(force=True)
        candidates.append(b0)
        if self._fallback_dir not in candidates:
            candidates.append(self._fallback_dir)

        last_err = None
        for root in candidates:
            try:
                _ensure_dir(root)
                _ensure_dir(self._pw_dir(root))
                _ensure_dir(self._proc_dir(root))

                run_p = self._pw_dir(root) / f"{self._run_folder_name}.log"
                run_fp = open(run_p, "a", encoding="utf-8", newline="")

                tele_p = self._proc_dir(root) / f"{self._run_folder_name}.csv"
                tele_fp = open(tele_p, "a", encoding="utf-8-sig", newline="")

                self._run_dir = root
                self._run_log_path = run_p
                self._run_log_fp = run_fp

                self._tele_path = tele_p
                self._tele_fp = tele_fp
                self._tele_writer = None
                return
            except Exception as e:
                last_err = e
                try:
                    if self._run_log_fp:
                        self._run_log_fp.close()
                except Exception:
                    pass
                try:
                    if self._tele_fp:
                        self._tele_fp.close()
                except Exception:
                    pass
                self._run_log_fp = None
                self._tele_fp = None

        try:
            self.sig_error.emit(f"[LogService] RUN reopen failed: {last_err!r}")
        except Exception:
            pass

    def _write_telemetry(self, row: Dict[str, Any], ts: Optional[float]) -> None:
        if not self._run_open or not self._tele_fp:
            return

        row2 = dict(row or {})
        now_ts = _now_ts(ts)

        row2["time"] = _time_str(now_ts)
        row2["elapsed_sec"] = round(now_ts - self._run_open_ts, 3) if self._run_open_ts > 0 else ""

        if "pressure_torr" not in row2 and "pressure" in row2:
            row2["pressure_torr"] = row2.get("pressure")
        if "dep.rate" not in row2 and "rate_Aps" in row2:
            row2["dep.rate"] = row2.get("rate_Aps")

        row2.setdefault("pressure_torr", "")
        row2.setdefault("dac1", "")
        row2.setdefault("dac2", "")
        row2.setdefault("dep.rate", None)
        row2.setdefault("thickness_A", None)
        row2.setdefault("step", "")
        row2.setdefault("detail", "")

        if isinstance(row2.get("detail"), str):
            row2["detail"] = row2["detail"].replace("\r", " ").replace("\n", " ")

        if self._tele_writer is None:
            self._tele_writer = csv.DictWriter(self._tele_fp, fieldnames=self._tele_fieldnames)
            try:
                if self._tele_fp.tell() == 0:
                    self._tele_writer.writeheader()
            except Exception:
                try:
                    self._tele_writer.writeheader()
                except Exception:
                    pass

        def _norm_cell(k: str, v: Any) -> Any:
            if v is None:
                return "None" if k in ("dep.rate", "thickness_A") else ""
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return "None" if k in ("dep.rate", "thickness_A") else ""
            return v

        filtered = {k: _norm_cell(k, row2.get(k, None)) for k in self._tele_fieldnames}

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

    def _maybe_migrate_back_to_nas(self) -> None:
        """
        ✅ NAS 복구 감지 시:
        - fallback에 쌓인 로그/CSV를 NAS 파일 끝에 “합쳐서” 복구
        - 이후 NAS 쪽으로 재오픈하여 계속 기록
        """
        now = time.time()
        if (now - self._last_migrate_check_ts) < self._migrate_interval_s:
            return
        self._last_migrate_check_ts = now

        # base_dir가 실제로 접근 가능한지 확인
        try:
            root = self._resolve_base_dir(force=True)
        except Exception:
            return
        if root != self._base_dir:
            return  # 아직 NAS 불가

        # 1) day log migrate (오늘 것만)
        day = _date_str()
        fb_day = self._hmi_dir(self._fallback_dir) / f"{day}.log"
        nas_day = self._hmi_dir(self._base_dir) / f"{day}.log"

        if fb_day.exists():
            try:
                _ensure_dir(nas_day.parent)

                # 현재 fallback day log를 열고 있으면 닫기
                if self._day_log_path and str(self._day_log_path).startswith(str(self._fallback_dir)):
                    self._close_day_log()

                # append
                with open(fb_day, "r", encoding="utf-8", errors="replace") as rf, \
                     open(nas_day, "a", encoding="utf-8", newline="") as wf:
                    wf.write(rf.read())

                fb_day.unlink(missing_ok=True)
            except Exception:
                pass

        # 2) run migrate (run open이고, run_dir가 fallback일 때)
        if self._run_open and self._run_folder_name and self._run_dir == self._fallback_dir:
            fb_run = self._pw_dir(self._fallback_dir) / f"{self._run_folder_name}.log"
            nas_run = self._pw_dir(self._base_dir) / f"{self._run_folder_name}.log"

            fb_csv = self._proc_dir(self._fallback_dir) / f"{self._run_folder_name}.csv"
            nas_csv = self._proc_dir(self._base_dir) / f"{self._run_folder_name}.csv"

            # 열려있는 핸들 닫기
            try:
                if self._run_log_fp:
                    self._run_log_fp.flush()
                    self._run_log_fp.close()
            except Exception:
                pass
            self._run_log_fp = None
            self._run_log_path = None

            try:
                if self._tele_fp:
                    self._tele_fp.flush()
                    self._tele_fp.close()
            except Exception:
                pass
            self._tele_fp = None
            self._tele_writer = None
            self._tele_path = None

            try:
                _ensure_dir(nas_run.parent)
                _ensure_dir(nas_csv.parent)

                if fb_run.exists():
                    with open(fb_run, "r", encoding="utf-8", errors="replace") as rf, \
                         open(nas_run, "a", encoding="utf-8", newline="") as wf:
                        wf.write(rf.read())
                    fb_run.unlink(missing_ok=True)

                if fb_csv.exists():
                    # CSV 헤더 중복 방지: NAS 파일이 이미 있으면 fallback 첫 줄(header) 스킵
                    expected_header = ",".join(self._tele_fieldnames)

                    nas_has_data = nas_csv.exists() and nas_csv.stat().st_size > 0
                    with open(fb_csv, "r", encoding="utf-8-sig", errors="replace") as rf, \
                         open(nas_csv, "a", encoding="utf-8-sig", newline="") as wf:
                        first = rf.readline()
                        if nas_has_data and first.strip() == expected_header:
                            pass
                        else:
                            wf.write(first)
                        wf.write(rf.read())
                    fb_csv.unlink(missing_ok=True)

                # NAS로 재오픈
                self._run_dir = self._base_dir
                self._run_log_path = nas_run
                self._run_log_fp = open(nas_run, "a", encoding="utf-8", newline="")

                self._tele_path = nas_csv
                self._tele_fp = open(nas_csv, "a", encoding="utf-8-sig", newline="")
                self._tele_writer = None

            except Exception:
                # migrate 실패 시 다음 주기에 재시도
                self._run_dir = self._fallback_dir


# ============================================================
# Public Service API
# ============================================================

class LogService(QObject):
    sig_line = Signal(str)
    sig_error = Signal(str)

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

    # ---------- UI widget hook ----------
    def attach_text_widget(self, widget: Any, *, channel: str) -> None:
        """
        QPlainTextEdit/QTextEdit의 append 계열 호출을 후킹해서:
        - UI 표시는 그대로 유지
        - 동시에 LogService로 “원문”을 보내 파일 저장

        channel="HMI" / "PROCESS"
        """
        if widget is None:
            return

        if getattr(widget, "_evap_ls_wrapped", False):
            return

        ch = (channel or "").upper().strip()

        try:
            if hasattr(widget, "appendPlainText"):
                orig = widget.appendPlainText

                def wrapped(s: object) -> None:
                    try:
                        txt = str(s)
                        # 멀티라인이면 line 단위로 저장(파일 가독성)
                        for line in re.split(r"\r?\n", txt.rstrip("\n")):
                            if line:
                                self.ui_line(line, channel=ch)
                    except Exception:
                        pass
                    orig(str(s))

                widget.appendPlainText = wrapped  # type: ignore[attr-defined]

            if hasattr(widget, "append"):
                orig2 = widget.append

                def wrapped2(s: object) -> None:
                    try:
                        txt = str(s)
                        for line in re.split(r"\r?\n", txt.rstrip("\n")):
                            if line:
                                self.ui_line(line, channel=ch)
                    except Exception:
                        pass
                    orig2(str(s))

                widget.append = wrapped2  # type: ignore[attr-defined]

            widget._evap_ls_wrapped = True  # type: ignore[attr-defined]
        except Exception:
            pass

    def ui_line(self, text: str, *, channel: str) -> None:
        self._worker.post(CmdUiLine(channel=str(channel), text=str(text)))

    # ---------- run control ----------
    # NOTE:
    # - 실제 run open/close 호출 주체는 ProcessController/Engine이어야 함
    # - main.py는 UI만 담당하고, 정상 공정 흐름에서 직접 open_run()/close_run()를 호출하지 않음
    def open_run(self, run_id: str, recipe_name: str = "", meta: Optional[Dict[str, Any]] = None) -> None:
        self._worker.post(CmdOpenRun(run_id=run_id, recipe_name=recipe_name, meta=meta))

    def close_run(self) -> None:
        self._worker.post(CmdCloseRun())

    # ---------- telemetry ----------
    def telemetry(self, row: Dict[str, Any]) -> None:
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