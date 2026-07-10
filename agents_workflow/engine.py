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
    workspace_config_path,
    workspace_root,
    write_workspace_config,
)
from .definitions import WorkflowDefinition, list_workflows, load_agent_profile, load_workflow
from .paths import DEFAULT_WORKFLOW_ID, artifact_root, data_home, global_memory_root, memory_root, scratch_root
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
    mode: str = "auto",
    requested_agents: list[str] | None = None,
    targets: list[dict[str, Any]] | None = None,
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
    route_plan = _build_route_plan(requirement, mode=mode, requested_agents=requested_agents, targets=targets)
    start_step = definition.first_step_id if route_plan["mode"] == "full_workflow" else route_plan["current_step"]
    platform = "codex"
    requirement_pointer = _requirement_pointer(requirement)
    initial_status = "prd_draft" if route_plan["mode"] == "full_workflow" else "running"

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
            (run_id, project_id, workflow_id, start_step, initial_status, 0, 0, ts, ts),
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
                "requires_development": requires_frontend,
                "route_plan": route_plan,
                "loop": _route_plan_loop_summary(route_plan),
                "task_mode": route_plan["mode"],
                "completion_criteria": route_plan["completion"],
                "exit_conditions": route_plan["exit_conditions"],
                "targets": route_plan["targets"],
                "project_root": str(workspace_root()),
                "workflow_home": str(data_home()),
                "artifact_root": str(artifact_root()),
                "scratch_root": str(scratch_root()),
                "agent_dir": str(workspace_root() / ".codex" / "agents"),
                "memory_root": str(memory_root()),
                "workflow_state": route_plan["workflow_state"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        category="context",
        created_by="workflow",
    )
    write_artifact(run_id, "route_plan", json.dumps(route_plan, ensure_ascii=False, indent=2), category="context", created_by="workflow")
    emit_event(run_id, "workflow.started", route_plan["start_message"], {"project_id": project_id, "start_step": start_step, "mode": route_plan["mode"]})

    if route_plan["mode"] == "full_workflow":
        enqueue_step(run_id, start_step, {"iteration": 1})
    else:
        for step in route_plan["steps"]:
            enqueue_dynamic_step(run_id, step, {"route_plan": route_plan, "completion": route_plan["completion"], "iteration": 1})

    result = {
        "project_id": project_id,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "artifact_dir": str(artifact_root()),
        "project_dir": str(workspace_root()),
        "workflow_home": str(data_home()),
        "scratch_dir": str(scratch_root()),
        "agent_dir": str(workspace_root() / ".codex" / "agents"),
        "memory_dir": str(memory_root()),
        "config_path": str(workspace_config_path()),
        "execution_policy": _execution_policy(config),
        "task_type": route_plan["mode"],
        "route_plan": route_plan,
        "loop": _route_plan_loop_summary(route_plan),
        "completion_criteria": route_plan["completion"],
        "exit_conditions": route_plan["exit_conditions"],
    }
    result["next_handoff"] = prepare_agent_handoff(run_id=run_id)
    log_workflow("project.create.completed", "Codex 多 Agent Loop 项目已创建。", run_id=run_id, project_id=project_id, payload=result)
    return result


