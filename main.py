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

        # ✅ ACS UI 표시용 상태: 링크(link)와 압력 수신 성공(healthy) 분리
        _acs_ui = {
            "link": False,
            "healthy": False,
            "last_rx_mono": 0.0,   # 마지막 pressure 수신 시간(monotonic)
        }

        def _set_acs_ui_connected(is_ok: bool, *, clear_pressure: bool = False) -> None:
            # 상단 상태라인(PLC CONNECTED | ACS CONNECTED/...) 반영
            try:
                plc_binder.set_external_connected("ACS", bool(is_ok))
            except Exception:
                pass

            # 압력 라벨 지우기(-----)
            if clear_pressure:
                _update_pressure(None)

        def _acs_connected(ok: bool) -> None:
            # 링크 상태만 반영
            _acs_ui["link"] = bool(ok)

            # 링크가 끊기면 즉시 ----- 로
            if not ok:
                _acs_ui["healthy"] = False
                _set_acs_ui_connected(False, clear_pressure=True)
                return

            # 링크는 살아있어도, 최근 pressure를 못 받았으면 UI는 DISCONNECTED 유지
            _set_acs_ui_connected(bool(_acs_ui["link"] and _acs_ui["healthy"]))

        def _on_acs_pressure(v: object) -> None:
            # ✅ pressure 이벤트가 와도 값이 None/빈문자면 "1회라도 못 받은 것"으로 간주
            if v is None:
                _acs_ui["healthy"] = False
                _set_acs_ui_connected(False, clear_pressure=True)  # -> _update_pressure(None) -> "-----"
                return

            s = str(v).strip()
            if not s:
                _acs_ui["healthy"] = False
                _set_acs_ui_connected(False, clear_pressure=True)
                return

            # ✅ 정상 값이면 그때만 CONNECTED로 복구
            _acs_ui["last_rx_mono"] = time.monotonic()
            _acs_ui["healthy"] = True
            _set_acs_ui_connected(True)
            _update_pressure(v)

        def _on_acs_error(msg: object) -> None:
            # ✅ “압력 1회라도 못 받음(통신 실패)”이면 즉시 DISCONNECTED + ----- 표시
            _hmi_log(msg)
            _acs_ui["healthy"] = False
            _set_acs_ui_connected(False, clear_pressure=True)

        acs_service.sig_pressure.connect(_on_acs_pressure)
        acs_service.sig_connected.connect(_acs_connected)
        acs_service.sig_error.connect(_on_acs_error)

        # ✅ “누락(1초 동안 pressure 이벤트가 안 옴)”도 실패로 간주해서 ----- 표시
        _acs_stale_timer = QTimer(hmi)
        _acs_stale_timer.setInterval(200)  # 0.2초마다 체크(가벼움)
        def _acs_stale_tick() -> None:

            # 링크가 살아있는데도 최근 값이 안 들어오면(=1회라도 누락) 바로 ----- 로 내림
            if not _acs_ui["link"]:
                return
            
            if not _acs_ui["healthy"]:
                return
            
            last = float(_acs_ui["last_rx_mono"] or 0.0)

            if last <= 0:
                # 아직 한 번도 받은 적 없다면 표시 ----- 유지
                _acs_ui["healthy"] = False
                _set_acs_ui_connected(False, clear_pressure=True)
                return
            
            if (time.monotonic() - last) > 1.6:  # ✅ 1초(스트림) + 여유(오탐 방지), 누락은 DISCONNECTED 처리
                _acs_ui["healthy"] = False
                _set_acs_ui_connected(False, clear_pressure=True)

        _acs_stale_timer.timeout.connect(_acs_stale_tick)
        _acs_stale_timer.start()

        # 시작 시 기본은 ----- 표시
        _update_pressure(None)

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
