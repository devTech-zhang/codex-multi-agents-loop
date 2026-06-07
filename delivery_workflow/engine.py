from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .capabilities import lark_doc_capability
from .config import WORKSPACE_CONFIG_NAME, code_platform_for_step, lark_chat_id as config_lark_chat_id, lark_config, lark_dry_run as config_lark_dry_run, lark_enabled, lark_identity, load_config, quality_gate_config, write_workspace_config
from .definitions import WorkflowDefinition, list_workflows, load_workflow
from .lark import build_prd_approval_resolved_card, create_doc_as_bot, extract_doc_url, lark_available, send_approval_card_as_bot, send_text_as_bot
from .lark_daemon import ensure_lark_event_consumer
from .paths import DEFAULT_WORKFLOW_ID, PLUGIN_ROOT, artifact_root, source_root
from .platforms import build_agent_command, execution_needs_permission, maybe_run_command, select_dev_executor
from .storage import connect, new_id, now_iso, row_dict, row_dicts
from .worker_daemon import start_worker_continuation


class WorkflowError(RuntimeError):
    pass


def create_project(
    *,
    requirement: str,
    title: str | None = None,
    platform: str | None = None,
    owner_id: str | None = None,
    source: str = "codex",
    workflow_id: str = DEFAULT_WORKFLOW_ID,
    auto_start: bool | None = None,
    auto_run_to_gate: bool | None = None,
    business_goal: str | None = None,
    requires_frontend: bool = True,
    requires_backend: bool = True,
    lark_chat_id: str | None = None,
) -> dict[str, Any]:
    _ensure_project_workspace_files()
    config = load_config()
    platform = code_platform_for_step(config, fallback=platform)
    if lark_chat_id is None:
        lark_chat_id = config_lark_chat_id(config)
    workflow_config = config.get("workflow") or {}
    if auto_start is None:
        auto_start = bool(workflow_config.get("auto_start", True))
    if auto_run_to_gate is None:
        auto_run_to_gate = bool(workflow_config.get("auto_run_to_gate", True))
    requirement = requirement.strip()
    if not requirement:
        raise WorkflowError("requirement cannot be empty")
    definition = load_workflow(workflow_id)
    ts = now_iso()
    project_title = title or requirement[:40]
    project_id = _new_project_id(project_title)
    run_id = new_id("run")
    start_step = "prd-v1"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO projects(id,title,requirement,platform,source,owner_id,lark_chat_id,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (project_id, project_title, requirement, platform, source, owner_id, lark_chat_id, "created", ts, ts),
        )
        conn.execute(
            """
            INSERT INTO workflow_runs(id,project_id,workflow_id,current_step,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (run_id, project_id, workflow_id, start_step, "running", ts, ts),
        )
    write_artifact(run_id, "raw_requirement", requirement, category="raw", created_by="workflow")
    write_artifact(
        run_id,
        "project_context",
        json.dumps(
            {
                "project_id": project_id,
                "run_id": run_id,
                "platform": platform,
                "dev_executor": select_dev_executor(platform),
                "code_platforms": config.get("code_platforms", {}),
                "quality_gate": quality_gate_config(config),
                "workflow_id": workflow_id,
                "lark_chat_id": lark_chat_id,
                "project_root": str(Path.cwd()),
                "artifact_root": str(artifact_root()),
                "source_root": str(source_root()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        category="context",
        created_by="workflow",
    )
    write_artifact(
        run_id,
        "requirement-intake_gate",
        json.dumps(
            {
                "project_name": project_title,
                "requirement_summary": requirement,
                "business_goal": business_goal or "待进一步明确业务目标",
                "requires_frontend": requires_frontend,
                "requires_backend": requires_backend,
            },
            ensure_ascii=False,
            indent=2,
        ),
        category="gate",
        created_by="workflow",
    )
    emit_event(
        run_id,
        "workflow.started",
        "项目已创建，Worker 自动启动并进入 PRD v1 流程。",
        {"project_id": project_id, "start_step": start_step},
    )
    if auto_start:
        enqueue_step(run_id, start_step)
    auto_run_result = None
    if auto_start and auto_run_to_gate:
        auto_run_result = run_worker_until_blocked(run_id=run_id, stop_steps={"prd-approval"}, max_jobs=20)
    result = {
        "project_id": project_id,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "artifact_dir": str(artifact_root()),
        "source_dir": str(source_root()),
        "config_path": str(Path.cwd() / WORKSPACE_CONFIG_NAME),
        "execution_policy": _execution_policy(config),
    }
    if auto_run_result is not None:
        result["auto_run"] = auto_run_result
    return result


def get_run(run_id: str) -> dict[str, Any]:
    with connect() as conn:
        run = row_dict(conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone())
        if not run:
            raise WorkflowError(f"workflow run not found: {run_id}")
        project = row_dict(conn.execute("SELECT * FROM projects WHERE id = ?", (run["project_id"],)).fetchone())
    run["project"] = project
    return run


def list_projects(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        return row_dicts(conn.execute("SELECT * FROM projects ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall())


def get_project_status(project_id: str) -> dict[str, Any]:
    with connect() as conn:
        run = row_dict(
            conn.execute(
                "SELECT * FROM workflow_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        )
    if not run:
        raise WorkflowError(f"project not found or has no workflow run: {project_id}")
    return status(run["id"])


def delete_project(project_id: str, *, confirm_project_id: str, delete_artifacts: bool = True) -> dict[str, Any]:
    if not project_id or project_id != confirm_project_id:
        raise WorkflowError("confirm_project_id must exactly match project_id")
    with connect() as conn:
        project = row_dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
        if not project:
            raise WorkflowError(f"project not found: {project_id}")
        run_rows = conn.execute("SELECT id FROM workflow_runs WHERE project_id = ?", (project_id,)).fetchall()
        run_ids = [row["id"] for row in run_rows]
        for run_id in run_ids:
            conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM gates WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM artifacts WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM jobs WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM step_runs WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM workflow_runs WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    artifact_dir = artifact_root()
    artifacts_deleted = False
    if delete_artifacts and artifact_dir.exists():
        shutil.rmtree(artifact_dir)
        artifacts_deleted = True
    return {
        "ok": True,
        "project_id": project_id,
        "deleted_runs": run_ids,
        "artifact_dir": str(artifact_dir),
        "artifacts_deleted": artifacts_deleted,
    }


def request_bug_fix(
    *,
    issue: str,
    project_id: str | None = None,
    reporter: str | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    issue = issue.strip()
    if not issue:
        raise WorkflowError("bug fix issue cannot be empty")
    run = _latest_run_for_bug_fix(project_id)
    artifact = write_artifact(
        run["id"],
        "manual_bug_fix_request",
        json.dumps(
            {
                "issue": issue,
                "reporter": reporter or "user",
                "source": source,
                "created_at": now_iso(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        category="bug-fix",
        created_by="user",
    )
    job = enqueue_step(run["id"], "bug-fix", {"source": source, "reporter": reporter})
    emit_event(run["id"], "bug_fix.requested", "已收到人工修复问题请求，进入 bug-fix 流程。", {"issue": issue, "artifact": artifact, "job": job})
    return {"ok": True, "run_id": run["id"], "project_id": run["project_id"], "artifact": artifact, "job": job}


def list_jobs(run_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect() as conn:
        return row_dicts(
            conn.execute(f"SELECT * FROM jobs{where} ORDER BY created_at DESC LIMIT ?", (*params, limit)).fetchall()
        )


def enqueue_step(run_id: str, step_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    definition = load_workflow(get_run(run_id)["workflow_id"])
    step = definition.step(step_id)
    job_id = new_id("job")
    ts = now_iso()
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM jobs WHERE run_id = ? AND step_id = ? AND status IN ('pending','running')",
            (run_id, step_id),
        ).fetchone()
        if existing:
            return dict(existing)
        conn.execute(
            """
            INSERT INTO jobs(id,run_id,step_id,job_type,status,payload_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (job_id, run_id, step_id, step["executor"], "pending", json.dumps(payload or {}, ensure_ascii=False), ts, ts),
        )
    emit_event(run_id, "job.enqueued", f"step {step_id} enqueued", {"job_id": job_id})
    return {"id": job_id, "run_id": run_id, "step_id": step_id, "status": "pending"}


def run_worker_once(run_id: str | None = None) -> dict[str, Any]:
    with connect() as conn:
        if run_id:
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'pending' AND run_id = ? ORDER BY created_at LIMIT 1",
                (run_id,),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return {"ok": True, "idle": True}
        job = dict(row)
        ts = now_iso()
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = ?, updated_at = ? WHERE id = ?",
            (ts, ts, job["id"]),
        )
    try:
        result = execute_step(job["run_id"], job["step_id"], json.loads(job.get("payload_json") or "{}"))
        with connect() as conn:
            ts = now_iso()
            conn.execute(
                "UPDATE jobs SET status = 'done', result_json = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False), ts, ts, job["id"]),
            )
        return {"ok": True, "job_id": job["id"], "result": result}
    except Exception as exc:
        with connect() as conn:
            ts = now_iso()
            conn.execute(
                "UPDATE jobs SET status = 'failed', error = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                (str(exc), ts, ts, job["id"]),
            )
        emit_event(job["run_id"], "job.failed", str(exc), {"job_id": job["id"], "step_id": job["step_id"]})
        return {"ok": False, "job_id": job["id"], "error": str(exc)}


