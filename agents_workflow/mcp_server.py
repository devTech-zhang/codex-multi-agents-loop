from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .config import initialize_project_workspace, use_workspace_root
from .engine import (
    complete_agent_step,
    confirm_prd,
    create_agent_task,
    create_project,
    current_project_status,
    dispatch_next_agent_task,
    inspect_workflow,
    list_artifacts,
    memory_pack,
    manager_summary,
    prepare_agent_handoff,
    pull_global_memory,
    read_artifact,
    request_prd_review,
    sync_global_memory,
    status,
)


TOOLS = [
    {
        "name": "codex_multi_agents_loop_init",
        "description": "初始化当前项目的 Codex 多 Agent Loop：写入 .codex/agents、SQLite 薄状态账本、memory 和产物目录。",
        "inputSchema": {"type": "object", "properties": {"overwrite_config": {"type": "boolean"}, "overwrite_agents": {"type": "boolean"}}},
    },
    {
        "name": "codex_multi_agents_loop_init_project",
        "description": "codex_multi_agents_loop_init 的项目级别名，用于强调会把 Agent 加载到当前项目目录。",
        "inputSchema": {"type": "object", "properties": {"overwrite_config": {"type": "boolean"}, "overwrite_agents": {"type": "boolean"}}},
    },
    {
        "name": "codex_multi_agents_loop_create",
        "description": "创建一次 Codex 多 Agent Loop run，并准备第一个自定义 Agent 的调用任务。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "requirement": {"type": "string"},
                "title": {"type": "string"},
                "business_goal": {"type": "string"},
                "requires_frontend": {"type": "boolean"},
                "mode": {"type": "string", "enum": ["auto", "full_workflow", "single_agent_task", "multi_agent_task"]},
                "requested_agents": {"type": "array", "items": {"type": "string"}},
                "targets": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["requirement"],
        },
    },
    {
        "name": "codex_multi_agents_loop_create_task",
        "description": "员工 Agent 直接被 @ 且没有 pending job 时使用：为当前 Agent 创建轻量定向任务，可跳过完整 PM/PRD 流程。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "requirement": {"type": "string"},
                "agent": {"type": "string"},
                "title": {"type": "string"},
                "targets": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["requirement", "agent"],
        },
    },
    {
        "name": "codex_multi_agents_loop_status",
        "description": "查询工作流运行状态、任务、事件和产物；未传 run_id 时读取当前目录最新运行。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}},
    },
    {
        "name": "codex_multi_agents_loop_prepare_handoff",
        "description": "主管 Agent 使用：为下一个 pending job 生成自定义 Agent 的 spawn 或显式 @ 调用任务，但不领取任务。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "agent": {"type": "string"}}},
    },
    {
        "name": "codex_multi_agents_loop_dispatch_next",
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
        "name": "codex_multi_agents_loop_complete_agent_step",
        "description": "把项目级子 Agent 的最终输出写回工作流产物，并根据 metadata.loop_evaluation 生成闭环评估、自动入队下一轮或阻塞。",
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
        "name": "codex_multi_agents_loop_manager_summary",
        "description": "主管 Agent 汇总当前闭环任务状态、latest_evaluation、待处理子任务、近期事件和已输出产物路径。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}},
    },
    {
        "name": "codex_multi_agents_loop_confirm_prd",
        "description": "用户确认当前最新产品交接说明后调用；工作流将进入架构、UI、研发、QA 后续链路。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}},
    },
    {
        "name": "codex_multi_agents_loop_request_prd_review",
        "description": "用户要求多 Agent 评审最新产品交接说明时调用；会入队架构、UI、研发、QA 评审，待各 @ Agent 领取后自动入队产品 Agent 整合下一版。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "note": {"type": "string"}}},
    },
    {
        "name": "codex_multi_agents_loop_memory_pack",
        "description": "读取全局 Agent 记忆的少量相关 memory cards，用于任务包注入，避免全量记忆导致 token 膨胀。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "max_chars": {"type": "integer"},
            },
        },
    },
    {
        "name": "codex_multi_agents_loop_memory_pull",
        "description": "把全局 Agent 记忆中命中的少量经验拉入当前项目 Agent 记忆，项目 Agent 仍是唯一执行体。",
        "inputSchema": {"type": "object", "properties": {"agent": {"type": "string"}, "limit": {"type": "integer"}}},
    },
    {
        "name": "codex_multi_agents_loop_memory_sync",
        "description": "把当前项目 Agent 记忆摘要同步到全局 Agent 记忆；可 dry_run 预览，不同步完整产物正文。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "agent": {"type": "string"},
                "dry_run": {"type": "boolean"},
            },
        },
    },
    {
        "name": "codex_multi_agents_loop_list_artifacts",
        "description": "列出一次工作流运行的所有产物。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "codex_multi_agents_loop_read_artifact",
        "description": "读取指定工作流产物的最新版本。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "name": {"type": "string"}}, "required": ["run_id", "name"]},
    },
    {
        "name": "codex_multi_agents_loop_inspect",
        "description": "查看文件化 Codex 多 Agent Loop 定义。",
        "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string"}}},
    },
]


