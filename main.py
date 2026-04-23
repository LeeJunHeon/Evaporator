# main.py
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

_BASE_DIR = Path(__file__).resolve().parent
if not (_BASE_DIR / "config").exists():
    _BASE_DIR = _BASE_DIR.parent

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

# ✅ 그래프 위젯(별도 파일)
from ui.windows.hmi_window import HmiWindow

from config.plc_config import load_plc_settings
from controller.hmi_plc_binder import HmiPlcBinder
from controller.process_controller import ProcessController

from services.log_service import LogService
from services.history_store import HistoryStore
from services.run_summary_service import RunSummaryService
from services.recommendation_service import RecommendationService
from services.acs_service import ACSService
from services.turbovac_service import TurbovacService


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
# NAS 로그 경로
# ============================================================
NAS_LOG_ROOT = Path(r"\\VanaM_NAS\VanaM_toShare\JH_Lee\Logs\Evaporator")


# ============================================================
# main
# ============================================================
def main():
    app = QApplication(sys.argv)
    if sys.platform == "win32":
        from PySide6.QtGui import QFont
        # Windows에서 기본 폰트가 한자 계열로 fallback되는 것을 막기 위해 맑은 고딕 강제 지정
        font = app.font()
        font.setFamily("Malgun Gothic")
        app.setFont(font)

    hmi = HmiWindow()

    # ------------------------------
    # PLC 바인딩 시작 (✅ 기존 로직 유지)
    # ------------------------------
    ini_path = _BASE_DIR / "config" / "devices.ini"
    plc_settings = load_plc_settings(ini_path)

    # ✅ PLC 연결/버튼 팝업/원복 로직은 HmiPlcBinder가 계속 담당 (그대로 유지)
    plc_binder = HmiPlcBinder(hmi.ui, plc_settings)
    plc_binder.start()

    # ✅ HMI 로그는 PLC binder가 찍는 포맷([HH:MM:SS] ...)으로 통일
    def _hmi_log(msg: object) -> None:
        s = str(msg).rstrip('\n')
        # binder의 _set_hmi_log를 우선 사용해 스크롤/포맷 정책을 통일; 없으면 타임스탬프 직접 부착
        try:
            fn = getattr(plc_binder, '_set_hmi_log', None)
            if callable(fn):
                fn(s)
                return
        except Exception:
            pass
        # 2) fallback: 시간 프리픽스만 붙여서 직접 append
        try:
            _append_text(getattr(hmi.ui, 'hmiLogWindow', None), f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {s}")
        except Exception:
            pass

    # ✅ STM은 Start에서(FTM ON 후) 연결한다.
    stm_service: Any = None

    # ACS는 프로그램 부팅 즉시 연결하고, query 모드($PRD)로 읽는다.
    # 1초마다 $PRD 쿼리를 전송하여 실시간 압력을 읽는다. (버퍼 누적 문제 방지)
    acs_service: Any = None
    try:
        acs_service = ACSService(
            ini_path=ini_path,
            poll_s=1.0,
            reconnect_interval_s=1.0,
            use_stream=True,
            stream_interval_a=1,
            channel=1,
        )

        # ✅ ACS UI/상태 관리는 binder가 전담
        plc_binder.set_acs_service(acs_service)
        acs_service.start()
        
    except Exception as e:
        acs_service = None
        try:
            plc_binder.set_acs_service(None)
        except Exception:
            pass
        _hmi_log(f"[BOOT][WARN] ACSService init failed: {e!r}")

    # ✅ TMP(Turbovac)는 프로그램 부팅 시 객체만 생성하고,
    #    실제 연결 시도는 PLC에서 TMP_SW=ON readback이 확인된 시점에 binder가 시작한다.
    turbovac_service: Any = None
    try:
        turbovac_service = TurbovacService(
            ini_path=ini_path,
            poll_s=1.0,
            reconnect_interval_s=1.0,
        )
        _hmi_log("[BOOT] TMP service prepared (attach on PLC TMP_SW ON confirm)")
    except Exception as e:
        turbovac_service = None
        _hmi_log(f"[BOOT][WARN] TurbovacService init failed: {e!r}")

    # ✅ TMP 서비스 주입 (수동 버튼 / TMP status 표시 / TMP 인터락용)
    try:
        plc_binder.set_turbovac_service(turbovac_service)
    except Exception as e:
        _hmi_log(f"[BOOT][WARN] set_turbovac_service failed: {e!r}")

    # ------------------------------
    # LogService (신규)
    # ------------------------------
    log_service: Any = None
    try:
        # ✅ LogService가 NAS_LOG_ROOT 아래의 3개 폴더(HMIWindowLog/ProcessWindowLog/ProcessLog)를 관리
        log_service = LogService(app_name="Evaporator", base_dir=NAS_LOG_ROOT)
        log_service.start()
    except Exception:
        log_service = None

    history_store: Any = None
    run_summary_service: Any = None
    recommendation_service: Any = None
    try:
        if log_service is not None:
            history_store = HistoryStore.from_log_service(log_service)
            run_summary_service = RunSummaryService(history_store=history_store, log_service=log_service)
            recommendation_service = RecommendationService(history_store=history_store)
    except Exception as e:
        history_store = None
        run_summary_service = None
        recommendation_service = None
        _hmi_log(f"[BOOT][WARN] history/recommendation init failed: {e!r}")

    # ------------------------------
    # ProcessController (신규)
    # ------------------------------
    process_controller: Any = None
    try:
        process_controller = ProcessController(
            plc=getattr(plc_binder, "_plc"),
            log=log_service,
            stm=None,
            acs=acs_service,
            turbovac=turbovac_service,
        )

        try:
            plc_binder.set_process_controller(process_controller)
        except Exception as e:
            _hmi_log(f"[BOOT][WARN] set_process_controller failed: {e!r}")
    except Exception as e:
        process_controller = None
        _hmi_log(f"[BOOT][WARN] ProcessController init failed: {e!r}")

    def _wait_process_controller_stop(timeout_s: float = 30.0) -> bool:
        """
        앱 종료 시 ProcessController가 실제로 멈출 때까지 잠깐 기다린다.
        stop()은 '요청'만 보내는 구조이므로, 곧바로 서비스 종료하면
        engine safety shutdown / DAC ramp-down이 중간에 끊길 수 있다.
        """
        if process_controller is None:
            return True

        is_running_fn = getattr(process_controller, "is_running", None)
        if not callable(is_running_fn):
            return True

        deadline = time.monotonic() + max(0.5, float(timeout_s))

        # Qt 이벤트 루프를 주기적으로 처리해야 engine 스레드의 finished 시그널이 전달됨
        while time.monotonic() < deadline:
            try:
                if not bool(is_running_fn()):
                    return True
            except Exception:
                return True

            try:
                app.processEvents()
            except Exception:
                pass

            time.sleep(0.05)

        return False

    # 종료 시 신규 서비스 정리
    def _shutdown_new_services() -> None:
        # 1) 실행 중 공정이 있으면 먼저 stop 요청
        if process_controller is not None:
            try:
                if process_controller.is_running():
                    process_controller.stop()
                    _wait_process_controller_stop(timeout_s=30.0)
            except Exception:
                pass

            # 1-b) Google Chat notifier flush (best-effort, 실패해도 종료 흐름 방해 없음)
            try:
                shutdown_fn = getattr(process_controller, "shutdown_notifier", None)
                if callable(shutdown_fn):
                    shutdown_fn()
            except Exception:
                pass

        # run 파일 open/close는 ProcessWindow가 담당한다.

        # 2-a) ACS 스트림 먼저 종료 (장비에 명시적으로 CON 스트림 종료 명령 전달)
        # → 다음 실행 시 버퍼에 오래된 스트림 데이터가 없도록 보장
        try:
            _acs = acs_service
            if _acs is not None:
                worker = getattr(_acs, "_worker", None)
                if worker is not None:
                    acs_dev = getattr(worker, "_acs", None)
                    if acs_dev is not None and hasattr(acs_dev, "stop_stream_safe"):
                        acs_dev.stop_stream_safe()
        except Exception:
            pass

        # 2-b) 외부 장비 서비스 stop
        for svc in (
            getattr(hmi, "_stm_service", None),
            getattr(hmi, "_acs_service", None),
            getattr(hmi, "_turbovac_service", None),
        ):
            if svc is None:
                continue
            fn = getattr(svc, "stop", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

        # 4) 제일 마지막에 PLC binder stop
        try:
            if plc_binder is not None:
                plc_binder.stop()
        except Exception:
            pass

        # 5) 맨 마지막에 LogService stop
        try:
            if log_service is not None:
                log_service.stop()
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
        turbovac_service=turbovac_service,
        run_summary_service=run_summary_service,
        recommendation_service=recommendation_service,
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
