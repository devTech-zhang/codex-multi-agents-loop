from __future__ import annotations

import json
import os
import re
from typing import Any
from pathlib import Path

from .config import lark_config, lark_dry_run, lark_identity, load_config
from .engine import WorkflowError, create_project, handle_lark_card_event, request_bug_fix
from .lark import send_text_as_bot
from .lark_sdk import load_sdk_credentials, sdk_log_level, sdk_preflight
from .paths import PLUGIN_ROOT


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
            return P2CardActionTriggerResponse(_response_payload("success", "审批已提交", result.get("response_card")))
        except WorkflowError as exc:
            _print({"ok": False, "stage": "card_action_failed", "error": str(exc), "payload": payload})
            return P2CardActionTriggerResponse(_response_payload("warning", str(exc), None))

    def do_message_receive(data: Any) -> None:
        payload = sdk_event_to_payload(lark, data)
        result = handle_lark_message_event(payload)
        if result:
            _print(result)

    builder = lark.EventDispatcherHandler.builder("", "", log_level).register_p2_card_action_trigger(do_card_action_trigger)
    if bool((lark_config(config).get("bot_commands") or {}).get("enabled", True)):
        builder = builder.register_p2_im_message_receive_v1(do_message_receive)
    event_handler = builder.build()
    client = lark.ws.Client(credentials.app_id, credentials.app_secret, event_handler=event_handler, log_level=log_level)
    _print({"ok": True, "stage": "sdk_event_consumer_ready", "transport": "sdk_websocket", "event": "card.action.trigger", "credentials": preflight["credentials"]})
    client.start()


def sdk_event_to_payload(lark_module: Any, data: Any) -> dict[str, Any]:
    raw = lark_module.JSON.marshal(data)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise WorkflowError("lark sdk event payload must be a JSON object")
    return payload


def handle_lark_message_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    config = load_config()
    bugfix = _parse_bug_fix_message(payload)
    if bugfix:
        result = request_bug_fix(
            issue=bugfix["issue"],
            project_id=bugfix.get("project_id"),
            reporter=bugfix.get("operator"),
            source="lark-im",
        )
        chat_id = bugfix.get("chat_id")
        if chat_id:
            send_text_as_bot(
                chat_id,
                f"已收到修复请求，进入 Bug 修复流程：{result['project_id']}",
                identity=lark_identity(config),
                dry_run=lark_dry_run(config),
                idempotency_key=f"bot-command-bugfix:{bugfix.get('message_id')}",
            )
        result["stage"] = "bot_command_bug_fix_requested"
        return result
    command = _parse_create_project_message(payload)
    if not command:
        return None

    chat_id = command.get("chat_id")
    workspace = _bot_command_workspace()
    if workspace is None:
        message = "当前 event-consumer 运行在插件源码目录。请在业务项目目录运行 `scripts/deliveryflow lark event-consumer`。"
        if chat_id:
            send_text_as_bot(chat_id, message, identity=lark_identity(config), dry_run=lark_dry_run(config), idempotency_key=f"bot-command-workspace:{command.get('message_id')}")
        return {"ok": False, "stage": "bot_command_rejected", "reason": "missing business workspace", "message": message}

    old_cwd = Path.cwd()
    os.chdir(workspace)
    try:
        created = create_project(
            requirement=command["requirement"],
            title=command.get("title"),
            source="lark-im",
            owner_id=command.get("operator"),
            lark_chat_id=chat_id,
        )
    finally:
        os.chdir(old_cwd)

    if chat_id:
        send_text_as_bot(
            chat_id,
            f"已创建交付项目：{created['project_id']}，Workflow 已自动推进到 PRD 审批节点。",
            identity=lark_identity(config),
            dry_run=lark_dry_run(config),
            idempotency_key=f"bot-command-created:{created['run_id']}",
        )
    return {"ok": True, "stage": "bot_command_project_created", "project": created, "workspace": str(workspace)}


def _parse_bug_fix_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    content = _message_text(message.get("content"))
    text = _strip_bot_mentions(content).strip()
    if not re.search(r"(修复|处理|解决).{0,8}(bug|Bug|BUG|问题|缺陷|报错)", text):
        return None
    project_match = re.search(r"(proj_[a-zA-Z0-9_-]+)", text)
    issue = re.sub(r"^.*?(修复|处理|解决).{0,8}(bug|Bug|BUG|问题|缺陷|报错)[:：,，\\s]*", "", text, count=1).strip()
    return {
        "message_id": message.get("message_id"),
        "chat_id": message.get("chat_id"),
        "issue": issue or text,
        "project_id": project_match.group(1) if project_match else None,
        "operator": _message_operator(event),
    }


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


def _response_payload(toast_type: str, content: str, card: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"toast": {"type": toast_type, "content": content[:180]}}
    if card:
        payload["card"] = {"type": "raw", "data": card}
    return payload


def _parse_create_project_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    content = _message_text(message.get("content"))
    text = _strip_bot_mentions(content).strip()
    if not re.search(r"(新建|创建|开始).{0,8}(项目|需求|workflow|工作流)", text, re.IGNORECASE):
        return None
    requirement = re.sub(r"^.*?(新建|创建|开始).{0,8}(项目|需求|workflow|工作流)[:：,，\\s]*", "", text, count=1, flags=re.IGNORECASE).strip()
    if not requirement:
        requirement = text
    title = _extract_title(requirement)
    return {
        "message_id": message.get("message_id"),
        "chat_id": message.get("chat_id"),
        "requirement": requirement,
        "title": title,
        "operator": _message_operator(event),
    }


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return content
        if isinstance(data, dict):
            value = data.get("text") or data.get("content")
            return value if isinstance(value, str) else json.dumps(data, ensure_ascii=False)
    return ""


def _strip_bot_mentions(text: str) -> str:
    text = re.sub(r"@_user_\\d+", "", text)
    return re.sub(r"@\S+", "", text)


def _extract_title(requirement: str) -> str:
    first = re.split(r"[。\\n]", requirement, maxsplit=1)[0].strip()
    first = re.sub(r"^(标题|项目名|项目名称)[:：]", "", first).strip()
    return first[:40] or requirement[:40]


def _message_operator(event: dict[str, Any]) -> str | None:
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    for key in ("open_id", "user_id", "union_id"):
        value = sender_id.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _bot_command_workspace() -> Path | None:
    if Path.cwd().resolve() == PLUGIN_ROOT.resolve():
        return None
    return Path.cwd()


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
