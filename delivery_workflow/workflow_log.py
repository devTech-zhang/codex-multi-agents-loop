from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .storage import now_iso


SENSITIVE_KEYS = {"app_secret", "secret", "token", "password", "authorization", "LARK_APP_SECRET"}
MAX_TEXT_LENGTH = 20000


def log_workflow(
    event: str,
    message: str,
    *,
    run_id: str | None = None,
    project_id: str | None = None,
    step_id: str | None = None,
    payload: dict[str, Any] | None = None,
    level: str = "info",
) -> None:
    entry = {
        "timestamp": now_iso(),
        "level": level,
        "event": event,
        "message": message,
        "project_id": project_id,
        "run_id": run_id,
        "step_id": step_id,
        "payload": _sanitize(payload or {}),
    }
    path = _workflow_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _workflow_log_path() -> Path:
    try:
        from .config import load_config, storage_path

        return storage_path(load_config(), "logs", ".codex/delivery-workflow/logs") / "workflow.log"
    except Exception:
        return Path.cwd() / ".codex" / "delivery-workflow" / "logs" / "workflow.log"


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                result[str(key)] = "***"
            else:
                result[str(key)] = _sanitize(item)
        return result
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _truncate(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(sensitive.lower() in lowered for sensitive in SENSITIVE_KEYS)


def _truncate(text: str) -> str:
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return f"{text[:MAX_TEXT_LENGTH]}\n...<truncated {len(text) - MAX_TEXT_LENGTH} chars>"
