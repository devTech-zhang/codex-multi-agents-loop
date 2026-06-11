from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

WORKSPACE_CONFIG_NAME = "workflow.config.json"
PLUGIN_CONFIG_NAME = "delivery-workflow.config.json"
PACKAGE_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = PACKAGE_ROOT.parent

DEFAULT_CONFIG_TEMPLATE: dict[str, Any] = {
    "storage": {
        "home": ".delivery-workflow",
        "db": ".delivery-workflow/delivery.db",
        "artifact_root": "delivery-project",
        "source_root": "source-code",
        "logs": ".delivery-workflow/logs",
    },
    "quality_gate": {
        "block": 0,
        "critical": 0,
        "major": 2,
        "minor": 5,
    },
    "workflow": {
        "auto_start": True,
        "auto_run_to_gate": True,
        "continue_after_gate": True,
        "continue_after_gate_max_jobs": 50,
        "mcp_auto_watch_after_create": True,
        "watch_timeout_seconds": 7200,
        "watch_poll_interval_seconds": 2.0,
    },
    "code_platforms": {
        "auto_detect": True,
        "default": "auto",
        "frontend": "auto",
        "backend": "auto",
        "other": "auto",
        "enable_agent_cli": False,
        "executors": {
            "codex": {
                "binary_candidates": ["codex"],
                "command": ["{binary}", "exec", "--file", "{prompt_path}"],
            },
            "claude-code": {
                "binary_candidates": ["claude"],
                "command": ["{binary}", "--disable-slash-commands", "--permission-mode", "acceptEdits", "-p"],
                "stdin_from_prompt": True,
            },
        },
    },
    "lark": {
        "enabled": True,
        "identity": "bot",
        "chat_id": "",
    },
}


def load_config() -> dict[str, Any]:
    path = workspace_config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"missing {WORKSPACE_CONFIG_NAME}; run `python3 -m delivery_workflow.cli config init` in the project workspace"
        )
    config = _read_json(path)
    return config


def config_sources() -> list[str]:
    path = workspace_config_path()
    return [str(path)] if path.exists() else []


def workspace_config_path() -> Path:
    for candidate in (Path.cwd() / WORKSPACE_CONFIG_NAME, Path.cwd() / PLUGIN_CONFIG_NAME):
        if candidate.exists():
            return candidate
    return PLUGIN_ROOT / PLUGIN_CONFIG_NAME


def write_workspace_config(*, overwrite: bool = False) -> Path:
    target = Path.cwd() / WORKSPACE_CONFIG_NAME
    if target.exists() and not overwrite:
        raise FileExistsError(f"config already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source = PLUGIN_ROOT / PLUGIN_CONFIG_NAME
    template = _read_json(source) if source.exists() else DEFAULT_CONFIG_TEMPLATE
    target.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def initialize_project_workspace(*, overwrite_config: bool = False) -> dict[str, Any]:
    from .workflow_log import log_workflow

    log_workflow("project_workspace.init.started", "开始初始化当前项目工作区。", payload={"overwrite_config": overwrite_config})
    config_path = Path.cwd() / WORKSPACE_CONFIG_NAME
    if overwrite_config or not config_path.exists():
        config_path = write_workspace_config(overwrite=overwrite_config)
    from .storage import init_db
    from .paths import artifact_root, log_root, source_root

    db = init_db()
    result = {
        "config_path": str(config_path),
        "db_path": str(db),
        "home": str(db.parent),
        "logs": str(log_root()),
        "artifact_root": str(artifact_root()),
        "source_root": str(source_root()),
    }
    log_workflow("project_workspace.init.completed", "当前项目工作区初始化完成。", payload=result)
    return result


def code_platform_for_step(config: dict[str, Any], step_id: str | None = None, fallback: str | None = None) -> str:
    platforms = config.get("code_platforms") or {}
    auto_detect = platforms.get("auto_detect", True)
    if step_id is None:
        value = platforms.get("default")
    elif step_id == "frontend-development":
        value = platforms.get("frontend")
    elif step_id == "backend-development":
        value = platforms.get("backend")
    else:
        value = platforms.get("other") or platforms.get("default")
    raw = value or fallback or platforms.get("default") or "auto"
    if raw == "auto" and not auto_detect:
        return "codex"
    return normalize_platform(raw)


def normalize_platform(platform: str | None) -> str:
    value = (platform or "auto").strip().lower().replace("_", "-")
    if value in {"claude", "claude-code"}:
        return "claude-code"
    if value in {"codex"}:
        return value
    if value == "auto":
        return detect_platform()
    return detect_platform()


def detect_platform() -> str:
    """Auto-detect available platform CLI. Priority: claude → codex."""
    for cmd, result in (("claude", "claude-code"), ("codex", "codex")):
        if shutil.which(cmd):
            return result
    return "codex"


def lark_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("lark") or {}


def lark_chat_id(config: dict[str, Any], project_chat_id: str | None = None) -> str | None:
    value = project_chat_id or lark_config(config).get("chat_id")
    return str(value).strip() if value else None


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


def quality_gate_config(config: dict[str, Any]) -> dict[str, int]:
    raw = config.get("quality_gate") or {}
    return {
        "block": int(raw.get("block", 0)),
        "critical": int(raw.get("critical", 0)),
        "major": int(raw.get("major", 2)),
        "minor": int(raw.get("minor", 5)),
    }


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"config must be a JSON object: {path}")
    return data
