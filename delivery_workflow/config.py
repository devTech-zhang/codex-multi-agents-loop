from __future__ import annotations

import json
from pathlib import Path
from typing import Any

WORKSPACE_CONFIG_NAME = "delivery-workflow.config.json"
PACKAGE_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = PACKAGE_ROOT.parent

DEFAULT_CONFIG_TEMPLATE: dict[str, Any] = {
    "storage": {
        "home": ".delivery-workflow",
        "db": ".delivery-workflow/delivery.db",
        "artifact_root": "delivery-projects",
    },
    "workflow": {
        "auto_start": True,
        "auto_run_to_gate": True,
    },
    "code_platforms": {
        "default": "codex",
        "frontend": "codex",
        "backend": "codex",
        "other": "codex",
        "enable_agent_cli": False,
        "executors": {
            "codex": {
                "binary_candidates": ["codex"],
                "command": ["{binary}", "exec", "--file", "{prompt_path}"],
            },
            "claude-code": {
                "binary_candidates": ["claude"],
                "command": ["{binary}", "-p"],
                "stdin_from_prompt": True,
            },
            "opencode": {
                "binary_candidates": ["opencode"],
                "command": ["{binary}", "run", "--file", "{prompt_path}"],
            },
        },
    },
    "lark": {
        "enabled": True,
        "identity": "bot",
        "chat_id": "",
        "dry_run": False,
        "send_step_notifications": True,
        "send_prd_approval_card": True,
        "create_prd_doc_without_chat": False,
        "prd_doc_title_template": "{project_title}PRD",
        "sender": {
            "mode": "current_process",
            "on_keychain_error": "host_escalation",
        },
        "sdk": {
            "credential_source": "lark-cli",
            "profile": "",
            "app_id": "",
            "app_secret": "",
            "log_level": "info",
        },
        "event": {
            "transport": "sdk_websocket",
        },
    },
}


def load_config() -> dict[str, Any]:
    path = workspace_config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"missing {WORKSPACE_CONFIG_NAME}; run `python3 -m delivery_workflow.cli config init` in the plugin workspace"
        )
    return _read_json(path)


def config_sources() -> list[str]:
    path = workspace_config_path()
    return [str(path)] if path.exists() else []


def workspace_config_path() -> Path:
    workspace_local = Path.cwd() / WORKSPACE_CONFIG_NAME
    if workspace_local.exists():
        return workspace_local
    return PLUGIN_ROOT / WORKSPACE_CONFIG_NAME


def write_workspace_config(*, overwrite: bool = False) -> Path:
    target = workspace_config_path()
    if target.exists() and not overwrite:
        raise FileExistsError(f"config already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(DEFAULT_CONFIG_TEMPLATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def code_platform_for_step(config: dict[str, Any], step_id: str | None = None, fallback: str | None = None) -> str:
    platforms = config.get("code_platforms") or {}
    if step_id is None:
        value = platforms.get("default")
    elif step_id == "frontend-development":
        value = platforms.get("frontend")
    elif step_id == "backend-development":
        value = platforms.get("backend")
    else:
        value = platforms.get("other") or platforms.get("default")
    return normalize_platform(value or fallback or platforms.get("default") or "codex")


def normalize_platform(platform: str | None) -> str:
    value = (platform or "codex").strip().lower().replace("_", "-")
    if value in {"claude", "claude-code"}:
        return "claude-code"
    if value in {"codex", "opencode"}:
        return value
    return value or "codex"


def lark_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("lark") or {}


def lark_chat_id(config: dict[str, Any], project_chat_id: str | None = None) -> str | None:
    value = project_chat_id or lark_config(config).get("chat_id")
    return str(value).strip() if value else None


def lark_dry_run(config: dict[str, Any]) -> bool:
    return bool(lark_config(config).get("dry_run"))


def lark_enabled(config: dict[str, Any]) -> bool:
    return bool(lark_config(config).get("enabled", True))


def lark_identity(config: dict[str, Any]) -> str:
    value = str(lark_config(config).get("identity") or "bot").strip().lower()
    return value if value in {"bot", "user"} else "bot"


def storage_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("storage") or {}


def storage_path(config: dict[str, Any], key: str, fallback: str) -> Path:
    value = storage_config(config).get(key) or fallback
    path = Path(str(value))
    return path if path.is_absolute() else Path.cwd() / path


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"config must be a JSON object: {path}")
    return data
