from __future__ import annotations

import json
import sys
from typing import Any

from .capabilities import doctor
from .engine import (
    create_project,
    delete_project,
    enqueue_step,
    get_project_status,
    inspect_workflow,
    list_artifacts,
    list_projects,
    read_artifact,
    retry_prd_approval_lark,
    run_worker_once,
    run_worker_until_blocked,
    status,
    submit_gate,
)


TOOLS = [
    {
        "name": "delivery_create_project",
        "description": "Create a delivery project and workflow run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "requirement": {"type": "string"},
                "title": {"type": "string"},
                "business_goal": {"type": "string"},
                "requires_frontend": {"type": "boolean"},
                "requires_backend": {"type": "boolean"},
            },
            "required": ["requirement"],
        },
    },
    {
        "name": "delivery_list_projects",
        "description": "List delivery projects in the current workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
        },
    },
    {
        "name": "delivery_get_project_status",
        "description": "Get progress for the latest workflow run of a project.",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "delivery_delete_project",
        "description": "Delete a delivery project and, by default, its delivery-projects/<project_id> artifacts. Requires exact project_id confirmation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "confirm_project_id": {"type": "string"},
                "delete_artifacts": {"type": "boolean"},
            },
            "required": ["project_id", "confirm_project_id"],
        },
    },
    {
        "name": "delivery_get_status",
        "description": "Get workflow run status, gates, jobs, events, and artifacts.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "delivery_submit_gate",
        "description": "Submit structured gate data for an interactive or approval step.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "step_id": {"type": "string"}, "data": {"type": "object"}},
            "required": ["run_id", "step_id", "data"],
        },
    },
    {
        "name": "delivery_enqueue_step",
        "description": "Enqueue a workflow step for background execution.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "step_id": {"type": "string"}}, "required": ["run_id", "step_id"]},
    },
    {
        "name": "delivery_worker_once",
        "description": "Run one pending workflow worker job.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "delivery_worker_until_blocked",
        "description": "Run pending worker jobs until the workflow is blocked, idle, failed, or reaches an optional stop step.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "max_jobs": {"type": "integer", "minimum": 1, "maximum": 200},
                "stop_steps": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "delivery_retry_prd_approval_lark",
        "description": "Retry PRD v2 Lark document creation and approval card sending for an existing run.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "delivery_list_artifacts",
        "description": "List artifacts for a workflow run.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "delivery_read_artifact",
        "description": "Read the latest version of an artifact.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "name": {"type": "string"}}, "required": ["run_id", "name"]},
    },
    {
        "name": "delivery_inspect_workflow",
        "description": "Inspect the file-based workflow definition.",
        "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}}},
    },
    {
        "name": "delivery_doctor",
        "description": "Check local Codex, Claude, Figma, and Lark document capabilities.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def main() -> None:
    while True:
        message = _read_message()
        if message is None:
            return
        response = _handle(message)
        if response is not None:
            _write_message(response)


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    try:
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "delivery-workflow", "version": "0.1.0"}}}
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = message.get("params") or {}
            result = _call_tool(params.get("name"), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}}
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method: {method}"}}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(exc)}}


def _call_tool(name: str, args: dict[str, Any]) -> Any:
    if name == "delivery_create_project":
        return create_project(
            requirement=args["requirement"],
            title=args.get("title"),
            business_goal=args.get("business_goal"),
            requires_frontend=args.get("requires_frontend", True),
            requires_backend=args.get("requires_backend", True),
        )
    if name == "delivery_list_projects":
        return list_projects(int(args.get("limit") or 20))
    if name == "delivery_get_project_status":
        return get_project_status(args["project_id"])
    if name == "delivery_delete_project":
        return delete_project(
            args["project_id"],
            confirm_project_id=args["confirm_project_id"],
            delete_artifacts=args.get("delete_artifacts", True),
        )
    if name == "delivery_get_status":
        return status(args["run_id"])
    if name == "delivery_submit_gate":
        return submit_gate(args["run_id"], args["step_id"], args["data"])
    if name == "delivery_enqueue_step":
        return enqueue_step(args["run_id"], args["step_id"])
    if name == "delivery_worker_once":
        return run_worker_once()
    if name == "delivery_worker_until_blocked":
        return run_worker_until_blocked(
            run_id=args.get("run_id"),
            max_jobs=int(args.get("max_jobs") or 50),
            stop_steps=set(args.get("stop_steps") or []),
        )
    if name == "delivery_retry_prd_approval_lark":
        return retry_prd_approval_lark(args["run_id"])
    if name == "delivery_list_artifacts":
        return list_artifacts(args["run_id"])
    if name == "delivery_read_artifact":
        return read_artifact(args["run_id"], args["name"])
    if name == "delivery_inspect_workflow":
        return inspect_workflow(args.get("workflow_id") or "delivery-workflow")
    if name == "delivery_doctor":
        return doctor()
    raise ValueError(f"unknown tool: {name}")


def _read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, value = line.decode("ascii").partition(":")
        headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def _write_message(message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
