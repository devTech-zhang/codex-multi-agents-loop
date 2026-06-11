from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .capabilities import doctor
from .config import initialize_project_workspace, lark_dry_run, lark_enabled, load_config
from .engine import (
    WorkflowError,
    create_project,
    current_project_status,
    delete_current_project,
    enqueue_step,
    inspect_workflow,
    list_artifacts,
    read_artifact,
    request_bug_fix,
    retry_prd_approval_lark,
    run_worker_once,
    run_worker_until_blocked,
    status,
    submit_gate,
    watch_run,
)


TOOLS = [
    {
        "name": "delivery_create_project",
        "description": "Create a delivery project and workflow run. If a real Feishu/Lark approval card is sent and the run blocks at approval, this tool automatically waits until the approval callback and subsequent worker jobs settle; do not ask the user whether to watch. When code_platforms.enable_agent_cli=false, agents must not implement generated development tasks themselves.",
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
        "name": "delivery_init_project_config",
        "description": "Initialize the current project workspace: workflow.config.json, .delivery-workflow/delivery.db, logs, delivery-project, and source-code.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "overwrite_config": {"type": "boolean"},
            },
        },
    },
    {
        "name": "delivery_get_current_project_status",
        "description": "Get progress for the current project in this workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "delivery_delete_current_project",
        "description": "Back up the current project to a zip file in this directory, then delete workflow records and workflow-owned project files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "backup": {"type": "boolean"},
            },
        },
    },
    {
        "name": "delivery_get_status",
        "description": "Get workflow run status, gates, jobs, events, and artifacts.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "delivery_submit_gate",
        "description": "Submit structured gate data for non-approval interactive steps. PRD/release approval gates are only submitted by Lark card callbacks or an explicit operator CLI action, never by the AI agent deciding in MCP.",
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
        "name": "delivery_watch_run",
        "description": "Wait in the foreground until a workflow run receives a gate/Lark callback event and settles with no pending/running jobs, or reaches a new stable step. Use this after creating a project blocked on Feishu/Lark approval; do not ask the user whether to continue pending workflow jobs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 86400},
                "poll_interval_seconds": {"type": "number", "minimum": 0.2, "maximum": 30},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "delivery_request_bug_fix",
        "description": "Trigger the bug-fix workflow for a real user-reported issue. The bug-fix step reads current PRD, design spec, tech plans, test cases, test report, and source-code before returning to regression testing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "reporter": {"type": "string"},
            },
            "required": ["issue"],
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
        "description": "Check local Codex, Claude, UI design, and Lark document capabilities.",
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
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "delivery-workflow", "version": __version__}}}
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
        created = create_project(
            requirement=args["requirement"],
            title=args.get("title"),
            business_goal=args.get("business_goal"),
            requires_frontend=args.get("requires_frontend", True),
            requires_backend=args.get("requires_backend", True),
        )
        return _maybe_auto_watch_created_project(created)
    if name == "delivery_init_project_config":
        return initialize_project_workspace(overwrite_config=bool(args.get("overwrite_config")))
    if name == "delivery_get_current_project_status":
        return current_project_status()
    if name == "delivery_delete_current_project":
        return delete_current_project(backup=args.get("backup", True))
    if name == "delivery_get_status":
        return status(args["run_id"])
    if name == "delivery_submit_gate":
        _guard_mcp_gate_submission(args["step_id"])
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
    if name == "delivery_watch_run":
        return watch_run(
            args["run_id"],
            timeout_seconds=args.get("timeout_seconds"),
            poll_interval_seconds=args.get("poll_interval_seconds"),
        )
    if name == "delivery_request_bug_fix":
        return request_bug_fix(
            issue=args["issue"],
            reporter=args.get("reporter"),
            source="mcp",
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


def _maybe_auto_watch_created_project(created: dict[str, Any]) -> dict[str, Any]:
    if not _mcp_auto_watch_enabled():
        current_status = _status_for_created_project(created)
        return _created_project_response(created, {"ok": True, "skipped": True, "reason": "workflow.mcp_auto_watch_after_create is false"}, current_status)
    run_id = created.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return _created_project_response(created, {"ok": True, "skipped": True, "reason": "missing run_id"}, None)
    current = status(run_id)
    current_step = current["run"].get("current_step")
    if current_step != "prd-approval":
        return _created_project_response(created, {"ok": True, "skipped": True, "reason": "run is not blocked at an approval gate", "current_step": current_step}, current)
    if not any(gate.get("step_id") == current_step and gate.get("status") == "open" for gate in current.get("gates", [])):
        return _created_project_response(created, {"ok": True, "skipped": True, "reason": "approval gate is not open", "current_step": current_step}, current)
    if not _real_lark_approval_card_was_sent(created):
        return _created_project_response(created, {"ok": True, "skipped": True, "reason": "no real Feishu/Lark approval card was sent", "current_step": current_step}, current)
    watch = watch_run(run_id)
    latest_status = watch.get("status") if isinstance(watch.get("status"), dict) else status(run_id)
    return _created_project_response(created, watch, latest_status)


def _status_for_created_project(created: dict[str, Any]) -> dict[str, Any] | None:
    run_id = created.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    try:
        return status(run_id)
    except Exception:
        return None


def _created_project_response(created: dict[str, Any], watch: dict[str, Any], latest_status: dict[str, Any] | None) -> dict[str, Any]:
    final_state = None
    if latest_status:
        run = latest_status.get("run") or {}
        final_state = {
            "project_id": run.get("project_id") or created.get("project_id"),
            "run_id": run.get("id") or created.get("run_id"),
            "status": run.get("status"),
            "current_step": run.get("current_step"),
            "open_gates": [
                gate.get("step_id")
                for gate in latest_status.get("gates", [])
                if gate.get("status") == "open"
            ],
            "pending_or_running_jobs": [
                job.get("step_id")
                for job in latest_status.get("jobs", [])
                if job.get("status") in {"pending", "running"}
            ],
        }
    return {
        "project": created,
        "watch": watch,
        "status": latest_status,
        "final_state": final_state,
        "agent_instruction": "Report the top-level final_state/status as the source of truth after auto-watch; do not summarize the initial project.auto_run snapshot as the current state.",
    }


def _mcp_auto_watch_enabled() -> bool:
    workflow = load_config().get("workflow") or {}
    return bool(workflow.get("mcp_auto_watch_after_create", True))


def _real_lark_approval_card_was_sent(created: dict[str, Any]) -> bool:
    config = load_config()
    if not lark_enabled(config) or lark_dry_run(config):
        return False
    auto_run = created.get("auto_run") if isinstance(created.get("auto_run"), dict) else {}
    results = auto_run.get("results") if isinstance(auto_run.get("results"), list) else []
    for item in reversed(results):
        result = item.get("result") if isinstance(item, dict) else {}
        gate = result.get("gate") if isinstance(result, dict) else {}
        lark = gate.get("lark") if isinstance(gate, dict) else {}
        card_result = lark.get("card_result") if isinstance(lark, dict) else {}
        if isinstance(card_result, dict) and card_result.get("ok"):
            return True
    return False


def _guard_mcp_gate_submission(step_id: str) -> None:
    if step_id == "prd-approval":
        raise WorkflowError(
            f"{step_id} must wait for the Feishu/Lark approval card callback, or be submitted by an explicit human CLI action; MCP agents are not allowed to approve or reject on behalf of the operator."
        )


def _read_message() -> dict[str, Any] | None:
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if line:
            return json.loads(line.decode("utf-8"))


def _write_message(message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(body + b"\n")
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
