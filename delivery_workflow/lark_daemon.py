from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

from .config import lark_config, lark_dry_run, lark_enabled, load_config
from .lark_sdk import sdk_preflight
from .paths import PLUGIN_ROOT, WORKFLOW_FILE, data_home
from .storage import now_iso


def ensure_lark_event_consumer(run_id: str | None = None) -> dict[str, Any]:
    config = load_config()
    lark = lark_config(config)
    event = lark.get("event") or {}
    if not lark_enabled(config):
        return _skipped("lark disabled by config")
    if lark_dry_run(config):
        return _skipped("lark dry_run is true")
    if str(event.get("transport") or "sdk_websocket") != "sdk_websocket":
        return _skipped("lark.event.transport is not sdk_websocket")
    if not bool(event.get("auto_start_consumer", True)):
        return _skipped("lark.event.auto_start_consumer is false")

    home = data_home()
    home.mkdir(parents=True, exist_ok=True)
    pid_file = home / "lark-event-consumer.pid.json"
    log_file = home / "lark-event-consumer.log"
    code_fingerprint = _code_fingerprint()

    existing = _read_pid_file(pid_file)
    if _pid_is_alive(existing.get("pid")):
        if existing.get("code_fingerprint") != code_fingerprint:
            restarted = _terminate_process(existing.get("pid"), expected_command=existing.get("command"))
            stopped = restarted and _wait_until_dead(existing.get("pid"), timeout_seconds=3.0)
            if restarted and not stopped:
                stopped = _kill_process(existing.get("pid")) and _wait_until_dead(existing.get("pid"), timeout_seconds=2.0)
            if not stopped:
                return {
                    "ok": False,
                    "running": True,
                    "started": False,
                    "pid": existing.get("pid"),
                    "workspace": existing.get("workspace"),
                    "reason": "lark event consumer is stale but could not be restarted",
                    "stale": True,
                    "expected_code_fingerprint": code_fingerprint,
                    "actual_code_fingerprint": existing.get("code_fingerprint"),
                    "log": str(log_file),
                }
            with log_file.open("a", encoding="utf-8") as log:
                log.write(
                    f"\n[{now_iso()}] restarting stale lark event consumer pid={existing.get('pid')} "
                    f"old_fingerprint={existing.get('code_fingerprint') or '<missing>'} "
                    f"new_fingerprint={code_fingerprint}\n"
                )
            time.sleep(0.2)
        else:
            return {
                "ok": True,
                "running": True,
                "started": False,
                "pid": existing["pid"],
                "workspace": existing.get("workspace"),
                "code_fingerprint": code_fingerprint,
                "log": str(log_file),
            }

    existing = _read_pid_file(pid_file)
    if _pid_is_alive(existing.get("pid")):
        return {
            "ok": True,
            "running": True,
            "started": False,
            "pid": existing["pid"],
            "workspace": existing.get("workspace"),
            "code_fingerprint": existing.get("code_fingerprint"),
            "log": str(log_file),
        }

    preflight = sdk_preflight(config)
    if not preflight.get("ok"):
        return {
            "ok": False,
            "running": False,
            "started": False,
            "reason": "lark sdk websocket preflight failed",
            "preflight": preflight,
            "log": str(log_file),
        }

    command = [str(PLUGIN_ROOT / "scripts" / "deliveryflow"), "lark", "event-consumer"]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"\n[{now_iso()}] starting lark event consumer for run {run_id or '<none>'}\n")
        process = subprocess.Popen(
            command,
            cwd=str(Path.cwd()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    time.sleep(0.2)
    if process.poll() is not None:
        return {
            "ok": False,
            "running": False,
            "started": False,
            "pid": process.pid,
            "returncode": process.returncode,
            "reason": "lark event consumer exited immediately",
            "command": command,
            "workspace": str(Path.cwd()),
            "log": str(log_file),
        }

    state = {
        "pid": process.pid,
        "workspace": str(Path.cwd()),
        "command": command,
        "started_at": now_iso(),
        "run_id": run_id,
        "code_fingerprint": code_fingerprint,
        "plugin_root": str(PLUGIN_ROOT),
    }
    pid_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "running": True, "started": True, "pid": process.pid, "workspace": str(Path.cwd()), "command": command, "code_fingerprint": code_fingerprint, "log": str(log_file)}


def _read_pid_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _pid_is_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if _process_is_zombie(pid):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_is_zombie(pid: int) -> bool:
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return "Z" in result.stdout.strip()


def _terminate_process(pid: Any, *, expected_command: Any = None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not _command_looks_like_consumer(expected_command) and not _process_looks_like_consumer(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return True


def _kill_process(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return True


def _process_looks_like_consumer(pid: int) -> bool:
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    command = result.stdout.strip()
    return _command_looks_like_consumer(command)


def _command_looks_like_consumer(command: Any) -> bool:
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    if not isinstance(command, str):
        return False
    return "deliveryflow" in command and "lark" in command and "event-consumer" in command


def _wait_until_dead(pid: Any, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_is_alive(pid)


def _code_fingerprint() -> str:
    files = [
        Path(__file__),
        PLUGIN_ROOT / "delivery_workflow" / "engine.py",
        PLUGIN_ROOT / "delivery_workflow" / "lark_events.py",
        PLUGIN_ROOT / "delivery_workflow" / "worker_daemon.py",
        WORKFLOW_FILE,
        PLUGIN_ROOT / "scripts" / "deliveryflow",
    ]
    digest = sha256()
    for path in files:
        digest.update(str(path.relative_to(PLUGIN_ROOT)).encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def _skipped(reason: str) -> dict[str, Any]:
    return {"ok": True, "running": False, "started": False, "skipped": True, "reason": reason}
