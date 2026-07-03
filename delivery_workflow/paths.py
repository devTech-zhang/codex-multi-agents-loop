from __future__ import annotations

from pathlib import Path

from .config import load_config, storage_path


PACKAGE_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = PACKAGE_ROOT.parent
WORKFLOW_ROOT = PLUGIN_ROOT / "workflow"
AGENT_ROOT = PLUGIN_ROOT / "agents"
DEFAULT_WORKFLOW_ID = "codex-delivery-workflow"


def data_home() -> Path:
    return storage_path(load_config(), "home", ".codex/delivery-workflow")


def db_path() -> Path:
    return storage_path(load_config(), "db", ".codex/delivery-workflow/workflow.sqlite3")


def artifact_root() -> Path:
    return storage_path(load_config(), "artifact_root", "docs/delivery")


def source_root() -> Path:
    return storage_path(load_config(), "source_root", ".codex/delivery-workflow/workspace")


def log_root() -> Path:
    return storage_path(load_config(), "logs", ".codex/delivery-workflow/logs")


def memory_root() -> Path:
    return storage_path(load_config(), "memory_root", ".codex/delivery-workflow/memory")
