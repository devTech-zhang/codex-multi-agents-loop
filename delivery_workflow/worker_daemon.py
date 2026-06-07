from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .config import load_config
from .paths import PLUGIN_ROOT, data_home
from .storage import now_iso


def start_worker_continuation(run_id: str, *, reason: str = "gate-submitted") -> dict[str, Any]:
    config = load_config()
    workflow = config.get("workflow") or {}
    if not bool(workflow.get("continue_after_gate", True)):
        return {"ok": True, "started": False, "running": False, "skipped": True, "reason": "workflow.continue_after_gate is false"}

    max_jobs = int(workflow.get("continue_after_gate_max_jobs") or 50)
    home = data_home()
    home.mkdir(parents=True, exist_ok=True)
    safe_run_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in run_id)
    pid_file = home / f"worker-{safe_run_id}.pid.json"
    log_file = home / f"worker-{safe_run_id}.log"

    existing = _read_pid_file(pid_file)
    if _pid_is_alive(existing.get("pid")):
        return {
            "ok": True,
            "started": False,
            "running": True,
            "pid": existing["pid"],
            "workspace": existing.get("workspace"),
            "log": str(log_file),
        }

    command = [
        str(PLUGIN_ROOT / "scripts" / "deliveryflow"),
        "worker",
        "until-blocked",
        "--run-id",
        run_id,
        "--max-jobs",
        str(max_jobs),
    ]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"\n[{now_iso()}] starting worker continuation for run {run_id}; reason={reason}\n")
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

    state = {
        "pid": process.pid,
        "workspace": str(Path.cwd()),
        "command": command,
        "started_at": now_iso(),
        "run_id": run_id,
        "reason": reason,
    }
    pid_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "started": True, "running": True, "pid": process.pid, "workspace": str(Path.cwd()), "command": command, "log": str(log_file)}


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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
