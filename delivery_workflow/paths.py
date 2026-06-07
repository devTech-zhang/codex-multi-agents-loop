from __future__ import annotations

from pathlib import Path

from .config import load_config, storage_path


PACKAGE_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = PACKAGE_ROOT.parent
WORKFLOW_FILE = PACKAGE_ROOT / "workflow.yaml"
DEFAULT_WORKFLOW_ID = "delivery-workflow"


def data_home() -> Path:
    return storage_path(load_config(), "home", ".delivery-workflow")


def db_path() -> Path:
    return storage_path(load_config(), "db", ".delivery-workflow/delivery.db")


def artifact_root() -> Path:
    return storage_path(load_config(), "artifact_root", "delivery-project")


def source_root() -> Path:
    return storage_path(load_config(), "source_root", "source-code")


def log_root() -> Path:
    return storage_path(load_config(), "logs", ".delivery-workflow/logs")
