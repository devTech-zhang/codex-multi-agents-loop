from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .config import initialize_project_workspace
from .engine import (
    complete_agent_step,
    confirm_prd,
    create_project,
    current_project_status,
    dispatch_next_agent_task,
    inspect_workflow,
    list_artifacts,
    manager_summary,
    prepare_agent_handoff,
    read_artifact,
    request_prd_review,
    status,
)


TOOLS = [
    {
        "name": "codex_delivery_workflow_init",
        "description": "初始化当前项目的 Codex 交付工作流：写入 .codex/agents、SQLite 薄状态账本、memory 和产物目录。",
        "inputSchema": {"type": "object", "properties": {"overwrite_config": {"type": "boolean"}, "overwrite_agents": {"type": "boolean"}}},
    },
    {
        "name": "codex_delivery_workflow_init_project",
        "description": "codex_delivery_workflow_init 的项目级别名，用于强调会把 Agent 加载到当前项目目录。",
        "inputSchema": {"type": "object", "properties": {"overwrite_config": {"type": "boolean"}, "overwrite_agents": {"type": "boolean"}}},
    },
    {
        "name": "codex_delivery_workflow_create",
        "description": "创建一次 Codex 交付工作流运行，并准备第一个自定义 Agent 的调用任务。",
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
        "name": "codex_delivery_workflow_prepare_handoff",
        "description": "主管 Agent 使用：为下一个 pending job 生成自定义 Agent 的 spawn 或显式 @ 调用任务，但不领取任务。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "agent": {"type": "string"}}},
    },
    {
        "name": "codex_delivery_workflow_dispatch_next",
        "description": "自定义项目 Agent 使用：领取自己的 pending job，支持显式 @ 和主管 spawn 两种调用来源。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "agent": {"type": "string"},
                "invocation_mode": {"type": "string", "enum": ["explicit_at", "manager_spawn"]},
            },
        },
    },
    {
        "name": "codex_delivery_workflow_complete_agent_step",
        "description": "把项目级子 Agent 的最终输出写回工作流产物，并推进到下一步骤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "job_id": {"type": "string"},
                "output": {"type": ["string", "object"]},
                "spawned_agent_id": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["run_id", "job_id", "output"],
        },
    },
    {
        "name": "codex_delivery_workflow_manager_summary",
        "description": "主管 Agent 汇总当前大任务状态、待处理子任务、近期事件和已输出产物路径。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}},
    },
    {
        "name": "codex_delivery_workflow_confirm_prd",
        "description": "老板确认当前最新 PRD 后调用；工作流将进入 UI、前端、后端、QA 后续交付链路。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}},
    },
    {
        "name": "codex_delivery_workflow_request_prd_review",
        "description": "老板要求多 Agent 评审最新 PRD 时调用；会入队 UI、前端、后端、QA 评审，待各 @ Agent 领取后自动入队产品 Agent 整合下一版。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "note": {"type": "string"}}},
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
    if name in {"codex_delivery_workflow_init", "codex_delivery_workflow_init_project"}:
        return initialize_project_workspace(overwrite_config=bool(args.get("overwrite_config")), overwrite_agents=bool(args.get("overwrite_agents")))
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
    if name == "codex_delivery_workflow_prepare_handoff":
        return prepare_agent_handoff(run_id=args.get("run_id"), agent=args.get("agent"))
    if name == "codex_delivery_workflow_dispatch_next":
        return dispatch_next_agent_task(
            run_id=args.get("run_id"),
            agent=args.get("agent"),
            invocation_mode=args.get("invocation_mode", "explicit_at"),
        )
    if name == "codex_delivery_workflow_complete_agent_step":
        return complete_agent_step(
            run_id=args["run_id"],
            job_id=args["job_id"],
            output=args["output"],
            spawned_agent_id=args.get("spawned_agent_id"),
            metadata=args.get("metadata") or {},
        )
    if name == "codex_delivery_workflow_manager_summary":
        return manager_summary(run_id=args.get("run_id"))
    if name == "codex_delivery_workflow_confirm_prd":
        return confirm_prd(run_id=args.get("run_id"))
    if name == "codex_delivery_workflow_request_prd_review":
        return request_prd_review(run_id=args.get("run_id"), note=args.get("note"))
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
