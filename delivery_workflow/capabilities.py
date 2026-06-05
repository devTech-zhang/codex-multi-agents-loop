from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


LARK_DOC_SKILL = Path.home() / ".agents" / "skills" / "lark-doc" / "SKILL.md"
LARK_AI_AGENT_STEPS = [
    {
        "step": 1,
        "name": "安装 AI Agent 能力",
        "command": ["npx", "@larksuite/cli@latest", "install"],
        "requires_user_approval": True,
    },
    {
        "step": 2,
        "name": "配置应用凭证",
        "command": ["lark-cli", "config", "init", "--new"],
        "requires_browser": True,
    },
    {
        "step": 3,
        "name": "登录授权",
        "command": ["lark-cli", "auth", "login", "--recommend"],
        "requires_browser": True,
    },
    {
        "step": 4,
        "name": "验证授权状态",
        "command": ["lark-cli", "auth", "status"],
    },
]


def command_available(name: str) -> bool:
    return shutil.which(name) is not None


def lark_doc_capability() -> dict[str, Any]:
    cli = shutil.which("lark-cli")
    result: dict[str, Any] = {
        "ok": False,
        "lark_cli": cli,
        "docs_help": False,
        "lark_doc_skill": str(LARK_DOC_SKILL) if LARK_DOC_SKILL.exists() else None,
        "install_command": ["npx", "@larksuite/cli@latest", "install"],
        "ai_agent_quickstart": LARK_AI_AGENT_STEPS,
        "install_hint": "按 larksuite/cli README.zh.md 的 AI Agent 快速开始执行：先获得用户批准运行 `npx @larksuite/cli@latest install`，再引导用户完成 `lark-cli config init --new` 和 `lark-cli auth login --recommend`，最后用 `lark-cli auth status` 验证。",
    }
    if not cli:
        return result
    completed = subprocess.run(
        [cli, "docs", "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    result["docs_help"] = completed.returncode == 0
    result["ok"] = bool(result["docs_help"])
    if completed.stderr:
        result["stderr"] = completed.stderr[-800:]
    return result


def install_lark_cli() -> dict[str, Any]:
    """Run the Lark CLI README AI Agent install step.

    This intentionally does not fall back to `npm install -g @larksuite/cli`.
    The documented AI Agent entrypoint is `npx @larksuite/cli@latest install`.
    """
    existing = shutil.which("lark-cli")
    if existing:
        return {"ok": True, "skipped": True, "reason": "lark-cli already exists", "lark_cli": existing}
    command = ["npx", "@larksuite/cli@latest", "install"]
    if not shutil.which("npx"):
        return {
            "ok": False,
            "skipped": True,
            "reason": "npx is not available",
            "command": command,
        }
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=600)
    return {
        "ok": completed.returncode == 0,
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def figma_mcp_capability() -> dict[str, Any]:
    return {
        "ok": True,
        "required_tools": ["figma:get_design_context", "figma-generate-design"],
        "note": "UI 工作流会生成 Figma MCP 任务包；实际执行依赖 Codex/Figma MCP 上下文。",
    }


def doctor() -> dict[str, Any]:
    return {
        "commands": {
            "codex": command_available("codex"),
            "claude": command_available("claude"),
            "lark-cli": command_available("lark-cli"),
        },
        "lark_doc": lark_doc_capability(),
        "figma_mcp": figma_mcp_capability(),
    }
