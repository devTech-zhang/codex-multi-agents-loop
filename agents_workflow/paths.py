from __future__ import annotations

from pathlib import Path

from .config import (
    GLOBAL_MEMORY_DIR,
    PROJECT_ARTIFACT_DIR,
    PROJECT_SCRATCH_DIR,
    PROJECT_WORKFLOW_DIR,
    load_config,
    storage_path,
    workspace_root,
)


PACKAGE_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = PACKAGE_ROOT.parent
WORKFLOW_ROOT = PLUGIN_ROOT / "workflow"
AGENT_ROOT = PLUGIN_ROOT / "agents"
DEFAULT_WORKFLOW_ID = "codex-multi-agents-loop"


def data_home() -> Path:
    return storage_path(load_config(), "home", PROJECT_WORKFLOW_DIR)


def db_path() -> Path:
    return storage_path(load_config(), "db", f"{PROJECT_WORKFLOW_DIR}/workflow.sqlite3")


def artifact_root() -> Path:
    return storage_path(load_config(), "artifact_root", PROJECT_ARTIFACT_DIR)


def project_root() -> Path:
    return workspace_root()


def scratch_root() -> Path:
    return storage_path(load_config(), "scratch_root", PROJECT_SCRATCH_DIR)


def log_root() -> Path:
    return storage_path(load_config(), "logs", f"{PROJECT_WORKFLOW_DIR}/logs")


def memory_root() -> Path:
    return storage_path(load_config(), "memory_root", f"{PROJECT_WORKFLOW_DIR}/memory")


def global_memory_root() -> Path:
    return storage_path(load_config(), "global_memory_root", GLOBAL_MEMORY_DIR)
