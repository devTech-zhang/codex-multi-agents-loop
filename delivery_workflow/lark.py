from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def lark_available() -> bool:
    return shutil.which("lark-cli") is not None


def create_doc_as_bot(title: str, markdown: str, *, identity: str = "bot", dry_run: bool = False) -> dict[str, Any]:
    command = [
        "lark-cli",
        "docs",
        "+create",
        "--as",
        identity,
        "--api-version",
        "v2",
        "--doc-format",
        "markdown",
        "--content",
        f"# {title}\n\n{markdown}",
    ]
    return _run_json(command, timeout=120, dry_run=dry_run)


def send_text_as_bot(chat_id: str, text: str, *, identity: str = "bot", dry_run: bool = False, idempotency_key: str | None = None) -> dict[str, Any]:
    command = [
        "lark-cli",
        "im",
        "+messages-send",
        "--as",
        identity,
        "--chat-id",
        chat_id,
        "--text",
        text,
    ]
    if idempotency_key:
        command.extend(["--idempotency-key", idempotency_key])
    return _run_json(command, timeout=60, dry_run=dry_run)


def send_approval_card_as_bot(
    *,
    chat_id: str,
    project_title: str,
    project_id: str,
    run_id: str,
    step_id: str,
    doc_url: str,
    identity: str = "bot",
    dry_run: bool = False,
    idempotency_key: str | None = None,
    workspace: str | None = None,
    approval_round: int | None = None,
) -> dict[str, Any]:
    card = build_prd_approval_card(
        project_title=project_title,
        project_id=project_id,
        run_id=run_id,
        step_id=step_id,
        doc_url=doc_url,
        workspace=workspace or str(Path.cwd()),
        approval_round=approval_round,
    )
    command = [
        "lark-cli",
        "im",
        "+messages-send",
        "--as",
        identity,
        "--chat-id",
        chat_id,
        "--msg-type",
        "interactive",
        "--content",
        json.dumps(card, ensure_ascii=False, separators=(",", ":")),
    ]
    if idempotency_key:
        command.extend(["--idempotency-key", idempotency_key])
    return _run_json(command, timeout=60, dry_run=dry_run)


def build_prd_approval_card(
    *,
    project_title: str,
    project_id: str,
    run_id: str,
    step_id: str,
    doc_url: str,
    workspace: str | None = None,
    approval_round: int | None = None,
) -> dict[str, Any]:
    action_value = {
        "project_id": project_id,
        "run_id": run_id,
        "step_id": step_id,
        "doc_url": doc_url,
    }
    if workspace:
        action_value["workspace"] = workspace
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": _prd_approval_title(project_title, approval_round)},
            "template": "blue",
        },
        "elements": [
            {"tag": "markdown", "content": f"**项目**：{project_title}\n\n**项目 ID**：`{project_id}`\n\n请查看 PRD v2 文档后审批。"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看 PRD 文档"},
                        "type": "default",
                        "url": doc_url,
                    },
                ],
            },
            {
                "tag": "form",
                "name": "prd_approval_form",
                "elements": [
                    {
                        "tag": "input",
                        "name": "reject_reason",
                        "required": False,
                        "placeholder": {"tag": "plain_text", "content": "拒绝时请填写理由；通过可留空"},
                        "input_type": "multiline_text",
                        "rows": 3,
                        "max_length": 1000,
                    },
                    {
                        "tag": "button",
                        "action_type": "form_submit",
                        "name": "approve_prd_button",
                        "text": {"tag": "plain_text", "content": "通过"},
                        "type": "primary",
                        "value": {**action_value, "action": "approve_prd", "approved": True},
                    },
                    {
                        "tag": "button",
                        "action_type": "form_submit",
                        "name": "reject_prd_button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {**action_value, "action": "reject_prd", "approved": False},
                    },
                ],
            },
        ],
    }


def _prd_approval_title(project_title: str, approval_round: int | None) -> str:
    if approval_round and approval_round > 1:
        return f"{project_title} PRD 第 {approval_round} 轮复审"
    return f"{project_title} PRD 第 1 轮审批"


def build_prd_approval_resolved_card(
    *,
    project_title: str,
    project_id: str,
    run_id: str,
    doc_url: str,
    approved: bool,
    reason: str,
    approver: str,
    approval_round: int | None = None,
) -> dict[str, Any]:
    status_text = "该审批已通过" if approved else "该审批已拒绝"
    template = "green" if approved else "red"
    reason_text = reason or ("通过" if approved else "未填写")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": _prd_approval_title(project_title, approval_round)},
            "template": template,
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**项目**：{project_title}\n\n"
                    f"**项目 ID**：`{project_id}`\n\n"
                    f"**Run ID**：`{run_id}`\n\n"
                    f"**状态**：{status_text}\n\n"
                    f"**理由**：{reason_text}\n\n"
                    f"**操作人**：`{approver}`"
                ),
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看 PRD 文档"},
                        "type": "default",
                        "url": doc_url,
                    }
                ],
            },
        ],
    }


def extract_doc_url(result: dict[str, Any]) -> str | None:
    for key in ("url", "document_url"):
        value = result.get(key) if isinstance(result, dict) else None
        if isinstance(value, str) and value:
            return value
    found = _find_url(result)
    if found:
        return found
    return None


def _find_url(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("url", "document_url", "share_url"):
            item = value.get(key)
            if isinstance(item, str) and item.startswith(("http://", "https://")):
                return item
        for item in value.values():
            found = _find_url(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_url(item)
            if found:
                return found
    return None


def _run_json(command: list[str], *, timeout: int, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"ok": True, "dry_run": True, "command": command}
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    payload: dict[str, Any]
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    payload.setdefault("ok", completed.returncode == 0)
    payload["returncode"] = completed.returncode
    payload["command"] = command
    if completed.stderr:
        payload["stderr"] = completed.stderr[-4000:]
    if completed.stdout and not payload:
        payload["stdout"] = completed.stdout[-4000:]
    if _is_keychain_unavailable(payload):
        payload["error_type"] = "keychain_unavailable"
        payload["remediation"] = "当前沙箱无法访问 macOS Keychain。Workflow 应返回 host_escalation 元信息，由宿主 Codex 请求在沙箱外执行同一飞书动作，或由常驻原生 worker 发送。"
    return payload


def _is_keychain_unavailable(payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=False).lower()
    return "keychain" in text and ("not initialized" in text or "get failed" in text)
