from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import load_config, normalize_platform


@dataclass(frozen=True)
class PlatformCommand:
    executor: str
    command: list[str]
    available: bool
    enabled: bool
    input_text: str | None = None


def select_dev_executor(platform: str) -> str:
    normalized = normalize_platform(platform)
    return "codex"


def build_agent_command(platform: str, prompt_path: Path) -> PlatformCommand:
    config = load_config()
    normalized = normalize_platform(platform)
    executor = select_dev_executor(normalized)
    enabled = bool((config.get("code_platforms") or {}).get("enable_agent_cli"))
    command_key = "codex"
    command_config = ((config.get("code_platforms") or {}).get("executors") or {}).get(command_key, {})
    binary_candidates = command_config.get("binary_candidates") or _default_binary_candidates(command_key)
    binary = _first_available_binary(binary_candidates)
    binary_name = binary or str(binary_candidates[0])
    template = command_config.get("command") or _default_command_template(command_key)
    prompt_text = prompt_path.read_text(encoding="utf-8") if _uses_prompt_text(command_config, template) else None
    command = [
        str(part).format(binary=binary_name, prompt_path=str(prompt_path), prompt_text=prompt_text or "")
        for part in template
    ]
    input_text = prompt_path.read_text(encoding="utf-8") if bool(command_config.get("stdin_from_prompt")) else None
    return PlatformCommand(executor=executor, command=command, available=bool(binary), enabled=enabled, input_text=input_text)


def maybe_run_command(command: PlatformCommand) -> dict[str, Any]:
    if not command.enabled:
        return {"executed": False, "reason": "code_platforms.enable_agent_cli is false", "command": command.command}
    if not command.available:
        return {"executed": False, "reason": f"{command.executor} CLI is not available", "command": command.command}
    completed = subprocess.run(
        command.command,
        check=False,
        capture_output=True,
        text=True,
        timeout=1800,
        input=command.input_text,
    )
    result = {
        "executed": True,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr[-12000:],
        "command": command.command,
    }
    if execution_needs_permission(result):
        result["blocked"] = True
        result["reason"] = "agent CLI requested interactive write/permission approval"
    return result


def execution_needs_permission(execution: dict[str, Any]) -> bool:
    text = "\n".join(str(execution.get(key) or "") for key in ("stdout", "stderr")).lower()
    needles = [
        "等待你的写入审批",
        "等待写入审批",
        "审批写入",
        "需要你的审批",
        "需要您批准",
        "需要批准",
        "请批准",
        "need your approval to write",
        "i need your approval to write",
        "requires your approval",
        "need your approval",
        "awaiting user approval",
        "awaiting approval",
        "after approval",
        "waiting for approval",
        "permission denied",
    ]
    return any(needle in text for needle in needles)


def _first_available_binary(candidates: list[str]) -> str | None:
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _default_binary_candidates(platform: str) -> list[str]:
    return ["codex"]


def _default_command_template(platform: str) -> list[str]:
    return ["{binary}", "exec", "--file", "{prompt_path}"]


def _uses_prompt_text(command_config: dict[str, Any], template: list[str]) -> bool:
    if bool(command_config.get("stdin_from_prompt")):
        return False
    return any("{prompt_text}" in str(part) for part in template)
