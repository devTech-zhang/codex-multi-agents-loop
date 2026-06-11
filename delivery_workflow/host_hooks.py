from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .storage import now_iso
from .workflow_log import log_workflow


SENSITIVE_COMMAND_PATTERNS = [
    r"\b(cat|less|more|head|tail)\s+(?:[^\n;&|]*\s)?(?:\.env|[^\s;&|]*/\.env)\b",
    r"\b(printenv|env)\b(?:\s*$|\s*[;&|])",
]
DESTRUCTIVE_COMMAND_PATTERNS = [
    r"\brm\s+-rf\s+(?:--\s+)?(?:/|\$HOME|~|\.|\.\.)\s*(?:$|[;&|])",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-fdx\b",
    r"\bchmod\s+-R\s+777\b",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delivery Workflow host hook entrypoint.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("record-file-change")
    sub.add_parser("record-command")
    sub.add_parser("stop-guard")
    sub.add_parser("safety-guard")
    args = parser.parse_args(argv)

    payload = _read_hook_payload()
    if args.command == "record-file-change":
        _record_event("host_hook.file_changed", "宿主 Hook 记录到文件写入。", _file_change_payload(payload))
        return 0
    if args.command == "record-command":
        _record_event("host_hook.command_executed", "宿主 Hook 记录到命令执行。", _command_payload(payload))
        return 0
    if args.command == "stop-guard":
        _record_event("host_hook.turn_stopped", "宿主 Hook 记录到一轮 Agent 响应结束。", _compact_payload(payload))
        return 0
    if args.command == "safety-guard":
        return _safety_guard(payload)
    return 0


def _read_hook_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return payload if isinstance(payload, dict) else {"value": payload}


def _record_event(event: str, message: str, payload: dict[str, Any]) -> None:
    entry = {"timestamp": now_iso(), "event": event, "message": message, "payload": _sanitize(payload)}
    path = Path.cwd() / ".delivery-workflow" / "logs" / "host-hooks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    log_workflow(event, message, payload=payload)


def _file_change_payload(payload: dict[str, Any]) -> dict[str, Any]:
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
    return {
        "tool_name": payload.get("tool_name"),
        "file_path": str(path) if path else None,
        "target_area": _target_area(str(path)) if path else None,
        "cwd": payload.get("cwd"),
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
    }


def _command_payload(payload: dict[str, Any]) -> dict[str, Any]:
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    command = str(tool_input.get("command") or "")
    return {
        "tool_name": payload.get("tool_name"),
        "command": _sanitize_command(command),
        "command_kind": _command_kind(command),
        "cwd": payload.get("cwd"),
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
    }


def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "hook_event_name": payload.get("hook_event_name"),
        "cwd": payload.get("cwd"),
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "stop_hook_active": payload.get("stop_hook_active"),
    }


def _safety_guard(payload: dict[str, Any]) -> int:
    command = ""
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if payload.get("tool_name") == "Bash":
        command = str(tool_input.get("command") or "")
    reason = _blocked_command_reason(command)
    if not reason:
        return 0
    _record_event(
        "host_hook.command_blocked",
        "宿主 Hook 阻止了高风险命令。",
        {"command": _sanitize_command(command), "reason": reason},
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )
    return 0


def _blocked_command_reason(command: str) -> str | None:
    lowered = command.lower()
    for pattern in DESTRUCTIVE_COMMAND_PATTERNS:
        if re.search(pattern, lowered):
            return "Delivery Workflow hook blocked a destructive command; ask the user before running it."
    for pattern in SENSITIVE_COMMAND_PATTERNS:
        if re.search(pattern, lowered):
            return "Delivery Workflow hook blocked a command that may expose environment secrets."
    return None


def _target_area(path: str) -> str:
    normalized = path.replace("\\", "/")
    if "/source-code/frontend/" in normalized or normalized.startswith("source-code/frontend/"):
        return "frontend"
    if "/source-code/backend/" in normalized or normalized.startswith("source-code/backend/"):
        return "backend"
    if "/delivery-project/" in normalized or normalized.startswith("delivery-project/"):
        return "artifact"
    if "/.delivery-workflow/" in normalized or normalized.startswith(".delivery-workflow/"):
        return "workflow-state"
    return "other"


def _command_kind(command: str) -> str:
    lowered = command.lower()
    if "playwright" in lowered:
        return "browser-test"
    if re.search(r"\bnpm\s+run\s+(build|test|lint|typecheck|check)\b", lowered):
        return "node-verification"
    if re.search(r"\b(npm|pnpm|yarn)\s+install\b", lowered):
        return "dependency-install"
    if re.search(r"\b(npm|pnpm|yarn)\s+run\s+dev\b", lowered):
        return "dev-server"
    if re.search(r"\bcurl\b", lowered):
        return "api-check"
    if re.search(r"\b(pytest|python3?\s+-m\s+unittest|vitest|tsc)\b", lowered):
        return "test-or-compile"
    return "other"


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _sanitize_command(value)
    return value


def _sanitize_command(command: str) -> str:
    sanitized = re.sub(r"(LARK_APP_SECRET|APP_SECRET|TOKEN|PASSWORD|SECRET)=\S+", r"\1=***", command)
    sanitized = re.sub(r"(--?(?:token|password|secret|app-secret)\s+)\S+", r"\1***", sanitized, flags=re.IGNORECASE)
    return sanitized[-4000:]


if __name__ == "__main__":
    raise SystemExit(main())