def create_agent_task(
    *,
    requirement: str,
    agent: str,
    title: str | None = None,
    targets: list[dict[str, Any]] | None = None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    if agent not in PROJECT_AGENT_IDS or agent == MANAGER_AGENT_ID:
        raise WorkflowError(f"unsupported direct agent task target: {agent}")
    return create_project(
        requirement=requirement,
        title=title,
        owner_id=owner_id,
        source="direct_agent",
        mode="single_agent_task",
        requested_agents=[agent],
        targets=targets,
    )


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
    step = {**step, "iteration": int(payload.get("iteration") or step.get("iteration") or 1)}
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
        "boundary_rule": f"project-manager 应调用 {target_agent} 自定义 Agent，不能亲自领取或完成该 Agent 的 job。",
        "spawn_tool": "spawn_agent",
        "spawn_message": _spawn_message(job, step, package),
        "claim_tool": "codex_multi_agents_loop_dispatch_next",
        "complete_tool": "codex_multi_agents_loop_complete_agent_step",
        "task_package": package,
        "handoff_message": _handoff_message(job, step, package),
    }
    emit_event(
        job["run_id"],
        "agent.handoff_ready",
        f"已准备 {target_agent} 自定义 Agent 的任务，可由主管 spawn 或用户显式 @{target_agent}。",
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
    step = {**step, "iteration": int(payload.get("iteration") or step.get("iteration") or 1)}
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
        "complete_tool": "codex_multi_agents_loop_complete_agent_step",
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
        clauses = ["jobs.status = 'pending'", "workflow_runs.status NOT IN ('completed','failed','blocked')"]
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
            f"{mention} 请领取并完成 Codex 多 Agent Loop 任务。",
            f"- run_id: `{job['run_id']}`",
            f"- job_id: `{job['id']}`",
            f"- step_id: `{step['id']}`",
            f"- 任务包: `{package['path']}`",
            f"- 必须输出: {outputs}",
            "",
            "调用策略：",
            f"- 用户已经显式使用 `{mention}` 时，由该原生项目 Agent 直接领取，主管不要重复 spawn。",
            f"- 用户说“继续下一步”“你继续”或没有显式点名员工时，主管调用 `spawn_agent(agent_type=\"{agent}\", message=<spawn_message>)`。",
            "- 调用时不要传 model 或 reasoning_effort，让 Codex 使用项目 Agent TOML 的模型、思考等级和昵称配置。",
            "- 主管不能亲自领取员工 job，也不能代写员工产物。",
            "",
            "执行要求：",
            f"1. 先调用 `codex_multi_agents_loop_dispatch_next`，参数传 `run_id=\"{job['run_id']}\"`、`agent=\"{agent}\"`；显式 @ 使用 `invocation_mode=\"explicit_at\"`，主管 spawn 使用 `invocation_mode=\"manager_spawn\"`。",
            "2. 按返回的 `task_message` 或任务包文件执行，不要改其他 Agent 的状态。",
            "3. 完成后调用 `codex_multi_agents_loop_complete_agent_step` 回填 Markdown 结果。",
            "4. 回填后由 `@project-manager` 读取账本并归纳状态、产物和下一步。",
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
            f"先调用 codex_multi_agents_loop_dispatch_next，传 agent=\"{agent}\"、invocation_mode=\"manager_spawn\"。",
            "完成后调用 codex_multi_agents_loop_complete_agent_step 回填结果，并更新同名 Agent 的共享记忆。",
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
    route_plan = _read_route_plan(run["id"])
    loop = _route_plan_loop_summary(route_plan) if route_plan else {}
    latest_evaluation = _latest_loop_evaluation(run["id"])
    return {
        "manager_agent": MANAGER_AGENT_ID,
        "run_id": run["id"],
        "run_status": run["status"],
        "current_step": run["current_step"],
        "prd_version": run.get("prd_version", 0),
        "review_round": run.get("review_round", 0),
        "loop": loop,
        "goal": loop.get("goal", ""),
        "exit_conditions": loop.get("exit_conditions", []),
        "latest_evaluation": latest_evaluation,
        "loop_progress": _loop_progress_summary(loop, completed_steps, pending_jobs, running_jobs, latest_evaluation),
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


def memory_pack(agent: str | None = None, query: str | None = None, limit: int = 6, max_chars: int = 4000) -> dict[str, Any]:
    root = global_memory_root()
    cards = _read_global_memory_cards(agent=agent, query=query)
    selected: list[dict[str, Any]] = []
    used = 0
    for card in cards[: max(0, limit)]:
        size = len(json.dumps(card, ensure_ascii=False))
        if selected and used + size > max_chars:
            break
        selected.append(card)
        used += size
    return {
        "ok": True,
        "global_memory_root": str(root),
        "agent": agent or "",
        "query": query or "",
        "limit": limit,
        "max_chars": max_chars,
        "cards": selected,
        "token_strategy": "只注入命中的少量 memory cards，不把全局记忆全文塞进任务包。",
    }


def pull_global_memory(agent: str | None = None, limit: int = 6) -> dict[str, Any]:
    pack = memory_pack(agent=agent, limit=limit)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for card in pack["cards"]:
        grouped.setdefault(card.get("agent") or "general", []).append(card)

    updated: list[dict[str, Any]] = []
    for agent_name, cards in grouped.items():
        if agent and agent_name != agent:
            continue
        target = memory_root() / f"{agent_name}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(f"# {agent_name} 记忆\n\n", encoding="utf-8")
        bullets = "\n".join(f"- {card.get('summary', '').strip()}" for card in cards if card.get("summary"))
        if not bullets:
            continue
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## 全局经验拉取 / {now_iso()}\n\n{bullets}\n")
        updated.append({"agent": agent_name, "path": str(target), "cards": len(cards)})
    return {"ok": True, "updated": updated, "pack": pack}


def sync_global_memory(run_id: str | None = None, agent: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    run = get_run(run_id) if run_id else _latest_run_for_current_project()
    current = status(run["id"])
    root = global_memory_root()
    cards: list[dict[str, Any]] = []
    for item in current.get("agent_memory", []):
        agent_name = item.get("agent_name")
        if agent and agent_name != agent:
            continue
        summary = (item.get("last_summary") or "").strip()
        memory_path = Path(item.get("memory_path") or "")
        if not summary and memory_path.is_file():
            summary = memory_path.read_text(encoding="utf-8")[-800:].strip()
        if not summary:
            continue
        cards.append(
            {
                "agent": agent_name,
                "project_id": run["project_id"],
                "run_id": run["id"],
                "project_title": (run.get("project") or {}).get("title", ""),
                "summary": summary[:700],
                "source": "project-memory",
                "confidence": 0.75,
                "updated_at": now_iso(),
            }
        )

    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        for card in cards:
            agent_dir = root / str(card["agent"])
            agent_dir.mkdir(parents=True, exist_ok=True)
            with (agent_dir / "cards.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(card, ensure_ascii=False, sort_keys=True) + "\n")
            summary_path = agent_dir / "summary.md"
            with summary_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n## {card['updated_at']} / {card['project_title']}\n\n- {card['summary']}\n")

    return {
        "ok": True,
        "dry_run": dry_run,
        "run_id": run["id"],
        "global_memory_root": str(root),
        "cards": cards,
        "written": 0 if dry_run else len(cards),
        "relationship": "项目 Agent 负责执行；全局记忆只保存跨项目经验摘要，供下次检索注入。",
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
    step = {**step, "iteration": int(payload.get("iteration") or step.get("iteration") or 1)}
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
    transition = _complete_step_transition(run_id, step, outputs, result, job=job)
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
    _finalize_loop_learning_if_completed(run_id)
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
        raise WorkflowError(f"cannot confirm product handoff while run status is {run['status']}")
    _move_to_next(run["id"], "software-architect", status_value="running")
    enqueue_step(run["id"], "software-architect", {"confirmed_prd_version": latest_prd["version"]})
    emit_event(
        run["id"],
        "prd.confirmed",
        f"用户已确认产品交接说明 V{latest_prd['version']}，开始进入架构/UI/研发/QA 链路。",
        {"prd_version": latest_prd["version"], "prd_artifact": latest_prd["id"]},
    )
    return {"ok": True, "run_id": run["id"], "prd_version": latest_prd["version"], "next_step": "software-architect"}


def request_prd_review(run_id: str | None = None, note: str | None = None) -> dict[str, Any]:
    run = get_run(run_id) if run_id else _latest_run_for_current_project()
    latest_prd = read_artifact(run["id"], "prd")
    round_no = int(run.get("review_round") or 0) + 1
    target_version = int(latest_prd["version"])
    review_steps = []
    for reviewer in ["software-architect", "ui-designer", "development-engineer", "qa-engineer"]:
        step = _prd_review_step(reviewer=reviewer, prd_version=target_version, round_no=round_no)
        review_steps.append(enqueue_dynamic_step(run["id"], step, {"owner_note": note or "", "target_prd_version": target_version, "review_round": round_no}))
    _set_run_state(run["id"], status_value="reviewing", current_step=f"prd-v{target_version}-review-r{round_no}", review_round=round_no)
    emit_event(
        run["id"],
        "prd.review_requested",
        f"用户要求多 Agent 评审产品交接说明 V{target_version}，已入队第 {round_no} 轮评审，等待对应 @ Agent 领取。",
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


def _build_route_plan(
    requirement: str,
    *,
    mode: str = "auto",
    requested_agents: list[str] | None = None,
    targets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if mode not in {"auto", "full_workflow", "single_agent_task", "multi_agent_task"}:
        raise WorkflowError(f"unsupported task mode: {mode}")
    requested_agents = _normalize_requested_agents(requested_agents or [])
    explicit_targets = targets or []
    if mode in {"single_agent_task", "multi_agent_task"}:
        if not requested_agents:
            raise WorkflowError(f"{mode} requires at least one requested agent")
        if mode == "single_agent_task" and len(requested_agents) != 1:
            raise WorkflowError("single_agent_task requires exactly one requested agent")
        steps = [_targeted_agent_step(agent=agent, requirement=requirement, target=_target_for_agent(agent, explicit_targets), index=index) for index, agent in enumerate(requested_agents, start=1)]
        return _route_plan(
            requirement=requirement,
            mode=mode,
            current_step=mode.replace("_", "-"),
            steps=steps,
            start_message=f"Codex 定向 Agent 任务已启动，参与 Agent：{', '.join(requested_agents)}。",
        )
    if mode == "full_workflow":
        return _full_workflow_route_plan(requirement)

    # 文档目标是文件名列表，不能覆盖入参中的结构化 Agent targets，否则类型和路由语义都会混淆。
    document_targets = _detect_document_targets(requirement)
    if document_targets:
        steps = [_document_maintenance_step(target) for target in document_targets]
        return _route_plan(
            requirement=requirement,
            mode="multi_agent_task" if len(steps) > 1 else "single_agent_task",
            current_step="multi-agent-task" if len(steps) > 1 else "single-agent-task",
            steps=steps,
            start_message=f"Codex 定向文档任务已启动，目标：{', '.join(step['target_label'] for step in steps)}。",
        )
    return _full_workflow_route_plan(requirement)


def _full_workflow_route_plan(requirement: str) -> dict[str, Any]:
    loop_fields = _loop_plan_fields(
        mode="full_workflow",
        requirement=requirement,
        targets=[],
        completion_type="owner_review_then_full_chain",
    )
    return {
        "mode": "full_workflow",
        **loop_fields,
        "current_step": "product-manager",
        "requested_agents": ["product-manager"],
        "targets": [],
        "steps": [],
        "completion": {
            "type": "owner_review_then_full_chain",
            "required_step_ids": ["product-manager"],
            "requires_owner_review": True,
            "continue_full_workflow": True,
        },
        "workflow_state": "产品交接说明 V1 完成后会暂停，等待用户确认或发起多 Agent 评审。",
        "start_message": "Codex 多 Agent Loop 已启动，等待 product-manager 输出产品交接说明 V1。",
    }


def _route_plan(*, requirement: str, mode: str, current_step: str, steps: list[dict[str, Any]], start_message: str) -> dict[str, Any]:
    targets = [
        {
            "agent": step["agent"],
            "target": step.get("target_label") or step["name"],
            "outputs": step.get("outputs", []),
            "step_id": step["id"],
        }
        for step in steps
    ]
    loop_fields = _loop_plan_fields(
        mode=mode,
        requirement=requirement,
        targets=targets,
        completion_type="all_selected_agents_done",
    )
    return {
        "mode": mode,
        **loop_fields,
        "current_step": current_step,
        "requested_agents": [step["agent"] for step in steps],
        "targets": targets,
        "steps": steps,
        "completion": {
            "type": "all_selected_agents_done",
            "required_step_ids": [step["id"] for step in steps],
            "requires_owner_review": False,
            "continue_full_workflow": False,
        },
        "workflow_state": "定向 Agent 任务只调度 route plan 中声明的 Agent；全部目标完成且无 pending/running job 后自动结束。",
        "start_message": start_message,
    }


def _loop_plan_fields(*, mode: str, requirement: str, targets: list[dict[str, Any]], completion_type: str) -> dict[str, Any]:
    goal = _loop_goal(requirement)
    return {
        "loop_kind": _loop_kind(mode),
        "goal": goal,
        "constraints": _loop_constraints(mode),
        "exit_conditions": _loop_exit_conditions(mode=mode, targets=targets, completion_type=completion_type),
        "max_iterations": 3,
        "current_iteration": 1,
        "evaluation": {
            "phase": "phase_1_summary_only",
            "strategy": "本阶段只显式记录目标、约束和退出条件；暂不自动创建下一轮 iteration。",
        },
    }


def _loop_kind(mode: str) -> str:
    return {
        "full_workflow": "multi_agent_delivery_loop",
        "single_agent_task": "single_agent_loop",
        "multi_agent_task": "multi_agent_collaboration_loop",
    }.get(mode, "project_loop")


def _loop_goal(requirement: str) -> str:
    return requirement.strip().splitlines()[0][:200] or "完成当前用户目标"


def _loop_constraints(mode: str) -> list[str]:
    common = [
        "真实项目源码、项目文档和业务配置必须位于 project_root，不得写入 .codex 控制区。",
        "每个 Agent 只处理自己职责范围内的最小必要任务。",
        "产物必须提供可追踪路径或明确证据，不能只在聊天里口头声明完成。",
    ]
    if mode == "full_workflow":
        return [
            *common,
            "产品交接说明完成后必须等待用户确认或要求多 Agent 评审。",
            "用户确认前不得自动进入架构、UI、研发或 QA 链路。",
        ]
    return [
        *common,
        "定向任务只调度 route_plan 中声明的 Agent。",
        "所有声明目标完成且无 pending/running job 后直接结束，不强行进入完整流程。",
    ]


def _loop_exit_conditions(*, mode: str, targets: list[dict[str, Any]], completion_type: str) -> list[str]:
    if completion_type == "owner_review_then_full_chain":
        return [
            "product-manager 输出产品交接说明 V1 并维护 PRODUCT.md 相关内容。",
            "PM 已向用户说明产物路径、核心范围、风险和待确认问题。",
            "工作流进入 waiting_owner_review，等待用户确认、要求评审或要求补充。",
        ]
    conditions = [
        f"{target['agent']} 完成目标：{target['target']}；输出：{', '.join(target.get('outputs') or []) or '当前目标结果'}。"
        for target in targets
    ]
    conditions.append("route_plan 声明的所有目标均已完成，且没有 pending/running job。")
    conditions.append("PM summary 能说明目标已完成、产物路径和是否存在剩余风险。")
    return conditions


def _route_plan_loop_summary(route_plan: dict[str, Any]) -> dict[str, Any]:
    if not route_plan:
        return {}
    return {
        "loop_kind": route_plan.get("loop_kind", ""),
        "goal": route_plan.get("goal", ""),
        "constraints": route_plan.get("constraints", []),
        "exit_conditions": route_plan.get("exit_conditions", []),
        "max_iterations": route_plan.get("max_iterations", 3),
        "current_iteration": route_plan.get("current_iteration", 1),
        "evaluation": route_plan.get("evaluation", {}),
    }


def _loop_progress_summary(
    loop: dict[str, Any],
    completed_steps: list[str],
    pending_jobs: list[str],
    running_jobs: list[str],
    latest_evaluation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not loop:
        return {}
    blocked_by_jobs = bool(pending_jobs or running_jobs)
    latest_evaluation = latest_evaluation or {}
    # PM 摘要只展示“已经结构化确认”的闭环状态，避免从聊天文本里猜测任务是否完成。
    return {
        "current_iteration": latest_evaluation.get("iteration") or loop.get("current_iteration", 1),
        "max_iterations": loop.get("max_iterations", 3),
        "completed_steps": completed_steps,
        "pending_jobs": pending_jobs,
        "running_jobs": running_jobs,
        "exit_condition_status": (
            "pending_jobs_remaining"
            if blocked_by_jobs
            else ("met" if latest_evaluation.get("exit_conditions_met") is True else latest_evaluation.get("decision", "ready_for_pm_review"))
        ),
        "missing_evidence": latest_evaluation.get("missing_evidence", []),
        "next_agent": latest_evaluation.get("next_agent", ""),
        "next_target": latest_evaluation.get("next_target", ""),
        "note": "闭环推进只基于结构化 loop_evaluation；没有明确下一轮证据时不做过度推断。",
    }


def _normalize_requested_agents(agents: list[str]) -> list[str]:
    normalized: list[str] = []
    for agent in agents:
        if agent not in PROJECT_AGENT_IDS or agent == MANAGER_AGENT_ID:
            raise WorkflowError(f"unsupported requested agent: {agent}")
        if agent not in normalized:
            normalized.append(agent)
    return normalized


def _detect_document_targets(requirement: str) -> list[str]:
    lowered = requirement.lower()
    has_doc_intent = any(term in lowered for term in ["优化", "更新", "完善", "修正", "整理", "维护", "补充", "文档", "md", ".md"])
    if not has_doc_intent:
        return []
    targets: list[str] = []
    # 文档类定向任务按文件归属拆给对应 Agent，避免 PM 把 PRODUCT.md / AGENTS.md 一口气代做完。
    if "product.md" in lowered or "product" in lowered and ".md" in lowered or "产品文档" in requirement or "产品说明" in requirement:
        targets.append("PRODUCT.md")
    if "agents.md" in lowered or "agent" in lowered and ".md" in lowered or "ai 协作" in lowered or "ai协作" in lowered or "项目规则" in requirement:
        targets.append("AGENTS.md")
    if "design.md" in lowered or "设计文档" in requirement or "设计规范" in requirement:
        targets.append("DESIGN.md")
    return targets


def _document_maintenance_step(target_document: str) -> dict[str, Any]:
    mapping = {
        "PRODUCT.md": {
            "id": "docs-product-md",
            "name": "PRODUCT.md 文档维护",
            "agent": "product-manager",
            "artifact_category": "documentation",
            "outputs": ["PRODUCT.md"],
            "responsibility": "只维护 PRODUCT.md 的产品功能列表、业务能力索引、范围、验收点和未决事项。",
        },
        "AGENTS.md": {
            "id": "docs-agents-md",
            "name": "AGENTS.md 文档维护",
            "agent": "software-architect",
            "artifact_category": "documentation",
            "outputs": ["AGENTS.md"],
            "responsibility": "只维护 AGENTS.md 的 AI 工作规则、读取顺序、代码边界、验证命令和禁止事项。",
        },
        "DESIGN.md": {
            "id": "docs-design-md",
            "name": "DESIGN.md 文档维护",
            "agent": "ui-designer",
            "artifact_category": "documentation",
            "outputs": ["DESIGN.md"],
            "responsibility": "只维护 DESIGN.md 的视觉系统、组件规则、token 证据和 UI 验收点。",
        },
    }
    if target_document not in mapping:
        raise WorkflowError(f"unsupported document target: {target_document}")
    item = mapping[target_document]
    return {
        "id": item["id"],
        "name": item["name"],
        "category": "documentation",
        "executor": "agent",
        "agent": item["agent"],
        "artifact_category": item["artifact_category"],
        "inputs": ["raw_requirement", "project_context"],
        "outputs": item["outputs"],
        "kind": "route_task",
        "target_document": target_document,
        "target_label": target_document,
        "responsibility": item["responsibility"],
    }


def _target_for_agent(agent: str, targets: list[dict[str, Any]]) -> dict[str, Any]:
    for target in targets:
        if target.get("agent") == agent:
            return target
    return {}


def _targeted_agent_step(*, agent: str, requirement: str, target: dict[str, Any], index: int) -> dict[str, Any]:
    label = str(target.get("target") or _default_target_label(agent, requirement))
    outputs = target.get("outputs")
    if isinstance(outputs, str):
        outputs = [outputs]
    if not isinstance(outputs, list) or not outputs:
        outputs = [_default_output_for_agent(agent)]
    step_id = str(target.get("step_id") or f"task-{agent}-{index}")
    return {
        "id": step_id,
        "name": str(target.get("name") or f"{agent} 定向任务"),
        "category": "targeted",
        "executor": "agent",
        "agent": agent,
        "artifact_category": str(target.get("artifact_category") or "agent-task"),
        "inputs": ["raw_requirement", "project_context"],
        "outputs": outputs,
        "kind": "route_task",
        "target_label": label,
        "responsibility": str(target.get("responsibility") or f"只完成分配给 {agent} 的目标：{label}。"),
    }


def _default_target_label(agent: str, requirement: str) -> str:
    short = requirement.strip().splitlines()[0][:80]
    return short or f"{agent} direct task"


def _default_output_for_agent(agent: str) -> str:
    return {
        "product-manager": "prd",
        "software-architect": "architecture_design",
        "ui-designer": "design_spec",
        "development-engineer": "development_report",
        "qa-engineer": "qa_report",
    }.get(agent, f"{agent}_report")


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
    route_plan = _read_route_plan(run_id)
    loop_summary = _route_plan_loop_summary(route_plan) if route_plan else {}
    # 闭环目标和 complete metadata 示例一起注入任务包，让员工 Agent 以结构化结果驱动下一轮。
    return "\n\n".join(
        [
            f"# Loop 步骤：{step['id']}",
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
            "## 闭环目标",
            json.dumps(
                {
                    "goal": loop_summary.get("goal", ""),
                    "loop_kind": loop_summary.get("loop_kind", ""),
                    "current_iteration": step.get("iteration") or loop_summary.get("current_iteration", 1),
                    "max_iterations": loop_summary.get("max_iterations", 3),
                    "constraints": loop_summary.get("constraints", []),
                    "exit_conditions": loop_summary.get("exit_conditions", []),
                    "evaluation_strategy": loop_summary.get("evaluation", {}),
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
            "## 全局经验记忆",
            _format_global_memory_for_prompt(agent),
            "## 输入列表",
            "\n\n".join(refs) if refs else "No explicit input artifacts.",
            "## 项目工作区",
            json.dumps(
                {
                    "project_root": str(workspace_root()),
                    "workflow_home": str(data_home()),
                    "artifact_root": str(artifact_root()),
                    "scratch_root": str(scratch_root()),
                    "memory_root": str(memory_root()),
                },
                ensure_ascii=False,
                indent=2,
            ),
            "## 必须输出",
            json.dumps(step.get("outputs", []), ensure_ascii=False, indent=2),
            "## complete 工具 metadata 示例",
            json.dumps(
                {
                    "loop_evaluation": {
                        "exit_conditions_met": True,
                        "missing_evidence": [],
                        "next_agent": "",
                        "next_target": "",
                        "reason": "已完成本轮目标并提供证据。",
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
            "## 输出契约",
            _output_contract(step),
        ]
    )


def _format_global_memory_for_prompt(agent: str) -> str:
    try:
        pack = memory_pack(agent=agent, limit=5, max_chars=2400)
    except Exception:
        return "暂无可用全局经验记忆。"
    cards = pack.get("cards") or []
    if not cards:
        return "暂无可用全局经验记忆。"
    return "\n".join(f"- {card.get('summary', '').strip()}" for card in cards if card.get("summary")) or "暂无可用全局经验记忆。"


def _read_route_plan(run_id: str) -> dict[str, Any]:
    try:
        content = read_artifact(run_id, "route_plan")["content"]
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {}
    except (WorkflowError, json.JSONDecodeError, TypeError):
        return {}


def _read_global_memory_cards(agent: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
    root = global_memory_root()
    if not root.exists():
        return []
    agent_dirs = [root / agent] if agent else [path for path in root.iterdir() if path.is_dir()]
    terms = [term for term in re.split(r"\s+", query or "") if term]
    cards: list[dict[str, Any]] = []
    for agent_dir in agent_dirs:
        path = agent_dir / "cards.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                card = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = json.dumps(card, ensure_ascii=False)
            if terms and not all(term in text for term in terms):
                continue
            cards.append(card)
    cards.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return cards


def _output_contract(step: dict[str, Any]) -> str:
    evidence_clause = (
        "\n\n闭环输出要求：必须包含 `执行结果`、`证据/产物路径`、`自评是否满足退出条件`、"
        "`剩余风险`、`建议下一步` 五部分；没有验证证据时必须明确写出缺口，并在 complete 工具的 "
        "`metadata.loop_evaluation` 中设置 `exit_conditions_met=false`、`missing_evidence` 和合法的 `next_agent/next_target`。"
    )
    if step.get("kind") == "route_task":
        target = step.get("target_label") or step.get("target_document") or "当前目标"
        responsibility = step.get("responsibility", f"只完成当前目标：{target}。")
        return (
            f"这是定向 Agent 任务，不是完整 PRD 交付流程。目标：{target}。\n"
            f"{responsibility}\n"
            "必须先读取目标项目中的现有文件和相关上下文，再按职责边界执行或给出可落地结果。\n"
            "完成后只回填当前目标的结果；不要自行推进完整流程，也不要代替其他 Agent 处理其它目标。"
            f"{evidence_clause}"
        )
    if step["id"] == "product-manager":
        return "输出一份完整中文产品交接说明 V1 Markdown，并维护项目根目录 PRODUCT.md 的功能列表。完成后不要继续架构、UI 或开发，等待用户确认或多 Agent 评审。" + evidence_clause
    if step.get("kind") == "prd_review":
        return "输出一份中文产品交接说明评审意见 Markdown，只评价最新说明的问题、风险、遗漏和修改建议，不直接改产品交接说明。" + evidence_clause
    if step.get("kind") == "prd_revision":
        return "整合各 Agent 评审意见，输出下一版完整中文产品交接说明 Markdown。必须说明相对上一版的变更点。完成后等待用户再次确认。" + evidence_clause
    return "只返回当前步骤的一份完整 Markdown 结果。不要自行推进工作流状态，不要创建额外步骤。" + evidence_clause


def _agent_execution_boundary(agent: str) -> str:
    return (
        f"只有 {agent} 类型的 Agent 才允许执行本任务。它可以来自用户显式 @{agent}，也可以由主管调用 "
        f"`spawn_agent(agent_type=\"{agent}\", message=<任务包>)` 创建。\n"
        f"如果你是 project-manager 或普通当前会话，不要亲自领取或代替 {agent} 输出产物；应 spawn 对应自定义 Agent。\n"
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
        # 动态步骤只存在于 job payload 中；解析后先验证对象形状，避免 None 或数组进入字段读取逻辑。
        payload_value = payload if payload is not None else json.loads(job.get("payload_json") or "{}")
        if not isinstance(payload_value, dict):
            raise WorkflowError(f"dynamic job payload must be an object: {job['step_id']}")
        step = payload_value.get("step")
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
    review_inputs = [
        f"prd_review_{reviewer}_v{prd_version}_r{round_no}"
        for reviewer in ["software-architect", "ui-designer", "development-engineer", "qa-engineer"]
    ]
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


def _evaluate_loop_progress(run_id: str, step: dict[str, Any], outputs: list[dict[str, Any]], result: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """把 Agent 显式回填的闭环判断落盘。

    这里刻意不解析自然语言输出：只有 metadata.loop_evaluation 才能驱动下一轮，
    避免 PM/工具层凭感觉误判任务是否完成。
    """
    route_plan = _read_route_plan(run_id)
    # metadata 来自 MCP JSON 入参，必须先在边界处收窄类型，后续闭环逻辑才能安全读取字段。
    metadata_value = result.get("metadata")
    metadata: dict[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
    evaluation_value = metadata.get("loop_evaluation")
    raw: dict[str, Any] = evaluation_value if isinstance(evaluation_value, dict) else {}
    iteration = int(step.get("iteration") or _job_iteration(job) or 1)
    max_iterations = int(route_plan.get("max_iterations") or 3)
    # 默认视为本轮满足条件：只有 Agent 明确提交缺口，系统才会考虑自动补下一轮。
    exit_conditions_met = bool(raw.get("exit_conditions_met", True))
    missing_evidence = _string_list(raw.get("missing_evidence"))
    next_agent = str(raw.get("next_agent") or "").strip()
    # next_agent 必须是合法员工 Agent；缺失或非法时宁可 blocked，也不让系统自行猜负责人。
    if next_agent not in PROJECT_AGENT_IDS or next_agent == MANAGER_AGENT_ID:
        next_agent = ""
    next_target = str(raw.get("next_target") or "").strip()
    decision = _evaluation_decision(
        exit_conditions_met=exit_conditions_met,
        missing_evidence=missing_evidence,
        next_agent=next_agent,
        next_target=next_target,
        iteration=iteration,
        max_iterations=max_iterations,
    )
    evaluation = {
        "run_id": run_id,
        "job_id": job["id"],
        "step_id": step["id"],
        "iteration": iteration,
        "max_iterations": max_iterations,
        "agent": step.get("agent", ""),
        "exit_conditions_met": exit_conditions_met,
        "missing_evidence": missing_evidence,
        "next_agent": next_agent,
        "next_target": next_target,
        "decision": decision,
        "reason": str(raw.get("reason") or ("Agent 未声明缺口，按当前步骤结果继续。" if exit_conditions_met else "Agent 声明退出条件未满足。")),
        "outputs": [{"name": item.get("name"), "path": item.get("path"), "version": item.get("version")} for item in outputs],
        "created_at": now_iso(),
    }
    artifact = _write_loop_evaluation_artifact(run_id, evaluation)
    evaluation["artifact"] = artifact
    emit_event(run_id, "loop.evaluated", f"{step.get('agent')} 完成第 {iteration} 轮闭环评估：{decision}", evaluation)
    return evaluation


def _evaluation_decision(
    *,
    exit_conditions_met: bool,
    missing_evidence: list[str],
    next_agent: str,
    next_target: str,
    iteration: int,
    max_iterations: int,
) -> str:
    if exit_conditions_met:
        return "complete_or_continue"
    if iteration >= max_iterations:
        return "blocked_max_iterations"
    if next_agent and next_target:
        return "continue_iteration"
    if missing_evidence:
        return "blocked_missing_next_agent"
    return "blocked_missing_evidence"


def _write_loop_evaluation_artifact(run_id: str, evaluation: dict[str, Any]) -> dict[str, Any]:
    # evaluation 作为 artifact 保存，SQLite 仍只承担索引和状态职责，避免 schema 为每轮判断膨胀。
    return write_artifact(
        run_id,
        "loop_evaluation",
        json.dumps(evaluation, ensure_ascii=False, indent=2),
        category="evaluation",
        created_by="workflow",
        metadata={"iteration": evaluation["iteration"], "decision": evaluation["decision"], "agent": evaluation["agent"]},
    )


def _latest_loop_evaluation(run_id: str) -> dict[str, Any]:
    try:
        return json.loads(read_artifact(run_id, "loop_evaluation")["content"])
    except (WorkflowError, json.JSONDecodeError, TypeError):
        return {}


def _maybe_enqueue_next_iteration(run_id: str, evaluation: dict[str, Any], *, excluding_job_id: str) -> dict[str, Any] | None:
    """保守自动循环：只有结构化 evaluation 明确给出 next_agent/next_target 才入队。"""
    decision = evaluation.get("decision")
    if decision == "complete_or_continue":
        return None
    if decision == "continue_iteration":
        # 只有当前 run 没有其它活动 job 时才自动入队，防止并发任务互相覆盖状态。
        if _has_active_jobs(run_id, excluding_job_id=excluding_job_id):
            return None
        next_iteration = int(evaluation["iteration"]) + 1
        step = _loop_retry_step(
            agent=str(evaluation["next_agent"]),
            target=str(evaluation["next_target"]),
            iteration=next_iteration,
            previous_evaluation=evaluation,
        )
        enqueue_dynamic_step(run_id, step, {"iteration": next_iteration, "previous_loop_evaluation": evaluation})
        _set_run_state(run_id, status_value="running", current_step=step["id"])
        return {
            "next_step": step["id"],
            "event_type": "loop.iteration_enqueued",
            "message": f"闭环第 {evaluation['iteration']} 轮未满足退出条件，已入队第 {next_iteration} 轮：{step['agent']} / {step['target_label']}。",
            "loop_evaluation": evaluation,
        }
    # 缺少合法下一步或达到轮次上限时进入 blocked，由用户或 PM 决定下一步，而不是工具层越权推断。
    _set_run_state(run_id, status_value="blocked", current_step="loop-blocked")
    return {
        "next_step": None,
        "event_type": "loop.blocked",
        "message": f"闭环未满足退出条件且无法安全自动推进：{evaluation.get('reason', '')}",
        "loop_evaluation": evaluation,
    }


def _loop_retry_step(*, agent: str, target: str, iteration: int, previous_evaluation: dict[str, Any]) -> dict[str, Any]:
    slug = _path_segment(target)[:48] or "retry"
    # 动态 retry step 使用稳定 id，方便 artifacts、events 和 PM summary 追踪同一轮补证任务。
    return {
        "id": f"loop-i{iteration}-{agent}-{slug}",
        "name": f"闭环第 {iteration} 轮：{agent}",
        "category": "loop-iteration",
        "executor": "agent",
        "agent": agent,
        "artifact_category": "loop-iteration",
        "inputs": ["raw_requirement", "project_context", "loop_evaluation"],
        "outputs": [_default_output_for_agent(agent)],
        "kind": "route_task",
        "target_label": target,
        "responsibility": f"根据上一轮闭环评估补齐缺口：{target}。",
        "iteration": iteration,
        "previous_decision": previous_evaluation.get("decision", ""),
    }


def _next_iteration_index(run_id: str) -> int:
    latest = _latest_loop_evaluation(run_id)
    return int(latest.get("iteration") or 0) + 1


def _finalize_loop_learning_if_completed(run_id: str) -> None:
    run = get_run(run_id)
    if run["status"] != "completed":
        return
    try:
        read_artifact(run_id, "loop_learning")
        return
    except WorkflowError:
        pass
    # learning 只沉淀到当前项目的 Agent memory；全局记忆必须由显式 memory_sync 触发，避免跨项目副作用。
    route_plan = _read_route_plan(run_id)
    evaluation = _latest_loop_evaluation(run_id)
    artifacts = [item for item in list_artifacts(run_id) if item["name"] not in {"loop_learning"}]
    participants = sorted({item["created_by"] for item in artifacts if item.get("created_by") and item["created_by"] != "workflow"})
    learning = {
        "run_id": run_id,
        "goal": route_plan.get("goal", ""),
        "final_decision": evaluation.get("decision", "completed"),
        "participants": participants,
        "artifacts": [{"name": item["name"], "path": item["path"], "version": item["version"]} for item in artifacts],
        "missing_evidence": evaluation.get("missing_evidence", []),
        "reusable_learning": _learning_summary(route_plan, evaluation, participants),
        "created_at": now_iso(),
    }
    content = json.dumps(learning, ensure_ascii=False, indent=2)
    artifact = write_artifact(run_id, "loop_learning", content, category="learning", created_by="workflow")
    _update_agent_memory(run_id, MANAGER_AGENT_ID, {"loop_learning": content}, [artifact])


def _learning_summary(route_plan: dict[str, Any], evaluation: dict[str, Any], participants: list[str]) -> str:
    goal = route_plan.get("goal") or "当前目标"
    decision = evaluation.get("decision") or "completed"
    people = ", ".join(participants) if participants else "无员工 Agent"
    missing = "；".join(evaluation.get("missing_evidence") or []) or "无"
    return f"目标「{goal}」最终状态 {decision}；参与 Agent：{people}；关键缺口：{missing}。后续同类任务优先复用本次路由和证据要求。"


def _job_iteration(job: dict[str, Any]) -> int:
    payload = json.loads(job.get("payload_json") or "{}")
    return int(payload.get("iteration") or 1)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _complete_step_transition(run_id: str, step: dict[str, Any], outputs: list[dict[str, Any]], result: dict[str, Any], *, job: dict[str, Any]) -> dict[str, Any]:
    evaluation = _evaluate_loop_progress(run_id, step, outputs, result, job)
    kind = step.get("kind")
    # 先让 full workflow / PRD review 保留原有业务停顿点，再只在安全边界内触发闭环 retry。
    if kind == "route_task":
        return _complete_route_task_transition(run_id, step, job=job, evaluation=evaluation)
    if step["id"] == "product-manager" or kind == "prd_revision":
        prd_artifact = next((item for item in outputs if item["name"] == "prd"), None)
        version = int(prd_artifact["version"]) if prd_artifact else _latest_artifact_version(run_id, "prd")
        _set_run_state(run_id, status_value="waiting_owner_review", current_step="owner-review", prd_version=version)
        return {
            "next_step": None,
            "event_type": "prd.ready_for_owner_review",
            "message": f"{step.get('agent', 'product-manager')} 已完成产品交接说明 V{version}，等待用户确认或发起多 Agent 评审。",
            "prd_version": version,
        }
    if kind == "prd_review":
        _record_review(run_id, step, outputs, result)
        if _has_pending_review_jobs(run_id, int(step["review_round"]), excluding_job_id=job["id"]):
            _set_run_state(run_id, status_value="reviewing", current_step=f"prd-v{step['target_prd_version']}-review-r{step['review_round']}")
            return {
                "next_step": None,
                "event_type": "prd.review_agent_completed",
            "message": f"{step.get('agent')} 已完成产品交接说明 V{step['target_prd_version']} 评审，等待其他 Agent。",
            }
        revision = _prd_revision_step(prd_version=int(step["target_prd_version"]), round_no=int(step["review_round"]))
        enqueue_dynamic_step(run_id, revision, {"target_prd_version": step["target_prd_version"], "review_round": step["review_round"]})
        _set_run_state(run_id, status_value="prd_revision", current_step=revision["id"])
        return {
            "next_step": revision["id"],
            "event_type": "prd.review_completed",
            "message": f"产品交接说明 V{step['target_prd_version']} 第 {step['review_round']} 轮评审已完成，已入队 product-manager 整合下一版，等待 @product-manager 领取。",
        }
    next_step = _next_step(step)
    _move_to_next(run_id, next_step)
    if next_step:
        enqueue_step(run_id, next_step, {"iteration": step.get("iteration") or 1})
    else:
        retry = _maybe_enqueue_next_iteration(run_id, evaluation, excluding_job_id=job["id"])
        if retry:
            return retry
    return {"next_step": next_step}


def _complete_route_task_transition(run_id: str, step: dict[str, Any], *, job: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
    if _has_pending_route_task_jobs(run_id, excluding_job_id=job["id"]):
        _set_run_state(run_id, status_value="running", current_step="targeted-agent-task")
        return {
            "next_step": None,
            "event_type": "route.target_completed",
            "message": f"{step.get('agent')} 已完成 {step.get('target_label', step['id'])}，等待其他定向目标完成。",
        }
    retry = _maybe_enqueue_next_iteration(run_id, evaluation, excluding_job_id=job["id"])
    if retry:
        return retry
    _move_to_next(run_id, None)
    return {
        "next_step": None,
        "event_type": "workflow.completed",
        "message": "定向 Agent 任务目标已全部完成，当前工作流任务自动结束。",
    }


def _latest_artifact_version(run_id: str, name: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT MAX(version) AS version FROM artifacts WHERE run_id = ? AND name = ?", (run_id, name)).fetchone()
    return int(row["version"] or 0)


def _has_pending_route_task_jobs(run_id: str, *, excluding_job_id: str) -> bool:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, payload_json FROM jobs WHERE run_id = ? AND status IN ('pending','running') AND id != ?",
            (run_id, excluding_job_id),
        ).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        step = payload.get("step")
        if isinstance(step, dict) and step.get("kind") == "route_task":
            return True
    return False


def _has_active_jobs(run_id: str, *, excluding_job_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE run_id = ? AND status IN ('pending','running') AND id != ?",
            (run_id, excluding_job_id),
        ).fetchone()
    return int(row["count"] or 0) > 0


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
        return f"准备调度待办 Agent：{', '.join(pending_jobs)}。用户未显式 @ 时，由主管 spawn 对应自定义 Agent。"
    if run["status"] == "waiting_owner_review":
        version = run.get("prd_version", 0)
        return f"请用户确认产品交接说明 V{version}，或要求多 Agent 评审后输出下一版。"
    if run["status"] == "blocked":
        return "闭环已阻塞：请读取 latest_evaluation 的 missing_evidence、next_agent 和 reason，决定补充证据、指定 Agent 或结束任务。"
    if run["status"] == "completed":
        return "闭环任务已完成，可以读取 loop_learning、最终产物和主管总结。"
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
        "rule": "用户显式 @ 时由原生项目 Agent 执行；否则主管按语义和 pending job 主动 spawn 同名自定义 Agent，MCP 负责状态、记忆和产物。",
    }


def _ensure_workspace_files() -> None:
    if not (workspace_root() / WORKSPACE_CONFIG_NAME).exists():
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
