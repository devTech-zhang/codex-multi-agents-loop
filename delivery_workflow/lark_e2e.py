from __future__ import annotations

import argparse
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from .capabilities import doctor
from .config import lark_config, load_config
from .engine import (
    _delete_project_records,
    create_project,
    execute_step,
    read_artifact,
    status,
    write_artifact,
)
from .lark import extract_doc_url
from .lark_sdk import sdk_preflight
from .paths import artifact_root


@dataclass(frozen=True)
class SmokeCase:
    action: str
    approved: bool
    title_suffix: str
    instruction: str


CASES = {
    "approve": SmokeCase(
        action="approve_prd",
        approved=True,
        title_suffix="通过",
        instruction="请在飞书群里点击这张测试卡片的“通过”按钮。",
    ),
    "reject": SmokeCase(
        action="reject_prd",
        approved=False,
        title_suffix="驳回",
        instruction="请在飞书群里点击这张测试卡片的“拒绝”按钮。",
    ),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lark-e2e-smoke")
    parser.add_argument("--title-prefix", default="飞书链路测试", help="测试项目标题前缀。")
    parser.add_argument("--timeout", type=int, default=300, help="每张卡片等待点击事件的秒数。")
    parser.add_argument(
        "--actions",
        default="approve,reject",
        help="要测试的动作，逗号分隔：approve,reject。",
    )
    parser.add_argument("--cleanup", action="store_true", help="测试结束后删除创建的 delivery 项目和产物。")
    parser.add_argument(
        "--live",
        action="store_true",
        help="真实创建飞书文档、发送飞书消息并等待长连接事件；未加该参数时只做本地预检。",
    )
    args = parser.parse_args(argv)

    actions = _parse_actions(args.actions)
    if not args.live:
        preflight = _preflight()
        _print(
            {
                "ok": preflight["ok"],
                "dry_run": True,
                "message": "这是会真实创建飞书文档、发送审批卡片、等待人工点击的验收脚本。确认要跑真实链路时加 --live。",
                "example": "scripts/lark-e2e-smoke --live --actions approve,reject",
                "preflight": preflight,
            }
        )
        return 0 if preflight["ok"] else 1

    try:
        result = run_live_smoke(actions=actions, title_prefix=args.title_prefix, timeout=args.timeout, cleanup=args.cleanup)
    except Exception as exc:
        _print({"ok": False, "error": str(exc)})
        return 1
    _print(result)
    return 0 if result.get("ok") else 1


def run_live_smoke(*, actions: list[str], title_prefix: str, timeout: int, cleanup: bool) -> dict[str, Any]:
    config = load_config()
    preflight = _preflight(config)
    if not preflight["ok"]:
        return {"ok": False, "stage": "preflight", "preflight": preflight}

    consumer = _EventConsumer()
    consumer.start()
    created_project_ids: list[str] = []
    case_results: list[dict[str, Any]] = []
    try:
        for action_name in actions:
            case = CASES[action_name]
            case_result = _run_case(case=case, title_prefix=title_prefix, timeout=timeout, consumer=consumer)
            case_results.append(case_result)
            created_project_ids.append(case_result["project_id"])
            if not case_result.get("ok"):
                return {"ok": False, "stage": action_name, "preflight": preflight, "cases": case_results}
    finally:
        consumer.stop()
        if cleanup and created_project_ids:
            try:
                for project_id in created_project_ids:
                    _delete_project_records(project_id)
                if artifact_root().exists():
                    shutil.rmtree(artifact_root())
            except Exception as exc:
                case_results.append({"ok": False, "stage": "cleanup", "project_ids": created_project_ids, "error": str(exc)})

    return {"ok": True, "preflight": preflight, "cases": case_results}


def _run_case(*, case: SmokeCase, title_prefix: str, timeout: int, consumer: "_EventConsumer") -> dict[str, Any]:
    ts = time.strftime("%Y%m%d-%H%M%S")
    title = f"{title_prefix}-{case.title_suffix}-{ts}"
    created = create_project(
        requirement=f"{title}：验证飞书文档、审批卡片、长连接事件和 Gate 提交流程。",
        title=title,
        auto_start=False,
        auto_run_to_gate=False,
        business_goal="验证飞书端到端链路",
        requires_frontend=False,
        requires_backend=False,
    )
    run_id = created["run_id"]
    project_id = created["project_id"]
    write_artifact(
        run_id,
        "prd_v2",
        _test_prd(title, case.title_suffix, run_id),
        category="prd",
        created_by="lark-e2e",
    )
    step_result = execute_step(run_id, "prd-approval")
    doc = json.loads(read_artifact(run_id, "prd_v2_lark_doc")["content"])
    card = json.loads(read_artifact(run_id, "prd_approval_card_message")["content"])
    doc_url = doc.get("url") or extract_doc_url(doc.get("result") or {})
    card_result = card.get("result") or {}
    case_info = {
        "ok": bool(card_result.get("ok")),
        "action": case.action,
        "project_id": project_id,
        "run_id": run_id,
        "title": title,
        "doc_url": doc_url,
        "step_result": step_result,
        "card_result": card_result,
        "instruction": case.instruction,
    }
    _print(case_info)
    if not card_result.get("ok"):
        return case_info

    handled = consumer.wait_for(run_id=run_id, action=case.action, timeout=timeout)
    if not handled:
        case_info.update({"ok": False, "stage": "wait_event", "error": f"{timeout}s 内未收到 {case.action} 长连接事件"})
        return case_info

    current = status(run_id)
    gate = next((item for item in current["gates"] if item["step_id"] == "prd-approval"), None)
    gate_data = json.loads(gate["data_json"] or "{}") if gate else {}
    case_info.update(
        {
            "ok": bool(handled.get("ok")) and gate_data.get("approved") is case.approved,
            "consumer_result": handled,
            "gate": gate,
        }
    )
    return case_info


class _EventConsumer:
    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.results: queue.Queue[dict[str, Any]] = queue.Queue()

    def start(self) -> None:
        command = [sys.executable, "-m", "delivery_workflow.cli", "lark", "event-consumer"]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        threading.Thread(target=self._read_stdout, args=(self.process.stdout,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self.process.stderr,), daemon=True).start()
        self._wait_ready(command)

    def stop(self) -> None:
        process = self.process
        if not process:
            return
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)

    def wait_for(self, *, run_id: str, action: str, timeout: int) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = self.results.get(timeout=min(1, max(0.1, deadline - time.monotonic())))
            except queue.Empty:
                continue
            gate = result.get("gate") or {}
            if result.get("stage") == "card_action_handled" and gate.get("run_id") == run_id and result.get("action") == action:
                return result
        return None

    def _wait_ready(self, command: list[str]) -> None:
        deadline = time.monotonic() + 30
        seen: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            if self.process and self.process.poll() is not None:
                raise RuntimeError(f"lark event consumer exited early: {seen[-10:]}")
            try:
                item = self.results.get(timeout=0.5)
            except queue.Empty:
                continue
            seen.append(item)
            if item.get("stage") == "sdk_event_consumer_ready":
                _print({"ok": True, "stage": "event_consumer_ready", "command": command})
                return
        raise RuntimeError(f"lark event consumer not ready in 30s: {seen[-10:]}")

    def _read_stdout(self, stream: Any) -> None:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                self.results.put(json.loads(line))
            except json.JSONDecodeError:
                _print({"ok": False, "stage": "event_stdout", "error": "invalid json", "line": line[-500:]})

    def _read_stderr(self, stream: Any) -> None:
        for line in stream:
            line = line.rstrip("\n")
            _print({"ok": True, "stage": "event_stderr", "line": line})