def run_worker_until_blocked(
    *,
    run_id: str | None = None,
    max_jobs: int = 50,
    stop_steps: set[str] | None = None,
) -> dict[str, Any]:
    stop_steps = stop_steps or set()
    results: list[dict[str, Any]] = []
    for _ in range(max_jobs):
        current_status = status(run_id) if run_id else None
        current = current_status["run"] if current_status else None
        if current and current["current_step"] in stop_steps and any(
            gate["step_id"] == current["current_step"] and gate["status"] == "open" for gate in current_status["gates"]
        ):
            break
        item = run_worker_once(run_id=run_id)
        results.append(item)
        if not item.get("ok"):
            return {"ok": False, "stopped": "failed", "results": results}
        if item.get("idle"):
            return {"ok": True, "stopped": "idle", "results": results}
        result = item.get("result") or {}
        if result.get("blocked"):
            return {"ok": True, "stopped": "blocked", "results": results}
        if result.get("step_id") in stop_steps:
            break
    return {"ok": True, "stopped": "limit_or_stop_step", "results": results}


def execute_step(run_id: str, step_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    run = get_run(run_id)
    definition = load_workflow(run["workflow_id"])
    step = definition.step(step_id)
    _mark_step(run_id, step, "running", payload)
    _notify_step(run, step, "started", payload)
    executor = step["executor"]
    if executor == "gate":
        result = _open_gate(run_id, step)
        _mark_step(run_id, step, "blocked", result)
        _notify_step(run, step, "blocked", result)
        return result
    if executor == "agent":
        result = _run_agent_step(run, definition, step)
    elif executor == "validator":
        result = _run_validator(run_id, step)
    elif executor == "dev-runner":
        result = _run_dev_step(run, definition, step)
    elif executor == "lark-doc":
        result = _publish_lark_doc(run_id, step)
    elif executor == "notify":
        result = _notify(run_id, step)
    else:
        result = _run_system_step(run_id, step)
    _mark_step(run_id, step, "completed", result)
    _notify_step(run, step, "completed", result)
    next_step = _next_step(step, result)
    _move_to_next(run_id, next_step)
    if next_step:
        enqueue_step(run_id, next_step)
    return {"step_id": step_id, "status": "completed", "result": result, "next_step": next_step}


def submit_gate(run_id: str, step_id: str, data: dict[str, Any]) -> dict[str, Any]:
    run = get_run(run_id)
    definition = load_workflow(run["workflow_id"])
    step = definition.step(step_id)
    schema = step.get("gate", {}).get("schema", {})
    errors = _validate_gate(schema, data)
    if errors:
        raise WorkflowError("; ".join(errors))
    ts = now_iso()
    with connect() as conn:
        gate = conn.execute("SELECT * FROM gates WHERE run_id = ? AND step_id = ?", (run_id, step_id)).fetchone()
        if not gate:
            raise WorkflowError(f"gate is not open: {step_id}")
        if gate["status"] != "open":
            raise WorkflowError(f"gate is not open: {step_id}")
        conn.execute(
            "UPDATE gates SET status = 'submitted', data_json = ?, updated_at = ? WHERE run_id = ? AND step_id = ?",
            (json.dumps(data, ensure_ascii=False), ts, run_id, step_id),
        )
    write_artifact(
        run_id,
        f"{step_id}_gate",
        json.dumps(data, ensure_ascii=False, indent=2),
        category="gate",
        created_by="workflow",
    )
    _mark_step(run_id, step, "completed", {"gate": data})
    next_step = _next_step(step, data)
    _move_to_next(run_id, next_step)
    if next_step:
        enqueue_step(run_id, next_step)
    emit_event(run_id, "gate.submitted", f"gate {step_id} submitted", data)
    _notify_gate_submitted(run, step, data)
    return {"ok": True, "run_id": run_id, "step_id": step_id, "next_step": next_step}


def handle_lark_card_event(payload: dict[str, Any]) -> dict[str, Any]:
    value = _extract_lark_action_value(payload)
    action = value.get("action")
    if action not in {"approve_prd", "reject_prd"}:
        raise WorkflowError(f"unsupported lark card action: {action}")
    run_id = value.get("run_id")
    step_id = value.get("step_id") or "prd-approval"
    if not isinstance(run_id, str) or not run_id:
        raise WorkflowError("lark card event missing run_id")
    approved = bool(value.get("approved"))
    approver = _extract_lark_operator(payload)
    reason = _extract_lark_approval_reason(payload, value)
    if not approved and not reason:
        raise WorkflowError("拒绝 PRD 时必须填写拒绝理由")
    comment = reason or "通过"
    with _temporary_lark_workspace(value):
        run = get_run(run_id)
        _validate_lark_approval_card_current(run_id, value)
        result = submit_gate(
            run_id,
            step_id,
            {
                "approved": approved,
                "approver": approver,
                "comment": comment,
            },
        )
        resolved_card = build_prd_approval_resolved_card(
            project_title=run["project"]["title"],
            project_id=run["project_id"],
            run_id=run_id,
            doc_url=str(value.get("doc_url") or ""),
            approved=approved,
            reason=comment,
            approver=approver,
            approval_round=_latest_prd_approval_round(run_id),
        )
        emit_event(run_id, "lark.card_event.handled", f"飞书审批卡片事件已处理: {action}", {"action": action, "approved": approved, "reason": comment})
        continuation = start_worker_continuation(run_id, reason=f"lark-card:{action}")
        emit_event(run_id, "workflow.continuation.started" if continuation.get("ok") else "workflow.continuation.failed", "飞书审批回调已触发 workflow 后续推进。", continuation)
    return {"ok": True, "action": action, "approved": approved, "reason": comment, "approver": approver, "gate": result, "response_card": resolved_card, "continuation": continuation}


def watch_run(
    run_id: str,
    *,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float | None = None,
) -> dict[str, Any]:
    config = load_config()
    workflow = config.get("workflow") or {}
    timeout = float(timeout_seconds if timeout_seconds is not None else workflow.get("watch_timeout_seconds") or 7200)
    interval = float(poll_interval_seconds if poll_interval_seconds is not None else workflow.get("watch_poll_interval_seconds") or 2.0)
    started_at = now_iso()
    initial_status = status(run_id)
    initial_step = initial_status["run"]["current_step"]
    deadline = time.monotonic() + max(timeout, 0.0)

    observed_events: list[dict[str, Any]] = []
    while True:
        current_status = status(run_id)
        new_events = _events_since(run_id, started_at)
        gate_events = [event for event in new_events if event["event_type"] in {"gate.submitted", "lark.card_event.handled", "workflow.continuation.started", "workflow.continuation.failed"}]
        if gate_events:
            observed_events = gate_events
        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "run_id": run_id,
                "reason": "timeout",
                "initial_step": initial_step,
                "status": current_status,
                "events": observed_events or new_events,
            }
        if observed_events and _watch_should_continue_waiting_for_reapproval(current_status, observed_events):
            time.sleep(max(interval, 0.2))
            continue
        if observed_events and _watch_status_is_settled(current_status):
            return {
                "ok": True,
                "run_id": run_id,
                "reason": "workflow settled after event",
                "initial_step": initial_step,
                "status": current_status,
                "events": observed_events,
            }
        if current_status["run"]["current_step"] != initial_step and _watch_status_is_settled(current_status):
            return {
                "ok": True,
                "run_id": run_id,
                "reason": "current step changed and workflow settled",
                "initial_step": initial_step,
                "status": current_status,
                "events": new_events,
            }
        time.sleep(max(interval, 0.2))


