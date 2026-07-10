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
            "note": "真实子 Agent 执行依赖当前项目的 .codex/agents 和 .codex/config.toml 注册层；支持用户显式 @ 或主管按 name 主动 spawn，MCP 负责领取、状态、记忆和产物。",
        },
    }
