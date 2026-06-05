from __future__ import annotations

import argparse
import json
import time
from typing import Any

from .capabilities import doctor, install_lark_cli
from .config import config_sources, load_config, write_workspace_config
from .lark_events import run_lark_long_connection_consumer
from .engine import (
    WorkflowError,
    create_project,
    delete_project,
    enqueue_step,
    get_project_status,
    inspect_workflow,
    list_artifacts,
    list_jobs,
    list_projects,
    read_artifact,
    retry_prd_approval_lark,
    run_worker_once,
    run_worker_until_blocked,
    status,
    submit_gate,
    workflows,
    write_artifact,
)
from .storage import init_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deliveryflow")
    sub = parser.add_subparsers(dest="command")

    doctor_p = sub.add_parser("doctor")
    doctor_p.add_argument(
        "--install-lark-cli",
        action="store_true",
        help="按 larksuite/cli README 的 AI Agent 快速开始第 1 步执行 `npx @larksuite/cli@latest install`。",
    )
    sub.add_parser("init")
    config_p = sub.add_parser("config")
    config_sub = config_p.add_subparsers(dest="config_command")
    config_sub.add_parser("show")
    config_init = config_sub.add_parser("init")
    config_init.add_argument("--overwrite", action="store_true")

    workflow = sub.add_parser("workflow")
    workflow_sub = workflow.add_subparsers(dest="workflow_command")
    workflow_sub.add_parser("list")
    inspect_p = workflow_sub.add_parser("inspect")
    inspect_p.add_argument("--workflow-id", default="delivery-workflow")
    status_p = workflow_sub.add_parser("status")
    status_group = status_p.add_mutually_exclusive_group(required=True)
    status_group.add_argument("--run-id")
    status_group.add_argument("--project-id")
    enqueue_p = workflow_sub.add_parser("enqueue")
    enqueue_p.add_argument("--run-id", required=True)
    enqueue_p.add_argument("--step-id", required=True)
    gate_p = workflow_sub.add_parser("submit-gate")
    gate_p.add_argument("--run-id", required=True)
    gate_p.add_argument("--step-id", required=True)
    gate_p.add_argument("--data-json", required=True)

    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command")
    create_p = project_sub.add_parser("create")
    create_p.add_argument("--requirement", required=True)
    create_p.add_argument("--title")
    create_p.add_argument("--owner-id")
    create_p.add_argument("--business-goal")
    create_p.add_argument("--requires-frontend", dest="requires_frontend", action="store_true", default=True)
    create_p.add_argument("--no-requires-frontend", dest="requires_frontend", action="store_false")
    create_p.add_argument("--requires-backend", dest="requires_backend", action="store_true", default=True)
    create_p.add_argument("--no-requires-backend", dest="requires_backend", action="store_false")
    list_p = project_sub.add_parser("list")
    list_p.add_argument("--limit", type=int, default=20)
    project_status_p = project_sub.add_parser("status")
    project_status_p.add_argument("project_id")
    delete_p = project_sub.add_parser("delete")
    delete_p.add_argument("project_id")
    delete_p.add_argument("--confirm-project-id", required=True)
    delete_p.add_argument("--keep-artifacts", action="store_true")

    artifact = sub.add_parser("artifact")
    artifact_sub = artifact.add_subparsers(dest="artifact_command")
    artifact_list = artifact_sub.add_parser("list")
    artifact_list.add_argument("--run-id", required=True)
    artifact_read = artifact_sub.add_parser("read")
    artifact_read.add_argument("--run-id", required=True)
    artifact_read.add_argument("--name", required=True)
    artifact_write = artifact_sub.add_parser("write")
    artifact_write.add_argument("--run-id", required=True)
    artifact_write.add_argument("--name", required=True)
    artifact_write.add_argument("--file", required=True)
    artifact_write.add_argument("--category", default="manual")
    artifact_write.add_argument("--created-by", default="user")

    jobs = sub.add_parser("job")
    jobs_sub = jobs.add_subparsers(dest="job_command")
    jobs_list = jobs_sub.add_parser("list")
    jobs_list.add_argument("--run-id")
    jobs_list.add_argument("--status")
    jobs_list.add_argument("--limit", type=int, default=50)

    worker = sub.add_parser("worker")
    worker_sub = worker.add_subparsers(dest="worker_command")
    worker_sub.add_parser("once")
    until_blocked = worker_sub.add_parser("until-blocked")
    until_blocked.add_argument("--run-id")
    until_blocked.add_argument("--max-jobs", type=int, default=50)
    until_blocked.add_argument("--stop-step", action="append", default=[])
    start = worker_sub.add_parser("start")
    start.add_argument("--interval", type=float, default=5.0)
    start.add_argument("--max-iterations", type=int)

    lark = sub.add_parser("lark")
    lark_sub = lark.add_subparsers(dest="lark_command")
    lark_sub.add_parser("event-consumer")
    retry_prd = lark_sub.add_parser("retry-prd-approval")
    retry_prd.add_argument("--run-id", required=True)

    args = parser.parse_args(argv)
    try:
        return _dispatch(args, parser)
    except Exception as exc:
        print_json({"ok": False, "error": str(exc)})
        return 1


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command == "doctor":
        result: dict[str, Any] = {"ok": True, "doctor": doctor()}
        if args.install_lark_cli and not result["doctor"]["lark_doc"]["ok"]:
            result["lark_cli_install"] = install_lark_cli()
            result["doctor_after_install"] = doctor()
        print_json(result)
        return 0
    if args.command == "init":
        print_json({"ok": True, "db_path": str(init_db())})
        return 0
    if args.command == "config":
        if args.config_command == "show":
            print_json({"ok": True, "sources": config_sources(), "config": load_config()})
            return 0
        if args.config_command == "init":
            path = write_workspace_config(overwrite=args.overwrite)
            print_json({"ok": True, "path": str(path)})
            return 0
    if args.command == "workflow":
        if args.workflow_command == "list":
            print_json({"ok": True, "workflows": workflows()})
            return 0
        if args.workflow_command == "inspect":
            print_json({"ok": True, "workflow": inspect_workflow(args.workflow_id)})
            return 0
        if args.workflow_command == "status":
            data = status(args.run_id) if args.run_id else get_project_status(args.project_id)
            print_json({"ok": True, "status": data})
            return 0
        if args.workflow_command == "enqueue":
            print_json({"ok": True, "job": enqueue_step(args.run_id, args.step_id)})
            return 0
        if args.workflow_command == "submit-gate":
            print_json(submit_gate(args.run_id, args.step_id, _json_obj(args.data_json)))
            return 0
    if args.command == "project":
        if args.project_command == "create":
            print_json(
                {
                    "ok": True,
                    "project": create_project(
                        requirement=args.requirement,
                        title=args.title,
                        owner_id=args.owner_id,
                        business_goal=args.business_goal,
                        requires_frontend=args.requires_frontend,
                        requires_backend=args.requires_backend,
                    ),
                }
            )
            return 0
        if args.project_command == "list":
            print_json({"ok": True, "projects": list_projects(args.limit)})
            return 0
        if args.project_command == "status":
            print_json({"ok": True, "status": get_project_status(args.project_id)})
            return 0
        if args.project_command == "delete":
            print_json(
                delete_project(
                    args.project_id,
                    confirm_project_id=args.confirm_project_id,
                    delete_artifacts=not args.keep_artifacts,
                )
            )
            return 0
    if args.command == "artifact":
        if args.artifact_command == "list":
            print_json({"ok": True, "artifacts": list_artifacts(args.run_id)})
            return 0
        if args.artifact_command == "read":
            print_json({"ok": True, "artifact": read_artifact(args.run_id, args.name)})
            return 0
        if args.artifact_command == "write":
            content = open(args.file, encoding="utf-8").read()
            print_json(
                {
                    "ok": True,
                    "artifact": write_artifact(
                        args.run_id,
                        args.name,
                        content,
                        category=args.category,
                        created_by=args.created_by,
                    ),
                }
            )
            return 0
    if args.command == "job" and args.job_command == "list":
        print_json({"ok": True, "jobs": list_jobs(args.run_id, args.status, args.limit)})
        return 0
    if args.command == "worker":
        if args.worker_command == "once":
            print_json(run_worker_once())
            return 0
        if args.worker_command == "until-blocked":
            print_json(run_worker_until_blocked(run_id=args.run_id, max_jobs=args.max_jobs, stop_steps=set(args.stop_step or [])))
            return 0
        if args.worker_command == "start":
            iterations = 0
            while args.max_iterations is None or iterations < args.max_iterations:
                iterations += 1
                print_json(run_worker_once())
                time.sleep(max(args.interval, 1.0))
            return 0
    if args.command == "lark":
        if args.lark_command == "event-consumer":
            run_lark_long_connection_consumer()
            return 0
        if args.lark_command == "retry-prd-approval":
            print_json(retry_prd_approval_lark(args.run_id))
            return 0
    parser.print_help()
    return 2


def _json_obj(text: str) -> dict[str, Any]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise WorkflowError("JSON value must be an object")
    return data


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