def retry_prd_approval_lark(run_id: str) -> dict[str, Any]:
    run = get_run(run_id)
    definition = load_workflow(run["workflow_id"])
    step = definition.step("prd-approval")
    try:
        read_artifact(run_id, "prd_v2")
    except WorkflowError as exc:
        raise WorkflowError("cannot retry PRD approval lark actions before prd_v2 exists") from exc
    result = _publish_prd_v2_doc_and_send_approval(run_id, step)
    emit_event(run_id, "lark.prd_approval.retry", "已重试 PRD v2 飞书文档和审批卡片发送。", result)
    return {"ok": bool(result.get("ok")), "run_id": run_id, "result": result}


def status(run_id: str) -> dict[str, Any]:
    run = get_run(run_id)
    with connect() as conn:
        steps = row_dicts(conn.execute("SELECT * FROM step_runs WHERE run_id = ? ORDER BY started_at", (run_id,)).fetchall())
        gates = row_dicts(conn.execute("SELECT * FROM gates WHERE run_id = ? ORDER BY created_at", (run_id,)).fetchall())
        jobs = row_dicts(conn.execute("SELECT * FROM jobs WHERE run_id = ? ORDER BY created_at DESC LIMIT 20", (run_id,)).fetchall())
        artifacts = list_artifacts(run_id)
        events = row_dicts(conn.execute("SELECT * FROM events WHERE run_id = ? ORDER BY created_at DESC LIMIT 20", (run_id,)).fetchall())
    return {"run": run, "step_runs": steps, "gates": gates, "jobs": jobs, "artifacts": artifacts, "events": events}


def _events_since(run_id: str, created_at: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return row_dicts(
            conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND created_at > ? ORDER BY created_at",
                (run_id, created_at),
            ).fetchall()
        )


def _watch_status_is_settled(current_status: dict[str, Any]) -> bool:
    active_jobs = [job for job in current_status.get("jobs", []) if job.get("status") in {"pending", "running"}]
    if active_jobs:
        return False
    current_step = (current_status.get("run") or {}).get("current_step")
    gates = current_status.get("gates", [])
    if any(gate.get("step_id") == current_step and gate.get("status") == "open" for gate in gates):
        return True
    run_status = (current_status.get("run") or {}).get("status")
    return run_status in {"completed", "failed"} or not active_jobs


def _watch_should_continue_waiting_for_reapproval(current_status: dict[str, Any], events: list[dict[str, Any]]) -> bool:
    current_step = (current_status.get("run") or {}).get("current_step")
    if current_step != "prd-approval":
        return False
    gates = current_status.get("gates", [])
    if not any(gate.get("step_id") == "prd-approval" and gate.get("status") == "open" for gate in gates):
        return False
    approved = _latest_approval_event_result(events)
    return approved is False


def _latest_approval_event_result(events: list[dict[str, Any]]) -> bool | None:
    for event in reversed(events):
        payload = event.get("payload_json")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        if not isinstance(payload, dict):
            continue
        if "approved" in payload:
            return bool(payload.get("approved"))
        gate = payload.get("gate")
        if isinstance(gate, dict) and "approved" in gate:
            return bool(gate.get("approved"))
    return None


def write_artifact(
    run_id: str,
    name: str,
    content: str,
    *,
    category: str,
    created_by: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with connect() as conn:
        previous = conn.execute("SELECT MAX(version) AS version FROM artifacts WHERE run_id = ? AND name = ?", (run_id, name)).fetchone()
        version = int(previous["version"] or 0) + 1
    path = artifact_root() / _path_segment(created_by) / category / f"v{version}" / _artifact_filename(name, content)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    item_id = new_id("art")
    ts = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO artifacts(id,run_id,name,category,path,version,created_by,metadata_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (item_id, run_id, name, category, str(path), version, created_by, json.dumps(metadata or {}, ensure_ascii=False), ts),
        )
    return {"id": item_id, "run_id": run_id, "name": name, "path": str(path), "version": version}


def read_artifact(run_id: str, name: str) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE run_id = ? AND name = ? ORDER BY version DESC LIMIT 1",
            (run_id, name),
        ).fetchone()
    if not row:
        raise WorkflowError(f"artifact not found: {name}")
    item = dict(row)
    item["content"] = Path(item["path"]).read_text(encoding="utf-8")
    return item


