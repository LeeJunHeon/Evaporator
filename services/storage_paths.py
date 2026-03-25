from __future__ import annotations

import re
import tempfile
from pathlib import Path


def _safe_storage_name(value: str, max_len: int = 64) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^\w\-. ]+", "_", text)
    text = re.sub(r"\s+", "_", text)
    if len(text) > max_len:
        return text[:max_len]
    return text


def default_temp_log_root(app_name: str = "Evaporator") -> Path:
    name = _safe_storage_name(app_name or "Evaporator") or "Evaporator"
    return Path(tempfile.gettempdir()) / f"Evaporator_{name}_Logs"


def default_temp_log_fallback_root(app_name: str = "Evaporator") -> Path:
    return default_temp_log_root(app_name) / "local"
