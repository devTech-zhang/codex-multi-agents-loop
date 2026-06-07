from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


LARK_DOC_SKILL_CODEX = Path.home() / ".agents" / "skills" / "lark-doc" / "SKILL.md"
LARK_DOC_SKILL_CLAUDE = Path.home() / ".claude" / "plugins" / "lark-doc" / "skills" / "lark-doc" / "SKILL.md"
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


def _find_lark_doc_skill() -> str | None:
    for path in (LARK_DOC_SKILL_CODEX, LARK_DOC_SKILL_CLAUDE):
        if path.exists():
            return str(path)
    return None


def lark_doc_capability() -> dict[str, Any]:
    cli = shutil.which("lark-cli")
    result: dict[str, Any] = {
        "ok": False,
        "lark_cli": cli,
        "docs_help": False,
        "lark_doc_skill": _find_lark_doc_skill(),
        "platforms_checked": {
            "codex_path": str(LARK_DOC_SKILL_CODEX) if LARK_DOC_SKILL_CODEX.exists() else None,
            "claude_path": str(LARK_DOC_SKILL_CLAUDE) if LARK_DOC_SKILL_CLAUDE.exists() else None,
        },
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


def ui_design_capability() -> dict[str, Any]:
    return {
        "ok": True,
        "required_tools": [],
        "note": "UI 工作流产出 DESIGN.md 风格设计规范，供前端实现使用。",
    }


def detect_primary_platform() -> str | None:
    """Detect the primary available platform CLI. Priority: claude → codex → opencode."""
    for cmd in ("claude", "codex", "opencode"):
        if command_available(cmd):
            return cmd
    return None


def doctor() -> dict[str, Any]:
    available = {
        "claude": command_available("claude"),
        "codex": command_available("codex"),
        "opencode": command_available("opencode"),
        "lark-cli": command_available("lark-cli"),
    }
    return {
        "detected_platform": detect_primary_platform(),
        "detected_hint": _platform_hint(detect_primary_platform()),
        "commands": available,
        "lark_doc": lark_doc_capability(),
        "ui_design": ui_design_capability(),
    }


def _platform_hint(platform: str | None) -> str | None:
    hints = {
        "claude": "当前环境检测到 Claude Code，workflow 将以 `claude -p` 模式执行编码任务。",
        "codex": "当前环境检测到 Codex，workflow 将生成 Codex 执行包。",
        "opencode": "当前环境检测到 OpenCode。",
    }
    return hints.get(platform) if platform else "未检测到任何平台 CLI，workflow 将只生成任务包。"