def list_artifacts(run_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return row_dicts(conn.execute("SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at", (run_id,)).fetchall())


def emit_event(run_id: str, event_type: str, message: str, payload: dict[str, Any] | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO events(id,run_id,event_type,message,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (new_id("evt"), run_id, event_type, message, json.dumps(payload or {}, ensure_ascii=False), now_iso()),
        )


def inspect_workflow(workflow_id: str = DEFAULT_WORKFLOW_ID) -> dict[str, Any]:
    definition = load_workflow(workflow_id)
    return {
        "id": definition.workflow_id,
        "name": definition.name,
        "version": definition.version,
        "steps": definition.steps,
    }


def workflows() -> list[dict[str, str]]:
    return list_workflows()


def _open_gate(run_id: str, step: dict[str, Any]) -> dict[str, Any]:
    schema = step.get("gate", {}).get("schema", {})
    lark_result = _publish_prd_v2_doc_and_send_approval(run_id, step) if step["id"] == "prd-approval" else None
    ts = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO gates(id,run_id,step_id,status,schema_json,data_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id, step_id) DO UPDATE SET
              status='open',
              schema_json=excluded.schema_json,
              data_json=NULL,
              updated_at=excluded.updated_at
            """,
            (new_id("gate"), run_id, step["id"], "open", json.dumps(schema, ensure_ascii=False), None, ts, ts),
        )
    card = {
        "type": "approval" if step.get("gate", {}).get("kind") == "approval" else "input",
        "title": step["name"],
        "step_id": step["id"],
        "schema": schema,
        "actions": step.get("gate", {}).get("actions", []),
    }
    if lark_result is not None:
        card["lark"] = lark_result
    emit_event(run_id, "gate.opened", f"gate {step['id']} opened", card)
    return {"blocked": True, "gate": card}


def _run_agent_step(run: dict[str, Any], definition: WorkflowDefinition, step: dict[str, Any]) -> dict[str, Any]:
    prompt = _render_prompt(run["id"], definition, step)
    package = write_artifact(
        run["id"],
        f"{step['id']}_agent_task",
        prompt,
        category="agent-task",
        created_by=step.get("agent", "agent"),
        metadata={"agent": step.get("agent"), "step_id": step["id"]},
    )
    command = build_agent_command(run["project"]["platform"], Path(package["path"]))
    execution = maybe_run_command(command)
    _ensure_agent_execution_completed(execution, step["id"])
    outputs = []
    for artifact_name in step.get("outputs", []):
        content = _agent_output_content(step, artifact_name, prompt, execution)
        outputs.append(write_artifact(run["id"], artifact_name, content, category=step.get("artifact_category", "agent"), created_by=step.get("agent", "agent")))
    return {"agent": step.get("agent"), "task_package": package, "execution": execution, "outputs": outputs}


def _run_dev_step(run: dict[str, Any], definition: WorkflowDefinition, step: dict[str, Any]) -> dict[str, Any]:
    prompt = _render_prompt(run["id"], definition, step)
    platform = code_platform_for_step(load_config(), step.get("id"), fallback=run["project"]["platform"])
    executor = select_dev_executor(platform)
    package = write_artifact(
        run["id"],
        f"{step['id']}_dev_task",
        prompt,
        category="dev-task",
        created_by=f"{executor}-adapter",
        metadata={"executor": executor, "platform": platform},
    )
    command = build_agent_command(platform, Path(package["path"]))
    execution = maybe_run_command(command)
    _ensure_agent_execution_completed(execution, step["id"])
    if step["id"] == "regression-testing" and not execution.get("executed"):
        raise WorkflowError(f"{step['id']} requires real QA execution before quality gate evaluation: {execution.get('reason') or 'agent CLI not executed'}")
    result_name = step.get("outputs", [f"{step['id']}_result"])[0]
    result = write_artifact(
        run["id"],
        result_name,
        _dev_result_content(run["id"], step, executor, platform, package, execution),
        category="dev-result",
        created_by=f"{executor}-adapter",
    )
    payload: dict[str, Any] = {"executor": executor, "task_package": package, "execution": execution, "result": result}
    if step["id"] == "regression-testing":
        quality_gate = _evaluate_quality_gate(run["id"], result_name)
        payload["quality_gate"] = quality_gate
        payload["can_proceed"] = quality_gate["passed"]
    return payload


def _run_validator(run_id: str, step: dict[str, Any]) -> dict[str, Any]:
    missing = []
    for name in step.get("requires", []):
        try:
            artifact = read_artifact(run_id, name)
        except WorkflowError:
            missing.append(name)
            continue
        if not artifact["content"].strip():
            missing.append(name)
    result = {"can_proceed": not missing, "missing_artifacts": missing, "step_id": step["id"]}
    write_artifact(run_id, step.get("outputs", [f"{step['id']}_validation"])[0], json.dumps(result, ensure_ascii=False, indent=2), category="validation", created_by="validator")
    if missing:
        raise WorkflowError(f"validator blocked {step['id']}: missing {', '.join(missing)}")
    return result


def _run_system_step(run_id: str, step: dict[str, Any]) -> dict[str, Any]:
    outputs = []
    for name in step.get("outputs", []):
        outputs.append(write_artifact(run_id, name, f"# {step['name']}\n\n系统步骤 `{step['id']}` 已完成。\n", category=step.get("artifact_category", "system"), created_by="workflow"))
    return {"outputs": outputs}


def _publish_lark_doc(run_id: str, step: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    capability = lark_doc_capability()
    run = get_run(run_id)
    source_name = step.get("source_artifact", "final_delivery_report")
    source = read_artifact(run_id, source_name)
    if not lark_enabled(config):
        result = {"ok": True, "skipped": True, "reason": "lark disabled by config", "source_artifact": source_name}
        write_artifact(
            run_id,
            step.get("output_artifact", f"{source_name}_lark_doc"),
            json.dumps(result, ensure_ascii=False, indent=2),
            category="lark-doc",
            created_by="lark-bot",
        )
        return result
    if not capability["ok"]:
        raise WorkflowError(f"缺少飞书文档能力: {capability['install_hint']}")
    default_title = "{project_title}最终交付报告" if source_name == "final_delivery_report" else "{project_title}" + step["name"]
    title_template = str(step.get("title_template") or lark_config(config).get("final_report_doc_title_template") or default_title)
    title = title_template.format(project_title=run["project"]["title"], project_id=run["project_id"], run_id=run_id)
    result = create_doc_as_bot(title, source["content"], identity=lark_identity(config), dry_run=config_lark_dry_run(config))
    if not result.get("ok"):
        raise WorkflowError(f"飞书文档创建失败: {json.dumps(result, ensure_ascii=False)[-800:]}")
    doc_url = extract_doc_url(result)
    if not doc_url and config_lark_dry_run(config):
        doc_url = f"dry-run://lark-doc/{run['project_id']}/final-report"
    output_artifact = step.get("output_artifact", "final_report_lark_doc")
    artifact = write_artifact(
        run_id,
        output_artifact,
        json.dumps(
            {
                "title": title,
                "url": doc_url,
                "source_artifact": source_name,
                "command": result.get("command"),
                "result": result,
            },
            ensure_ascii=False,
            indent=2,
        ),
        category="lark-doc",
        created_by="lark-bot",
    )
    _append_lark_doc_manifest(run_id, title=title, source_artifact=source_name, doc_url=doc_url, artifact_name=output_artifact)
    if doc_url:
        _send_lark_text(run, f"最终交付报告飞书文档已发布：{doc_url}", event_key=f"{run_id}:final-report-doc:published")
    return {"capability": capability, "artifact": artifact, "doc_url": doc_url, "lark_cli": result}


def _notify(run_id: str, step: dict[str, Any]) -> dict[str, Any]:
    message = step.get("message", f"{step['name']} completed")
    emit_event(run_id, "notification", message, {"step_id": step["id"]})
    _send_lark_text(get_run(run_id), message, event_key=f"{run_id}:{step['id']}:notify")
    return {"message": message}


def _publish_prd_v2_doc_and_send_approval(run_id: str, step: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    lark = lark_config(config)
    run = get_run(run_id)
    chat_id = _lark_chat_id(run)
    dry_run = _lark_dry_run()
    if not lark_enabled(config):
        result = {"ok": True, "skipped": True, "reason": "lark disabled by config"}
        emit_event(run_id, "lark.prd_approval.skipped", "配置已关闭飞书动作，未创建 PRD 飞书文档和审批卡片。", result)
        return result
    if not chat_id and not bool(lark.get("create_prd_doc_without_chat")):
        result = {"ok": True, "skipped": True, "reason": "missing lark chat_id"}
        emit_event(run_id, "lark.prd_approval.skipped", "未配置飞书群聊，跳过 PRD 文档和审批卡片。", result)
        return result
    if not dry_run and not lark_available():
        result = {"ok": False, "skipped": True, "reason": "lark-cli not found; run doctor and install via npx @larksuite/cli@latest install"}
        emit_event(run_id, "lark.prd_approval.skipped", "缺少 lark-cli，未创建 PRD 飞书文档和审批卡片。", result)
        return result

    prd_v2 = read_artifact(run_id, "prd_v2")
    prd_doc_markdown = _compose_prd_v2_lark_markdown(run_id, prd_v2["content"])
    approval_round = int(prd_v2.get("version") or 1)
    title_template = str(lark.get("prd_doc_title_template") or "{project_title}PRD")
    title = title_template.format(project_title=run["project"]["title"], project_id=run["project_id"], run_id=run_id)
    _send_lark_text(run, f"PRD v2 已生成，开始创建飞书文档：{title}", event_key=f"{run_id}:prd-v2-doc:start")
    doc_result = create_doc_as_bot(title, prd_doc_markdown, identity=lark_identity(config), dry_run=dry_run)
    doc_url = extract_doc_url(doc_result)
    if not doc_url and dry_run:
        doc_url = f"dry-run://lark-doc/{run['project_id']}/prd-v2"
    doc_artifact = write_artifact(
        run_id,
        "prd_v2_lark_doc",
        json.dumps({"title": title, "url": doc_url, "source_artifact": "prd_v2", "result": doc_result}, ensure_ascii=False, indent=2),
        category="prd",
        created_by="lark-bot",
    )
    write_artifact(run_id, "prd_v2_lark_markdown", prd_doc_markdown, category="prd", created_by="workflow")
    _append_lark_doc_manifest(run_id, title=title, source_artifact="prd_v2", doc_url=doc_url, artifact_name="prd_v2_lark_doc")
    if not doc_result.get("ok"):
        payload = {"result": doc_result}
        if doc_result.get("error_type") == "keychain_unavailable":
            payload["retry_command"] = _host_lark_retry_command(run_id)
            payload["host_escalation"] = _host_escalation_payload(payload["retry_command"])
        emit_event(run_id, "lark.prd_doc.failed", "PRD v2 飞书文档创建失败。", payload)
        return {"ok": False, "doc": doc_artifact, "doc_result": doc_result}
    if not chat_id:
        result = {"ok": True, "doc": doc_artifact, "doc_url": doc_url, "card_skipped": True, "reason": "missing lark_chat_id"}
        emit_event(run_id, "lark.prd_approval.doc_created", "PRD v2 飞书文档已创建，未配置飞书群聊所以未发送审批卡片。", result)
        return result
    if not doc_url:
        result = {"ok": False, "doc": doc_artifact, "card_skipped": True, "reason": "lark doc url missing", "doc_result": doc_result}
        emit_event(run_id, "lark.prd_approval.card_skipped", "PRD v2 飞书文档 URL 缺失，未发送审批卡片。", result)
        return result

    if not bool(lark.get("send_prd_approval_card", True)):
        result = {"ok": True, "doc": doc_artifact, "doc_url": doc_url, "card_skipped": True, "reason": "approval card disabled by config"}
        emit_event(run_id, "lark.prd_approval.card_skipped", "配置已关闭 PRD 审批卡片发送。", result)
        return result
    rejection_reason = _latest_prd_rejection_reason(run_id)
    if approval_round > 1 and rejection_reason:
        _send_lark_text(
            run,
            f"PRD v2 已按照拒绝原因：{rejection_reason} 修改，准备发起第 {approval_round} 轮复审。",
            event_key=f"{run_id}:prd-approval-card:review-round-{approval_round}:start",
        )
    listener_result = ensure_lark_event_consumer(run_id)
    emit_event(
        run_id,
        "lark.event_consumer.ready" if listener_result.get("ok") else "lark.event_consumer.failed",
        "飞书审批按钮长连接监听已就绪。" if listener_result.get("ok") else "飞书审批按钮长连接监听未启动成功。",
        listener_result,
    )
    _send_lark_text(run, f"开始发送 PRD 审批卡片，文档链接：{doc_url}", event_key=f"{run_id}:prd-approval-card:start")
    card_result = send_approval_card_as_bot(
        chat_id=chat_id,
        project_title=run["project"]["title"],
        project_id=run["project_id"],
        run_id=run_id,
        step_id=step["id"],
        doc_url=doc_url,
        identity=lark_identity(config),
        dry_run=dry_run,
        idempotency_key=_approval_card_idempotency_key(run_id, approval_round, int(doc_artifact["version"])),
        workspace=str(Path.cwd()),
        approval_round=approval_round,
    )
    card_artifact = write_artifact(
        run_id,
        "prd_approval_card_message",
        json.dumps({"chat_id": chat_id, "doc_url": doc_url, "result": card_result}, ensure_ascii=False, indent=2),
        category="approval",
        created_by="lark-bot",
    )
    message = "PRD v2 飞书文档和审批卡片已发送。" if card_result.get("ok") else "PRD v2 飞书审批卡片发送失败。"
    payload = {"doc_url": doc_url, "card_result": card_result, "listener": listener_result}
    if card_result.get("error_type") == "keychain_unavailable":
        payload["retry_command"] = _host_lark_retry_command(run_id)
        payload["host_escalation"] = _host_escalation_payload(payload["retry_command"])
    emit_event(run_id, "lark.prd_approval.card_sent" if card_result.get("ok") else "lark.prd_approval.card_failed", message, payload)
    return {"ok": bool(card_result.get("ok")), "doc": doc_artifact, "doc_url": doc_url, "card": card_artifact, "card_result": card_result, "listener": listener_result}


def _execution_policy(config: dict[str, Any]) -> dict[str, Any]:
    enabled = bool((config.get("code_platforms") or {}).get("enable_agent_cli"))
    return {
        "enable_agent_cli": enabled,
        "mode": "agent_cli_enabled" if enabled else "prepared_only",
        "rule": "enable_agent_cli=false 时，Workflow 只生成任务包和结构化产物，当前 Agent 不得自行接管前后端实现或替人工提交审批。",
    }


def _ensure_project_workspace_files() -> None:
    if not (Path.cwd() / WORKSPACE_CONFIG_NAME).exists() and not (Path.cwd() / "delivery-workflow.config.json").exists():
        write_workspace_config(overwrite=False)
    source_root().mkdir(parents=True, exist_ok=True)


def _latest_run_for_bug_fix(project_id: str | None) -> dict[str, Any]:
    with connect() as conn:
        if project_id:
            row = conn.execute(
                "SELECT * FROM workflow_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM workflow_runs ORDER BY created_at DESC LIMIT 1").fetchone()
    run = row_dict(row)
    if not run:
        raise WorkflowError("cannot request bug fix before a workflow project exists")
    return run


def _dev_result_content(
    run_id: str,
    step: dict[str, Any],
    executor: str,
    platform: str,
    package: dict[str, Any],
    execution: dict[str, Any],
) -> str:
    if step["id"] == "regression-testing" and execution.get("executed"):
        stdout = str(execution.get("stdout") or "").strip()
        if stdout:
            return stdout if stdout.startswith(("{", "[", "#")) else f"# {step['name']}\n\n{stdout}\n"
    payload: dict[str, Any] = {
        "executor": executor,
        "platform": platform,
        "task_package": package["path"],
        "execution": execution,
        "status": "prepared" if not execution.get("executed") else "executed",
    }
    if step["id"] == "regression-testing":
        payload["quality_gate"] = {
            "thresholds": quality_gate_config(load_config()),
            "bug_counts": {"block": 0, "critical": 0, "major": 0, "minor": 0},
            "note": "QA Agent 必须在真实执行测试后覆盖本字段；未执行时只能作为待执行任务包。",
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _evaluate_quality_gate(run_id: str, artifact_name: str) -> dict[str, Any]:
    thresholds = quality_gate_config(load_config())
    try:
        content = read_artifact(run_id, artifact_name)["content"]
    except WorkflowError:
        content = "{}"
    counts = _extract_bug_counts(content)
    failures = {level: {"count": counts[level], "threshold": thresholds[level]} for level in thresholds if counts[level] > thresholds[level]}
    return {
        "passed": not failures,
        "thresholds": thresholds,
        "bug_counts": counts,
        "failures": failures,
    }


def _extract_bug_counts(content: str) -> dict[str, int]:
    counts = {"block": 0, "critical": 0, "major": 0, "minor": 0}
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        candidates = [
            payload.get("bug_counts"),
            (payload.get("quality_gate") or {}).get("bug_counts") if isinstance(payload.get("quality_gate"), dict) else None,
            payload.get("bugs"),
        ]
        for candidate in candidates:
            if isinstance(candidate, dict):
                for key in counts:
                    value = candidate.get(key) or candidate.get(key.capitalize()) or candidate.get(key.upper())
                    if isinstance(value, int):
                        counts[key] = value
                return counts
            if isinstance(candidate, list):
                for bug in candidate:
                    if isinstance(bug, dict):
                        severity = str(bug.get("severity") or bug.get("level") or "").strip().lower()
                        if severity in counts:
                            counts[severity] += 1
                return counts
    aliases = {"block": r"Block", "critical": r"Critical", "major": r"Major", "minor": r"Minor"}
    for key, label in aliases.items():
        match = re.search(rf"{label}\s*[:：=]\s*(\d+)", content, re.IGNORECASE)
        if match:
            counts[key] = int(match.group(1))
    return counts


def _compose_prd_v2_lark_markdown(run_id: str, prd_v2_content: str) -> str:
    prd_v1 = _artifact_content_or_empty(run_id, "prd_v1")
    review = _artifact_content_or_empty(run_id, "requirement_review_report")
    rejection = _latest_prd_rejection_reason(run_id)
    return "\n\n".join(
        [
            "## 版本变化表格",
            "| 版本 | 来源 | 主要变化 |",
            "| --- | --- | --- |",
            "| v1 | 原始需求与需求录入 Gate | 初始完整 PRD |",
            f"| v2 | 多 Agent 评审汇总{'与审批拒绝理由' if rejection else ''} | 采纳合理需求修订，保留未采纳说明 |",
            "## 各 Agent 评审意见汇总",
            review or "暂无评审意见 artifact。",
            "## 相比 v1 变更点",
            _summarize_change_points(prd_v2_content),
            "## 未采纳意见",
            _extract_section_or_placeholder(prd_v2_content, ["未采纳意见", "未采纳", "不采纳"]),
            "## 最终完整 PRD 内容",
            prd_v2_content.strip() or prd_v1.strip() or "暂无 PRD 内容。",
        ]
    )


def _artifact_content_or_empty(run_id: str, name: str) -> str:
    try:
        return read_artifact(run_id, name)["content"].strip()
    except WorkflowError:
        return ""


def _summarize_change_points(content: str) -> str:
    explicit = _extract_section_or_placeholder(content, ["相比 v1 变更点", "变更点", "版本变化"], placeholder="")
    if explicit:
        return explicit
    headings = [line.strip("# ").strip() for line in content.splitlines() if line.startswith("#")]
    if headings:
        return "\n".join(f"- 补充或确认：{heading}" for heading in headings[:12])
    return "- 已根据评审意见整理 PRD v2；上游未输出显式变更点，需产品经理在下一轮补全。"


def _extract_section_or_placeholder(content: str, headings: list[str], placeholder: str = "上游 PRD v2 未显式列出，本节由 workflow 保留占位，待产品经理下一轮补全。") -> str:
    lines = content.splitlines()
    for index, line in enumerate(lines):
        normalized = line.strip().lstrip("#").strip()
        if any(heading in normalized for heading in headings):
            collected: list[str] = []
            for item in lines[index + 1 :]:
                if item.startswith("#"):
                    break
                collected.append(item)
            value = "\n".join(collected).strip()
            return value or placeholder
    return placeholder


def _append_lark_doc_manifest(
    run_id: str,
    *,
    title: str,
    source_artifact: str,
    doc_url: str | None,
    artifact_name: str,
) -> None:
    current: dict[str, Any] = {"group_name": f"{get_run(run_id)['project']['title']} 项目资料", "documents": []}
    try:
        current = json.loads(read_artifact(run_id, "lark_doc_manifest")["content"])
    except (WorkflowError, json.JSONDecodeError):
        pass
    docs = [doc for doc in current.get("documents", []) if doc.get("source_artifact") != source_artifact]
    docs.append({"title": title, "source_artifact": source_artifact, "artifact_name": artifact_name, "url": doc_url})
    current["documents"] = docs
    write_artifact(run_id, "lark_doc_manifest", json.dumps(current, ensure_ascii=False, indent=2), category="lark-doc", created_by="workflow")


def _host_escalation_payload(command: str) -> dict[str, Any]:
    return {
        "required": True,
        "reason": "lark-cli 在当前沙箱无法读取 macOS Keychain 凭证，需要由 Codex 宿主请求沙箱外执行，继续完成自动化飞书发送。",
        "command": command,
        "persistent_prefix_rule": [str(PLUGIN_ROOT / "scripts" / "deliveryflow-host-lark"), "retry-prd-approval"],
    }


def _host_lark_retry_command(run_id: str) -> str:
    script = PLUGIN_ROOT / "scripts" / "deliveryflow-host-lark"
    return f"{shlex.quote(str(script))} retry-prd-approval --workspace {shlex.quote(str(Path.cwd()))} --run-id {shlex.quote(run_id)}"


def _notify_step(run: dict[str, Any], step: dict[str, Any], phase: str, payload: dict[str, Any]) -> None:
    message = _step_notification_message(step, phase)
    emit_event(run["id"], f"workflow.step.{phase}", message, {"step_id": step["id"], "payload": payload})
    _send_lark_text(run, message, event_key=f"{run['id']}:{step['id']}:{phase}")


def _notify_gate_submitted(run: dict[str, Any], step: dict[str, Any], data: dict[str, Any]) -> None:
    if step["id"] == "prd-approval":
        state = "通过" if data.get("approved") else "拒绝"
        message = f"PRD 审批已{state}，审批人：{data.get('approver', 'unknown')}"
    else:
        message = f"{step['name']} 已提交。"
    emit_event(run["id"], "workflow.gate.submitted.notification", message, {"step_id": step["id"], "gate": data})
    _send_lark_text(run, message, event_key=f"{run['id']}:{step['id']}:submitted")


def _step_notification_message(step: dict[str, Any], phase: str) -> str:
    special = {
        ("prd-v1", "started"): "开始创建 PRD v1。",
        ("prd-v1", "completed"): "PRD v1 已创建完成。",
        ("prd-validation", "started"): "开始执行 PRD v1 确定性校验。",
        ("prd-validation", "completed"): "PRD v1 确定性校验已通过。",
        ("multi-role-review", "started"): "产品、前端、后端、QA 四个 Agent 开始评审 PRD v1。",
        ("multi-role-review", "completed"): "产品、前端、后端、QA 四个 Agent 已完成 PRD v1 评审。",
        ("review-summary", "started"): "开始汇总评审意见并生成 PRD v2。",
        ("review-summary", "completed"): "评审意见已汇总，PRD v2 已生成。",
        ("prd-approval", "started"): "开始创建 PRD v2 飞书文档并发送审批确认卡片。",
        ("prd-approval", "blocked"): "PRD 审批 Gate 已打开，等待飞书审批卡片事件。",
    }
    if (step["id"], phase) in special:
        return special[(step["id"], phase)]
    if phase == "started":
        return f"开始执行：{step['name']}。"
    if phase == "completed":
        return f"已完成：{step['name']}。"
    if phase == "blocked":
        return f"{step['name']} 已进入等待状态。"
    return f"{step['name']} 状态变更：{phase}。"


def _send_lark_text(run: dict[str, Any], message: str, *, event_key: str) -> dict[str, Any] | None:
    config = load_config()
    if not lark_enabled(config) or not bool(lark_config(config).get("send_step_notifications", True)):
        return None
    chat_id = _lark_chat_id(run)
    if not chat_id:
        return None
    result = send_text_as_bot(chat_id, message, identity=lark_identity(config), dry_run=config_lark_dry_run(config), idempotency_key=event_key)
    emit_event(
        run["id"],
        "lark.notify.sent" if result.get("ok") else "lark.notify.failed",
        message,
        {"chat_id": chat_id, "result": result},
    )
    return result


def _lark_chat_id(run: dict[str, Any]) -> str | None:
    return config_lark_chat_id(load_config(), (run.get("project") or {}).get("lark_chat_id"))


def _lark_dry_run() -> bool:
    return config_lark_dry_run(load_config())


def _extract_lark_action_value(payload: dict[str, Any]) -> dict[str, Any]:
    candidates: list[Any] = [
        payload.get("value"),
        (payload.get("action") or {}).get("value") if isinstance(payload.get("action"), dict) else None,
        ((payload.get("event") or {}).get("action") or {}).get("value") if isinstance(payload.get("event"), dict) and isinstance((payload.get("event") or {}).get("action"), dict) else None,
        ((payload.get("event") or {}).get("operator") or {}).get("value") if isinstance(payload.get("event"), dict) and isinstance((payload.get("event") or {}).get("operator"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                continue
        if isinstance(candidate, dict) and "action" in candidate:
            return candidate
    if "action" in payload:
        return payload
    raise WorkflowError("cannot find lark card action value")


def _extract_lark_approval_reason(payload: dict[str, Any], value: dict[str, Any]) -> str:
    form_values: list[Any] = [
        value.get("form_value"),
        payload.get("form_value"),
        (payload.get("action") or {}).get("form_value") if isinstance(payload.get("action"), dict) else None,
        ((payload.get("event") or {}).get("action") or {}).get("form_value") if isinstance(payload.get("event"), dict) and isinstance((payload.get("event") or {}).get("action"), dict) else None,
    ]
    for form_value in form_values:
        if isinstance(form_value, str):
            try:
                form_value = json.loads(form_value)
            except json.JSONDecodeError:
                form_value = {}
        if isinstance(form_value, dict):
            reason = form_value.get("reject_reason") or form_value.get("reason") or form_value.get("comment")
            if isinstance(reason, str) and reason.strip():
                return reason.strip()
    direct = value.get("reject_reason") or value.get("reason") or value.get("comment")
    return direct.strip() if isinstance(direct, str) else ""


def _validate_lark_approval_card_current(run_id: str, value: dict[str, Any]) -> None:
    submitted_doc_url = value.get("doc_url")
    if not isinstance(submitted_doc_url, str) or not submitted_doc_url:
        return
    try:
        latest_doc = json.loads(read_artifact(run_id, "prd_v2_lark_doc")["content"])
    except (WorkflowError, json.JSONDecodeError):
        return
    latest_doc_url = latest_doc.get("url")
    if isinstance(latest_doc_url, str) and latest_doc_url and submitted_doc_url != latest_doc_url:
        raise WorkflowError("stale PRD approval card: document URL is not the latest PRD v2 document")


def _approval_card_idempotency_key(run_id: str, prd_version: int, doc_version: int) -> str:
    run_suffix = run_id.rsplit("_", 1)[-1] or run_id[-8:]
    return f"pa-{run_suffix[:12]}-p{prd_version}-d{doc_version}"


def _latest_prd_rejection_reason(run_id: str) -> str:
    try:
        gate = json.loads(read_artifact(run_id, "prd-approval_gate")["content"])
    except (WorkflowError, json.JSONDecodeError):
        return ""
    if gate.get("approved") is False:
        return str(gate.get("comment") or gate.get("reason") or "").strip()
    return ""


def _latest_prd_approval_round(run_id: str) -> int:
    try:
        return int(read_artifact(run_id, "prd_v2_lark_doc")["version"])
    except (WorkflowError, TypeError, ValueError):
        try:
            return int(read_artifact(run_id, "prd_v2")["version"])
        except (WorkflowError, TypeError, ValueError):
            return 1


@contextmanager
def _temporary_lark_workspace(value: dict[str, Any]):
    workspace = value.get("workspace")
    if not isinstance(workspace, str) or not workspace:
        yield
        return
    path = Path(workspace).expanduser()
    if not path.is_absolute() or not path.exists() or not path.is_dir():
        raise WorkflowError(f"invalid lark card workspace: {workspace}")
    old_cwd = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old_cwd)


def _extract_lark_operator(payload: dict[str, Any]) -> str:
    candidates = [
        payload.get("operator"),
        payload.get("user"),
        (payload.get("event") or {}).get("operator") if isinstance(payload.get("event"), dict) else None,
        (payload.get("event") or {}).get("user") if isinstance(payload.get("event"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
        if isinstance(candidate, dict):
            for key in ("open_id", "user_id", "union_id", "name"):
                value = candidate.get(key)
                if isinstance(value, str) and value:
                    return value
    return "lark-card-user"


def _render_prompt(run_id: str, definition: WorkflowDefinition, step: dict[str, Any]) -> str:
    refs = []
    for name in step.get("inputs", []):
        try:
            artifact = read_artifact(run_id, name)
            refs.append(f"## Artifact: {name}\n\n{artifact['content']}")
        except WorkflowError:
            refs.append(f"## Artifact: {name}\n\n<missing>")
    instruction = definition.reference_text(step)
    agent_registry = _agent_registry_text()
    return "\n\n".join(
        [
            f"# Workflow Step: {step['id']}",
            f"Agent: {step.get('agent', 'workflow')}",
            f"Category: {step['category']}",
            "## Shared Agent Registry",
            agent_registry,
            instruction,
            "## Inputs",
            "\n\n".join(refs) if refs else "No explicit input artifacts.",
            "## Project Workspace",
            json.dumps(
                {
                    "project_root": str(Path.cwd()),
                    "config": str(Path.cwd() / WORKSPACE_CONFIG_NAME),
                    "artifact_root": str(artifact_root()),
                    "source_root": str(source_root()),
                    "frontend_source": str(source_root() / "frontend"),
                    "backend_source": str(source_root() / "backend"),
                    "quality_gate": quality_gate_config(load_config()),
                },
                ensure_ascii=False,
                indent=2,
            ),
            "## Required Outputs",
            json.dumps(step.get("outputs", []), ensure_ascii=False, indent=2),
            "## Output Contract",
            (
                "文档、方案、评审、报告类步骤必须在 stdout 输出完整 Markdown 产物正文，"
                "Workflow 会把 stdout 确定性写入当前步骤声明的 artifact。"
                "开发执行类步骤必须真实创建或修改项目文件，并在 stdout 输出 changed_files、commands_run、test_result 和 summary。"
                "不要输出“等待写入审批/需要批准后再写”之类的交互请求；如果无法写入，直接说明阻断原因并返回非成功结果。"
            ),
        ]
    )


def _agent_registry_text() -> str:
    path = PLUGIN_ROOT / "delivery_workflow" / "references" / "00-agent-registry.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "Agent registry missing: delivery_workflow/references/00-agent-registry.md"


def _ensure_agent_execution_completed(execution: dict[str, Any], step_id: str) -> None:
    if not execution.get("executed"):
        return
    if execution_needs_permission(execution):
        raise WorkflowError(f"{step_id} was blocked by interactive Agent CLI write/permission approval; configure a non-interactive permission mode or rerun manually")
    if int(execution.get("returncode") or 0) != 0:
        detail = str(execution.get("stderr") or execution.get("stdout") or "")[-800:]
        raise WorkflowError(f"{step_id} Agent CLI failed: {detail}")


def _agent_output_content(step: dict[str, Any], artifact_name: str, prompt: str, execution: dict[str, Any]) -> str:
    if execution.get("executed") and int(execution.get("returncode") or 0) == 0:
        stdout = str(execution.get("stdout") or "").strip()
        if stdout:
            return stdout if stdout.startswith("#") else f"# {step['name']} / {artifact_name}\n\n{stdout}\n"
    return _agent_output_template(step, artifact_name, prompt, execution)


def _agent_output_template(step: dict[str, Any], artifact_name: str, prompt: str, execution: dict[str, Any]) -> str:
    return (
        f"# {step['name']} / {artifact_name}\n\n"
        f"此产物由 `{step.get('agent', 'agent')}` 的任务包生成。\n\n"
        "## 执行状态\n\n"
        f"```json\n{json.dumps(execution, ensure_ascii=False, indent=2)}\n```\n\n"
        "## 任务摘要\n\n"
        f"- Step: `{step['id']}`\n"
        f"- Prompt chars: {len(prompt)}\n"
        "- Agent 负责补全业务判断；Workflow 负责状态流转、Gate 和产物归档。\n"
    )


def _validate_gate(schema: dict[str, Any], data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field, spec in schema.items():
        if spec.get("required") and field not in data:
            errors.append(f"{field} is required")
            continue
        if field in data:
            expected = spec.get("type")
            if expected == "boolean" and not isinstance(data[field], bool):
                errors.append(f"{field} must be boolean")
            if expected == "string" and not isinstance(data[field], str):
                errors.append(f"{field} must be string")
    return errors


def _next_step(step: dict[str, Any], result: dict[str, Any]) -> str | None:
    transitions = step.get("next", {})
    if not transitions:
        return None
    if result.get("approved") is True and "approved" in transitions:
        return transitions["approved"]
    if result.get("approved") is False and "rejected" in transitions:
        return transitions["rejected"]
    if result.get("can_proceed") is False and "failed" in transitions:
        return transitions["failed"]
    return transitions.get("default")


def _move_to_next(run_id: str, next_step: str | None) -> None:
    ts = now_iso()
    status_value = "completed" if next_step is None else "running"
    current_step = next_step or ""
    with connect() as conn:
        conn.execute("UPDATE workflow_runs SET current_step = ?, status = ?, updated_at = ? WHERE id = ?", (current_step, status_value, ts, run_id))
        conn.execute(
            "UPDATE projects SET status = ?, updated_at = ? WHERE id = (SELECT project_id FROM workflow_runs WHERE id = ?)",
            (status_value, ts, run_id),
        )


def _mark_step(run_id: str, step: dict[str, Any], status_value: str, payload: dict[str, Any]) -> None:
    ts = now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO step_runs(id,run_id,step_id,category,executor,status,input_json,started_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id, step_id) DO UPDATE SET
              status=excluded.status,
              output_json=CASE WHEN excluded.status IN ('completed','blocked') THEN ? ELSE step_runs.output_json END,
              completed_at=CASE WHEN excluded.status IN ('completed','blocked') THEN ? ELSE step_runs.completed_at END
            """,
            (
                new_id("step"),
                run_id,
                step["id"],
                step["category"],
                step["executor"],
                status_value,
                json.dumps(payload, ensure_ascii=False),
                ts,
                json.dumps(payload, ensure_ascii=False),
                ts,
            ),
        )


def _artifact_filename(name: str, content: str) -> str:
    suffix = ".json" if content.lstrip().startswith(("{", "[")) else ".md"
    return f"{name.replace('_', '-')}{suffix}"


def _path_segment(value: str) -> str:
    segment = re.sub(r"[^a-zA-Z0-9.-]+", "-", value.strip().lower().replace("_", "-")).strip("-")
    return segment or "workflow"


def _project_id_for_run(run_id: str) -> str:
    with connect() as conn:
        row = conn.execute("SELECT project_id FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        raise WorkflowError(f"workflow run not found: {run_id}")
    return str(row["project_id"])


def _new_project_id(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        slug = "project"
    base = new_id("proj")
    return f"{base}-{slug[:40]}"