for tool in TOOLS:
    schema = tool.setdefault("inputSchema", {"type": "object", "properties": {}})
    properties = schema.setdefault("properties", {})
    properties.setdefault(
        "project_root",
        {
            "type": "string",
            "description": "当前 Codex 会话的项目根目录；当 MCP server 的 cwd 是插件目录时用于定位真实工作区。",
        },
    )


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
            params = message.get("params") or {}
            protocol_version = params.get("protocolVersion") if isinstance(params.get("protocolVersion"), str) else "2024-11-05"
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "codex-multi-agents-loop", "version": __version__},
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params_value = message.get("params")
            params: dict[str, Any] = params_value if isinstance(params_value, dict) else {}
            tool_name = params.get("name")
            if not isinstance(tool_name, str) or not tool_name.strip():
                raise ValueError("tool name must be a non-empty string")
            arguments_value = params.get("arguments")
            if arguments_value is None:
                arguments: dict[str, Any] = {}
            elif isinstance(arguments_value, dict):
                arguments = arguments_value
            else:
                raise ValueError("tool arguments must be an object")
            # MCP 请求属于不可信 JSON 边界，完成类型校验后再进入具体工具分发逻辑。
            result = _call_tool(tool_name, arguments)
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}}
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method: {method}"}}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(exc)}}


def _call_tool(name: str, args: dict[str, Any]) -> Any:
    project_root = args.get("project_root")
    clean_args = {key: value for key, value in args.items() if key != "project_root"}
    with use_workspace_root(project_root):
        return _call_tool_in_workspace(name, clean_args)


def _call_tool_in_workspace(name: str, args: dict[str, Any]) -> Any:
    if name in {"codex_multi_agents_loop_init", "codex_multi_agents_loop_init_project"}:
        return initialize_project_workspace(overwrite_config=bool(args.get("overwrite_config")), overwrite_agents=bool(args.get("overwrite_agents")))
    if name == "codex_multi_agents_loop_create":
        return create_project(
            requirement=args["requirement"],
            title=args.get("title"),
            business_goal=args.get("business_goal"),
            requires_frontend=args.get("requires_frontend", True),
            mode=args.get("mode", "auto"),
            requested_agents=args.get("requested_agents") or None,
            targets=args.get("targets") or None,
        )
    if name == "codex_multi_agents_loop_create_task":
        return create_agent_task(
            requirement=args["requirement"],
            agent=args["agent"],
            title=args.get("title"),
            targets=args.get("targets") or None,
        )
    if name == "codex_multi_agents_loop_status":
        return status(args["run_id"]) if args.get("run_id") else current_project_status()
    if name == "codex_multi_agents_loop_prepare_handoff":
        return prepare_agent_handoff(run_id=args.get("run_id"), agent=args.get("agent"))
    if name == "codex_multi_agents_loop_dispatch_next":
        return dispatch_next_agent_task(
            run_id=args.get("run_id"),
            agent=args.get("agent"),
            invocation_mode=args.get("invocation_mode", "explicit_at"),
        )
    if name == "codex_multi_agents_loop_complete_agent_step":
        return complete_agent_step(
            run_id=args["run_id"],
            job_id=args["job_id"],
            output=args["output"],
            spawned_agent_id=args.get("spawned_agent_id"),
            metadata=args.get("metadata") or {},
        )
    if name == "codex_multi_agents_loop_manager_summary":
        return manager_summary(run_id=args.get("run_id"))
    if name == "codex_multi_agents_loop_confirm_prd":
        return confirm_prd(run_id=args.get("run_id"))
    if name == "codex_multi_agents_loop_request_prd_review":
        return request_prd_review(run_id=args.get("run_id"), note=args.get("note"))
    if name == "codex_multi_agents_loop_memory_pack":
        return memory_pack(
            agent=args.get("agent"),
            query=args.get("query"),
            limit=int(args.get("limit") or 6),
            max_chars=int(args.get("max_chars") or 4000),
        )
    if name == "codex_multi_agents_loop_memory_pull":
        return pull_global_memory(agent=args.get("agent"), limit=int(args.get("limit") or 6))
    if name == "codex_multi_agents_loop_memory_sync":
        return sync_global_memory(run_id=args.get("run_id"), agent=args.get("agent"), dry_run=bool(args.get("dry_run")))
    if name == "codex_multi_agents_loop_list_artifacts":
        return list_artifacts(args["run_id"])
    if name == "codex_multi_agents_loop_read_artifact":
        return read_artifact(args["run_id"], args["name"])
    if name == "codex_multi_agents_loop_inspect":
        return inspect_workflow(args.get("workflow_id") or "codex-multi-agents-loop")
    raise ValueError(f"unknown tool: {name}")


def _read_message() -> dict[str, Any] | None:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    line = stream.readline()
    if not line:
        return None
    if isinstance(line, str):
        line_text = line
        line_bytes = line.encode("utf-8")
    else:
        line_bytes = line
        line_text = line.decode("utf-8")
    if line_text.lstrip().startswith("{"):
        return json.loads(line_text)

    headers: dict[str, str] = {}
    while line_bytes not in {b"\r\n", b"\n", b""}:
        name, separator, value = line_text.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
        line = stream.readline()
        if not line:
            return None
        if isinstance(line, str):
            line_text = line
            line_bytes = line.encode("utf-8")
        else:
            line_bytes = line
            line_text = line.decode("utf-8")

    content_length = headers.get("content-length")
    if not content_length:
        raise ValueError("missing Content-Length header")
    body = stream.read(int(content_length))
    if isinstance(body, str):
        body_text = body
    else:
        body_text = body.decode("utf-8")
    return json.loads(body_text)


def _write_message(message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(body + "\n")
        sys.stdout.flush()
        return
    stream.write((body + "\n").encode("utf-8"))
    stream.flush()


if __name__ == "__main__":
    main()
