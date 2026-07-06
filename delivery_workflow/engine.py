from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import (
    MANAGER_AGENT_ID,
    PROJECT_AGENT_IDS,
    WORKSPACE_CONFIG_NAME,
    initialize_agent_memory_files,
    load_config,
    materialize_project_agents,
    write_workspace_config,
)
from .definitions import WorkflowDefinition, list_workflows, load_agent_profile, load_workflow
from .paths import DEFAULT_WORKFLOW_ID, artifact_root, memory_root, source_root
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
    business_goal: str | None = None,
    requires_frontend: bool = True,
    requires_backend: bool = True,
) -> dict[str, Any]:
    _ensure_workspace_files()
    materialize_project_agents(overwrite=False)
    initialize_agent_memory_files(overwrite=False)
    config = load_config()
    requirement = requirement.strip()
    if not requirement:
        raise WorkflowError("requirement cannot be empty")

    definition = load_workflow(workflow_id)
    ts = now_iso()
    project_title = title or requirement[:40]
    project_id = _new_project_id(project_title)
    run_id = new_id("run")
    start_step = definition.first_step_id
    platform = "codex"
    requirement_pointer = _requirement_pointer(requirement)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO projects(id,title,requirement,platform,source,owner_id,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (project_id, project_title, requirement_pointer, platform, source, owner_id, "created", ts, ts),
        )
        conn.execute(
            """
            INSERT INTO workflow_runs(id,project_id,workflow_id,current_step,status,prd_version,review_round,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (run_id, project_id, workflow_id, start_step, "prd_draft", 0, 0, ts, ts),
        )
    _initialize_run_memories(run_id)

    write_artifact(run_id, "raw_requirement", requirement, category="raw", created_by="workflow")
    write_artifact(
        run_id,
        "project_context",
        json.dumps(
            {
                "project_id": project_id,
                "run_id": run_id,
                "platform": platform,
                "workflow_id": workflow_id,
                "business_goal": business_goal or "",
                "requires_frontend": requires_frontend,
                "requires_backend": requires_backend,
                "project_root": str(Path.cwd()),
                "artifact_root": str(artifact_root()),
                "source_root": str(source_root()),
                "agent_dir": str(Path.cwd() / ".codex" / "agents"),
                "memory_root": str(memory_root()),
                "workflow_state": "PRD V1 完成后会暂停，等待老板确认或发起多 Agent 评审。",
            },
            ensure_ascii=False,
            indent=2,
        ),
        category="context",
        created_by="workflow",
    )
    emit_event(run_id, "workflow.started", "Codex 交付工作流已启动，等待 product-manager 输出 PRD V1。", {"project_id": project_id, "start_step": start_step})

    enqueue_step(run_id, start_step)

    result = {
        "project_id": project_id,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "artifact_dir": str(artifact_root()),
        "source_dir": str(source_root()),
        "agent_dir": str(Path.cwd() / ".codex" / "agents"),
        "memory_dir": str(memory_root()),
        "config_path": str(Path.cwd() / WORKSPACE_CONFIG_NAME),
        "execution_policy": _execution_policy(config),
    }
    result["next_handoff"] = prepare_agent_handoff(run_id=run_id, agent=start_step)
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


def enqueue_dynamic_step(run_id: str, step: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    run = get_run(run_id)
    if run["status"] in {"completed", "failed"}:
        raise WorkflowError(f"cannot enqueue {step['id']}: workflow run {run_id} is {run['status']}")
    payload = {**(payload or {}), "step": step}
    job_id = new_id("job")
    ts = now_iso()
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM jobs WHERE run_id = ? AND step_id = ? AND status IN ('pending','running')",
            (run_id, step["id"]),
        ).fetchone()
        if existing:
            return dict(existing)
        conn.execute(
            """
            INSERT INTO jobs(id,run_id,step_id,job_type,status,payload_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (job_id, run_id, step["id"], step["executor"], "pending", json.dumps(payload, ensure_ascii=False), ts, ts),
        )
    emit_event(run_id, "job.enqueued", f"dynamic step {step['id']} enqueued", {"job_id": job_id, "step": step})
    return {"id": job_id, "run_id": run_id, "step_id": step["id"], "status": "pending"}


def prepare_agent_handoff(run_id: str | None = None, agent: str | None = None) -> dict[str, Any]:
    _ensure_workspace_files()
    job = _next_pending_job(run_id=run_id, agent=agent)
    if job is None:
        return {"ok": True, "idle": True}

    run = get_run(job["run_id"])
    definition = load_workflow(run["workflow_id"])
    payload = json.loads(job.get("payload_json") or "{}")
    step = _step_for_job(run, job, payload)
    prompt = _render_prompt(job["run_id"], definition, step)
    package = _agent_task_package(job, step, prompt, dispatch_mode="custom_agent_task")
    profile = load_agent_profile(step.get("agent", "workflow"))
    target_agent = step.get("agent", "agent")
    result = {
        "ok": True,
        "idle": False,
        "run_id": job["run_id"],
        "job_id": job["id"],
        "step_id": step["id"],
        "agent": target_agent,
        "mention": f"@{target_agent}",
        "agent_type": target_agent,
        "model": profile.get("model"),
        "model_reasoning_effort": profile.get("model_reasoning_effort"),
        "skills": profile.get("skills", []),
        "nickname_candidates": profile.get("nickname_candidates", []),
        "required_outputs": step.get("outputs", []),
        "workflow_phase": step.get("kind") or step["id"],
        "auto_spawn_allowed": True,
        "manager_next_action": "spawn_custom_agent",
        "boundary_rule": f"delivery-manager 应调用 {target_agent} 自定义 Agent，不能亲自领取或完成该 Agent 的 job。",
        "spawn_tool": "spawn_agent",
        "spawn_message": _spawn_message(job, step, package),
        "claim_tool": "codex_delivery_workflow_dispatch_next",
        "complete_tool": "codex_delivery_workflow_complete_agent_step",
        "task_package": package,
        "handoff_message": _handoff_message(job, step, package),
    }
    emit_event(
        job["run_id"],
        "agent.handoff_ready",
        f"已准备 {target_agent} 自定义 Agent 的任务，可由主管 spawn 或老板显式 @{target_agent}。",
        {"job_id": job["id"], "step_id": step["id"], "agent": target_agent, "task_package": package["path"]},
    )
    return result


def dispatch_next_agent_task(
    run_id: str | None = None,
    agent: str | None = None,
    invocation_mode: str = "explicit_at",
) -> dict[str, Any]:
    _ensure_workspace_files()
    if invocation_mode not in {"explicit_at", "manager_spawn"}:
        raise WorkflowError(f"unsupported invocation_mode: {invocation_mode}")
    job = _next_pending_job(run_id=run_id, agent=agent)
    if job is None:
        return {"ok": True, "idle": True}

    with connect() as conn:
        ts = now_iso()
        claimed = conn.execute(
            """
            UPDATE jobs
            SET status = 'running', attempts = attempts + 1, claimed_agent = ?, invocation_mode = ?, started_at = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (agent or job["step_id"], invocation_mode, ts, ts, job["id"]),
        )
        if claimed.rowcount != 1:
            return {"ok": True, "idle": True, "reason": "job_already_claimed", "job_id": job["id"]}

    run = get_run(job["run_id"])
    definition = load_workflow(run["workflow_id"])
    payload = json.loads(job.get("payload_json") or "{}")
    step = _step_for_job(run, job, payload)
    _mark_step(job["run_id"], step, "running", {"job_id": job["id"], **payload})
    prompt = _render_prompt(job["run_id"], definition, step)
    package = _agent_task_package(job, step, prompt, dispatch_mode=invocation_mode)
    profile = load_agent_profile(step.get("agent", "workflow"))
    model = profile.get("model")
    reasoning_effort = profile.get("model_reasoning_effort")
    result = {
        "ok": True,
        "idle": False,
        "run_id": job["run_id"],
        "job_id": job["id"],
        "step_id": step["id"],
        "agent": step.get("agent"),
        "agent_type": step.get("agent"),
        "model": model,
        "model_reasoning_effort": reasoning_effort,
        "skills": profile.get("skills", []),
        "nickname_candidates": profile.get("nickname_candidates", []),
        "required_outputs": step.get("outputs", []),
        "workflow_phase": step.get("kind") or step["id"],
        "claim_mode": "custom_project_agent",
        "invocation_mode": invocation_mode,
        "project_agent_hint": f"你必须是 {step.get('agent')} 类型的项目 Agent；无论来自显式 @ 还是主管 spawn，都按任务包执行并用 MCP 工具回填结果。",
        "task_package": package,
        "task_message": prompt,
        "complete_tool": "codex_delivery_workflow_complete_agent_step",
    }
    emit_event(
        job["run_id"],
        "agent.job_claimed",
        f"{step.get('agent')} 已通过 {invocation_mode} 领取任务：{step['id']}",
        {"job_id": job["id"], "step_id": step["id"], "agent": step.get("agent"), "invocation_mode": invocation_mode},
    )
    return result


def _next_pending_job(run_id: str | None = None, agent: str | None = None) -> dict[str, Any] | None:
    with connect() as conn:
        clauses = ["jobs.status = 'pending'", "workflow_runs.status NOT IN ('completed','failed')"]
        params: list[Any] = []
        if run_id:
            clauses.append("jobs.run_id = ?")
            params.append(run_id)
        rows = conn.execute(
            f"""
            SELECT jobs.* FROM jobs
            JOIN workflow_runs ON workflow_runs.id = jobs.run_id
            WHERE {" AND ".join(clauses)}
            ORDER BY jobs.created_at
            """,
            params,
        ).fetchall()
    row = _select_dispatch_row(rows, agent)
    return dict(row) if row is not None else None


def _select_dispatch_row(rows: list[Any], agent: str | None) -> Any | None:
    if not rows:
        return None
    if not agent:
        return rows[0]
    for row in rows:
        job = dict(row)
        payload = json.loads(job.get("payload_json") or "{}")
        step = payload.get("step")
        if isinstance(step, dict):
            if step.get("agent") == agent:
                return row
            continue
        if job.get("step_id") == agent:
            return row
    return None


def _agent_task_package(job: dict[str, Any], step: dict[str, Any], prompt: str, *, dispatch_mode: str) -> dict[str, Any]:
    existing = _latest_agent_task_package(job["run_id"], f"{step['id']}_agent_task", job["id"])
    if existing:
        return existing
    return write_artifact(
        job["run_id"],
        f"{step['id']}_agent_task",
        prompt,
        category="agent-task",
        created_by=step.get("agent", "agent"),
        metadata={"agent": step.get("agent"), "job_id": job["id"], "step_id": step["id"], "dispatch_mode": dispatch_mode},
    )


def _latest_agent_task_package(run_id: str, name: str, job_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM artifacts WHERE run_id = ? AND name = ? ORDER BY version DESC",
            (run_id, name),
        ).fetchall()
    for row in rows:
        item = dict(row)
        metadata = json.loads(item.get("metadata_json") or "{}")
        if metadata.get("job_id") == job_id:
            return {"id": item["id"], "run_id": item["run_id"], "name": item["name"], "path": item["path"], "version": item["version"]}
    return None


def _handoff_message(job: dict[str, Any], step: dict[str, Any], package: dict[str, Any]) -> str:
    agent = step.get("agent", "agent")
    mention = f"@{agent}"
    outputs = ", ".join(step.get("outputs", [])) or "当前步骤声明的输出"
    return "\n".join(
        [
            f"下一步由 `{agent}` 自定义 Agent 执行：",
            "",
            f"{mention} 请领取并完成 Codex 交付工作流任务。",
            f"- run_id: `{job['run_id']}`",
            f"- job_id: `{job['id']}`",
            f"- step_id: `{step['id']}`",
            f"- 任务包: `{package['path']}`",
            f"- 必须输出: {outputs}",
            "",
            "调用策略：",
            f"- 老板已经显式使用 `{mention}` 时，由该原生项目 Agent 直接领取，主管不要重复 spawn。",
            f"- 老板说“继续下一步”“你继续”或没有显式点名员工时，主管调用 `spawn_agent(agent_type=\"{agent}\", message=<spawn_message>)`。",
            "- 调用时不要传 model 或 reasoning_effort，让 Codex 使用项目 Agent TOML 的模型、思考等级和昵称配置。",
            "- 主管不能亲自领取员工 job，也不能代写员工产物。",
            "",
            "执行要求：",
            f"1. 先调用 `codex_delivery_workflow_dispatch_next`，参数传 `run_id=\"{job['run_id']}\"`、`agent=\"{agent}\"`；显式 @ 使用 `invocation_mode=\"explicit_at\"`，主管 spawn 使用 `invocation_mode=\"manager_spawn\"`。",
            "2. 按返回的 `task_message` 或任务包文件执行，不要改其他 Agent 的状态。",
            "3. 完成后调用 `codex_delivery_workflow_complete_agent_step` 回填 Markdown 结果。",
            "4. 回填后由 `@delivery-manager` 读取账本并归纳状态、产物和下一步。",
        ]
    )


def _spawn_message(job: dict[str, Any], step: dict[str, Any], package: dict[str, Any]) -> str:
    agent = step.get("agent", "agent")
    return "\n".join(
        [
            f"你是当前项目的 {agent} Agent，请领取并完成自己的交付工作流任务。",
            f"run_id: {job['run_id']}",
            f"job_id: {job['id']}",
            f"任务包: {package['path']}",
            f"先调用 codex_delivery_workflow_dispatch_next，传 agent=\"{agent}\"、invocation_mode=\"manager_spawn\"。",
            "完成后调用 codex_delivery_workflow_complete_agent_step 回填结果，并更新同名 Agent 的共享记忆。",
        ]
    )


def manager_summary(run_id: str | None = None) -> dict[str, Any]:
    current = status(run_id) if run_id else current_project_status()
    run = current["run"]
    completed_steps = [step["step_id"] for step in current["step_runs"] if step.get("status") == "completed"]
    running_jobs = [job["step_id"] for job in current["jobs"] if job.get("status") == "running"]
    pending_jobs = [job["step_id"] for job in current["jobs"] if job.get("status") == "pending"]
    artifacts = [
        {
            "name": artifact["name"],
            "category": artifact["category"],
            "created_by": artifact["created_by"],
            "version": artifact["version"],
            "path": artifact["path"],
        }
        for artifact in current["artifacts"]
    ]
    last_event = current["events"][0] if current["events"] else None
    next_action = _manager_next_action(run, pending_jobs, running_jobs)
    return {
        "manager_agent": MANAGER_AGENT_ID,
        "run_id": run["id"],
        "run_status": run["status"],
        "current_step": run["current_step"],
        "prd_version": run.get("prd_version", 0),
        "review_round": run.get("review_round", 0),
        "completed_steps": completed_steps,
        "running_jobs": running_jobs,
        "pending_jobs": pending_jobs,
        "artifacts": artifacts,
        "reviews": current.get("reviews", []),
        "agent_memories": current.get("agent_memory", []),
        "next_action": next_action,
        "last_update": last_event["message"] if last_event else "",
        "last_event": last_event,
    }


def complete_agent_step(
    *,
    run_id: str,
    job_id: str,
    output: str | dict[str, Any],
    spawned_agent_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ? AND run_id = ?", (job_id, run_id)).fetchone()
    if not row:
        raise WorkflowError(f"job not found: {job_id}")
    job = dict(row)
    if job["status"] != "running":
        raise WorkflowError(f"job {job_id} must be running before completion, current status: {job['status']}")

    run = get_run(run_id)
    definition = load_workflow(run["workflow_id"])
    payload = json.loads(job.get("payload_json") or "{}")
    step = _step_for_job(run, job, payload)
    output_map = _normalize_agent_output(output, step.get("outputs", []))
    outputs = [
        write_artifact(
            run_id,
            artifact_name,
            content,
            category=step.get("artifact_category", "agent"),
            created_by=step.get("agent", "agent"),
            metadata={"agent": step.get("agent"), "job_id": job_id, "spawned_agent_id": spawned_agent_id, **(metadata or {})},
        )
        for artifact_name, content in output_map.items()
    ]
    result = {
        "agent": step.get("agent"),
        "spawned_agent_id": spawned_agent_id,
        "invocation_mode": job.get("invocation_mode") or "explicit_at",
        "outputs": outputs,
        "metadata": metadata or {},
    }
    _mark_step(run_id, step, "completed", result)
    _update_agent_memory(run_id, step.get("agent", "agent"), output_map, outputs)
    transition = _complete_step_transition(run_id, step, outputs, result, job_id=job_id)
    next_step = transition.get("next_step")
    completed = {"step_id": step["id"], "status": "completed", "result": result, "outputs": outputs, "next_step": next_step}
    with connect() as conn:
        ts = now_iso()
        conn.execute(
            "UPDATE jobs SET status = 'done', result_json = ?, finished_at = ?, updated_at = ? WHERE id = ?",
            (json.dumps(completed, ensure_ascii=False), ts, ts, job_id),
        )
    event_type = transition.get("event_type") or "step.completed"
    event_message = transition.get("message") or f"step {step['id']} completed by custom project agent"
    emit_event(run_id, event_type, event_message, completed)
    return {"ok": True, **completed, "transition": transition}


def status(run_id: str) -> dict[str, Any]:
    run = get_run(run_id)
    with connect() as conn:
        steps = row_dicts(conn.execute("SELECT * FROM step_runs WHERE run_id = ? ORDER BY started_at", (run_id,)).fetchall())
        jobs = row_dicts(conn.execute("SELECT * FROM jobs WHERE run_id = ? ORDER BY created_at DESC LIMIT 20", (run_id,)).fetchall())
        artifacts = list_artifacts(run_id)
        events = row_dicts(conn.execute("SELECT * FROM events WHERE run_id = ? ORDER BY created_at DESC LIMIT 20", (run_id,)).fetchall())
        reviews = row_dicts(conn.execute("SELECT * FROM workflow_reviews WHERE run_id = ? ORDER BY created_at", (run_id,)).fetchall())
        memories = row_dicts(conn.execute("SELECT * FROM agent_memory WHERE run_id = ? ORDER BY agent_name", (run_id,)).fetchall())
    return {
        "run": run,
        "step_runs": steps,
        "gates": [],
        "jobs": jobs,
        "artifacts": artifacts,
        "reviews": reviews,
        "agent_memory": memories,
        "events": events,
    }


def confirm_prd(run_id: str | None = None) -> dict[str, Any]:
    run = get_run(run_id) if run_id else _latest_run_for_current_project()
    latest_prd = read_artifact(run["id"], "prd")
    if run["status"] not in {"waiting_owner_review", "prd_draft", "prd_revision"}:
        raise WorkflowError(f"cannot confirm PRD while run status is {run['status']}")
    _move_to_next(run["id"], "ui-designer", status_value="running")
    enqueue_step(run["id"], "ui-designer", {"confirmed_prd_version": latest_prd["version"]})
    emit_event(
        run["id"],
        "prd.confirmed",
        f"老板已确认 PRD V{latest_prd['version']}，开始进入 UI/前后端/QA 交付链路。",
        {"prd_version": latest_prd["version"], "prd_artifact": latest_prd["id"]},
    )
    return {"ok": True, "run_id": run["id"], "prd_version": latest_prd["version"], "next_step": "ui-designer"}


def request_prd_review(run_id: str | None = None, note: str | None = None) -> dict[str, Any]:
    run = get_run(run_id) if run_id else _latest_run_for_current_project()
    latest_prd = read_artifact(run["id"], "prd")
    round_no = int(run.get("review_round") or 0) + 1
    target_version = int(latest_prd["version"])
    review_steps = []
    for reviewer in ["ui-designer", "frontend-impl", "backend-impl", "qa-tester"]:
        step = _prd_review_step(reviewer=reviewer, prd_version=target_version, round_no=round_no)
        review_steps.append(enqueue_dynamic_step(run["id"], step, {"owner_note": note or "", "target_prd_version": target_version, "review_round": round_no}))
    _set_run_state(run["id"], status_value="reviewing", current_step=f"prd-v{target_version}-review-r{round_no}", review_round=round_no)
    emit_event(
        run["id"],
        "prd.review_requested",
        f"老板要求多 Agent 评审 PRD V{target_version}，已入队第 {round_no} 轮评审，等待对应 @ Agent 领取。",
        {"prd_version": target_version, "review_round": round_no, "note": note or ""},
    )
    return {"ok": True, "run_id": run["id"], "prd_version": target_version, "review_round": round_no, "review_jobs": review_steps}


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
    path = artifact_root() / _path_segment(run_id) / _path_segment(created_by) / category / f"v{version}" / _artifact_filename(name, content)
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


def _render_prompt(run_id: str, definition: WorkflowDefinition, step: dict[str, Any]) -> str:
    refs = []
    for name in step.get("inputs", []):
        try:
            artifact = read_artifact(run_id, name)
            refs.append(f"## 输入产物：{name}\n\n{artifact['content']}")
        except WorkflowError:
            refs.append(f"## 输入产物：{name}\n\n<missing>")
    profile = load_agent_profile(step.get("agent", "workflow"))
    agent = step.get("agent", "agent")
    memory_path = memory_root() / f"{agent}.md"
    memory_text = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    run = get_run(run_id)
    return "\n\n".join(
        [
            f"# 工作流步骤：{step['id']}",
            f"工作流：{definition.workflow_id}",
            f"Agent: {agent}",
            "## 执行边界",
            _agent_execution_boundary(agent),
            "## 当前运行状态",
            json.dumps(
                {
                    "run_id": run_id,
                    "run_status": run["status"],
                    "current_step": run["current_step"],
                    "prd_version": run.get("prd_version", 0),
                    "review_round": run.get("review_round", 0),
                    "step_kind": step.get("kind") or step["id"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            "## Agent 画像",
            json.dumps(profile, ensure_ascii=False, indent=2),
            "## Agent 执行说明",
            str(profile.get("instructions") or "").strip(),
            "## Agent 记忆空间",
            json.dumps({"memory_path": str(memory_path)}, ensure_ascii=False, indent=2),
            memory_text[-4000:] if memory_text else "暂无记忆。",
            "## 输入列表",
            "\n\n".join(refs) if refs else "No explicit input artifacts.",
            "## 项目工作区",
            json.dumps(
                {
                    "project_root": str(Path.cwd()),
                    "artifact_root": str(artifact_root()),
                    "source_root": str(source_root()),
                    "memory_root": str(memory_root()),
                },
                ensure_ascii=False,
                indent=2,
            ),
            "## 必须输出",
            json.dumps(step.get("outputs", []), ensure_ascii=False, indent=2),
            "## 输出契约",
            _output_contract(step),
        ]
    )


def _output_contract(step: dict[str, Any]) -> str:
    if step["id"] == "product-manager":
        return "输出一份完整中文 PRD V1 Markdown。完成后不要继续 UI/开发，等待老板确认或多 Agent 评审。"
    if step.get("kind") == "prd_review":
        return "输出一份中文 PRD 评审意见 Markdown，只评价最新 PRD 的问题、风险、遗漏和修改建议，不直接改 PRD。"
    if step.get("kind") == "prd_revision":
        return "整合各 Agent 评审意见，输出下一版完整中文 PRD Markdown。必须说明相对上一版的变更点。完成后等待老板再次确认。"
    return "只返回当前步骤的一份完整 Markdown 结果。不要自行推进工作流状态，不要创建额外步骤。"


def _agent_execution_boundary(agent: str) -> str:
    return (
        f"只有 {agent} 类型的 Agent 才允许执行本任务。它可以来自老板显式 @{agent}，也可以由主管调用 "
        f"`spawn_agent(agent_type=\"{agent}\", message=<任务包>)` 创建。\n"
        f"如果你是 delivery-manager 或普通当前会话，不要亲自领取或代替 {agent} 输出产物；应 spawn 对应自定义 Agent。\n"
        "同名 Agent 无论通过哪种方式创建，都必须读取并更新同一份角色记忆。"
    )


def _normalize_agent_output(output: str | dict[str, Any], artifact_names: list[str]) -> dict[str, str]:
    if not artifact_names:
        return {}
    if isinstance(output, dict):
        values: dict[str, str] = {}
        for name in artifact_names:
            raw = output.get(name)
            if raw is None:
                raw = output.get("content") if len(artifact_names) == 1 else None
            if raw is None:
                raise WorkflowError(f"missing output artifact content: {name}")
            values[name] = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, indent=2)
        return values
    if len(artifact_names) != 1:
        raise WorkflowError("string output can only be used when the step declares exactly one output artifact")
    return {artifact_names[0]: output}


def _step_for_job(run: dict[str, Any], job: dict[str, Any], payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return load_workflow(run["workflow_id"]).step(job["step_id"])
    except KeyError:
        payload = payload if payload is not None else json.loads(job.get("payload_json") or "{}")
        step = payload.get("step")
        if not isinstance(step, dict):
            raise WorkflowError(f"dynamic job missing step definition: {job['step_id']}")
        return step


def _prd_review_step(*, reviewer: str, prd_version: int, round_no: int) -> dict[str, Any]:
    return {
        "id": f"prd-v{prd_version}-review-r{round_no}-{reviewer}",
        "name": f"PRD V{prd_version} 第 {round_no} 轮 {reviewer} 评审",
        "category": "review",
        "executor": "agent",
        "agent": reviewer,
        "artifact_category": "review",
        "inputs": ["prd"],
        "outputs": [f"prd_review_{reviewer}_v{prd_version}_r{round_no}"],
        "kind": "prd_review",
        "target_prd_version": prd_version,
        "review_round": round_no,
    }


def _prd_revision_step(*, prd_version: int, round_no: int) -> dict[str, Any]:
    review_inputs = [f"prd_review_{reviewer}_v{prd_version}_r{round_no}" for reviewer in ["ui-designer", "frontend-impl", "backend-impl", "qa-tester"]]
    return {
        "id": f"prd-v{prd_version}-revision-r{round_no}",
        "name": f"PRD V{prd_version} 第 {round_no} 轮评审意见整合",
        "category": "product",
        "executor": "agent",
        "agent": "product-manager",
        "artifact_category": "product",
        "inputs": ["prd", *review_inputs],
        "outputs": ["prd"],
        "kind": "prd_revision",
        "target_prd_version": prd_version,
        "review_round": round_no,
    }


def _complete_step_transition(run_id: str, step: dict[str, Any], outputs: list[dict[str, Any]], result: dict[str, Any], *, job_id: str) -> dict[str, Any]:
    kind = step.get("kind")
    if step["id"] == "product-manager" or kind == "prd_revision":
        prd_artifact = next((item for item in outputs if item["name"] == "prd"), None)
        version = int(prd_artifact["version"]) if prd_artifact else _latest_artifact_version(run_id, "prd")
        _set_run_state(run_id, status_value="waiting_owner_review", current_step="owner-review", prd_version=version)
        return {
            "next_step": None,
            "event_type": "prd.ready_for_owner_review",
            "message": f"{step.get('agent', 'product-manager')} 已完成 PRD V{version}，等待老板确认或发起多 Agent 评审。",
            "prd_version": version,
        }
    if kind == "prd_review":
        _record_review(run_id, step, outputs, result)
        if _has_pending_review_jobs(run_id, int(step["review_round"]), excluding_job_id=job_id):
            _set_run_state(run_id, status_value="reviewing", current_step=f"prd-v{step['target_prd_version']}-review-r{step['review_round']}")
            return {
                "next_step": None,
                "event_type": "prd.review_agent_completed",
                "message": f"{step.get('agent')} 已完成 PRD V{step['target_prd_version']} 评审，等待其他 Agent。",
            }
        revision = _prd_revision_step(prd_version=int(step["target_prd_version"]), round_no=int(step["review_round"]))
        enqueue_dynamic_step(run_id, revision, {"target_prd_version": step["target_prd_version"], "review_round": step["review_round"]})
        _set_run_state(run_id, status_value="prd_revision", current_step=revision["id"])
        return {
            "next_step": revision["id"],
            "event_type": "prd.review_completed",
            "message": f"PRD V{step['target_prd_version']} 第 {step['review_round']} 轮评审已完成，已入队 product-manager 整合下一版 PRD，等待 @product-manager 领取。",
        }
    next_step = _next_step(step)
    _move_to_next(run_id, next_step)
    if next_step:
        enqueue_step(run_id, next_step)
    return {"next_step": next_step}


def _latest_artifact_version(run_id: str, name: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT MAX(version) AS version FROM artifacts WHERE run_id = ? AND name = ?", (run_id, name)).fetchone()
    return int(row["version"] or 0)


def _has_pending_review_jobs(run_id: str, round_no: int, *, excluding_job_id: str | None = None) -> bool:
    pattern = f"prd-%-review-r{round_no}-%"
    exclude_sql = "AND id != ?" if excluding_job_id else ""
    params: list[Any] = [run_id, pattern]
    if excluding_job_id:
        params.append(excluding_job_id)
    with connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS count FROM jobs WHERE run_id = ? AND step_id LIKE ? AND status IN ('pending','running') {exclude_sql}",
            params,
        ).fetchone()
    return int(row["count"] or 0) > 0


def _record_review(run_id: str, step: dict[str, Any], outputs: list[dict[str, Any]], result: dict[str, Any]) -> None:
    output = outputs[0] if outputs else {}
    summary = ""
    if output.get("path"):
        text = Path(output["path"]).read_text(encoding="utf-8")
        summary = text.strip().splitlines()[0][:200] if text.strip() else ""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO workflow_reviews(id,run_id,target_artifact_id,round,reviewer_agent,opinion_path,summary,severity,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                new_id("rev"),
                run_id,
                result.get("metadata", {}).get("target_artifact_id"),
                int(step.get("review_round") or 0),
                step.get("agent", "agent"),
                output.get("path"),
                summary,
                result.get("metadata", {}).get("severity", ""),
                now_iso(),
            ),
        )


def _initialize_run_memories(run_id: str) -> None:
    root = memory_root()
    root.mkdir(parents=True, exist_ok=True)
    ts = now_iso()
    with connect() as conn:
        for agent_id in PROJECT_AGENT_IDS:
            path = root / f"{agent_id}.md"
            conn.execute(
                """
                INSERT INTO agent_memory(id,run_id,agent_name,memory_path,last_summary,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(run_id, agent_name) DO UPDATE SET memory_path=excluded.memory_path, updated_at=excluded.updated_at
                """,
                (new_id("mem"), run_id, agent_id, str(path), "", ts),
            )


def _update_agent_memory(run_id: str, agent: str, output_map: dict[str, str], outputs: list[dict[str, Any]]) -> None:
    root = memory_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{agent}.md"
    if not path.exists():
        path.write_text(f"# {agent} 记忆\n\n", encoding="utf-8")
    artifact_lines = "\n".join(f"- `{item['name']}` v{item['version']}: {item['path']}" for item in outputs)
    summary_source = "\n\n".join(output_map.values()).strip()
    summary = summary_source[:500].replace("\n", " ")
    entry = (
        f"\n## {now_iso()} / {run_id}\n\n"
        f"产物：\n{artifact_lines or '- 无'}\n\n"
        f"摘要：{summary or '无'}\n"
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_memory(id,run_id,agent_name,memory_path,last_summary,updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(run_id, agent_name) DO UPDATE SET
              memory_path=excluded.memory_path,
              last_summary=excluded.last_summary,
              updated_at=excluded.updated_at
            """,
            (new_id("mem"), run_id, agent, str(path), summary, now_iso()),
        )


def _manager_next_action(run: dict[str, Any], pending_jobs: list[str], running_jobs: list[str]) -> str:
    if running_jobs:
        return f"等待运行中的 Agent 完成：{', '.join(running_jobs)}"
    if pending_jobs:
        return f"准备调度待办 Agent：{', '.join(pending_jobs)}。老板未显式 @ 时，由主管 spawn 对应自定义 Agent。"
    if run["status"] == "waiting_owner_review":
        version = run.get("prd_version", 0)
        return f"请老板确认 PRD V{version}，或要求多 Agent 评审后输出下一版。"
    if run["status"] == "completed":
        return "交付流程已完成，可以读取最终 QA 报告和主管总结。"
    return "请根据当前状态决定创建任务、确认 PRD、发起评审或调度自定义 Agent。"


def _next_step(step: dict[str, Any]) -> str | None:
    return (step.get("next") or {}).get("default")


def _move_to_next(run_id: str, next_step: str | None, *, status_value: str | None = None) -> None:
    ts = now_iso()
    status_value = status_value or ("completed" if next_step is None else "running")
    current_step = next_step or ""
    with connect() as conn:
        conn.execute("UPDATE workflow_runs SET current_step = ?, status = ?, updated_at = ? WHERE id = ?", (current_step, status_value, ts, run_id))
        conn.execute(
            "UPDATE projects SET status = ?, updated_at = ? WHERE id = (SELECT project_id FROM workflow_runs WHERE id = ?)",
            (status_value, ts, run_id),
        )


def _set_run_state(
    run_id: str,
    *,
    status_value: str,
    current_step: str,
    prd_version: int | None = None,
    review_round: int | None = None,
) -> None:
    ts = now_iso()
    assignments = ["current_step = ?", "status = ?", "updated_at = ?"]
    params: list[Any] = [current_step, status_value, ts]
    if prd_version is not None:
        assignments.append("prd_version = ?")
        params.append(prd_version)
    if review_round is not None:
        assignments.append("review_round = ?")
        params.append(review_round)
    params.append(run_id)
    with connect() as conn:
        conn.execute(f"UPDATE workflow_runs SET {', '.join(assignments)} WHERE id = ?", params)
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
    return {
        "mode": "custom_agent_spawn",
        "rule": "老板显式 @ 时由原生项目 Agent 执行；否则主管按语义和 pending job 主动 spawn 同名自定义 Agent，MCP 负责状态、记忆和产物。",
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


def _requirement_pointer(requirement: str) -> str:
    return f"完整原始需求已归档到 raw_requirement 产物；字符数：{len(requirement)}。"
