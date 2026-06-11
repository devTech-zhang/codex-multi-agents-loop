from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .config import _read_dotenv, lark_config, load_config


def lark_available() -> bool:
    return shutil.which("lark-cli") is not None


def create_doc_as_bot(
    title: str,
    content: str,
    *,
    identity: str = "bot",
    dry_run: bool = False,
    doc_format: str = "markdown",
) -> dict[str, Any]:
    command = [
        "lark-cli",
        "docs",
        "+create",
        "--as",
        identity,
        "--api-version",
        "v2",
    ]
    content_path = _write_doc_content_file(title, content, doc_format=doc_format)
    command.extend(["--doc-format", doc_format, "--content", _content_file_arg(content_path)])
    return _run_json(command, timeout=120, dry_run=dry_run)


def _write_doc_content_file(title: str, content: str, *, doc_format: str) -> Path:
    root = Path.cwd() / ".delivery-workflow" / "tmp" / "lark-docs"
    root.mkdir(parents=True, exist_ok=True)
    suffix = ".md" if doc_format == "markdown" else ".xml"
    path = root / f"doc-{uuid.uuid4().hex}{suffix}"
    if doc_format == "markdown":
        body = content.strip()
        if body.startswith(f"# {title}\n") or body == f"# {title}":
            pass
        else:
            body = f"# {title}\n\n{body}" if body else f"# {title}"
    else:
        body = content.strip()
        if "<title" not in body[:500]:
            body = f"<title>{_xml_text(title)}</title>\n\n{body}" if body else f"<title>{_xml_text(title)}</title>"
    path.write_text(body, encoding="utf-8")
    return path


def _content_file_arg(path: Path) -> str:
    try:
        content_path = path.relative_to(Path.cwd())
    except ValueError:
        content_path = path
    return f"@{content_path}"


def _xml_text(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
    env = _lark_cli_env()
    if dry_run:
        payload: dict[str, Any] = {"ok": True, "dry_run": True, "command": command}
        env_report = _lark_cli_env_report(env)
        if env_report:
            payload["env"] = env_report
        return payload
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout, env=env)
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


def _lark_cli_env() -> dict[str, str] | None:
    try:
        sdk = lark_config(load_config()).get("sdk") or {}
    except Exception:
        sdk = {}
    project_env = _read_dotenv(Path.cwd() / ".env")
    sdk = dict(sdk)
    if project_env.get("LARK_APP_ID"):
        sdk["app_id"] = project_env["LARK_APP_ID"]
    if project_env.get("LARK_APP_SECRET"):
        sdk["app_secret"] = project_env["LARK_APP_SECRET"]
    app_id = str(sdk.get("app_id") or "").strip()
    app_secret = str(sdk.get("app_secret") or "").strip()
    if not app_id and not app_secret:
        return None
    env = os.environ.copy()
    if app_id:
        env["LARK_APP_ID"] = app_id
    if app_secret:
        env["LARK_APP_SECRET"] = app_secret
    return env


def _lark_cli_env_report(env: dict[str, str] | None) -> dict[str, str]:
    if not env:
        return {}
    report: dict[str, str] = {}
    if env.get("LARK_APP_ID"):
        report["LARK_APP_ID"] = _mask_app_id(env["LARK_APP_ID"])
    if env.get("LARK_APP_SECRET"):
        report["LARK_APP_SECRET"] = "***"
    return report


def _mask_app_id(app_id: str) -> str:
    if len(app_id) <= 8:
        return app_id
    return f"{app_id[:6]}...{app_id[-4:]}"


def _is_keychain_unavailable(payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=False).lower()
    return "keychain" in text and ("not initialized" in text or "get failed" in text)