def _preflight(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    lark = lark_config(config)
    capability = doctor()
    sdk_report = sdk_preflight(config)
    problems = []
    if not lark.get("enabled", True):
        problems.append("lark.enabled is false")
    if not lark.get("chat_id"):
        problems.append("lark.chat_id is missing")
    if lark.get("dry_run"):
        problems.append("lark.dry_run is true; live E2E requires real Lark calls")
    if not capability["lark_doc"]["ok"]:
        problems.append("lark docs capability is not ready")
    if not capability["commands"].get("lark-cli"):
        problems.append("lark-cli is not available")
    if not sdk_report.get("ok"):
        problems.extend(sdk_report.get("problems") or ["lark sdk websocket is not ready"])
    return {
        "ok": not problems,
        "problems": problems,
        "chat_id": lark.get("chat_id"),
        "event_report": sdk_report,
        "doctor": capability,
    }


def _parse_actions(value: str) -> list[str]:
    actions = [item.strip() for item in value.split(",") if item.strip()]
    invalid = [item for item in actions if item not in CASES]
    if invalid:
        raise SystemExit(f"unsupported actions: {', '.join(invalid)}")
    return actions or ["approve", "reject"]


def _test_prd(title: str, branch: str, run_id: str) -> str:
    return f"""# {title} PRD

## 测试目标

验证飞书链路是否完整可用：

- 创建飞书文档
- 发送文档链接通知
- 发送 PRD 审批卡片
- 通过长连接接收按钮事件
- 将按钮事件转成 `prd-approval` Gate 提交

## 分支

本卡片用于测试：{branch}

## Run

`{run_id}`
"""


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
