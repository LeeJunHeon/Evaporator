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
            poll_s=1.0,                # 워커 루프(읽기 템포). 스트림은 장비가 1초마다 보냄
            reconnect_interval_s=1.0,   # 실패 시 재시도 템포
            use_stream=True,            # ✅ CON 모드
            stream_interval_a=1,        # ✅ 1초
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
            turbovac=turbovac_service,
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

        # ✅ 종료 시 run 파일 먼저 닫기(안전)
        try:
            if log_service is not None:
                log_service.close_run()
        except Exception:
            pass

        # ✅ stop()만 시도
        for svc in (
            getattr(hmi, "_stm_service", None),
            getattr(hmi, "_acs_service", None),
            turbovac_service,
            log_service,
        ):
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
        turbovac_service=turbovac_service,
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
