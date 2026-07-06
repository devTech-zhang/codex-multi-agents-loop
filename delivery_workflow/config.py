from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

WORKSPACE_CONFIG_NAME = "workflow.config.json"
PLUGIN_CONFIG_NAME = "codex-delivery-workflow.config.json"
PACKAGE_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = PACKAGE_ROOT.parent
PROJECT_CODEX_DIR = ".codex"
PROJECT_CODEX_CONFIG = ".codex/config.toml"
PROJECT_AGENT_DIR = ".codex/agents"
PROJECT_WORKFLOW_DIR = ".codex/delivery-workflow"
PROJECT_MEMORY_DIR = ".codex/delivery-workflow/memory"
MANAGER_AGENT_ID = "delivery-manager"
CHILD_AGENT_IDS = ["product-manager", "ui-designer", "frontend-impl", "backend-impl", "qa-tester"]
PROJECT_AGENT_IDS = [MANAGER_AGENT_ID, *CHILD_AGENT_IDS]

LEGACY_NICKNAME_CANDIDATES = {
    "delivery-manager": ["主管", "Manager", "交付主管", "Delivery Manager"],
    "product-manager": ["PM 01", "PM 02", "PM 03", "PM 04"],
    "ui-designer": ["UI 01", "UI 02", "UI 03", "UI 04"],
    "frontend-impl": ["FE 01", "FE 02", "FE 03", "FE 04"],
    "backend-impl": ["BE 01", "BE 02", "BE 03", "BE 04"],
    "qa-tester": ["QA 01", "QA 02", "QA 03", "QA 04"],
}

DEFAULT_CONFIG_TEMPLATE: dict[str, Any] = {
    "storage": {
        "home": PROJECT_WORKFLOW_DIR,
        "db": f"{PROJECT_WORKFLOW_DIR}/workflow.sqlite3",
        "artifact_root": "docs/delivery",
        "source_root": f"{PROJECT_WORKFLOW_DIR}/workspace",
        "logs": f"{PROJECT_WORKFLOW_DIR}/logs",
        "memory_root": PROJECT_MEMORY_DIR,
    }
}


def load_config() -> dict[str, Any]:
    path = workspace_config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"missing {WORKSPACE_CONFIG_NAME}; run `python3 -m delivery_workflow.cli config init` in the project workspace"
        )
    config = _read_json(path)
    return config


def config_sources() -> list[str]:
    path = workspace_config_path()
    return [str(path)] if path.exists() else []


def workspace_config_path() -> Path:
    for candidate in (Path.cwd() / WORKSPACE_CONFIG_NAME, Path.cwd() / PLUGIN_CONFIG_NAME):
        if candidate.exists():
            return candidate
    return PLUGIN_ROOT / PLUGIN_CONFIG_NAME


def write_workspace_config(*, overwrite: bool = False) -> Path:
    target = Path.cwd() / WORKSPACE_CONFIG_NAME
    if target.exists() and not overwrite:
        raise FileExistsError(f"config already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    source = PLUGIN_ROOT / PLUGIN_CONFIG_NAME
    template = _read_json(source) if source.exists() else DEFAULT_CONFIG_TEMPLATE
    target.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def initialize_project_workspace(*, overwrite_config: bool = False, overwrite_agents: bool = False) -> dict[str, Any]:
    from .workflow_log import log_workflow

    log_workflow(
        "project_workspace.init.started",
        "开始初始化当前 Codex 交付工作流工作区。",
        payload={"overwrite_config": overwrite_config, "overwrite_agents": overwrite_agents},
    )
    config_path = Path.cwd() / WORKSPACE_CONFIG_NAME
    if overwrite_config or not config_path.exists():
        config_path = write_workspace_config(overwrite=overwrite_config)
    from .storage import init_db
    from .paths import artifact_root, log_root, memory_root, source_root

    db = init_db()
    agents = materialize_project_agents(overwrite=overwrite_agents)
    codex_config = materialize_project_codex_config()
    memories = initialize_agent_memory_files(overwrite=False)
    result = {
        "config_path": str(config_path),
        "codex_config_path": PROJECT_CODEX_CONFIG,
        "codex_config": codex_config,
        "db_path": str(db),
        "home": str(db.parent),
        "logs": str(log_root()),
        "artifact_root": str(artifact_root()),
        "source_root": str(source_root()),
        "agent_dir": str(Path.cwd() / PROJECT_AGENT_DIR),
        "agents": agents,
        "memory_root": str(memory_root()),
        "memories": memories,
        "next_prompt": "@delivery-manager 实现一个 <你的需求>",
    }
    log_workflow("project_workspace.init.completed", "当前 Codex 交付工作流工作区初始化完成。", payload=result)
    return result


def materialize_project_agents(*, overwrite: bool = False) -> list[dict[str, Any]]:
    agent_dir = Path.cwd() / PROJECT_AGENT_DIR
    agent_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, Any]] = []
    for agent_id in PROJECT_AGENT_IDS:
        target = agent_dir / f"{agent_id}.toml"
        if target.exists() and not overwrite:
            current = target.read_text(encoding="utf-8")
            migrated = _migrate_existing_project_agent(current, agent_id)
            if migrated != current:
                target.write_text(migrated, encoding="utf-8")
                written.append({"agent": agent_id, "path": str(target), "status": "migrated"})
            else:
                written.append({"agent": agent_id, "path": str(target), "status": "exists"})
            continue
        target.write_text(_project_agent_toml(agent_id), encoding="utf-8")
        written.append({"agent": agent_id, "path": str(target), "status": "written"})
    return written


