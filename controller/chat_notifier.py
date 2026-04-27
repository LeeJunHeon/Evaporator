# -*- coding: utf-8 -*-
"""
controller/chat_notifier.py

Google Chat 웹훅 알림 유틸 (Evap 전용, Chamber 구현 참고하여 단순화).

설계 원칙
- 메인 공정 흐름에 절대 영향 없음: 모든 네트워크/IO 예외는 내부에서 삼킴
- UI 프리징 방지: HTTP POST는 asyncio.to_thread 또는 데몬 스레드로 백그라운드 실행
  (PySide6 메인 스레드에서 asyncio 루프가 없을 때는 threading 폴백)
- 하위 호환: secrets.py 또는 GCHAT_WEBHOOK_URL 이 없어도 정상 기동
- 웹훅 URL 이 None 이면 모든 공개 API는 조용히 no-op
"""

from __future__ import annotations

import asyncio
import json
import ssl
import threading
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject

try:
    from secrets import GCHAT_WEBHOOK_URL as _WEBHOOK_URL  # type: ignore
except Exception:
    _WEBHOOK_URL = None


_STATUS_ICONS = {"INFO": "ℹ️", "SUCCESS": "✅", "FAIL": "❌"}


class ChatNotifier(QObject):
    """
    Evap 공정 시작/종료/에러 알림을 Google Chat 웹훅으로 전송.
    """

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.webhook_default: Optional[str] = _WEBHOOK_URL
        self._defer: bool = False
        self._buffer: List[Tuple[dict, Optional[str]]] = []
        self._last_started_params: Optional[dict] = None
        self._finished_sent: bool = False
        self._ctx = ssl.create_default_context()
        self._pending: set = set()
        self._started: bool = False

    # ----------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------
    def start(self) -> None:
        self._started = True

    def shutdown(self) -> None:
        try:
            self.flush()
        except Exception:
            pass

        if not self._pending:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        pending = [t for t in self._pending if not t.done()]
        if not pending:
            return

        try:
            loop.create_task(
                asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=2.0)
            )
        except Exception:
            pass

    # ----------------------------------------------------------
    # Internal: HTTP POST
    # ----------------------------------------------------------
    async def _post_async(self, payload: dict, webhook_url: Optional[str]) -> None:
        if not webhook_url or not payload:
            return
        try:
            data = json.dumps(payload).encode("utf-8")
        except Exception:
            return

        ctx = self._ctx

        def _blocking_post():
            try:
                req = urllib.request.Request(
                    webhook_url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=3, context=ctx) as resp:
                    resp.read()
            except Exception:
                pass

        try:
            await asyncio.to_thread(_blocking_post)
        except Exception:
            pass

    def _schedule_post(self, payload: dict, webhook_url: Optional[str]) -> None:
        if not webhook_url or not payload:
            return

        # asyncio 루프가 돌고 있으면 asyncio 경로 사용 (Chamber 스타일).
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._post_async(payload, webhook_url))
            self._pending.add(task)
            task.add_done_callback(lambda t: self._pending.discard(t))
            return
        except RuntimeError:
            pass
        except Exception:
            pass

        # 루프가 없으면(PySide6 메인 스레드 기본 상태) 데몬 스레드로 폴백.
        # UI 프리징 방지 목표는 동일하게 달성하면서 asyncio 종속성 제거.
        try:
            data = json.dumps(payload).encode("utf-8")
        except Exception:
            return
        ctx = self._ctx

        def _run_post():
            try:
                req = urllib.request.Request(
                    webhook_url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=3, context=ctx) as resp:
                    resp.read()
            except Exception:
                pass

        try:
            threading.Thread(target=_run_post, daemon=True).start()
        except Exception:
            self._buffer.append((payload, webhook_url))

    def _post_json(self, payload: dict, urgent: bool = False) -> None:
        url = self.webhook_default
        if not url:
            return
        if self._defer and not urgent:
            self._buffer.append((payload, url))
            return
        self._schedule_post(payload, url)

    def _post_text(self, text: str, urgent: bool = False) -> None:
        if not text:
            return
        self._post_json({"text": str(text)}, urgent=urgent)

    def _post_card(
        self,
        title: str,
        subtitle: str = "",
        status: str = "INFO",
        fields: Optional[Dict[str, Any]] = None,
        urgent: bool = False,
    ) -> None:
        icon = _STATUS_ICONS.get(str(status).upper(), "ℹ️")
        widgets: List[dict] = [
            {
                "decoratedText": {
                    "topLabel": "상태",
                    "text": f"{icon} {title}",
                }
            }
        ]
        if subtitle:
            widgets.append({"decoratedText": {"topLabel": "메모", "text": str(subtitle)}})

        if fields:
            for k, v in fields.items():
                if v is None:
                    continue
                try:
                    text_val = str(v)
                except Exception:
                    continue
                if not text_val.strip():
                    continue
                widgets.append({"decoratedText": {"topLabel": str(k), "text": text_val}})

        payload = {
            "cardsV2": [
                {
                    "cardId": "evap-notifier",
                    "card": {
                        "header": {
                            "title": "Evap Controller",
                            "subtitle": "Status Notification",
                        },
                        "sections": [{"widgets": widgets}],
                    },
                }
            ]
        }
        self._post_json(payload, urgent=urgent)

    def flush(self) -> None:
        if not self._buffer:
            return
        pending = self._buffer[:]
        self._buffer.clear()
        for payload, url in pending:
            self._schedule_post(payload, url)

    # ----------------------------------------------------------
    # Public API
    # ----------------------------------------------------------
    def notify_process_started(self, params: dict) -> None:
        try:
            params = dict(params or {})
            self._last_started_params = dict(params)
            self._finished_sent = False
            self._buffer.clear()

            process_name = str(
                params.get("process_name")
                or params.get("recipe_name")
                or "Untitled"
            )
            material_name = str(params.get("material_name", "") or "-")
            target_rate = params.get("target_rate")
            target_thickness = params.get("target_thickness")
            target_thickness_nm = (float(target_thickness) / 10.0) if target_thickness is not None else None
            delay_min = params.get("delay_min")
            use_power1 = bool(params.get("use_power1", False))
            use_power2 = bool(params.get("use_power2", False))

            def _fmt_num(v, fmt):
                try:
                    return fmt.format(float(v))
                except Exception:
                    return "-"

            fields = {
                "물질": material_name,
                "Target Rate": _fmt_num(target_rate, "{:.2f} Å/s"),
                "Target 두께": _fmt_num(target_thickness_nm, "{:.1f} nm"),   # ← /10 변환 후 전달
                "Shutter Delay": _fmt_num(delay_min, "{:.1f} 분"),
                "Power 채널": "P1" if use_power1 else ("P2" if use_power2 else "-"),
            }

            self._post_card(
                title=f"Evap 공정 시작: {process_name}",
                fields=fields,
                status="INFO",
                urgent=True,
            )
            self.flush()
        except Exception:
            pass

    def notify_process_finished_detail(self, ok: bool, detail: dict) -> None:
        try:
            if self._finished_sent:
                return
            detail = dict(detail or {})

            merged: dict = {}
            merged.update(self._last_started_params or {})
            merged.update(detail)

            process_name = str(
                merged.get("process_name")
                or merged.get("recipe_name")
                or "Untitled"
            )
            material_name = str(merged.get("material_name", "") or "-")
            target_thickness = merged.get("target_thickness")
            target_thickness_nm = (float(target_thickness) / 10.0) if target_thickness is not None else None

            def _fmt_num(v, fmt):
                try:
                    return fmt.format(float(v))
                except Exception:
                    return None

            if ok:
                fields: Dict[str, Any] = {
                    "물질": material_name,
                    "Target 두께": _fmt_num(target_thickness_nm, "{:.1f} nm"),   # ← /10 변환 후 전달
                }
                sw = detail.get("final_sw_thickness_nm")
                if sw is not None:
                    fields["최종 SW 두께"] = _fmt_num(sw, "{:.2f} nm")
                stm = detail.get("final_stm_thickness_nm")
                if stm is not None:
                    fields["최종 STM 두께"] = _fmt_num(stm, "{:.2f} nm")
                elapsed = detail.get("elapsed_sec")
                if elapsed is not None:
                    try:
                        total = int(float(elapsed))
                        fields["공정 시간"] = f"{total // 60}분 {total % 60}초"
                    except Exception:
                        pass

                self._post_card(
                    title=f"Evap 공정 완료: {process_name}",
                    fields=fields,
                    status="SUCCESS",
                    urgent=True,
                )
            else:
                is_user_stop = bool(detail.get("is_user_stop", False))
                errors = detail.get("errors") or []
                fail_phase = detail.get("phase") or detail.get("error_phase") or "-"
                fail_reason = (
                    detail.get("error_message")
                    or (errors[0] if errors else "")
                    or "알 수 없는 오류"
                )

                if is_user_stop:
                    fields = {
                        "물질": material_name,
                        "중지": "사용자가 Stop 버튼을 눌러 중지했습니다.",
                    }
                    self._post_card(
                        title=f"Evap 공정 중지: {process_name}",
                        fields=fields,
                        status="FAIL",
                        urgent=True,
                    )
                else:
                    fields = {
                        "물질": material_name,
                        "실패 단계": str(fail_phase),
                        "실패 원인": str(fail_reason),
                    }
                    last_dac = detail.get("last_dac")
                    if last_dac is not None:
                        fields["마지막 DAC"] = str(last_dac)

                    self._post_card(
                        title=f"Evap 공정 실패: {process_name}",
                        fields=fields,
                        status="FAIL",
                        urgent=True,
                    )

            self._finished_sent = True
            self.flush()
        except Exception:
            pass

    def notify_error(self, title: str, detail: str = "") -> None:
        try:
            self._post_card(
                title=title,
                subtitle=str(detail or ""),
                status="FAIL",
                urgent=True,
            )
            self.flush()
        except Exception:
            pass

    def notify_text(self, text: str, urgent: bool = True) -> None:
        try:
            self._post_text(str(text), urgent=urgent)
            self.flush()
        except Exception:
            pass
