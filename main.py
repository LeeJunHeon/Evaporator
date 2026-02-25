# main.py
from __future__ import annotations

import gc
import sys
import time
import contextlib
import re                      # ✅ 추가
from uuid import uuid4          # ✅ 추가
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

# ✅ 어디서 실행하든(import 깨짐 방지) 가장 먼저 보정
_BASE_DIR = Path(__file__).resolve().parent
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

from PySide6.QtWidgets import QApplication, QWidget, QMessageBox, QDialog, QVBoxLayout
from PySide6.QtCore import Qt, QTimer

# ✅ 그래프 위젯(별도 파일)
from ui.rt_plot_widget import DepositionPlotWidget
from ui.mainWindow import Ui_Form
from ui.config_dialog import ConfigDialog
from config.plc_config import load_plc_settings
from controller.hmi_plc_binder import HmiPlcBinder
from ui.material_catalog_dialog import MaterialCatalogDialog
from controller.process_controller import ProcessController
from services.log_service import LogService
from services.stm_service import STMService
from services.acs_service import ACSService


# ============================================================
# UI 텍스트 출력 유틸 (QPlainTextEdit / QTextEdit / QLabel 등 방어)
# ============================================================
def _append_text(widget: Any, text: str) -> None:
    if widget is None:
        return
    s = str(text).rstrip("\n")
    try:
        # QPlainTextEdit
        if hasattr(widget, "appendPlainText"):
            widget.appendPlainText(s)
            return
        # QTextEdit
        if hasattr(widget, "append"):
            widget.append(s)
            return
        # QLabel / QLineEdit
        if hasattr(widget, "setText") and hasattr(widget, "text"):
            prev = widget.text() or ""
            widget.setText((prev + "\n" + s).strip())
            return
    except Exception:
        pass


# ============================================================
# NAS 로그 경로(요구사항)
# ============================================================
NAS_LOG_ROOT = Path(r"\\VanaM_NAS\VanaM_toShare\JH_Lee\Logs\Evaporator")
PROCESS_LOG_DIR = NAS_LOG_ROOT / "ProcessLog"
PROCESS_WINDOW_LOG_DIR = NAS_LOG_ROOT / "ProcessWindowLog"
HMI_WINDOW_LOG_DIR = NAS_LOG_ROOT / "HMIWindowLog"


class DailyWindowLogWriter:
    """
    화면 로그용: 폴더/날짜별 1파일 (YYYY-MM-DD.log)
    - 파일명에 시간 안 넣음 (요구사항)
    - 날짜 넘어가면 자동으로 다음 파일로 rotate
    - NAS 쓰기 실패 시 로컬 fallback으로 저장
    """
    def __init__(self, folder: Path, *, fallback_root: Optional[Path] = None):
        self._folder = Path(folder)
        self._fallback_root = Path(fallback_root) if fallback_root else (Path.cwd() / "_Logs_local_Evaporator")
        self._cur_date = ""
        self._fp = None
        self._active_folder = None  # 성공한 저장 위치

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _ensure_open(self) -> None:
        d = self._today()
        if self._fp is not None and self._cur_date == d:
            return

        self.close()
        self._cur_date = d

        # 1) NAS 우선
        for base in (self._folder, self._fallback_root / self._folder.name):
            try:
                base.mkdir(parents=True, exist_ok=True)
                path = base / f"{d}.log"
                self._fp = open(path, "a", encoding="utf-8", newline="")
                self._active_folder = base
                return
            except Exception:
                self._fp = None
                self._active_folder = None

    def write(self, line: str) -> None:
        try:
            self._ensure_open()
            if not self._fp:
                return
            s = str(line).rstrip("\n")
            self._fp.write(s + "\n")
            self._fp.flush()
        except Exception:
            # 화면 로그는 공정에 영향 주면 안 되므로 silent fail
            pass

    def close(self) -> None:
        try:
            if self._fp:
                self._fp.flush()
                self._fp.close()
        except Exception:
            pass
        self._fp = None


class RunWindowLogWriter:
    """
    ProcessWindow 화면 로그용:
    ✅ 공정(run) 1개 = 파일 1개

    - open_run(run_id, recipe_name): 해당 run 파일 열기
    - write(line): run 파일이 열려 있을 때만 기록
    - close_run(): run 파일 닫기
    - NAS 실패 시 로컬 fallback으로 저장
    """
    def __init__(self, folder: Path, *, fallback_root: Optional[Path] = None):
        self._folder = Path(folder)
        self._fallback_root = Path(fallback_root) if fallback_root else (Path.cwd() / "_Logs_local_Evaporator")

        self._fp = None
        self._path: Optional[Path] = None
        self._active_folder: Optional[Path] = None

        self._run_id: Optional[str] = None
        self._recipe_name: str = ""

    @staticmethod
    def _sanitize_name(s: str, *, max_len: int = 80) -> str:
        """
        파일명 안전 처리:
        - 영문/숫자/한글/._- 만 허용, 나머지는 _
        - 길이 제한
        """
        s = str(s or "").strip()
        s = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", s)
        s = s.strip("._-")
        if not s:
            s = "run"
        return s[:max_len]

    def open_run(self, run_id: str, recipe_name: str = "") -> None:
        # 기존 열려있던 파일이 있으면 닫기
        self.close_run()

        rid = self._sanitize_name(run_id, max_len=80)
        rname = self._sanitize_name(recipe_name, max_len=60) if recipe_name else ""
        fname = f"{rid}.log" if not rname else f"{rid}__{rname}.log"

        # 1) NAS 우선, 실패하면 로컬 fallback
        for base in (self._folder, self._fallback_root / self._folder.name):
            try:
                base.mkdir(parents=True, exist_ok=True)
                path = base / fname
                self._fp = open(path, "a", encoding="utf-8", newline="")
                self._path = path
                self._active_folder = base
                self._run_id = rid
                self._recipe_name = rname
                return
            except Exception:
                self._fp = None
                self._path = None
                self._active_folder = None

    def write(self, line: str) -> None:
        """
        run 파일이 open된 상태에서만 기록.
        화면 로그 저장이 공정에 영향 주면 안 되므로 예외는 모두 silent.
        """
        try:
            if not self._fp:
                return
            s = str(line).rstrip("\n")
            self._fp.write(s + "\n")
            self._fp.flush()
        except Exception:
            pass

    def close_run(self) -> None:
        try:
            if self._fp:
                self._fp.flush()
                self._fp.close()
        except Exception:
            pass
        self._fp = None
        self._path = None
        self._active_folder = None
        self._run_id = None
        self._recipe_name = ""

    # 기존 코드(closeEvent) 호환용
    def close(self) -> None:
        self.close_run()


