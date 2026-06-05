from __future__ import annotations

import json
from typing import Any

from .config import load_config
from .engine import WorkflowError, handle_lark_card_event
from .lark_sdk import load_sdk_credentials, sdk_log_level, sdk_preflight


def run_lark_long_connection_consumer() -> None:
    config = load_config()
    preflight = sdk_preflight(config)
    if not preflight.get("ok"):
        raise RuntimeError(json.dumps(preflight, ensure_ascii=False))

    credentials = load_sdk_credentials(config)
    lark = _import_lark_oapi()
    P2CardActionTrigger, P2CardActionTriggerResponse = _import_card_action_models()
    log_level = _sdk_log_level(lark, sdk_log_level(config))

    def do_card_action_trigger(data: Any) -> Any:
        payload = sdk_event_to_payload(lark, data)
        try:
            result = handle_lark_card_event(payload)
            result["stage"] = "card_action_handled"
            _print(result)
            return P2CardActionTriggerResponse(_toast("success", "审批已提交"))
        except WorkflowError as exc:
            _print({"ok": False, "stage": "card_action_failed", "error": str(exc), "payload": payload})
            return P2CardActionTriggerResponse(_toast("warning", str(exc)))

    event_handler = (
        lark.EventDispatcherHandler.builder("", "", log_level)
        .register_p2_card_action_trigger(do_card_action_trigger)
        .build()
    )
    client = lark.ws.Client(credentials.app_id, credentials.app_secret, event_handler=event_handler, log_level=log_level)
    _print({"ok": True, "stage": "sdk_event_consumer_ready", "transport": "sdk_websocket", "event": "card.action.trigger", "credentials": preflight["credentials"]})
    client.start()


def sdk_event_to_payload(lark_module: Any, data: Any) -> dict[str, Any]:
    raw = lark_module.JSON.marshal(data)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise WorkflowError("lark sdk event payload must be a JSON object")
    return payload


def _import_lark_oapi() -> Any:
    try:
        import lark_oapi as lark
    except ModuleNotFoundError as exc:
        raise RuntimeError("missing Python SDK `lark-oapi`; run `uv pip install --python .venv/bin/python lark-oapi`") from exc
    return lark


def _import_card_action_models() -> tuple[Any, Any]:
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger, P2CardActionTriggerResponse
    except ModuleNotFoundError as exc:
        raise RuntimeError("installed lark-oapi does not provide P2CardActionTrigger; update with `uv pip install --python .venv/bin/python -U lark-oapi`") from exc
    return P2CardActionTrigger, P2CardActionTriggerResponse


def _sdk_log_level(lark_module: Any, value: str) -> Any:
    levels = {
        "debug": getattr(lark_module.LogLevel, "DEBUG", None),
        "info": getattr(lark_module.LogLevel, "INFO", None),
        "warn": getattr(lark_module.LogLevel, "WARN", None),
        "warning": getattr(lark_module.LogLevel, "WARN", None),
        "error": getattr(lark_module.LogLevel, "ERROR", None),
    }
    return levels.get(value) or getattr(lark_module.LogLevel, "INFO", None)


def _toast(toast_type: str, content: str) -> dict[str, Any]:
    return {"toast": {"type": toast_type, "content": content[:180]}}


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