def materialize_project_codex_config() -> dict[str, Any]:
    target = Path.cwd() / PROJECT_CODEX_CONFIG
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    original = target.read_text(encoding="utf-8") if existed else ""

    updated = original
    updated = _ensure_toml_key(updated, "features", "multi_agent", True, replace=True)
    updated = _ensure_toml_key(updated, "agents", "max_threads", 6, replace=False)
    updated = _ensure_toml_key(updated, "agents", "max_depth", 1, replace=False)

    for agent_id, values in _project_agent_config_entries().items():
        table = f'agents."{agent_id}"'
        updated = _ensure_toml_key(updated, table, "description", values["description"], replace=False)
        updated = _ensure_toml_key(updated, table, "config_file", values["config_file"], replace=True)
        updated = _ensure_toml_key(updated, table, "nickname_candidates", values["nickname_candidates"], replace=False)

    if updated != original:
        target.write_text(updated, encoding="utf-8")
        status = "merged" if existed else "written"
    else:
        status = "exists"

    return {
        "path": str(target),
        "status": status,
        "agents": PROJECT_AGENT_IDS,
        "restart_required": True,
        "note": "项目级 Agent 注册层已写入 .codex/config.toml；需要重启或新开 Codex 会话后 spawn_agent 类型注册才会刷新。",
    }