def _wrap_append_for_daily_log(widget: Any, writer: Any) -> None:
    """
    QPlainTextEdit.appendPlainText / QTextEdit.append 호출을 가로채서
    화면에 찍히는 그대로 파일에도 1줄씩 저장.
    (HmiPlcBinder처럼 widget에 직접 appendPlainText 하는 케이스까지 잡기 위해 필요)
    """
    if widget is None or writer is None:
        return
    if getattr(widget, "_daily_log_wrapped", False):
        return
    try:
        setattr(widget, "_daily_log_wrapped", True)
    except Exception:
        pass

    if hasattr(widget, "appendPlainText"):
        orig = widget.appendPlainText

        def wrapped(s):
            writer.write(s)
            return orig(s)

        widget.appendPlainText = wrapped
        return

    if hasattr(widget, "append"):
        orig = widget.append

        def wrapped(s):
            writer.write(s)
            return orig(s)

        widget.append = wrapped
        return
    

# ============================================================
# HMI 창
# ============================================================
class HmiWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.ui = Ui_Form()
        self.ui.setupUi(self)

        # ✅ HMI 화면 로그 -> \\...\Evaporator\HMIWindowLog\YYYY-MM-DD.log
        self._hmi_window_log_writer = DailyWindowLogWriter(HMI_WINDOW_LOG_DIR)
        _wrap_append_for_daily_log(getattr(self.ui, "hmiLogWindow", None), self._hmi_window_log_writer)

        self.setWindowTitle("HMI")
        self.ui.stackedWidget.setCurrentIndex(0)  # HMI page

        self.process_window: Optional["ProcessWindow"] = None
        self._closing_all = False

        # ✅ Config / runtime objects (기존 유지)
        self._ini_path = _BASE_DIR / "config" / "devices.ini"
        self._plc_binder: HmiPlcBinder | None = None

        # ✅ 신규 공정/서비스 (있으면 주입)
        self._process_controller: Any = None
        self._log_service: Any = None
        self._stm_service: Any = None
        self._acs_service: Any = None

        # Process 버튼: Process 창 앞으로 (기존 유지)
        self.ui.processBtn.clicked.connect(self.goto_process_window)

        # Config 버튼: 팝업 열기 (기존 유지)
        if hasattr(self.ui, "configBtn"):
            self.ui.configBtn.clicked.connect(self.open_config_dialog)

    def goto_process_window(self):
        # ✅ 닫혀있거나 아직 없으면 새로 생성 (기존 유지)
        if self.process_window is None:
            self.process_window = ProcessWindow()
            self.process_window.set_hmi_window(self)

            self.process_window.setAttribute(Qt.WA_DeleteOnClose, True)
            self.process_window.destroyed.connect(self._on_process_destroyed)

            # ✅ 신규 공정/로그/센서 주입
            self.process_window.set_runtime_objects(
                self._plc_binder,           # ✅ positional 1
                self._ini_path,             # ✅ positional 2
                process_controller=self._process_controller,
                log_service=self._log_service,
                stm_service=self._stm_service,
                acs_service=self._acs_service,
            )

        self.process_window.show()
        self.process_window.raise_()
        self.process_window.activateWindow()

    def _on_process_destroyed(self, *args):
        self.process_window = None

    def set_runtime_objects(
        self,
        plc_binder: HmiPlcBinder,
        ini_path: Path,
        *,
        process_controller: Any = None,
        log_service: Any = None,
        stm_service: Any = None,
        acs_service: Any = None,
    ) -> None:
        """main()에서 만든 런타임 객체 주입(PLC/센서 재연결에 사용)"""
        self._plc_binder = plc_binder
        self._ini_path = Path(ini_path)

        self._process_controller = process_controller
        self._log_service = log_service
        self._stm_service = stm_service
        self._acs_service = acs_service

    def _set_process_controller_devices(self, stm: Any, acs: Any) -> None:
        """ProcessController가 stm/acs를 참조한다면 여기서 갈아끼움."""
        pc = self._process_controller
        if pc is None:
            return
        for attr, val in (("stm", stm), ("acs", acs)):
            try:
                if hasattr(pc, attr):
                    setattr(pc, attr, val)
                elif hasattr(pc, f"_{attr}"):
                    setattr(pc, f"_{attr}", val)
            except Exception:
                pass

    def open_config_dialog(self) -> None:
        """Config 팝업 → Save(=Accepted)면 즉시 장비 재연결 (기존 유지)"""
        dlg = ConfigDialog(ini_path=self._ini_path, parent=self)
        ret = dlg.exec()
        if ret == QDialog.Accepted:
            self._apply_config_and_reconnect()

    def _apply_config_and_reconnect(self) -> None:
        errors: list[str] = []

        # ✅ 1) PLC 즉시 재연결
        try:
            if self._plc_binder:
                new_plc_settings = load_plc_settings(self._ini_path)
                self._plc_binder.reload_settings(new_plc_settings)
        except Exception as e:
            errors.append(f"PLC reconnect failed: {e}")

        # ✅ 2) ACS는 "항상 켜져있는 장비"이므로 config 저장 시 즉시 reload 적용
        try:
            if self._acs_service is not None:
                # devices.ini 반영
                if hasattr(self._acs_service, "reload_from_ini"):
                    self._acs_service.reload_from_ini(self._ini_path)
                # CON 모드 유지(1초)
                if hasattr(self._acs_service, "set_stream_mode"):
                    self._acs_service.set_stream_mode(stream=True, stream_interval_a=1)
        except Exception as e:
            errors.append(f"ACS reconnect failed: {e}")

        # ✅ 3) STM은 Start에서만(F.T.M ON 후) 연결 (기존 규칙 유지)
        if errors:
            QMessageBox.warning(self, "Reconnect", "일부 장비 재연결 실패:\n" + "\n".join(errors))
        else:
            QMessageBox.information(
                self,
                "Reconnect",
                "저장 완료. (PLC/ACS 즉시 적용)\nSTM은 다음 Start에서 FTM ON 후 새로 연결됩니다.",
            )

    def _confirm_exit(self) -> bool:
        ret = QMessageBox.question(
            self,
            "종료 확인",
            "정말 종료하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return ret == QMessageBox.Yes

    def closeEvent(self, event):
        if getattr(self, "_closing_all", False):
            try:
                if getattr(self, "_hmi_window_log_writer", None):
                    self._hmi_window_log_writer.close()
            except Exception:
                pass
            event.accept()
            return

        if not self._confirm_exit():
            event.ignore()
            return

        self._closing_all = True

        # ✅ Process가 열려있으면 같이 닫기 (기존 유지)
        if self.process_window is not None:
            try:
                self.process_window._closing_all = True
                self.process_window.close()
            except Exception:
                pass
            self.process_window = None

        # ✅ accept 전에 닫아도 OK (flush 보장)
        try:
            if getattr(self, "_hmi_window_log_writer", None):
                self._hmi_window_log_writer.close()
        except Exception:
            pass

        event.accept()


# ============================================================
# Process 창
# ============================================================
class ProcessWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.ui = Ui_Form()
        self.ui.setupUi(self)

        # ✅ Process 화면 로그 -> 공정(run) 1개 = 파일 1개
        self._process_window_log_writer = RunWindowLogWriter(PROCESS_WINDOW_LOG_DIR)
        _wrap_append_for_daily_log(getattr(self.ui, "logWindow", None), self._process_window_log_writer)

        # ✅ 공정별 ProcessWindowLog 파일 식별자
        self._active_run_id: Optional[str] = None
        self._active_recipe_name: str = ""

        self.setWindowTitle("Process")
        self.ui.stackedWidget.setCurrentIndex(1)  # Process page

        self.hmi_window: Optional[HmiWindow] = None
        self._closing_all = False
        self._close_stop_guard = False   # ✅ closeEvent에서 stop 중복 실행 방지

        self._process_controller: Any = None
        self._log_service: Any = None
        self._stm_service: Any = None
        self._acs_service: Any = None

        self._material_1 = None
        self._material_2 = None

        self.ui.materialEdit.clicked.connect(lambda: self._open_material_dialog(1))
        self.ui.materialEdit2.clicked.connect(lambda: self._open_material_dialog(2))

        self.ui.hmiBtn.clicked.connect(self.goto_hmi_window)

        self.ui.startProcess.clicked.connect(self._on_start_clicked)   
        self.ui.stopProcess.clicked.connect(self._on_stop_clicked)   

        # =========================
        # ✅ RT 표시(1초) + 그래프
        # =========================
        self._last_rate: Optional[float] = None
        self._last_thickness: Optional[float] = None
        self._last_power: Optional[float] = None   # ✅ 추가

        self._plot: Optional[DepositionPlotWidget] = None
        self._init_rt_plot()  # graphWidget 자리에 plot 삽입

        self._rt_timer = QTimer(self)
        self._rt_timer.setInterval(1000)  # ✅ 1초
        self._rt_timer.timeout.connect(self._tick_rt_ui)

    def set_hmi_window(self, hmi_window: HmiWindow):
        self.hmi_window = hmi_window

    def set_runtime_objects(self, plc_binder, ini_path: Path, *, process_controller=None, log_service=None,
                            stm_service=None, acs_service=None) -> None:
        self._plc_binder = plc_binder
        self._ini_path = Path(ini_path)

        self._process_controller = process_controller
        self._log_service = log_service
        self._stm_service = stm_service
        self._acs_service = acs_service

        # ProcessController 시그널 → UI 업데이트
        pc = self._process_controller
        if pc is not None:
            try:
                pc.sig_ui_log.connect(lambda s: _append_text(getattr(self.ui, "logWindow", None), s))
            except Exception:
                pass
            try:
                pc.sig_status.connect(self._on_status)
            except Exception:
                pass
            try:
                pc.sig_error.connect(self._on_error)
            except Exception:
                pass
            try:
                pc.sig_finished.connect(self._on_finished)
            except Exception:
                pass

    def goto_hmi_window(self):
        if not self.hmi_window:
            return
        self.hmi_window.show()
        self.hmi_window.raise_()
        self.hmi_window.activateWindow()

    def _bind_stm_ui(self, stm):
        self._unbind_stm_ui()
        stm.sig_rate.connect(self._on_stm_rate)
        stm.sig_thickness.connect(self._on_stm_thickness)

    def _unbind_stm_ui(self):
        if not self._stm_service:
            return
        try: self._stm_service.sig_rate.disconnect(self._on_stm_rate)
        except: pass
        try: self._stm_service.sig_thickness.disconnect(self._on_stm_thickness)
        except: pass

    def _on_stm_rate(self, rate):
        # ✅ 값만 저장 (UI/그래프는 1초 타이머에서)
        try:
            self._last_rate = float(rate)
        except Exception:
            self._last_rate = None

    def _on_stm_thickness(self, th):
        try:
            self._last_thickness = float(th)
        except Exception:
            self._last_thickness = None

    def _ensure_sensors_connected_fresh(self) -> bool:
        ini_path = self.hmi_window._ini_path

        # ✅ 이전 STM만 정리 (ACS는 유지)
        self._shutdown_sensors_and_release_memory()

        try:
            # ✅ 1) FTM 먼저 ON (STM 연결 선행 조건)
            binder = getattr(self.hmi_window, "_plc_binder", None)
            if binder is None:
                _append_text(self.ui.logWindow, "[DEV][ERR] plc_binder is None (cannot turn on FTM_SW) -> abort STM connect")
                return False

            binder.enqueue_write("FTM_SW", True)
            _append_text(self.ui.logWindow, "[DEV] FTM_SW -> ON (before STM connect)")

            # ✅ 2) 잠깐 대기(장비 전원/통신 준비)
            time.sleep(1.5)

            # ✅ 3) STM 연결(폴링 시작)
            stm = STMService(ini_path=ini_path)
            stm.start()

            self._stm_service = stm
            self.hmi_window._stm_service = stm

            # ✅ 4) ACS는 main()에서 이미 실행 중인 인스턴스를 그대로 사용
            self._acs_service = getattr(self.hmi_window, "_acs_service", None)

            # ✅ 5) ProcessController 장치 참조 갱신
            self.hmi_window._set_process_controller_devices(stm, self._acs_service)

            _append_text(self.ui.logWindow, "[DEV] STM connected (ACS kept alive).")
            return True

        except Exception as e:
            _append_text(self.ui.logWindow, f"[DEV][ERR] STM start failed: {e!r}")
            self._stm_service = None
            self.hmi_window._stm_service = None
            # ✅ ACS는 유지 (절대 None으로 덮지 않음)
            return False

    def _shutdown_sensors_and_release_memory(self) -> None:
        """Stop에서 호출: STM 연결 해제 + 객체 해제 + gc.collect()
        ✅ ACS는 항상 켜져있는 장비이므로 여기서 stop하지 않는다.
        """

        # ✅ 먼저 UI 시그널 해제
        try:
            self._unbind_stm_ui()
        except Exception:
            pass

        # ✅ 1) STM만 stop()
        if self._stm_service is not None:
            try:
                self._stm_service.stop()
                _append_text(self.ui.logWindow, "[DEV] STM stop() called")
            except Exception as e:
                _append_text(self.ui.logWindow, f"[DEV][WARN] STM stop failed: {e!r}")

        # ✅ 2) ProcessController에서 STM 참조만 끊는다(ACS는 유지)
        try:
            pc = self._process_controller
            if pc is not None:
                if hasattr(pc, "stm"): setattr(pc, "stm", None)
                if hasattr(pc, "_stm"): setattr(pc, "_stm", None)
        except Exception:
            pass

        # ✅ 3) 참조 제거(ACS는 건드리지 않음)
        self._stm_service = None
        if self.hmi_window is not None:
            self.hmi_window._stm_service = None

        # 4) GC
        gc.collect()
        _append_text(self.ui.logWindow, "[DEV] STM released + gc.collect() (ACS kept alive)")

    def _on_start_clicked(self) -> None:
        pc = self._process_controller
        if pc is None:
            QMessageBox.warning(self, "Process", "ProcessController가 연결되지 않았습니다.")
            return

        # ✅ 1) UI 입력값 먼저 검증
        run_cfg = self._collect_ui_run_cfg()
        if run_cfg is None:
            return

        # ✅ (추가) 이번 Start에 사용할 run_id 생성 + 파일 open
        # - 공정 준비/STM 연결 과정 로그도 run 파일에 같이 남기려면 여기서 open하는 게 맞음
        self._active_run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
        mat = str(run_cfg.get("material_name", "") or "").strip()
        self._active_recipe_name = f"EVAP_{mat}" if mat else "EVAP"

        with contextlib.suppress(Exception):
            if getattr(self, "_process_window_log_writer", None):
                self._process_window_log_writer.open_run(self._active_run_id, self._active_recipe_name)

        # ✅ 2) 그 다음 FTM ON → STM 연결
        if not self._ensure_sensors_connected_fresh():
            QMessageBox.warning(self, "Device", "STM 연결 실패")

            # ✅ (추가) Start 실패 → run 파일 닫기
            with contextlib.suppress(Exception):
                if getattr(self, "_process_window_log_writer", None):
                    self._process_window_log_writer.close_run()
            self._active_run_id = None
            self._active_recipe_name = ""
            return

        # ✅ 3) STM UI 바인딩 + RT 시작
        try:
            if self._stm_service is not None:
                self._bind_stm_ui(self._stm_service)
        except Exception:
            pass
        self._rt_start()

        # ✅ 4) 공정 시작
        if not hasattr(pc, "start_from_ui"):
            QMessageBox.warning(
                self,
                "Process",
                "start_from_ui가 구현되어 있지 않습니다.\nProcessController에 start_from_ui를 추가하세요."
            )

            # ✅ (추가) run 파일 닫기
            with contextlib.suppress(Exception):
                if getattr(self, "_process_window_log_writer", None):
                    self._process_window_log_writer.close_run()
            self._active_run_id = None
            self._active_recipe_name = ""

            self._rt_stop()
            self._shutdown_sensors_and_release_memory()
            self._reset_process_ui()
            return

        try:
            # ✅ (변경) run_id를 ProcessController로 전달
            pc.start_from_ui(run_cfg, run_id=self._active_run_id)

        except Exception as e:
            with contextlib.suppress(Exception):
                self._safe_shutdown_plc_best_effort()
            with contextlib.suppress(Exception):
                self._rt_stop()
            with contextlib.suppress(Exception):
                self._shutdown_sensors_and_release_memory()
            with contextlib.suppress(Exception):
                self._reset_process_ui()

            # ✅ (추가) Start 실패 → run 파일 닫기
            with contextlib.suppress(Exception):
                if getattr(self, "_process_window_log_writer", None):
                    self._process_window_log_writer.close_run()
            self._active_run_id = None
            self._active_recipe_name = ""

            QMessageBox.warning(self, "Process Start Failed", f"{e!r}")
            _append_text(self.ui.logWindow, f"[START FAIL] {e!r}")
            return
        
    def _on_stop_clicked(self) -> None:
        try:
            # ✅ 1) 제일 먼저 하드웨어 안전종료(셔터→DAC0→파워OFF)
            self._safe_shutdown_plc_best_effort()

            # ✅ 2) 그 다음 UI RT 멈춤
            self._rt_stop()

            # ✅ 3) 공정 로직 stop
            pc = self._process_controller
            if pc is not None:
                try:
                    pc.stop()
                    _append_text(self.ui.logWindow, "[UI] 공정 정지 요청")
                except Exception as e:
                    _append_text(self.ui.logWindow, f"[STOP FAIL] {e!r}")

            # ✅ 4) Stop에서 STM만 끊고 메모리 해제(ACS 유지)
            self._shutdown_sensors_and_release_memory()

            # ✅ Stop도 '공정 종료'이므로 UI 초기화
            self._reset_process_ui()

        finally:
            # ✅ (추가) 공정 파일 닫기 + run 상태 초기화
            with contextlib.suppress(Exception):
                if getattr(self, "_process_window_log_writer", None):
                    self._process_window_log_writer.close_run()
            self._active_run_id = None
            self._active_recipe_name = ""

    def _on_pause_clicked(self) -> None:
        pc = self._process_controller
        if pc is None:
            return
        try:
            pc.pause()
            _append_text(getattr(self.ui, "logWindow", None), "[UI] 일시정지 요청")
        except Exception as e:
            _append_text(getattr(self.ui, "logWindow", None), f"[PAUSE FAIL] {e!r}")

    def _on_resume_clicked(self) -> None:
        pc = self._process_controller
        if pc is None:
            return
        try:
            pc.resume()
            _append_text(getattr(self.ui, "logWindow", None), "[UI] 재개 요청")
        except Exception as e:
            _append_text(getattr(self.ui, "logWindow", None), f"[RESUME FAIL] {e!r}")

    def _on_status(self, st: Any) -> None:
        # ✅ 그래프용 power 추출(기존 유지)
        self._try_update_last_power(st)

        w = getattr(self.ui, "processMonitor_Process", None)
        if w is None or not hasattr(w, "setText"):
            return

        try:
            # ✅ engine.py가 보내는 “스텝별 표시 메시지” 우선
            m = str(getattr(st, "message", "") or "").strip()
            if m:
                w.setText(m)
                return

            # fallback: message가 없으면 기존처럼 phase/step
            phase = getattr(st, "phase", None)
            step = getattr(st, "step_name", None) or getattr(st, "step", None)
            w.setText(f"{phase} | {step}" if (phase or step) else str(st))

        except Exception:
            pass

    def _try_update_last_power(self, st: Any) -> None:
        """
        ProcessController가 status에 power/dac 관련 값을 넣어줄 수도 있고,
        dict로 올 수도 있어서 최대한 방어적으로 추출한다.
        - 우선순위: total/combined -> (dac1+dac2) -> single
        """
        try:
            # dict 형태
            if isinstance(st, dict):
                for k in ("power", "power_dac", "dac", "dac_power", "power_cmd", "dac_cmd"):
                    if k in st and st[k] is not None:
                        self._last_power = float(st[k])
                        return
                # 2채널
                if ("dac1" in st) or ("dac2" in st):
                    d1 = float(st.get("dac1") or 0.0)
                    d2 = float(st.get("dac2") or 0.0)
                    self._last_power = d1 + d2
                    return
                return

            # 객체 형태
            for name in ("power", "power_dac", "dac", "dac_power", "power_cmd", "dac_cmd"):
                if hasattr(st, name):
                    v = getattr(st, name)
                    if v is not None:
                        self._last_power = float(v)
                        return

            # 2채널 객체 후보
            if hasattr(st, "dac1") or hasattr(st, "dac2"):
                d1 = float(getattr(st, "dac1", 0.0) or 0.0)
                d2 = float(getattr(st, "dac2", 0.0) or 0.0)
                self._last_power = d1 + d2
                return
        except Exception:
            # power는 없어도 공정은 돌 수 있으니 무시
            return

    def _on_error(self, err: Any) -> None:
        try:
            where = getattr(err, "where", "")
            msg = getattr(err, "message", "")
            _append_text(getattr(self.ui, "logWindow", None), f"[ERROR] {where} | {msg}")
        except Exception:
            _append_text(getattr(self.ui, "logWindow", None), f"[ERROR] {err!r}")

    def _on_finished(self, result: Any) -> None:
        try:
            # ✅ UI RT 정지
            with contextlib.suppress(Exception):
                self._rt_stop()

            # ✅ 운영 안전: best-effort 안전정지
            with contextlib.suppress(Exception):
                self._safe_shutdown_plc_best_effort()

            # ✅ 센서/메모리 정리
            with contextlib.suppress(Exception):
                self._shutdown_sensors_and_release_memory()

            # ✅ finished 로그(파일에 남기기 위해 close_run 전에 출력)
            try:
                ok = bool(getattr(result, "ok", False))
                rid = getattr(result, "run_id", "")
                _append_text(getattr(self.ui, "logWindow", None), f"[FINISHED] ok={ok} run_id={rid}")
            except Exception:
                _append_text(getattr(self.ui, "logWindow", None), "[FINISHED]")

        finally:
            # ✅ (추가) 공정 파일 닫기 + run 상태 초기화
            with contextlib.suppress(Exception):
                if getattr(self, "_process_window_log_writer", None):
                    self._process_window_log_writer.close_run()
            self._active_run_id = None
            self._active_recipe_name = ""

            # ✅ 성공/실패/예외 상관없이 UI 초기화 보장
            with contextlib.suppress(Exception):
                self._reset_process_ui()

    def closeEvent(self, event):
        # ✅ Process 창 닫기(X) = Stop 버튼과 동일하게 동작
        #    (셔터 close → DAC 0 → Power off → pc.stop → STM/ACS stop+해제+GC)
        if not getattr(self, "_close_stop_guard", False):
            self._close_stop_guard = True
            try:
                self._on_stop_clicked()
            except Exception:
                # closeEvent에서 예외로 창이 안 닫히면 안 됨(무조건 닫히게)
                pass

        _append_text(getattr(self.ui, "logWindow", None), "[UI] Process window closed -> SAFE STOP (same as Stop button)")

        if self.hmi_window is not None:
            self.hmi_window.process_window = None

        try:
            if getattr(self, "_process_window_log_writer", None):
                self._process_window_log_writer.close()
        except Exception:
            pass

        event.accept()

    def _open_material_dialog(self, channel: int) -> None:
        sel = MaterialCatalogDialog.pick(base_dir=_BASE_DIR, parent=self)
        if not sel:
            return
        self._apply_material(channel, {
            "material": sel.material,
            "density_g_cm3": sel.density_g_cm3,
            "z_factor": sel.z_factor,
        })

    def _apply_material(self, channel: int, data: dict[str, Any]) -> None:
        mat = str(data.get("material", "")).strip()
        den = data.get("density_g_cm3")
        z = data.get("z_factor")

        if channel == 1:
            self._material_1 = dict(data)
            self.ui.materialEdit.setText(mat or "Select")
            self.ui.materialDensityEdit1.setText(f"{float(den):.4f}")
            self.ui.materialZfactorEdit1.setText(f"{float(z):.4f}")
        else:
            self._material_2 = dict(data)
            self.ui.materialEdit2.setText(mat or "Select")
            self.ui.materialDensityEdit2.setText(f"{float(den):.4f}")
            self.ui.materialZfactorEdit2.setText(f"{float(z):.4f}")

    # ================== 그래프 설정 ==================
    def _init_rt_plot(self) -> None:
        """ui.graphWidget 자리에 DepositionPlotWidget을 삽입"""
        host = getattr(self.ui, "graphWidget", None)
        if host is None:
            return

        # 이미 layout이 없으면 생성
        lay = host.layout()
        if lay is None:
            lay = QVBoxLayout(host)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
            host.setLayout(lay)

        # 기존 위젯 제거(중복 방지)
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        self._plot = DepositionPlotWidget(parent=host)
        lay.addWidget(self._plot)

    def _rt_start(self) -> None:
        if self._plot is not None:
            try:
                self._plot.clear()
            except Exception:
                pass
        if not self._rt_timer.isActive():
            self._rt_timer.start()

    def _rt_stop(self) -> None:
        if self._rt_timer.isActive():
            self._rt_timer.stop()

    def _tick_rt_ui(self) -> None:
        """1초마다 lineedit + 그래프 갱신"""
        rate = self._clamp_nonneg(self._last_rate)

        power_plc = self._read_plc_power_dac()
        if power_plc is not None:
            power = self._clamp_nonneg(power_plc)
            self._last_power = power
        else:
            power = self._clamp_nonneg(self._last_power)

        show_th = self._is_main_deposition()
        th = self._clamp_nonneg(self._last_thickness) if show_th else None

        try:
            self.ui.currentRateEdit.setText(f"{rate:.3f}" if rate is not None else "")
        except Exception:
            pass

        try:
            self.ui.currentThicknessEdit.setText(f"{th:.2f}" if th is not None else "")
        except Exception:
            pass

        if self._plot is not None:
            try:
                self._plot.append(rate=rate, power=power)
            except Exception:
                pass
    # ================== 그래프 설정 ==================

    @staticmethod
    def _clamp_nonneg(v: Optional[float]) -> Optional[float]:
        """음수는 0으로 클램프. (None은 그대로)"""
        if v is None:
            return None
        try:
            fv = float(v)
        except Exception:
            return None
        return 0.0 if fv < 0 else fv

    def _is_main_deposition(self) -> bool:
        """메인 공정(메인 셔터 OPEN)일 때만 True."""
        pc = self._process_controller
        try:
            if pc is None or (not pc.is_running()):
                return False
        except Exception:
            return False

        if self.hmi_window is None:
            return False
        binder = getattr(self.hmi_window, "_plc_binder", None)
        if binder is None:
            return False

        try:
            return bool(binder.read_coil("MAIN_SHUTTER_SW", default=False))
        except Exception:
            return False

    def _read_plc_power_dac(self) -> Optional[float]:
        """PLC 폴링(regs) 기준으로 현재 DAC 파워를 읽는다."""
        if self.hmi_window is None:
            return None
        binder = getattr(self.hmi_window, "_plc_binder", None)
        if binder is None:
            return None
        try:
            plc = binder.get_plc_service()
            snap = plc.get_last_snapshot() if plc is not None else None
            if snap is None:
                return None
            regs = getattr(snap, "regs", None) or {}
            p1 = float(regs.get("DAC_POWER_1", 0) or 0.0)
            p2 = float(regs.get("DAC_POWER_2", 0) or 0.0)

            # UI 선택 기준 표시:
            # - 1개 선택이면 해당 채널 DAC 표시
            # - 2개 선택이면 DAC 합계 표시
            use1 = bool(getattr(getattr(self.ui, "sourcePower1", None), "isChecked", lambda: False)())
            use2 = bool(getattr(getattr(self.ui, "sourcePower2", None), "isChecked", lambda: False)())
            if use1 and not use2:
                return p1
            if use2 and not use1:
                return p2
            return p1 + p2
        except Exception:
            return None

    def _reset_process_ui(self) -> None:
        """공정 종료 후: 입력값/물질 선택/그래프/표시값 초기화."""
        # 입력값(있으면 전부 비우기)
        for wname in ("deprateEdit", "deprateEdit2", "powerEdit", "powerEdit2", "thicknessEdit", "delayEdit"):
            w = getattr(self.ui, wname, None)
            if w is not None and hasattr(w, "setText"):
                with contextlib.suppress(Exception):
                    w.setText("")

        # Power 선택 해제
        for wname in ("sourcePower1", "sourcePower2"):
            w = getattr(self.ui, wname, None)
            if w is not None and hasattr(w, "setChecked"):
                with contextlib.suppress(Exception):
                    w.setChecked(False)

        # Material 선택 초기화(내부 상태 + 표시)
        self._material_1 = None
        self._material_2 = None
        with contextlib.suppress(Exception):
            self.ui.materialEdit.setText("Select")
            self.ui.materialDensityEdit1.setText("")
            self.ui.materialZfactorEdit1.setText("")
        with contextlib.suppress(Exception):
            self.ui.materialEdit2.setText("Select")
            self.ui.materialDensityEdit2.setText("")
            self.ui.materialZfactorEdit2.setText("")

        # 현재값 표시 초기화
        for wname in ("currentRateEdit", "currentThicknessEdit"):
            w = getattr(self.ui, wname, None)
            if w is not None and hasattr(w, "setText"):
                with contextlib.suppress(Exception):
                    w.setText("")

        # 공정 모니터 텍스트 초기화
        w = getattr(self.ui, "processMonitor_Process", None)
        if w is not None and hasattr(w, "setText"):
            with contextlib.suppress(Exception):
                w.setText("")

        # 그래프 초기화
        if self._plot is not None:
            with contextlib.suppress(Exception):
                self._plot.clear()

        # 내부 last 값 초기화
        self._last_rate = None
        self._last_thickness = None
        self._last_power = None

    # ================== UI 값 파싱 ==================
    def _read_float(self, wname: str) -> Optional[float]:
        w = getattr(self.ui, wname, None)
        if w is None or not hasattr(w, "text"):
            return None
        s = str(w.text()).strip()
        if not s:
            return None
        try:
            return float(s)
        except Exception:
            return None

    def _collect_ui_run_cfg(self) -> Optional[dict[str, Any]]:
        # ✅ Power 선택: 1개 또는 2개 허용
        p1 = bool(getattr(getattr(self.ui, "sourcePower1", None), "isChecked", lambda: False)())
        p2 = bool(getattr(getattr(self.ui, "sourcePower2", None), "isChecked", lambda: False)())
        if not (p1 or p2):
            QMessageBox.warning(self, "Input", "Power1/Power2 중 최소 1개는 선택해야 합니다.")
            return None

        # ✅ Target Dep.rate: 선택된 power에 해당하는 입력칸만 사용 (fallback 없음)
        rate1 = self._read_float("deprateEdit")
        rate2 = self._read_float("deprateEdit2")

        if p1 and not p2:
            # Power1만 선택 → Dep.rate 1만 본다
            if rate1 is None:
                QMessageBox.warning(self, "Input", "Power1 선택 시 Target Dep.rate 1을 입력하세요.")
                return None
            if rate1 <= 0:
                QMessageBox.warning(self, "Input", "Target Dep.rate 1은 0보다 커야 합니다.")
                return None
            target_rate = rate1

        elif p2 and not p1:
            # Power2만 선택 → Dep.rate 2만 본다
            if rate2 is None:
                QMessageBox.warning(self, "Input", "Power2 선택 시 Target Dep.rate 2를 입력하세요.")
                return None
            if rate2 <= 0:
                QMessageBox.warning(self, "Input", "Target Dep.rate 2는 0보다 커야 합니다.")
                return None
            target_rate = rate2

        else:
            # Power1 + Power2 동시 선택
            # 현재 ProcessController는 target_rate가 1개만 들어가도록 설계되어 있어서(= 엔진도 1개 목표로 동작),
            # 두 값을 다르게 받으면 이후 단계에서 모순/오동작 가능 → 둘 다 입력 + 동일값 강제
            if rate1 is None:
                QMessageBox.warning(self, "Input", "Power1+Power2 동시 사용 시 Target Dep.rate 1을 입력하세요.")
                return None
            if rate2 is None:
                QMessageBox.warning(self, "Input", "Power1+Power2 동시 사용 시 Target Dep.rate 2를 입력하세요.")
                return None
            if rate1 <= 0 or rate2 <= 0:
                QMessageBox.warning(self, "Input", "Target Dep.rate 1/2는 0보다 커야 합니다.")
                return None
            if abs(rate1 - rate2) > 1e-9:
                QMessageBox.warning(self, "Input", "Power1+Power2 동시 사용 시 Target Dep.rate 1과 2는 동일해야 합니다.")
                return None

            target_rate = rate1

        # 공통 입력값
        target_thk = self._read_float("thicknessEdit")
        delay_min = self._read_float("delayEdit")
        if target_thk is None:
            QMessageBox.warning(self, "Input", "Target Thickness를 입력하세요.")
            return None
        if target_thk <= 0:
            QMessageBox.warning(self, "Input", "Target Thickness는 0보다 커야 합니다.")
            return None
        if delay_min is None:
            delay_min = 0.0
        if delay_min < 0:
            QMessageBox.warning(self, "Input", "Delay(min)은 0 이상이어야 합니다.")
            return None

        if p1 and p2:
            if not (self._material_1 or self._material_2):
                QMessageBox.warning(self, "Input", "Power1+Power2 동시 사용 시 Material을 최소 1개는 선택해야 합니다.")
                return None
            base_mat = self._material_1 or self._material_2

            # 둘 다 선택되어 있을 때 둘 다 material이 존재하면 동일성 체크
            if self._material_1 and self._material_2:
                m1 = str(self._material_1.get("material", "")).strip()
                m2 = str(self._material_2.get("material", "")).strip()
                d1 = float(self._material_1.get("density_g_cm3", 0.0) or 0.0)
                d2 = float(self._material_2.get("density_g_cm3", 0.0) or 0.0)
                z1 = float(self._material_1.get("z_factor", 0.0) or 0.0)
                z2 = float(self._material_2.get("z_factor", 0.0) or 0.0)

                if (m1 != m2) or (abs(d1 - d2) > 1e-9) or (abs(z1 - z2) > 1e-9):
                    QMessageBox.warning(
                        self,
                        "Input",
                        "Power1+Power2 동시 사용은 '동일 물질' 가정입니다.\n"
                        "Material1/Material2가 서로 다릅니다. 동일하게 맞춰주세요.",
                    )
                    return None
        else:
            base_mat = self._material_1 if p1 else self._material_2

        mat_name = str((base_mat or {}).get("material", "")).strip()
        den = float((base_mat or {}).get("density_g_cm3", 0.0) or 0.0)
        zf = float((base_mat or {}).get("z_factor", 0.0) or 0.0)

        if not mat_name:
            QMessageBox.warning(self, "Input", "Material 이름이 비어있습니다. Material을 다시 선택하세요.")
            return None
        if den <= 0 or zf <= 0:
            QMessageBox.warning(self, "Input", "Material density/z-factor 값이 올바르지 않습니다.")
            return None

        cfg: dict[str, Any] = {
            "use_power1": p1,
            "use_power2": p2,

            "material_name": mat_name,
            "density": den,
            "z_factor": zf,

            # ✅ 목표 dep.rate는 이것 하나만
            "target_rate": float(target_rate),

            "target_thickness": float(target_thk),
            "delay_min": float(delay_min),

            # (참고용: 동일 물질 체크/디버깅용)
            "material_1": self._material_1,
            "material_2": self._material_2,
        }
        return cfg
    # ================== UI 값 파싱 ==================

    # ================== 공정 종료 함수 ==================
    def _safe_shutdown_plc_best_effort(self) -> None:
        """
        Stop 시 안전종료 순서:
        1) MAIN_SHUTTER close
        2) DAC 0
        3) POWER off
        """
        if self.hmi_window is None:
            return
        binder = getattr(self.hmi_window, "_plc_binder", None)
        if binder is None:
            _append_text(self.ui.logWindow, "[PLC][WARN] plc_binder is None")
            return

        # 1) main shutter close
        try:
            binder.enqueue_write("MAIN_SHUTTER_SW", False)
            _append_text(self.ui.logWindow, "[SAFE] MAIN_SHUTTER -> CLOSE")
        except Exception as e:
            _append_text(self.ui.logWindow, f"[SAFE][WARN] MAIN_SHUTTER close failed: {e!r}")

        # 2) DAC=0은 무조건 시도(미구현이면 그게 버그로 드러나야 함)
        try:
            binder.enqueue_write_reg("DAC_POWER_1", 0)
            binder.enqueue_write_reg("DAC_POWER_2", 0)
            _append_text(self.ui.logWindow, "[SAFE] DAC_POWER_1/2 -> 0")
        except Exception as e:
            _append_text(self.ui.logWindow, f"[SAFE][ERROR] DAC=0 FAILED (MUST FIX): {e!r}")

        # 3) power off
        try:
            binder.enqueue_write("POWER_1_SW", False)
            binder.enqueue_write("POWER_2_SW", False)
            _append_text(self.ui.logWindow, "[SAFE] POWER_1/2 -> OFF")
        except Exception as e:
            _append_text(self.ui.logWindow, f"[SAFE][WARN] POWER off failed: {e!r}")

        self._last_power = 0.0
    # ================== 공정 종료 함수 ==================


# ============================================================
# main
# ============================================================
def main():
    app = QApplication(sys.argv)

    hmi = HmiWindow()

    # ------------------------------
    # PLC 바인딩 시작 (✅ 기존 로직 유지)
    # ------------------------------
    ini_path = _BASE_DIR / "config" / "devices.ini"
    plc_settings = load_plc_settings(ini_path)

    # ✅ PLC 연결/버튼 팝업/원복 로직은 HmiPlcBinder가 계속 담당 (그대로 유지)
    plc_binder = HmiPlcBinder(hmi.ui, plc_settings)
    plc_binder.start()
    app.aboutToQuit.connect(plc_binder.stop)

    # ✅ HMI 로그는 PLC binder가 찍는 포맷([HH:MM:SS] ...)으로 통일
    def _hmi_log(msg: object) -> None:
        s = str(msg).rstrip('\n')
        # 1) 가능하면 HmiPlcBinder의 로그 포맷/스크롤 정책을 그대로 사용
        try:
            fn = getattr(plc_binder, '_set_hmi_log', None)
            if callable(fn):
                fn(s)
                return
        except Exception:
            pass
        # 2) fallback: 시간 프리픽스만 붙여서 직접 append
        try:
            _append_text(getattr(hmi.ui, 'hmiLogWindow', None), f"[{time.strftime('%H:%M:%S')}] {s}")
        except Exception:
            pass

    # ✅ STM은 Start에서(FTM ON 후) 연결한다.
    stm_service: Any = None

    # ✅ ACS는 프로그램 부팅 즉시 연결 + CON(1초) 스트리밍
    acs_service: Any = None
    try:
        acs_service = ACSService(
            ini_path=ini_path,
            poll_s=0.25,                # 워커 루프(읽기 템포). 스트림은 장비가 1초마다 보냄
            reconnect_interval_s=1.0,   # 실패 시 재시도 템포
            use_stream=True,            # ✅ CON 모드
            stream_interval_a=1,        # ✅ 1초
        )

        # ✅ ACS 연결 상태도 상단 상태라인에 항상 보이도록 기본값 세팅
        plc_binder.set_external_connected("ACS", False)

        # UI 표시: HMI 페이지 pressureValue 업데이트(없으면 무시)
        def _update_pressure(v: object) -> None:
            try:
                w = getattr(hmi.ui, "pressureValue", None)
                if w is None or not hasattr(w, "setText"):
                    return

                # 연결 끊김/값 없음
                if v is None:
                    w.setText("--- Torr")
                    return

                # 숫자 타입이면 그대로 포맷
                if isinstance(v, (int, float)):
                    w.setText(f"{float(v):.3e} Torr")
                    return

                # 문자열/기타 타입이면 숫자 파싱 시도
                s = str(v).strip()
                if not s:
                    w.setText("--- Torr")
                    return

                # "1.2e-6", "1.2e-6 Torr" 같은 케이스 처리
                token = s.split()[0]
                try:
                    w.setText(f"{float(token):.3e} Torr")
                except Exception:
                    # "OR", "OVR", "ERR..." 같은 비정상/상태 문자열은 그대로 표시
                    w.setText(s)

            except Exception:
                pass

        def _acs_connected(ok: bool) -> None:
            # ✅ 상단 상태라인에는 연결 상태만(PLC/ACS)
            try:
                plc_binder.set_external_connected("ACS", bool(ok))
            except Exception:
                pass

            # 연결 끊김이면 pressure 표시 제거
            if not ok:
                _update_pressure(None)

        acs_service.sig_pressure.connect(_update_pressure)
        acs_service.sig_connected.connect(_acs_connected)
        acs_service.sig_error.connect(_hmi_log)

        acs_service.start()
    except Exception as e:
        acs_service = None
        try:
            plc_binder.set_external_connected("ACS", False)
        except Exception:
            pass
        _hmi_log(f"[BOOT][WARN] ACSService init failed: {e!r}")

    # ------------------------------
    # LogService (신규)
    # ------------------------------
    log_service: Any = None
    try:
        try:
            log_service = LogService(app_name="Evaporator", base_dir=PROCESS_LOG_DIR)
        except TypeError:
            log_service = LogService()
        if hasattr(log_service, "start"):
            log_service.start()
    except Exception:
        log_service = None

    # ------------------------------
    # ProcessController (신규)
    # ------------------------------
    process_controller: Any = None
    try:
        process_controller = ProcessController(
            plc=getattr(plc_binder, "_plc"),
            log=log_service,
            stm=None,          # STM은 Start에서 붙임
            acs=acs_service,   # ✅ ACS는 부팅부터 유지
        )
    except Exception as e:
        process_controller = None
        _hmi_log(f"[BOOT][WARN] ProcessController init failed: {e!r}")

    # 종료 시 신규 서비스 정리
    def _shutdown_new_services() -> None:
        if process_controller is not None:
            try:
                if process_controller.is_running():
                    process_controller.stop()
            except Exception:
                pass

        # ✅ stop()만 시도
        for svc in (getattr(hmi, "_stm_service", None), getattr(hmi, "_acs_service", None), log_service):
            if svc is None:
                continue
            fn = getattr(svc, "stop", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

    app.aboutToQuit.connect(_shutdown_new_services)

    # ✅ HMI가 Config 저장 후 재연결할 수 있도록 주입(기존 기능 유지 + 신규 기능 추가)
    hmi.set_runtime_objects(
        plc_binder=plc_binder,
        ini_path=ini_path,
        process_controller=process_controller,
        log_service=log_service,
        stm_service=stm_service,
        acs_service=acs_service,   # ✅ 부팅에서 만든 ACSService 주입
    )

    # ✅ HMI 표시 (기존 유지)
    hmi.show()

    def _focus_hmi():
        hmi.raise_()
        hmi.activateWindow()

    QTimer.singleShot(0, _focus_hmi)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
