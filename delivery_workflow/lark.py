from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any


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
            body = f"<doc><title>{_xml_text(title)}</title>{body}</doc>" if body else f"<doc><title>{_xml_text(title)}</title></doc>"
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


def run_project_lark_cli(args: list[str], *, timeout: int = 120) -> dict[str, Any]:
    if not args:
        return {"ok": False, "error": "missing lark-cli arguments"}
    command = ["lark-cli", *args]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    result: dict[str, Any] = {"ok": completed.returncode == 0, "returncode": completed.returncode, "command": command}
    if completed.stdout:
        try:
            result["stdout_json"] = json.loads(completed.stdout)
        except json.JSONDecodeError:
            result["stdout"] = completed.stdout[-4000:]
    if completed.stderr:
        result["stderr"] = completed.stderr[-4000:]
    return result


def _is_keychain_unavailable(payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=False).lower()
    return "keychain" in text and ("not initialized" in text or "get failed" in text)
