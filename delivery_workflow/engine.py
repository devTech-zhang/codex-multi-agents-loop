from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .config import WORKSPACE_CONFIG_NAME, code_platform_for_step, load_config, write_workspace_config
from .definitions import WorkflowDefinition, list_workflows, load_agent_profile, load_workflow
from .paths import DEFAULT_WORKFLOW_ID, artifact_root, source_root
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
) -> dict[str, Any]:
    _ensure_workspace_files()
    config = load_config()
    workflow_config = config.get("workflow") or {}
    if auto_start is None:
        auto_start = bool(workflow_config.get("auto_start", True))
    if auto_run_to_gate is None:
        auto_run_to_gate = bool(workflow_config.get("auto_run_to_idle", True))
    requirement = requirement.strip()
    if not requirement:
        raise WorkflowError("requirement cannot be empty")

    definition = load_workflow(workflow_id)
    ts = now_iso()
    project_title = title or requirement[:40]
    project_id = _new_project_id(project_title)
    run_id = new_id("run")
    start_step = definition.first_step_id
    platform = code_platform_for_step(config, fallback=platform)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO projects(id,title,requirement,platform,source,owner_id,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (project_id, project_title, requirement, platform, source, owner_id, "created", ts, ts),
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
                "workflow_id": workflow_id,
                "business_goal": business_goal or "",
                "requires_frontend": requires_frontend,
                "requires_backend": requires_backend,
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
    emit_event(run_id, "workflow.started", "Codex 交付工作流已启动。", {"project_id": project_id, "start_step": start_step})

    if auto_start:
        enqueue_step(run_id, start_step)

    auto_run_result = None
    if auto_start and auto_run_to_gate:
        auto_run_result = run_worker_until_blocked(run_id=run_id, max_jobs=int(workflow_config.get("max_auto_jobs") or 20))

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
    log_workflow("project.create.completed", "Codex 交付工作流项目已创建。", run_id=run_id, project_id=project_id, payload=result)
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
        return row_dicts(conn.execute(f"SELECT * FROM jobs{where} ORDER BY created_at DESC LIMIT ?", (*params, limit)).fetchall())


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
        _mark_run_failed(job["run_id"])
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
        item = run_worker_once(run_id=run_id)
        results.append(item)
        if not item.get("ok"):
            return {"ok": False, "stopped": "failed", "results": results}
        if item.get("idle"):
            return {"ok": True, "stopped": "idle", "results": results}
        result = item.get("result") or {}
        if result.get("step_id") in stop_steps:
            return {"ok": True, "stopped": "stop_step", "results": results}
    return {"ok": True, "stopped": "limit", "results": results}


def execute_step(run_id: str, step_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    run = get_run(run_id)
    definition = load_workflow(run["workflow_id"])
    step = definition.step(step_id)
    _mark_step(run_id, step, "running", payload)
    try:
        if step["executor"] != "agent":
            raise WorkflowError(f"unsupported slim workflow executor: {step['executor']}")
        result = _run_agent_step(run, definition, step)
        _mark_step(run_id, step, "completed", result)
        next_step = _next_step(step)
        _move_to_next(run_id, next_step)
        if next_step:
            enqueue_step(run_id, next_step)
        completed = {"step_id": step_id, "status": "completed", "result": result, "next_step": next_step}
        emit_event(run_id, "step.completed", f"step {step_id} completed", completed)
        return completed
    except Exception as exc:
        _mark_step(run_id, step, "failed", {"error": str(exc)})
        _mark_run_failed(run_id)
        raise


def watch_run(
    run_id: str,
    *,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float | None = None,
) -> dict[str, Any]:
    config = load_config()
    workflow = config.get("workflow") or {}
    timeout = float(timeout_seconds if timeout_seconds is not None else workflow.get("watch_timeout_seconds") or 300)
    interval = float(poll_interval_seconds if poll_interval_seconds is not None else workflow.get("watch_poll_interval_seconds") or 2.0)
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        current = status(run_id)
        active_jobs = [job for job in current.get("jobs", []) if job.get("status") in {"pending", "running"}]
        if current["run"]["status"] in {"completed", "failed"} or not active_jobs:
            return {"ok": True, "run_id": run_id, "status": current}
        if time.monotonic() >= deadline:
            return {"ok": False, "run_id": run_id, "reason": "timeout", "status": current}
        time.sleep(max(interval, 0.2))


def status(run_id: str) -> dict[str, Any]:
    run = get_run(run_id)
    with connect() as conn:
        steps = row_dicts(conn.execute("SELECT * FROM step_runs WHERE run_id = ? ORDER BY started_at", (run_id,)).fetchall())
        jobs = row_dicts(conn.execute("SELECT * FROM jobs WHERE run_id = ? ORDER BY created_at DESC LIMIT 20", (run_id,)).fetchall())
        artifacts = list_artifacts(run_id)
        events = row_dicts(conn.execute("SELECT * FROM events WHERE run_id = ? ORDER BY created_at DESC LIMIT 20", (run_id,)).fetchall())
    return {"run": run, "step_runs": steps, "gates": [], "jobs": jobs, "artifacts": artifacts, "events": events}


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
    log_workflow(f"event.{event_type}", message, run_id=run_id, payload=payload or {})


def inspect_workflow(workflow_id: str = DEFAULT_WORKFLOW_ID) -> dict[str, Any]:
    definition = load_workflow(workflow_id)
    return {"id": definition.workflow_id, "name": definition.name, "version": definition.version, "steps": definition.steps}


def workflows() -> list[dict[str, str]]:
    return list_workflows()


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


def _render_prompt(run_id: str, definition: WorkflowDefinition, step: dict[str, Any]) -> str:
    refs = []
    for name in step.get("inputs", []):
        try:
            artifact = read_artifact(run_id, name)
            refs.append(f"## 输入产物：{name}\n\n{artifact['content']}")
        except WorkflowError:
            refs.append(f"## 输入产物：{name}\n\n<missing>")
    profile = load_agent_profile(step.get("agent", "workflow"))
    return "\n\n".join(
        [
            f"# 工作流步骤：{step['id']}",
            f"工作流：{definition.workflow_id}",
            f"Agent: {step.get('agent', 'workflow')}",
            "## Agent 画像",
            json.dumps(profile, ensure_ascii=False, indent=2),
            "## Agent 执行说明",
            str(profile.get("instructions") or "").strip(),
            "## 输入列表",
            "\n\n".join(refs) if refs else "No explicit input artifacts.",
            "## 项目工作区",
            json.dumps(
                {
                    "project_root": str(Path.cwd()),
                    "artifact_root": str(artifact_root()),
                    "source_root": str(source_root()),
                },
                ensure_ascii=False,
                indent=2,
            ),
            "## 必须输出",
            json.dumps(step.get("outputs", []), ensure_ascii=False, indent=2),
            "## 输出契约",
            "只返回当前步骤的一份完整 Markdown 结果。不要自行推进工作流状态，不要创建额外步骤。",
        ]
    )


def _ensure_agent_execution_completed(execution: dict[str, Any], step_id: str) -> None:
    if not execution.get("executed"):
        return
    if execution_needs_permission(execution):
        raise WorkflowError(f"{step_id} was blocked by interactive Codex permission approval")
    if int(execution.get("returncode") or 0) != 0:
        detail = str(execution.get("stderr") or execution.get("stdout") or "")[-800:]
        raise WorkflowError(f"{step_id} Codex CLI failed: {detail}")


def _agent_output_content(step: dict[str, Any], artifact_name: str, prompt: str, execution: dict[str, Any]) -> str:
    if execution.get("executed") and int(execution.get("returncode") or 0) == 0:
        stdout = str(execution.get("stdout") or "").strip()
        if stdout:
            return stdout if stdout.startswith("#") else f"# {step['name']} / {artifact_name}\n\n{stdout}\n"
    return (
        f"# {step['name']} / {artifact_name}\n\n"
        f"由 `{step.get('agent', 'agent')}` 生成。\n\n"
        "## 工作流步骤\n\n"
        f"- 步骤：`{step['id']}`\n"
        f"- Agent: `{step.get('agent', 'agent')}`\n"
        f"- 提示词字符数：{len(prompt)}\n\n"
        "## 执行状态\n\n"
        f"```json\n{json.dumps(execution, ensure_ascii=False, indent=2)}\n```\n\n"
        "## 摘要\n\n"
        "当前未启用 Codex CLI 执行，因此此产物记录预备任务包和上下文交接信息。\n"
    )


def _next_step(step: dict[str, Any]) -> str | None:
    return (step.get("next") or {}).get("default")


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
              output_json=CASE WHEN excluded.status IN ('completed','failed') THEN ? ELSE step_runs.output_json END,
              completed_at=CASE WHEN excluded.status IN ('completed','failed') THEN ? ELSE step_runs.completed_at END
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


def _execution_policy(config: dict[str, Any]) -> dict[str, Any]:
    enabled = bool((config.get("code_platforms") or {}).get("enable_agent_cli"))
    return {
        "enable_agent_cli": enabled,
        "mode": "codex_cli_enabled" if enabled else "prepared_only",
        "rule": "关闭时只写入任务包和预备产物，不启动 Codex CLI。",
    }


def _ensure_workspace_files() -> None:
    if not (Path.cwd() / WORKSPACE_CONFIG_NAME).exists():
        write_workspace_config(overwrite=False)


def _latest_run_for_current_project() -> dict[str, Any]:
    _ensure_workspace_files()
    with connect() as conn:
        row = conn.execute("SELECT * FROM workflow_runs ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        raise WorkflowError("no workflow run found in current workspace")
    return dict(row)


def _artifact_filename(name: str, content: str) -> str:
    suffix = ".json" if content.lstrip().startswith(("{", "[")) else ".md"
    return f"{name.replace('_', '-')}{suffix}"


def _path_segment(value: str) -> str:
    segment = re.sub(r"[^a-zA-Z0-9.-]+", "-", value.strip().lower().replace("_", "-")).strip("-")
    return segment or "workflow"


def _new_project_id(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        slug = "project"
    base = new_id("proj")
    return f"{base}-{slug[:40]}"
