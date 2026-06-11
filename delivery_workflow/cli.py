from __future__ import annotations

import argparse
import json
import time
from typing import Any

from .capabilities import doctor, install_lark_cli
from .config import config_sources, initialize_project_workspace, load_config
from .lark import run_project_lark_cli
from .engine import (
    WorkflowError,
    create_project,
    current_project_status,
    delete_current_project,
    enqueue_step,
    inspect_workflow,
    list_artifacts,
    list_jobs,
    read_artifact,
    request_bug_fix,
    run_worker_once,
    run_worker_until_blocked,
    status,
    submit_gate,
    watch_run,
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
    status_p.add_argument("--run-id", required=True)
    enqueue_p = workflow_sub.add_parser("enqueue")
    enqueue_p.add_argument("--run-id", required=True)
    enqueue_p.add_argument("--step-id", required=True)
    gate_p = workflow_sub.add_parser("submit-gate")
    gate_p.add_argument("--run-id", required=True)
    gate_p.add_argument("--step-id", required=True)
    gate_p.add_argument("--data-json", required=True)
    watch_p = workflow_sub.add_parser("watch")
    watch_p.add_argument("--run-id", required=True)
    watch_p.add_argument("--timeout-seconds", type=float)
    watch_p.add_argument("--poll-interval-seconds", type=float)

    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command")
    create_p = project_sub.add_parser("create")
    create_p.add_argument("--requirement", required=True)
    create_p.add_argument("--title")
    create_p.add_argument("--owner-id")
    create_p.add_argument("--lark-chat-id", help="项目群 chat_id，仅用于当前项目的飞书消息通知，不写入默认配置。")
    create_p.add_argument("--business-goal")
    create_p.add_argument("--requires-frontend", dest="requires_frontend", action="store_true", default=True)
    create_p.add_argument("--no-requires-frontend", dest="requires_frontend", action="store_false")
    create_p.add_argument("--requires-backend", dest="requires_backend", action="store_true", default=True)
    create_p.add_argument("--no-requires-backend", dest="requires_backend", action="store_false")
    project_sub.add_parser("status")
    bugfix_p = project_sub.add_parser("bug-fix")
    bugfix_p.add_argument("--issue", required=True)
    bugfix_p.add_argument("--reporter")
    delete_p = project_sub.add_parser("delete")
    delete_p.add_argument("--no-backup", action="store_true")

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
    lark_cli = lark_sub.add_parser("cli")
    lark_cli.add_argument("args", nargs=argparse.REMAINDER)

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
            print_json({"ok": True, "project": initialize_project_workspace(overwrite_config=args.overwrite)})
            return 0
    if args.command == "workflow":
        if args.workflow_command == "list":
            print_json({"ok": True, "workflows": workflows()})
            return 0
        if args.workflow_command == "inspect":
            print_json({"ok": True, "workflow": inspect_workflow(args.workflow_id)})
            return 0
        if args.workflow_command == "status":
            print_json({"ok": True, "status": status(args.run_id)})
            return 0
        if args.workflow_command == "enqueue":
            print_json({"ok": True, "job": enqueue_step(args.run_id, args.step_id)})
            return 0
        if args.workflow_command == "submit-gate":
            print_json(submit_gate(args.run_id, args.step_id, _json_obj(args.data_json)))
            return 0
        if args.workflow_command == "watch":
            print_json(
                watch_run(
                    args.run_id,
                    timeout_seconds=args.timeout_seconds,
                    poll_interval_seconds=args.poll_interval_seconds,
                )
            )
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
                        lark_chat_id=args.lark_chat_id,
                        business_goal=args.business_goal,
                        requires_frontend=args.requires_frontend,
                        requires_backend=args.requires_backend,
                    ),
                }
            )
            return 0
        if args.project_command == "status":
            print_json({"ok": True, "status": current_project_status()})
            return 0
        if args.project_command == "bug-fix":
            print_json(request_bug_fix(issue=args.issue, reporter=args.reporter, source="cli"))
            return 0
        if args.project_command == "delete":
            print_json(delete_current_project(backup=not args.no_backup))
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
        if args.lark_command == "cli":
            cli_args = list(args.args or [])
            if cli_args and cli_args[0] == "--":
                cli_args = cli_args[1:]
            print_json(run_project_lark_cli(cli_args))
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
