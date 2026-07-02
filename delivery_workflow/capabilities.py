from __future__ import annotations

import shutil
from typing import Any


def command_available(name: str) -> bool:
    return shutil.which(name) is not None


def doctor() -> dict[str, Any]:
    codex = shutil.which("codex")
    return {
        "detected_platform": "codex" if codex else None,
        "commands": {"codex": bool(codex)},
        "execution": {
            "ok": bool(codex),
            "codex_binary": codex,
            "note": "当 code_platforms.enable_agent_cli 为 false 时，工作流只写入任务包和预备产物，不启动 Codex CLI。",
        },
    }