def initialize_agent_memory_files(*, overwrite: bool = False) -> list[dict[str, Any]]:
    from .paths import memory_root

    root = memory_root()
    root.mkdir(parents=True, exist_ok=True)
    created: list[dict[str, Any]] = []
    for agent_id in PROJECT_AGENT_IDS:
        path = root / f"{agent_id}.md"
        if path.exists() and not overwrite:
            created.append({"agent": agent_id, "path": str(path), "status": "exists"})
            continue
        path.write_text(
            "\n".join(
                [
                    f"# {agent_id} 记忆",
                    "",
                    "本文件由 codex-delivery-workflow 初始化，用于记录该 Agent 在当前项目中的长期工作上下文。",
                    "Agent 每次处理任务前应读取本文件，完成后补充关键结论、产物路径、未决问题和下一步建议。",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        created.append({"agent": agent_id, "path": str(path), "status": "written"})
    return created


def storage_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("storage") or {}


def storage_path(config: dict[str, Any], key: str, fallback: str) -> Path:
    value = storage_config(config).get(key) or fallback
    path = Path(str(value))
    return path if path.is_absolute() else Path.cwd() / path


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"config must be a JSON object: {path}")
    return data


def _project_agent_toml(agent_id: str) -> str:
    if agent_id == MANAGER_AGENT_ID:
        profile = _manager_profile()
    else:
        profile = _load_template_agent(agent_id)
    fields = {
        "name": profile["name"],
        "description": profile["description"],
        "model": profile.get("model", "gpt-5"),
        "model_reasoning_effort": profile.get("model_reasoning_effort", "high"),
        "sandbox_mode": profile.get("sandbox_mode", "workspace-write"),
        "nickname_candidates": profile.get("nickname_candidates", []),
        "developer_instructions": _project_agent_instructions(agent_id, profile),
    }
    return _dump_toml(fields)


def _load_template_agent(agent_id: str) -> dict[str, Any]:
    path = PLUGIN_ROOT / "agents" / f"{agent_id}.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data.get("id") != agent_id:
        raise KeyError(f"agent profile id mismatch: {agent_id}")
    return data


def _manager_profile() -> dict[str, Any]:
    return {
        "name": MANAGER_AGENT_ID,
        "description": "交付主管 Agent。负责创建任务、按语义调度自定义子 Agent、维护 SQLite 薄状态账本、汇总产物、处理 PRD 审核和多 Agent 评审循环。",
        "model": "gpt-5.5",
        "model_reasoning_effort": "medium",
        "sandbox_mode": "workspace-write",
        "nickname_candidates": ["交付主管", "项目主管", "交付经理", "研发协调"],
        "developer_instructions": """
你是 delivery-manager，当前项目的交付主管 Agent。

职责边界：
- 你代表用户管理 codex-delivery-workflow，不亲自写 PRD、设计、前端、后端或 QA 报告。
- 你负责调用 MCP 工具创建任务、按语义和项目状态调度子 Agent、回收产物、更新 SQLite 薄状态账本、总结状态和给出下一步建议。
- 老板说“实现一个需求”或“创建一个项目”时，先确认当前项目已初始化，然后创建 workflow run；如果老板没有显式 @ 员工，主动 spawn product-manager，让它自行领取 pending job 后输出 PRD V1。
- PRD V1 完成后必须先向老板归纳：产物路径、核心范围、风险、待确认问题和可选下一步。
- 老板确认 PRD 后，调用确认工具继续 UI、前端、后端、QA。
- 老板要求“多角色/agent 评审”时，调用评审工具入队 ui-designer、frontend-impl、backend-impl、qa-tester 共同评审最新 PRD，再按 name 并行 spawn 对应自定义 Agent；评审完成后 spawn product-manager 整合下一版 PRD。

工作原则：
- 所有状态以 SQLite 为准，不用聊天上下文替代账本。
- SQLite 只保存结构化状态、版本、路径和短摘要；完整需求、PRD、设计、实现和 QA 报告必须读取 `docs/delivery/` 产物文件。
- 每次处理前读取 manager_summary 或 status。
- 每次处理后输出中文总结，说明当前状态、已产物、待办、需要老板决策的点。
- 如果老板直接 @ 子 Agent 处理过任务，你需要从 SQLite、events、artifacts 和该 Agent memory 中恢复上下文。
- 不直接代替子 Agent 执行专业任务；你的交付动作是准备任务包、调度自定义 Agent、再读取账本归纳结果。
- 老板显式 `@product-manager` 等员工时，由原生项目 Agent 直接处理，你不要重复 spawn。
- 老板说“继续下一步”“你继续”“往下走”且存在员工 pending job 时，调用 `spawn_agent(agent_type="<agent-name>", message="<任务包>")` 主动调度对应 Agent。
- 调用自定义 Agent 时不要传 `model` 或 `reasoning_effort`，让 Codex 使用 `.codex/agents/<agent-name>.toml` 中的模型、思考等级和昵称配置。
- 主管不能自己领取员工 job 或伪造员工产物；被 spawn 或显式 @ 的员工 Agent 必须自行领取并回填。
""",
    }


def _project_agent_instructions(agent_id: str, profile: dict[str, Any]) -> str:
    original = str(profile.get("developer_instructions") or "").strip()
    if agent_id == MANAGER_AGENT_ID:
        original = str(profile["developer_instructions"]).strip()
        role_line = "你是项目级交付主管。"
        invocation_line = "你不要递归调用 delivery-manager；应根据 pending job 调用对应员工 name 的自定义 Agent。"
    else:
        role_line = f"你是项目级 {agent_id}。"
        invocation_line = (
            f'delivery-manager 也可以通过 `spawn_agent(agent_type="{agent_id}", message="<任务包>")` '
            "调用同一角色配置；两种调用方式共享同一份角色记忆。"
        )
    return f"""{original}

项目级协作补充：
- {role_line} 你已通过当前项目的 `.codex/agents/{agent_id}.toml` 被 Codex 识别，老板可以直接通过 `@{agent_id}` 找你。
- {invocation_line}
- 当前项目的共享账本位于 `.codex/delivery-workflow/workflow.sqlite3`，共享产物位于 `docs/delivery/`，你的记忆文件位于 `.codex/delivery-workflow/memory/{agent_id}.md`。
- 每次开始工作前，先通过 codex-delivery-workflow MCP 工具读取当前状态；如果你是员工 Agent，优先领取属于自己的 pending job。
- 每次完成工作后，必须通过 MCP 工具回填结果或提醒 delivery-manager 回填，不能只在聊天里说“完成”。
- 完成后更新自己的记忆文件，记录本次产物路径、关键结论、未决问题和下次继续时需要读取的上下文。
- 如果老板直接点名你做临时评审或补充，但当前没有你的 pending job，要先说明当前账本状态，并建议由 `@delivery-manager` 创建任务或准备交接。
"""


def _project_agent_config_entries() -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for agent_id in PROJECT_AGENT_IDS:
        profile = _manager_profile() if agent_id == MANAGER_AGENT_ID else _load_template_agent(agent_id)
        entries[agent_id] = {
            "description": profile["description"],
            "config_file": f"agents/{agent_id}.toml",
            "nickname_candidates": profile.get("nickname_candidates", []),
        }
    return entries


def _remove_legacy_agent_type(content: str) -> str:
    return "".join(
        line
        for line in content.splitlines(keepends=True)
        if line.split("=", 1)[0].strip() != "agent_type"
    )


def _migrate_existing_project_agent(content: str, agent_id: str) -> str:
    migrated = _remove_legacy_agent_type(content)
    try:
        existing = tomllib.loads(migrated)
    except tomllib.TOMLDecodeError:
        return migrated
    if existing.get("nickname_candidates") != LEGACY_NICKNAME_CANDIDATES.get(agent_id):
        return migrated
    profile = _manager_profile() if agent_id == MANAGER_AGENT_ID else _load_template_agent(agent_id)
    nicknames = profile.get("nickname_candidates", [])
    rendered = ", ".join(json.dumps(item, ensure_ascii=False) for item in nicknames)
    lines: list[str] = []
    for line in migrated.splitlines(keepends=True):
        if line.split("=", 1)[0].strip() == "nickname_candidates":
            suffix = "\n" if line.endswith("\n") else ""
            lines.append(f"nickname_candidates = [{rendered}]{suffix}")
        else:
            lines.append(line)
    return "".join(lines)


def _dump_toml(values: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in values.items():
        if isinstance(value, list):
            rendered = ", ".join(json.dumps(item, ensure_ascii=False) for item in value)
            lines.append(f"{key} = [{rendered}]")
        elif "\n" in str(value):
            lines.append(f'{key} = """\n{str(value).strip()}\n"""')
        else:
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


_TOML_TABLE_RE = re.compile(r"^\s*(\[\[?)([^\]]+)\]\]?\s*(?:#.*)?$")


def _ensure_toml_key(text: str, table: str, key: str, value: Any, *, replace: bool) -> str:
    if text and not text.endswith("\n"):
        text += "\n"
    lines = text.splitlines(keepends=True)
    literal = _toml_literal(value)
    target_line = f"{key} = {literal}\n"
    start, end = _find_toml_table(lines, table)

    if start is None:
        prefix = "" if not lines else "\n"
        return "".join(lines) + f"{prefix}[{table}]\n{target_line}"

    key_re = re.compile(rf"^(\s*){re.escape(key)}\s*=")
    for index in range(start + 1, end):
        match = key_re.match(lines[index])
        if not match:
            continue
        if not replace:
            return "".join(lines)
        newline = "\n" if lines[index].endswith("\n") else ""
        lines[index] = f"{match.group(1)}{key} = {literal}{newline}"
        return "".join(lines)

    lines.insert(end, target_line)
    return "".join(lines)


def _find_toml_table(lines: list[str], table: str) -> tuple[int | None, int]:
    for index, line in enumerate(lines):
        match = _TOML_TABLE_RE.match(line)
        if not match:
            continue
        if match.group(1) != "[":
            continue
        if match.group(2).strip() != table:
            continue
        end = len(lines)
        for next_index in range(index + 1, len(lines)):
            if _TOML_TABLE_RE.match(lines[next_index]):
                end = next_index
                break
        return index, end
    return None, len(lines)


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)
