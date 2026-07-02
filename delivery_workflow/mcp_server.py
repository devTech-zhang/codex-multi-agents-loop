from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .config import initialize_project_workspace
from .engine import (
    create_project,
    current_project_status,
    inspect_workflow,
    list_artifacts,
    read_artifact,
    run_worker_once,
    run_worker_until_blocked,
    status,
)


TOOLS = [
    {
        "name": "codex_delivery_workflow_init",
        "description": "初始化当前目录的 Codex 交付工作流运行环境。",
        "inputSchema": {"type": "object", "properties": {"overwrite_config": {"type": "boolean"}}},
    },
    {
        "name": "codex_delivery_workflow_create",
        "description": "创建一次 Codex 交付工作流运行，并按配置派发五个子 Agent。",
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
        "name": "codex_delivery_workflow_status",
        "description": "查询工作流运行状态、任务、事件和产物；未传 run_id 时读取当前目录最新运行。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}},
    },
    {
        "name": "codex_delivery_workflow_worker_once",
        "description": "推进一个待执行的 Codex 交付工作流任务。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}},
    },
    {
        "name": "codex_delivery_workflow_worker_until_idle",
        "description": "连续推进待执行任务，直到空闲、失败、完成或达到最大任务数。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "max_jobs": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "codex_delivery_workflow_list_artifacts",
        "description": "列出一次工作流运行的所有产物。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "codex_delivery_workflow_read_artifact",
        "description": "读取指定工作流产物的最新版本。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "name": {"type": "string"}}, "required": ["run_id", "name"]},
    },
    {
        "name": "codex_delivery_workflow_inspect",
        "description": "查看文件化 Codex 交付工作流定义。",
        "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}}},
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
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "codex-delivery-workflow", "version": __version__},
                },
            }
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
    if name == "codex_delivery_workflow_init":
        return initialize_project_workspace(overwrite_config=bool(args.get("overwrite_config")))
    if name == "codex_delivery_workflow_create":
        return create_project(
            requirement=args["requirement"],
            title=args.get("title"),
            business_goal=args.get("business_goal"),
            requires_frontend=args.get("requires_frontend", True),
            requires_backend=args.get("requires_backend", True),
        )
    if name == "codex_delivery_workflow_status":
        return status(args["run_id"]) if args.get("run_id") else current_project_status()
    if name == "codex_delivery_workflow_worker_once":
        return run_worker_once(run_id=args.get("run_id"))
    if name == "codex_delivery_workflow_worker_until_idle":
        return run_worker_until_blocked(run_id=args.get("run_id"), max_jobs=int(args.get("max_jobs") or 20))
    if name == "codex_delivery_workflow_list_artifacts":
        return list_artifacts(args["run_id"])
    if name == "codex_delivery_workflow_read_artifact":
        return read_artifact(args["run_id"], args["name"])
    if name == "codex_delivery_workflow_inspect":
        return inspect_workflow(args.get("workflow_id") or "codex-delivery-workflow")
    raise ValueError(f"unknown tool: {name}")


def _read_message() -> dict[str, Any] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def _write_message(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
