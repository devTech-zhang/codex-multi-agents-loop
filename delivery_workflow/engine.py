from __future__ import annotations

import json
import re
import shutil
import time
import zipfile
from html import escape
from pathlib import Path
from typing import Any

from .capabilities import lark_doc_capability
from .config import WORKSPACE_CONFIG_NAME, code_platform_for_step, lark_chat_id as config_lark_chat_id, lark_enabled, lark_identity, load_config, quality_gate_config, write_workspace_config
from .definitions import WorkflowDefinition, list_workflows, load_workflow
from .lark import create_doc_as_bot, extract_doc_url, send_text_as_bot
from .paths import DEFAULT_WORKFLOW_ID, PLUGIN_ROOT, artifact_root, source_root
from .platforms import build_agent_command, execution_needs_permission, maybe_run_command, select_dev_executor
from .storage import connect, new_id, now_iso, row_dict, row_dicts
from .workflow_log import log_workflow


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
    log_workflow(
        "project.create.started",
        "开始创建交付项目。",
        payload={"title": title, "source": source, "workflow_id": workflow_id, "requires_frontend": requires_frontend, "requires_backend": requires_backend},
    )
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
    log_workflow(
        "project.create.records_created",
        "项目和 workflow run 记录已创建。",
        run_id=run_id,
        project_id=project_id,
        payload={"title": project_title, "platform": platform, "start_step": start_step},
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
    _send_lark_text(get_run(run_id), f"{project_title} 项目已创建，正在进行中....", event_key=f"{run_id}:project-created")
    if auto_start:
        enqueue_step(run_id, start_step)
    auto_run_result = None
    if auto_start and auto_run_to_gate:
        auto_run_result = run_worker_until_blocked(run_id=run_id, stop_steps={"development-doc-confirmation"}, max_jobs=30)
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
    log_workflow(
        "project.create.completed",
        "交付项目创建完成。",
        run_id=run_id,
        project_id=project_id,
        payload={"auto_start": auto_start, "auto_run_to_gate": auto_run_to_gate, "result": result},
    )
    return result


def get_run(run_id: str) -> dict[str, Any]:
    with connect() as conn:
        run = row_dict(conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone())
        if not run:
            raise WorkflowError(f"workflow run not found: {run_id}")
        project = row_dict(conn.execute("SELECT * FROM projects WHERE id = ?", (run["project_id"],)).fetchone())
    run["project"] = project
    return run


def current_project_status() -> dict[str, Any]:
    run = _latest_run_for_current_project()
    return status(run["id"])


def delete_current_project(*, backup: bool = True) -> dict[str, Any]:
    run = _latest_run_for_current_project()
    project_id = run["project_id"]
    log_workflow("project.delete.started", "开始删除当前项目。", run_id=run["id"], project_id=project_id, payload={"backup": backup})
    backup_path = _backup_current_project() if backup else None
    deleted = _delete_project_records(project_id)
    log_workflow(
        "project.delete.records_deleted",
        "当前项目数据库记录已删除，即将清理项目文件。",
        project_id=project_id,
        payload={"deleted_runs": deleted["run_ids"], "backup_path": str(backup_path) if backup_path else None},
    )
    deleted_paths = _delete_current_project_files()
    result = {
        "ok": True,
        "project_id": project_id,
        "deleted_runs": deleted["run_ids"],
        "backup_path": str(backup_path) if backup_path else None,
        "deleted_paths": deleted_paths,
    }
    return result


def _delete_project_records(project_id: str) -> dict[str, Any]:
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
    return {"project": project, "run_ids": run_ids}


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
    log_workflow("bug_fix.request.started", "开始处理人工修复问题请求。", run_id=run["id"], project_id=run["project_id"], payload={"issue": issue, "source": source, "reporter": reporter})
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
    run = get_run(run_id)
    if run["status"] in {"completed", "failed"}:
        raise WorkflowError(f"cannot enqueue {step_id}: workflow run {run_id} is {run['status']}")
    definition = load_workflow(run["workflow_id"])
    step = definition.step(step_id)
    job_id = new_id("job")
    ts = now_iso()
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM jobs WHERE run_id = ? AND step_id = ? AND status IN ('pending','running')",
            (run_id, step_id),
        ).fetchone()
        if existing:
            log_workflow("job.enqueue.skipped_existing", "已有 pending/running job，跳过重复入队。", run_id=run_id, step_id=step_id, payload={"job_id": existing["id"]})
            return dict(existing)
        conn.execute(
            """
            INSERT INTO jobs(id,run_id,step_id,job_type,status,payload_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (job_id, run_id, step_id, step["executor"], "pending", json.dumps(payload or {}, ensure_ascii=False), ts, ts),
        )
    emit_event(run_id, "job.enqueued", f"step {step_id} enqueued", {"job_id": job_id})
    log_workflow("job.enqueued", "Workflow step 已入队。", run_id=run_id, step_id=step_id, payload={"job_id": job_id, "payload": payload or {}})
    return {"id": job_id, "run_id": run_id, "step_id": step_id, "status": "pending"}


def run_worker_once(run_id: str | None = None) -> dict[str, Any]:
    with connect() as conn:
        if run_id:
            row = conn.execute(
                """
                SELECT jobs.* FROM jobs
                JOIN workflow_runs ON workflow_runs.id = jobs.run_id
                WHERE jobs.status = 'pending' AND jobs.run_id = ? AND workflow_runs.status NOT IN ('completed','failed')
                ORDER BY jobs.created_at LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT jobs.* FROM jobs
                JOIN workflow_runs ON workflow_runs.id = jobs.run_id
                WHERE jobs.status = 'pending' AND workflow_runs.status NOT IN ('completed','failed')
                ORDER BY jobs.created_at LIMIT 1
                """
            ).fetchone()
        if not row:
            log_workflow("worker.idle", "Worker 没有发现 pending job。", run_id=run_id)
            return {"ok": True, "idle": True}
        job = dict(row)
        ts = now_iso()
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = ?, updated_at = ? WHERE id = ?",
            (ts, ts, job["id"]),
        )
    log_workflow("worker.job.started", "Worker 开始执行 job。", run_id=job["run_id"], step_id=job["step_id"], payload={"job_id": job["id"]})
    try:
        result = execute_step(job["run_id"], job["step_id"], json.loads(job.get("payload_json") or "{}"))
        with connect() as conn:
            ts = now_iso()
            conn.execute(
                "UPDATE jobs SET status = 'done', result_json = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False), ts, ts, job["id"]),
            )
        log_workflow("worker.job.completed", "Worker job 执行完成。", run_id=job["run_id"], step_id=job["step_id"], payload={"job_id": job["id"], "result": result})
        return {"ok": True, "job_id": job["id"], "result": result}
    except Exception as exc:
        with connect() as conn:
            ts = now_iso()
            conn.execute(
                "UPDATE jobs SET status = 'failed', error = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                (str(exc), ts, ts, job["id"]),
            )
        emit_event(job["run_id"], "job.failed", str(exc), {"job_id": job["id"], "step_id": job["step_id"]})
        log_workflow("worker.job.failed", "Worker job 执行失败。", run_id=job["run_id"], step_id=job["step_id"], payload={"job_id": job["id"], "error": str(exc)}, level="error")
        return {"ok": False, "job_id": job["id"], "error": str(exc)}


def run_worker_until_blocked(
    *,
    run_id: str | None = None,
    max_jobs: int = 50,
    stop_steps: set[str] | None = None,
) -> dict[str, Any]:
    stop_steps = stop_steps or set()
    log_workflow("worker.loop.started", "Worker 连续推进开始。", run_id=run_id, payload={"max_jobs": max_jobs, "stop_steps": sorted(stop_steps)})
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
            result = {"ok": False, "stopped": "failed", "results": results}
            log_workflow("worker.loop.completed", "Worker 连续推进因失败停止。", run_id=run_id, payload=result, level="error")
            return result
        if item.get("idle"):
            result = {"ok": True, "stopped": "idle", "results": results}
            log_workflow("worker.loop.completed", "Worker 连续推进因空闲停止。", run_id=run_id, payload=result)
            return result
        result = item.get("result") or {}
        if result.get("blocked"):
            final = {"ok": True, "stopped": "blocked", "results": results}
            log_workflow("worker.loop.completed", "Worker 连续推进到阻塞 Gate。", run_id=run_id, payload=final)
            return final
        if result.get("step_id") in stop_steps:
            break
    final = {"ok": True, "stopped": "limit_or_stop_step", "results": results}
    log_workflow("worker.loop.completed", "Worker 连续推进到达限制或 stop step。", run_id=run_id, payload=final)
    return final


def execute_step(run_id: str, step_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    run = get_run(run_id)
    definition = load_workflow(run["workflow_id"])
    step = definition.step(step_id)
    log_workflow("step.started", f"开始执行步骤：{step['name']}。", run_id=run_id, project_id=run["project_id"], step_id=step_id, payload={"executor": step.get("executor"), "payload": payload})
    try:
        _mark_step(run_id, step, "running", payload)
        _notify_step(run, step, "started", payload)
        executor = step["executor"]
        if executor == "gate":
            result = _open_gate(run_id, step)
            _mark_step(run_id, step, "blocked", result)
            _notify_step(run, step, "blocked", result)
            log_workflow("step.blocked", f"步骤进入阻塞状态：{step['name']}。", run_id=run_id, project_id=run["project_id"], step_id=step_id, payload=result)
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
        next_step = _next_step(run_id, step, result)
        _move_to_next(run_id, next_step)
        if next_step:
            enqueue_step(run_id, next_step)
        completed = {"step_id": step_id, "status": "completed", "result": result, "next_step": next_step}
        log_workflow("step.completed", f"步骤执行完成：{step['name']}。", run_id=run_id, project_id=run["project_id"], step_id=step_id, payload=completed)
        return completed
    except Exception as exc:
        failure = {"error": str(exc)}
        _mark_step(run_id, step, "failed", failure)
        _mark_run_failed(run_id)
        _notify_step(run, step, "failed", failure)
        log_workflow("step.failed", f"步骤执行失败：{step['name']}。", run_id=run_id, project_id=run["project_id"], step_id=step_id, payload={"error": str(exc)}, level="error")
        raise


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
    next_step = _next_step(run_id, step, data)
    _move_to_next(run_id, next_step)
    if next_step:
        enqueue_step(run_id, next_step)
    emit_event(run_id, "gate.submitted", f"gate {step_id} submitted", data)
    _notify_gate_submitted(run, step, data)
    result = {"ok": True, "run_id": run_id, "step_id": step_id, "next_step": next_step}
    log_workflow("gate.submitted", "Gate 已提交。", run_id=run_id, project_id=run["project_id"], step_id=step_id, payload={"data": data, "result": result})
    return result


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
        gate_events = [event for event in new_events if event["event_type"] in {"gate.submitted"}]
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
    log_workflow("artifact.written", "产物已写入。", run_id=run_id, payload={"name": name, "category": category, "created_by": created_by, "path": str(path), "version": version})
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
    try:
        run = get_run(run_id)
        project_id = run.get("project_id")
    except Exception:
        project_id = None
    log_workflow(f"event.{event_type}", message, run_id=run_id, project_id=project_id, payload=payload or {})


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
    _log_agent_execution(run, step, command.executor, execution)
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
    _log_agent_execution(run, step, executor, execution)
    _ensure_agent_execution_completed(execution, step["id"])
    _ensure_dev_execution_verified(execution, step["id"])
    if step["id"] in {"qa-system-testing", "qa-regression-testing"} and not execution.get("executed"):
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
    if step["id"] in {"qa-system-testing", "qa-regression-testing"}:
        quality_gate = _evaluate_quality_gate(run["id"], result_name)
        payload["quality_gate"] = quality_gate
        payload["can_proceed"] = quality_gate["passed"]
    return payload


def _log_agent_execution(run: dict[str, Any], step: dict[str, Any], executor: str, execution: dict[str, Any]) -> None:
    log_workflow(
        "agent.execution",
        f"{executor} 执行结果已返回。",
        run_id=run["id"],
        project_id=run["project_id"],
        step_id=step["id"],
        payload={
            "executor": executor,
            "executed": execution.get("executed"),
            "returncode": execution.get("returncode"),
            "reason": execution.get("reason"),
            "blocked": execution.get("blocked"),
            "command": execution.get("command"),
            "stdout": execution.get("stdout"),
            "stderr": execution.get("stderr"),
        },
        level="error" if execution.get("returncode") not in {None, 0} or execution.get("blocked") else "info",
    )


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
    default_title = "{project_title} 最终交付报告" if source_name == "final_delivery_report" else "{project_title} " + step["name"]
    title_template = str(step.get("title_template") or default_title)
    title = title_template.format(project_title=run["project"]["title"], project_id=run["project_id"], run_id=run_id)
    if source_name == "prd_v2":
        publish_content = _compose_prd_v2_lark_markdown(run_id, source["content"])
        publish_xml = _compose_prd_v2_lark_xml(title, run_id, source["content"])
    else:
        publish_content = _compose_generic_lark_doc_source(run_id, source_name, source["content"], step)
        publish_xml = _compose_lark_doc_xml(title, publish_content)
    result = create_doc_as_bot(title, publish_xml, identity=lark_identity(config), dry_run=False, doc_format="xml")
    if not result.get("ok"):
        raise WorkflowError(f"飞书文档创建失败: {json.dumps(result, ensure_ascii=False)[-800:]}")
    doc_url = extract_doc_url(result)
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
    write_artifact(
        run_id,
        f"{output_artifact}_markdown",
        publish_content,
        category="lark-doc",
        created_by="workflow",
    )
    write_artifact(
        run_id,
        f"{output_artifact}_xml",
        publish_xml,
        category="lark-doc",
        created_by="workflow",
    )
    _append_lark_doc_manifest(run_id, title=title, source_artifact=source_name, doc_url=doc_url, artifact_name=output_artifact)
    return {"capability": capability, "artifact": artifact, "doc_url": doc_url, "lark_cli": result}


def _notify(run_id: str, step: dict[str, Any]) -> dict[str, Any]:
    message = step.get("message", f"{step['name']} completed")
    emit_event(run_id, "notification", message, {"step_id": step["id"]})
    if step["id"] == "delivery-notification":
        run = get_run(run_id)
        final_message = _compose_final_delivery_notification(run_id, run["project"]["title"])
        _send_lark_text(run, final_message, event_key=f"{run_id}:{step['id']}:notify")
    return {"message": message}


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


def _latest_run_for_current_project() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM workflow_runs ORDER BY created_at DESC LIMIT 1").fetchone()
    run = row_dict(row)
    if not run:
        raise WorkflowError("current project has no workflow run")
    return run


def _project_owned_paths() -> list[Path]:
    return [
        Path.cwd() / WORKSPACE_CONFIG_NAME,
        Path.cwd() / ".env",
        Path.cwd() / ".env.example",
        Path.cwd() / ".gitignore",
        Path.cwd() / ".claude",
        artifact_root(),
        source_root(),
        Path.cwd() / ".delivery-workflow",
    ]


def _backup_current_project() -> Path:
    backup_path = _current_project_backup_path()
    owned_paths = [path for path in _project_owned_paths() if path.exists()]
    with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in owned_paths:
            if path.resolve() == backup_path.resolve():
                continue
            if path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file():
                        archive.write(child, child.relative_to(Path.cwd()))
            elif path.is_file():
                archive.write(path, path.relative_to(Path.cwd()))
    return backup_path


def _current_project_backup_path() -> Path:
    stem = f"delivery-workflow-backup-{time.strftime('%Y%m%d-%H%M%S')}"
    backup_path = Path.cwd() / f"{stem}.zip"
    index = 2
    while backup_path.exists():
        backup_path = Path.cwd() / f"{stem}-{index}.zip"
        index += 1
    return backup_path


def _delete_current_project_files() -> list[str]:
    deleted: list[str] = []
    for path in _project_owned_paths():
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        deleted.append(str(path))
    return deleted


def _dev_result_content(
    run_id: str,
    step: dict[str, Any],
    executor: str,
    platform: str,
    package: dict[str, Any],
    execution: dict[str, Any],
) -> str:
    if step["id"] in {"frontend-development", "backend-development", "frontend-backend-integration", "development-smoke-self-test", "qa-system-testing", "qa-regression-testing", "bug-fix"} and execution.get("executed"):
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
    if step["id"] in {"development-smoke-self-test", "qa-system-testing", "qa-regression-testing"}:
        payload["quality_gate"] = {
            "thresholds": quality_gate_config(load_config()),
            "bug_counts": {"block": 0, "critical": 0, "major": 0, "minor": 0},
            "note": "Agent 必须在真实执行测试后覆盖本字段；未执行时只能作为待执行任务包。",
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
    review = _normalize_review_report(_artifact_content_or_empty(run_id, "requirement_review_report"))
    rejection = _latest_prd_rejection_reason(run_id)
    version_table = "\n".join(
        [
            "| 版本 | 来源 | 主要变化 |",
            "| --- | --- | --- |",
            "| v1 | 原始需求与需求录入 Gate | 初始完整 PRD |",
            f"| v2 | 多 Agent 评审汇总{'与审批拒绝理由' if rejection else ''} | 采纳合理需求修订，保留未采纳说明 |",
        ]
    )
    return "\n\n".join(
        [
            "## 版本变化表格",
            version_table,
            "## 各 Agent 评审意见汇总",
            review or "评审报告未包含可展示的评审意见，请检查 requirement_review_report 产物。",
            "## 相比 v1 变更点",
            _summarize_change_points(prd_v2_content),
            "## 未采纳意见",
            _extract_section_or_placeholder(prd_v2_content, ["未采纳意见", "未采纳", "不采纳"]),
            "## 最终完整 PRD 内容",
            prd_v2_content.strip() or prd_v1.strip() or "暂无 PRD 内容。",
        ]
    )


def _compose_prd_v2_lark_xml(title: str, run_id: str, prd_v2_content: str) -> str:
    prd_v1 = _artifact_content_or_empty(run_id, "prd_v1")
    review = _normalize_review_report(_artifact_content_or_empty(run_id, "requirement_review_report"))
    rejection = _latest_prd_rejection_reason(run_id)
    return "".join(
        [
            "<doc>",
            f"<title>{_xml_text(title)}</title>",
            "<h1>版本变化表格</h1>",
            "<table>",
            "<thead><tr><th background-color=\"light-gray\">版本</th><th background-color=\"light-gray\">来源</th><th background-color=\"light-gray\">主要变化</th></tr></thead>",
            "<tbody>",
            "<tr><td>v1</td><td>原始需求与需求录入 Gate</td><td>初始完整 PRD</td></tr>",
            f"<tr><td>v2</td><td>{_xml_text('多 Agent 评审汇总与审批拒绝理由' if rejection else '多 Agent 评审汇总')}</td><td>采纳合理需求修订，保留未采纳说明</td></tr>",
            "</tbody></table>",
            "<h1>各 Agent 评审意见汇总</h1>",
            _markdown_fragment_to_lark_xml(review or "评审报告未包含可展示的评审意见，请检查 requirement_review_report 产物。"),
            "<h1>相比 v1 变更点</h1>",
            _markdown_fragment_to_lark_xml(_summarize_change_points(prd_v2_content)),
            "<h1>未采纳意见</h1>",
            _markdown_fragment_to_lark_xml(_extract_section_or_placeholder(prd_v2_content, ["未采纳意见", "未采纳", "不采纳"])),
            "<h1>最终完整 PRD 内容</h1>",
            _markdown_fragment_to_lark_xml(prd_v2_content.strip() or prd_v1.strip() or "暂无 PRD 内容。"),
            "</doc>",
        ]
    )


def _compose_lark_doc_xml(title: str, markdown_content: str) -> str:
    body = _markdown_fragment_to_lark_xml(markdown_content.strip() or "暂无内容。")
    return "\n".join(
        [
            "<doc>",
            f"<title>{_xml_text(title)}</title>",
            body,
            "</doc>",
        ]
    )


def _compose_generic_lark_doc_source(run_id: str, source_name: str, content: str, step: dict[str, Any]) -> str:
    parts = [content.strip() or "暂无内容。"]
    included_names = set(step.get("include_artifacts", []))
    for artifact_name in step.get("include_artifacts", []):
        try:
            included = read_artifact(run_id, artifact_name)["content"].strip()
        except WorkflowError:
            included = ""
        if included:
            parts.append(f"## 附：{_artifact_display_name(artifact_name)}\n\n{included}")
    if source_name in {"frontend_tech_design", "backend_tech_design"} and "tech_review_report" not in included_names:
        try:
            review = read_artifact(run_id, "tech_review_report")["content"].strip()
        except WorkflowError:
            review = ""
        if review:
            parts.append(f"## 附：技术方案评审报告\n\n{review}")
    return "\n\n".join(parts)


def _artifact_display_name(name: str) -> str:
    labels = {
        "tech_review_report": "技术方案评审报告",
        "requirement_review_report": "多角色需求评审报告",
        "test_report": "测试报告",
    }
    return labels.get(name, name)


def _lark_doc_publish_label(source_name: str, step: dict[str, Any]) -> str:
    labels = {
        "prd_v2": "PRD v2",
        "ui_design_spec": "UI 设计规范",
        "frontend_tech_design": "前端技术方案",
        "backend_tech_design": "服务端设计方案",
        "smoke_test_cases": "冒烟测试用例",
        "test_report": "测试报告",
        "final_delivery_report": "最终交付报告",
    }
    return labels.get(source_name) or step.get("name") or "项目资料"


def _normalize_review_report(content: str) -> str:
    text = content.strip()
    if not text:
        return ""
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and re.match(r"^#{1,3}\s*(多角色需求评审报告|各\s*Agent\s*评审意见汇总|评审意见汇总)\s*$", lines[0].strip(), re.IGNORECASE):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _markdown_fragment_to_lark_xml(content: str) -> str:
    blocks: list[str] = []
    list_items: list[str] = []
    table_lines: list[str] = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            blocks.append("<ul>" + "".join(f"<li>{_xml_inline(item)}</li>" for item in list_items) + "</ul>")
            list_items = []

    def flush_table() -> None:
        nonlocal table_lines
        if table_lines:
            blocks.append(_markdown_table_to_lark_xml(table_lines))
            table_lines = []

    for raw_line in content.strip().splitlines():
        line = raw_line.strip()
        if not line:
            flush_table()
            flush_list()
            continue
        if _is_markdown_table_line(line):
            flush_list()
            table_lines.append(line)
            continue
        flush_table()
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            flush_list()
            level = min(len(heading.group(1)), 3)
            blocks.append(f"<h{level}>{_xml_inline(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        if bullet:
            list_items.append(bullet.group(1))
            continue
        flush_list()
        blocks.append(f"<p>{_xml_inline(line)}</p>")
    flush_table()
    flush_list()
    return "".join(blocks) if blocks else "<p>暂无内容。</p>"


def _is_markdown_table_line(line: str) -> bool:
    return line.startswith("|") and line.endswith("|") and line.count("|") >= 2


def _markdown_table_to_lark_xml(lines: list[str]) -> str:
    rows = [_split_markdown_table_row(line) for line in lines if _split_markdown_table_row(line)]
    if not rows:
        return ""
    separator_index = 1 if len(rows) > 1 and all(re.match(r"^:?-{3,}:?$", cell.strip()) for cell in rows[1]) else -1
    header = rows[0] if separator_index == 1 else []
    body_rows = rows[2:] if separator_index == 1 else rows
    parts = ["<table>"]
    if header:
        parts.append("<thead><tr>")
        parts.extend(f"<th background-color=\"light-gray\">{_xml_inline(cell)}</th>" for cell in header)
        parts.append("</tr></thead>")
    parts.append("<tbody>")
    for row in body_rows:
        parts.append("<tr>")
        parts.extend(f"<td>{_xml_inline(cell)}</td>" for cell in row)
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _split_markdown_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _xml_inline(text: str) -> str:
    parts = re.split(r"(`[^`]+`)", str(text))
    rendered: list[str] = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) >= 2:
            rendered.append(f"<code>{_xml_text(part[1:-1])}</code>")
        else:
            rendered.append(_xml_text(part))
    return "".join(rendered)


def _xml_text(text: str) -> str:
    return escape(str(text), quote=True)


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


def _notify_step(run: dict[str, Any], step: dict[str, Any], phase: str, payload: dict[str, Any]) -> None:
    message = _step_notification_message(step, phase)
    emit_event(run["id"], f"workflow.step.{phase}", message, {"step_id": step["id"], "payload": payload})
    if step["id"] == "development-doc-confirmation" and phase == "blocked":
        docs_message = _compose_pre_development_docs_notification(run["id"])
        _send_lark_text(run, docs_message, event_key=f"{run['id']}:{step['id']}:docs-ready")


def _notify_gate_submitted(run: dict[str, Any], step: dict[str, Any], data: dict[str, Any]) -> None:
    state = "通过" if data.get("approved") is True else "拒绝" if data.get("approved") is False else "已提交"
    message = f"{step['name']} 已{state}。"
    emit_event(run["id"], "workflow.gate.submitted.notification", message, {"step_id": step["id"], "gate": data})


def _compose_pre_development_docs_notification(run_id: str) -> str:
    lines = [
        "各 Agent 已将文档创建完成，请手动确认资料，如有不符合预期的地方可直接提出来更改，以下是文档链接：",
        *_lark_doc_link_lines(
            run_id,
            [
                ("PRD", "prd_v2"),
                ("UI 设计规范", "ui_design_spec"),
                ("前端技术方案", "frontend_tech_design"),
                ("后端技术方案", "backend_tech_design"),
                ("冒烟测试用例", "smoke_test_cases"),
            ],
        ),
    ]
    return "\n".join(lines)


def _compose_final_delivery_notification(run_id: str, project_title: str) -> str:
    lines = [
        f"{project_title} 项目进度已全部完成，以下是测试报告和最终交付报告：",
        *_lark_doc_link_lines(
            run_id,
            [
                ("测试报告", "test_report"),
                ("最终交付报告", "final_delivery_report"),
            ],
        ),
    ]
    return "\n".join(lines)


def _lark_doc_link_lines(run_id: str, items: list[tuple[str, str]]) -> list[str]:
    docs = _lark_doc_manifest_documents(run_id)
    lines: list[str] = []
    for label, source_artifact in items:
        doc = docs.get(source_artifact)
        if doc and doc.get("url"):
            lines.append(f"{label}：{doc['url']}")
    return lines


def _lark_doc_manifest_documents(run_id: str) -> dict[str, dict[str, Any]]:
    try:
        manifest = json.loads(read_artifact(run_id, "lark_doc_manifest")["content"])
    except (WorkflowError, json.JSONDecodeError):
        return {}
    docs = manifest.get("documents") if isinstance(manifest, dict) else None
    if not isinstance(docs, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in docs:
        if isinstance(item, dict) and item.get("source_artifact"):
            result[str(item["source_artifact"])] = item
    return result


def _step_notification_message(step: dict[str, Any], phase: str) -> str:
    special = {
        ("prd-v1", "started"): "开始创建 PRD v1。",
        ("prd-v1", "completed"): "PRD v1 已创建完成。",
        ("prd-validation", "started"): "开始执行 PRD v1 确定性校验。",
        ("prd-validation", "completed"): "PRD v1 确定性校验已通过。",
        ("multi-role-review", "started"): "产品、前端、后端、QA 四个 Agent 开始评审 PRD v1。",
        ("multi-role-review", "completed"): "产品、前端、后端、QA 四个 Agent 已完成 PRD v1 评审。",
        ("review-summary", "started"): "开始汇总评审意见并生成最终 PRD。",
        ("review-summary", "completed"): "评审意见已汇总，最终 PRD 已生成。",
        ("smoke-test-case-design", "started"): "开始生成冒烟测试用例。",
        ("smoke-test-case-design", "completed"): "冒烟测试用例已生成。",
        ("development-doc-confirmation", "blocked"): "开发前资料确认 Gate 已打开，等待用户确认是否进入开发。",
        ("frontend-backend-integration", "started"): "开始执行：前后端联调。",
        ("development-smoke-self-test", "started"): "开始执行：前后端开发冒烟自测。",
        ("qa-system-testing", "started"): "开始执行：QA 系统测试。",
        ("qa-regression-testing", "started"): "开始执行：QA 回归测试。",
        ("qa-test-report", "started"): "开始生成 QA 测试报告。",
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
    if not lark_enabled(config):
        return None
    chat_id = _lark_chat_id(run)
    if not chat_id:
        return None
    result = send_text_as_bot(chat_id, message, identity=lark_identity(config), dry_run=False, idempotency_key=event_key)
    emit_event(
        run["id"],
        "lark.notify.sent" if result.get("ok") else "lark.notify.failed",
        message,
        {"chat_id": chat_id, "result": result},
    )
    return result


def _lark_chat_id(run: dict[str, Any]) -> str | None:
    return config_lark_chat_id(load_config(), (run.get("project") or {}).get("lark_chat_id"))


def _latest_prd_rejection_reason(run_id: str) -> str:
    try:
        gate = json.loads(read_artifact(run_id, "development-doc-confirmation_gate")["content"])
    except (WorkflowError, json.JSONDecodeError):
        return ""
    if gate.get("approved") is False:
        return str(gate.get("comment") or gate.get("reason") or "").strip()
    return ""


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


def _ensure_dev_execution_verified(execution: dict[str, Any], step_id: str) -> None:
    if step_id not in {"frontend-development", "backend-development", "frontend-backend-integration", "development-smoke-self-test", "qa-system-testing", "qa-regression-testing", "bug-fix"}:
        return
    if not execution.get("executed"):
        return
    text = "\n".join(str(execution.get(key) or "") for key in ("stdout", "stderr")).lower()
    blockers = [
        "暂未运行",
        "未运行",
        "未执行",
        "无法运行",
        "不能运行",
        "需要批准",
        "需要您批准",
        "等待批准",
        "awaiting approval",
        "requires your approval",
        "need your approval",
        "not run",
        "not executed",
        "could not run",
        "unable to run",
        "blocked on running tests",
        "waiting for approval",
    ]
    if any(blocker in text for blocker in blockers):
        raise WorkflowError(f"{step_id} did not complete required self-test/regression execution; Agent output indicates validation was not run or is waiting for approval")


def _agent_output_content(step: dict[str, Any], artifact_name: str, prompt: str, execution: dict[str, Any]) -> str:
    file_content = _agent_output_file_content(step, artifact_name)
    if file_content:
        return file_content
    if execution.get("executed") and int(execution.get("returncode") or 0) == 0:
        stdout = str(execution.get("stdout") or "").strip()
        if stdout:
            return stdout if stdout.startswith("#") else f"# {step['name']} / {artifact_name}\n\n{stdout}\n"
    return _agent_output_template(step, artifact_name, prompt, execution)


def _agent_output_file_content(step: dict[str, Any], artifact_name: str) -> str | None:
    for path in _agent_output_file_candidates(step, artifact_name):
        if not path.exists() or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if content:
            return content
    return None


def _agent_output_file_candidates(step: dict[str, Any], artifact_name: str) -> list[Path]:
    candidates: dict[str, list[Path]] = {
        "prd_v2": [
            artifact_root() / "prd_v2.md",
            artifact_root() / "product-manager" / "prd" / "v1" / "prd-v2.md",
        ],
        "ui_design_spec": [
            artifact_root() / "ui_design_spec.md",
            artifact_root() / "DESIGN.md",
            artifact_root() / "ui-designer" / "ui" / "v1" / "ui-design-spec.md",
        ],
        "frontend_tech_design": [
            artifact_root() / "frontend_tech_design.md",
            artifact_root() / "frontend-engineer" / "tech" / "v1" / "frontend-tech-design.md",
        ],
        "backend_tech_design": [
            artifact_root() / "backend_tech_design.md",
            artifact_root() / "backend-engineer" / "tech" / "v1" / "backend-tech-design.md",
        ],
        "tech_review_report": [
            artifact_root() / "tech_review_report.md",
            artifact_root() / "tech-review-board" / "tech" / "v1" / "tech-review-report.md",
        ],
        "dev_tasks": [
            artifact_root() / "dev_tasks.md",
            artifact_root() / "dev_task_breakdown.md",
            artifact_root() / "delivery-manager" / "dev" / "v1" / "dev-tasks.md",
        ],
        "smoke_test_cases": [
            artifact_root() / "smoke_test_cases.md",
            artifact_root() / "test_cases.md",
            artifact_root() / "qa-engineer" / "test" / "v1" / "test-cases.md",
        ],
    }
    return candidates.get(artifact_name, [])


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


def _next_step(run_id: str, step: dict[str, Any], result: dict[str, Any]) -> str | None:
    transitions = step.get("next", {})
    if not transitions:
        return None
    if result.get("approved") is True and "approved" in transitions:
        return transitions["approved"]
    if result.get("approved") is False and "rejected" in transitions:
        return transitions["rejected"]
    if result.get("can_proceed") is False and "failed" in transitions:
        return transitions["failed"]
    next_step = transitions.get("default")
    if not _project_requires_backend(run_id):
        if step["id"] == "frontend-tech-design":
            return "smoke-test-case-design"
        if step["id"] == "publish-frontend-tech-doc":
            return "publish-smoke-test-cases-doc"
        if step["id"] == "frontend-development":
            return "frontend-backend-integration"
    return next_step


def _project_requires_backend(run_id: str) -> bool:
    try:
        data = json.loads(read_artifact(run_id, "requirement-intake_gate")["content"])
    except (WorkflowError, json.JSONDecodeError):
        return True
    return bool(data.get("requires_backend", True))


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


def _mark_run_failed(run_id: str) -> None:
    ts = now_iso()
    with connect() as conn:
        conn.execute("UPDATE workflow_runs SET status = 'failed', updated_at = ? WHERE id = ?", (ts, run_id))
        conn.execute(
            "UPDATE projects SET status = 'failed', updated_at = ? WHERE id = (SELECT project_id FROM workflow_runs WHERE id = ?)",
            (ts, run_id),
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
              output_json=CASE WHEN excluded.status IN ('completed','blocked','failed') THEN ? ELSE step_runs.output_json END,
              completed_at=CASE WHEN excluded.status IN ('completed','blocked','failed') THEN ? ELSE step_runs.completed_at END
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
