from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import lark_config


@dataclass(frozen=True)
class LarkSdkCredentials:
    app_id: str
    app_secret: str
    source: str
    profile: str | None = None


class LarkSdkConfigError(RuntimeError):
    pass


def sdk_preflight(config: dict[str, Any]) -> dict[str, Any]:
    package_ok = importlib.util.find_spec("lark_oapi") is not None
    try:
        credentials = load_sdk_credentials(config)
    except LarkSdkConfigError as exc:
        credentials = None
        credential_error = str(exc)
    else:
        credential_error = ""

    problems: list[str] = []
    if not package_ok:
        problems.append("Python SDK lark-oapi is not installed")
    if credential_error:
        problems.append(credential_error)

    return {
        "ok": not problems,
        "package": {
            "ok": package_ok,
            "import": "lark_oapi",
            "install_command": ["uv", "pip", "install", "--python", ".venv/bin/python", "lark-oapi"],
        },
        "credentials": _credential_report(credentials) if credentials else {"ok": False, "error": credential_error},
        "transport": "sdk_websocket",
        "event": "card.action.trigger",
        "problems": problems,
    }


def load_sdk_credentials(config: dict[str, Any]) -> LarkSdkCredentials:
    sdk = _sdk_config(config)
    explicit_app_id = str(sdk.get("app_id") or "").strip()
    explicit_secret = str(sdk.get("app_secret") or "").strip()
    if explicit_app_id and explicit_secret:
        return LarkSdkCredentials(app_id=explicit_app_id, app_secret=explicit_secret, source="delivery-workflow.config.json")

    source = str(sdk.get("credential_source") or "lark-cli").strip().lower()
    if source != "lark-cli":
        raise LarkSdkConfigError(f"unsupported lark.sdk.credential_source: {source}")

    app = _select_lark_cli_app(profile=str(sdk.get("profile") or "").strip(), app_id=explicit_app_id)
    if not app:
        raise LarkSdkConfigError("cannot find lark-cli app config; run `lark-cli config init --new` first")
    app_id = str(app.get("appId") or app.get("app_id") or "").strip()
    app_secret = str(app.get("appSecret") or app.get("app_secret") or "").strip()
    if explicit_app_id and app_id != explicit_app_id:
        raise LarkSdkConfigError(f"lark-cli app_id mismatch: expected {explicit_app_id}, got {app_id or '<empty>'}")
    if not app_id:
        raise LarkSdkConfigError("lark-cli app config is missing appId")
    if not app_secret or app_secret == "****":
        raise LarkSdkConfigError("lark-cli app config is missing readable appSecret; set lark.sdk.app_secret or make lark-cli credentials readable")
    return LarkSdkCredentials(app_id=app_id, app_secret=app_secret, source="lark-cli", profile=app_id)


def sdk_log_level(config: dict[str, Any]) -> str:
    return str(_sdk_config(config).get("log_level") or "info").strip().lower()


def _sdk_config(config: dict[str, Any]) -> dict[str, Any]:
    return lark_config(config).get("sdk") or {}


def _select_lark_cli_app(*, profile: str, app_id: str) -> dict[str, Any] | None:
    path = Path.home() / ".lark-cli" / "config.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    apps = data.get("apps") or []
    if not isinstance(apps, list):
        return None

    for app in apps:
        if not isinstance(app, dict):
            continue
        current_app_id = str(app.get("appId") or app.get("app_id") or "").strip()
        if app_id and current_app_id == app_id:
            return app
        if profile and current_app_id == profile:
            return app
    for app in apps:
        if isinstance(app, dict):
            return app
    return None


def _credential_report(credentials: LarkSdkCredentials) -> dict[str, Any]:
    return {
        "ok": True,
        "source": credentials.source,
        "profile": credentials.profile,
        "app_id": _mask_app_id(credentials.app_id),
    }


def _mask_app_id(app_id: str) -> str:
    if len(app_id) <= 8:
        return app_id
    return f"{app_id[:6]}...{app_id[-4:]}"
