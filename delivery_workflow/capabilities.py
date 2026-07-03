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
            "note": "真实子 Agent 执行依赖 Codex runtime 的 spawn_agent；MCP 只负责状态流转和产物归档。",
        },
    }
