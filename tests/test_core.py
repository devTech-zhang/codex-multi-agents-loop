from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from agents_workflow import __version__, engine
from agents_workflow.config import (
    DEFAULT_CONFIG_TEMPLATE,
    PROJECT_CODEX_CONFIG,
    WORKSPACE_CONFIG_NAME,
    initialize_project_workspace,
    load_config,
    materialize_project_agents,
)
from agents_workflow.definitions import load_workflow
from agents_workflow.engine import confirm_prd, create_project, read_artifact, request_prd_review, status
from agents_workflow import mcp_server
from agents_workflow.mcp_server import TOOLS


WORKFLOW_AGENTS = [
    "product-manager",
    "software-architect",
    "ui-designer",
    "development-engineer",
    "qa-engineer",
]

PROJECT_AGENTS = ["project-manager", *WORKFLOW_AGENTS]

PLUGIN_NAME = "codex-multi-agents-loop"
ROOT = Path(__file__).resolve().parents[1]

EXPECTED_OUTPUTS = [
    "prd",
    "architecture_design",
    "design_spec",
    "development_report",
    "qa_report",
]

EXPECTED_AGENT_SKILLS = {
    "project-manager": ["codex-multi-agents-loop"],
    "product-manager": ["product-manager"],
    "software-architect": ["software-architect"],
    "ui-designer": ["ui-designer"],
    "development-engineer": ["development-engineer"],
    "qa-engineer": ["qa-engineer"],
}

EXPECTED_AGENT_REASONING = {
    "project-manager": "medium",
    "product-manager": "high",
    "software-architect": "high",
    "ui-designer": "high",
    "development-engineer": "high",
    "qa-engineer": "high",
}

EXPECTED_AGENT_CORE_MARKERS = {
    "project-manager": ["角色内核", "跨角色混乱", "坏消息"],
    "product-manager": ["角色内核", "outcome", "三层 why"],
    "software-architect": ["角色内核", "拒绝 architecture astronautics", "可逆决策"],
    "ui-designer": ["角色内核", "可复用的视觉系统", "开发交接"],
    "development-engineer": ["角色内核", "真实用户体验", "可验证交付"],
    "qa-engineer": ["角色内核", "证据优先", "反幻想报告"],
}

UI_DESIGN_STANDARD_MARKERS = [
    "references/design-md-standard.md",
    "## Overview",
    "## Colors",
    "## Typography",
    "## Elevation",
    "## Components",
    "## Do's and Don'ts",
]


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _is_ascii_nickname(text: str) -> bool:
    return bool(text) and text.isascii()


def _mcp_frame(message: dict) -> bytes:
    body = json.dumps(message).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _toml_array(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(item, ensure_ascii=False) for item in items) + "]"


def _read_mcp_json_line(data: bytes) -> dict:
    lines = data.splitlines()
    assert len(lines) == 1, data.decode("utf-8", "replace")
    return json.loads(lines[0].decode("utf-8"))


class CodexMultiAgentsLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.write_config()

    def tearDown(self) -> None:
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def write_config(self, overrides: dict | None = None) -> None:
        config = deepcopy(DEFAULT_CONFIG_TEMPLATE)
        if overrides:
            for key, value in overrides.items():
                config[key] = value
        path = Path(WORKSPACE_CONFIG_NAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_default_config_is_codex_delivery_only_without_feishu_or_quality_gate(self) -> None:
        config = load_config()

        self.assertNotIn("lark", config)
        self.assertNotIn("quality_gate", config)
        self.assertNotIn("workflow", config)
        self.assertNotIn("code_platforms", config)
        self.assertEqual(config["storage"]["home"], ".codex/multi-agents-loop")
        self.assertEqual(config["storage"]["artifact_root"], ".codex/multi-agents-loop/runs")
        self.assertEqual(config["storage"]["scratch_root"], ".codex/multi-agents-loop/scratch")
        self.assertNotIn("source_root", config["storage"])
        self.assertEqual(config["storage"]["memory_root"], ".codex/multi-agents-loop/memory")
        self.assertEqual(config["storage"]["global_memory_root"], "~/.codex/codex-multi-agents-loop/global-memory")

    def test_workflow_definition_waits_for_owner_after_prd_v1(self) -> None:
        workflow = load_workflow()
        steps = workflow.steps

        self.assertEqual(workflow.workflow_id, PLUGIN_NAME)
        self.assertEqual(workflow.name, "Codex 项目级多 Agent Loop")
        self.assertEqual([step["id"] for step in steps], WORKFLOW_AGENTS)
        self.assertEqual([step["agent"] for step in steps], WORKFLOW_AGENTS)
        self.assertTrue(all(_has_cjk(step["name"]) for step in steps))
        self.assertTrue(all(step["name"] != step["agent"] for step in steps))
        self.assertTrue(all(step["executor"] == "agent" for step in steps))
        self.assertNotIn("lark-doc", {step["executor"] for step in steps})
        self.assertNotIn("gate", {step["executor"] for step in steps})
        self.assertEqual([step["outputs"][0] for step in steps], EXPECTED_OUTPUTS)
        self.assertEqual(steps[0].get("next", {}), {})
        self.assertEqual(steps[-1].get("next", {}), {})

    def test_agent_toml_files_include_manager_and_declared_child_agents(self) -> None:
        agent_dir = ROOT / "agents"
        files = {path.name for path in agent_dir.glob("*.toml")}

        self.assertEqual(files, {f"{agent}.toml" for agent in PROJECT_AGENTS})
        for agent in PROJECT_AGENTS:
            path = agent_dir / f"{agent}.toml"
            profile = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(profile["id"], agent)
            self.assertNotIn("agent_type", profile)
            self.assertEqual(profile["model"], "gpt-5.5")
            self.assertEqual(profile["model_reasoning_effort"], EXPECTED_AGENT_REASONING[agent])
            self.assertEqual(profile["sandbox_mode"], "workspace-write")
            self.assertGreaterEqual(len(profile["nickname_candidates"]), 1)
            self.assertTrue(all(_is_ascii_nickname(nickname) for nickname in profile["nickname_candidates"]))
            self.assertFalse(any(any(char.isdigit() for char in nickname) for nickname in profile["nickname_candidates"]))
            self.assertEqual(profile["skills"], EXPECTED_AGENT_SKILLS[agent])
            self.assertGreater(len(profile["developer_instructions"]), 800)
            self.assertNotIn("https://github.com/", profile["developer_instructions"])
            self.assertNotIn("agency-agents", profile["developer_instructions"])
            for marker in EXPECTED_AGENT_CORE_MARKERS[agent]:
                self.assertIn(marker, profile["developer_instructions"])
            if agent == "ui-designer":
                marker = UI_DESIGN_STANDARD_MARKERS[0]
                self.assertIn(marker, profile["instructions"])
                self.assertIn(marker, profile["developer_instructions"])
            for required in ("职责边界", "规范来源", "交付要求"):
                if agent != "project-manager":
                    self.assertIn(required, profile["developer_instructions"])
            self.assertTrue(_has_cjk(profile["description"]))
            serialized = json.dumps(profile, ensure_ascii=False).lower()
            self.assertNotIn("lark", serialized)
            self.assertNotIn("feishu", serialized)
            self.assertNotIn("飞书", serialized)

    def test_child_agent_skills_exist_and_are_chinese(self) -> None:
        for skill_names in EXPECTED_AGENT_SKILLS.values():
            for skill_name in skill_names:
                skill_path = ROOT / "skills" / skill_name / "SKILL.md"
                text = skill_path.read_text(encoding="utf-8")
                self.assertIn(f"name: {skill_name}", text)
                self.assertTrue(_has_cjk(text))
                self.assertNotIn("When To Use", text)
                self.assertNotIn("Core Rules", text)
                if skill_name == "ui-designer":
                    self.assertIn(UI_DESIGN_STANDARD_MARKERS[0], text)

    def test_plugin_names_and_docs_are_chinese(self) -> None:
        plugin = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        workflow = tomllib.loads((ROOT / "workflow" / f"{PLUGIN_NAME}.toml").read_text(encoding="utf-8"))
        marketplace = json.loads(
            (ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_en = (ROOT / "README.en.md").read_text(encoding="utf-8")
        skill = (ROOT / "skills" / PLUGIN_NAME / "SKILL.md").read_text(encoding="utf-8")

        self.assertEqual(plugin["name"], PLUGIN_NAME)
        self.assertEqual(plugin["version"], "0.0.1")
        self.assertEqual(pyproject["project"]["version"], plugin["version"])
        self.assertEqual(__version__, plugin["version"])
        self.assertEqual(workflow["version"], plugin["version"])
        self.assertEqual(pyproject["project"]["name"], PLUGIN_NAME)
        self.assertEqual(pyproject["project"]["scripts"][PLUGIN_NAME], "agents_workflow.cli:main")
        self.assertIn("agents_workflow", pyproject["tool"]["setuptools"]["package-data"])
        self.assertEqual(marketplace["plugins"][0]["name"], PLUGIN_NAME)
        self.assertEqual(
            marketplace["plugins"][0]["source"],
            {
                "source": "url",
                "url": "https://github.com/devTech-zhang/codex-multi-agents-loop.git",
                "ref": "main",
            },
        )
        self.assertEqual(plugin["mcpServers"], "./.mcp.json")
        self.assertIn("Codex 多 Agent Loop", plugin["interface"]["displayName"])
        self.assertTrue(_has_cjk(plugin["description"]))
        self.assertTrue(_has_cjk(plugin["interface"]["shortDescription"]))
        self.assertTrue(_has_cjk(plugin["interface"]["longDescription"]))
        self.assertIn(f"name: {PLUGIN_NAME}", skill)
        self.assertIn("scripts/upgrade-version.py", readme)
        self.assertIn("scripts/upgrade-version.py", readme_en)
        for text in (readme, skill):
            self.assertTrue(_has_cjk(text))
            self.assertNotIn("Codex Workflow", text)
            self.assertNotIn("When To Use", text)
            self.assertNotIn("Core Rules", text)
            self.assertNotIn("enable_agent_cli", text)
            self.assertNotIn("auto_run_to_idle", text)
            self.assertNotIn("code_platforms", text)

    def test_upgrade_version_script_dry_run_reports_next_patch_without_writing(self) -> None:
        script = ROOT / "scripts" / "upgrade-version.py"
        manifest_path = ROOT / ".codex-plugin" / "plugin.json"
        pyproject_path = ROOT / "pyproject.toml"
        manifest_before = manifest_path.read_text(encoding="utf-8")
        pyproject_before = pyproject_path.read_text(encoding="utf-8")
        current_version = json.loads(manifest_before)["version"]
        major, minor, patch = (int(part) for part in current_version.split("+", 1)[0].split("."))
        expected_prefix = f"{major}.{minor}.{patch + 1}+codex."

        completed = subprocess.run(
            [sys.executable, str(script), "patch", "--dry-run"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=3,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(f"{current_version} -> {expected_prefix}", completed.stdout)
        self.assertEqual(manifest_path.read_text(encoding="utf-8"), manifest_before)
        self.assertEqual(pyproject_path.read_text(encoding="utf-8"), pyproject_before)

    def test_public_plugin_has_generic_agent_rules_and_internal_design_standard(self) -> None:
        standard = ROOT / "skills" / "ui-designer" / "references" / "design-md-standard.md"
        self.assertTrue(standard.exists())
        standard_text = standard.read_text(encoding="utf-8")
        for marker in UI_DESIGN_STANDARD_MARKERS[1:]:
            self.assertIn(marker, standard_text)

        runtime_files = [
            *ROOT.glob("README*.md"),
            *(ROOT / "agents").glob("*.toml"),
            *(ROOT / "skills").glob("*/SKILL.md"),
            ROOT / ".codex-plugin" / "plugin.json",
            ROOT / "workflow" / f"{PLUGIN_NAME}.toml",
        ]
        runtime_text = chr(10).join(path.read_text(encoding="utf-8") for path in runtime_files)
        for forbidden in (
            "ym" + "-vibe-coding",
            "ym" + "-design-md-init",
            "ym" + "-figma-snapshot-restore",
            "Yun" + "mai",
            "米" + "家",
            "mi" + "ot",
            "React" + " Native",
        ):
            self.assertNotIn(forbidden, runtime_text)

    def test_mcp_config_has_single_plugin_source_without_editor_warning_expression(self) -> None:
        plugin = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        mcp_path = ROOT / plugin["mcpServers"]
        mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
        server = mcp["mcpServers"][PLUGIN_NAME]

        self.assertTrue((ROOT / ".mcp.json").exists())
        self.assertFalse((ROOT / ".codex-plugin" / "mcp.json").exists())
        self.assertEqual(plugin["mcpServers"], "./.mcp.json")
        self.assertEqual(mcp_path, ROOT / ".mcp.json")
        self.assertEqual(server["command"], "./scripts/codex-multi-agents-loop-mcp")
        self.assertEqual(server["cwd"], ".")
        self.assertNotIn("args", server)
        self.assertNotIn("CODEX_PLUGIN_ROOT", json.dumps(mcp))

    def test_init_materializes_project_agents_and_memory(self) -> None:
        initialized = initialize_project_workspace(overwrite_config=True, overwrite_agents=True)

        self.assertEqual(Path(initialized["config_path"]).resolve(), Path(WORKSPACE_CONFIG_NAME).resolve())
        self.assertIn(".codex/multi-agents-loop/runs", initialized["artifact_root"])
        self.assertIn(".codex/multi-agents-loop/scratch", initialized["scratch_root"])
        self.assertNotIn("source_root", initialized)
        self.assertEqual(Path(initialized["agent_dir"]).name, "agents")
        self.assertEqual(Path(initialized["codex_config_path"]), Path(PROJECT_CODEX_CONFIG))
        self.assertEqual({item["agent"] for item in initialized["agents"]}, set(PROJECT_AGENTS))
        self.assertEqual({item["agent"] for item in initialized["memories"]}, set(PROJECT_AGENTS))
        self.assertTrue((Path(".codex") / "agents" / "project-manager.toml").exists())
        self.assertTrue((Path(".codex") / "agents" / "product-manager.toml").exists())
        self.assertTrue(Path(PROJECT_CODEX_CONFIG).exists())
        self.assertTrue((Path(".codex") / "multi-agents-loop" / "memory" / "project-manager.md").exists())
        self.assertTrue((Path(".codex") / "multi-agents-loop" / "config.json").exists())
        self.assertTrue((Path(".codex") / "multi-agents-loop" / "runs").exists())
        self.assertTrue((Path(".codex") / "multi-agents-loop" / "scratch").exists())
        self.assertFalse((Path(".codex") / "multi-agents-loop" / "workspace").exists())
        self.assertFalse((Path("docs") / "delivery").exists())
        self.assertFalse(Path("workflow.config.json").exists())
        project_agent = tomllib.loads((Path(".codex") / "agents" / "product-manager.toml").read_text(encoding="utf-8"))
        self.assertEqual(project_agent["name"], "product-manager")
        self.assertNotIn("id", project_agent)
        self.assertNotIn("agent_type", project_agent)
        self.assertIn("用户可以直接通过 `@product-manager` 找你", project_agent["developer_instructions"])
        self.assertIn('spawn_agent(agent_type="product-manager"', project_agent["developer_instructions"])
        manager_agent = tomllib.loads((Path(".codex") / "agents" / "project-manager.toml").read_text(encoding="utf-8"))
        self.assertNotIn('spawn_agent(agent_type="project-manager"', manager_agent["developer_instructions"])
        self.assertNotIn("https://github.com/", manager_agent["developer_instructions"])
        self.assertNotIn("agency-agents", manager_agent["developer_instructions"])
        self.assertIn("跨角色混乱", manager_agent["developer_instructions"])
        self.assertIn("坏消息", manager_agent["developer_instructions"])
        ui_agent = tomllib.loads((Path(".codex") / "agents" / "ui-designer.toml").read_text(encoding="utf-8"))
        self.assertIn(UI_DESIGN_STANDARD_MARKERS[0], ui_agent["developer_instructions"])

        codex_config = tomllib.loads(Path(PROJECT_CODEX_CONFIG).read_text(encoding="utf-8"))
        self.assertTrue(codex_config["features"]["multi_agent"])
        self.assertEqual(codex_config["agents"]["max_threads"], 6)
        self.assertEqual(codex_config["agents"]["max_depth"], 1)
        for agent in PROJECT_AGENTS:
            role = codex_config["agents"][agent]
            self.assertEqual(role["config_file"], f"agents/{agent}.toml")
            self.assertTrue(_has_cjk(role["description"]))
            self.assertGreaterEqual(len(role["nickname_candidates"]), 1)
            self.assertTrue(all(_is_ascii_nickname(nickname) for nickname in role["nickname_candidates"]))

    def test_mcp_tools_use_invoking_project_root_from_pwd_when_server_cwd_is_plugin_cache(self) -> None:
        with tempfile.TemporaryDirectory() as project_dir, tempfile.TemporaryDirectory() as plugin_cwd:
            plugin_marker = Path(plugin_cwd) / ".codex-plugin" / "plugin.json"
            plugin_marker.parent.mkdir(parents=True)
            plugin_marker.write_text("{}", encoding="utf-8")
            os.chdir(plugin_cwd)
            with patch.dict(os.environ, {"PWD": project_dir}, clear=False):
                initialized = initialize_project_workspace(overwrite_config=True, overwrite_agents=True)
                created = create_project(requirement="做一个极简 TODO Web 应用", title="TODO Web")
                current = status(created["run_id"])

            project = Path(project_dir)
            plugin = Path(plugin_cwd)
            self.assertEqual(Path(initialized["config_path"]).resolve(), (project / WORKSPACE_CONFIG_NAME).resolve())
            self.assertEqual(
                Path(initialized["db_path"]).resolve(),
                (project / ".codex" / "multi-agents-loop" / "workflow.sqlite3").resolve(),
            )
            self.assertTrue((project / ".codex" / "agents" / "project-manager.toml").exists())
            self.assertTrue((project / ".codex" / "multi-agents-loop" / "runs").exists())
            self.assertTrue((project / ".codex" / "multi-agents-loop" / "scratch").exists())
            self.assertFalse((project / ".codex" / "multi-agents-loop" / "workspace").exists())
            self.assertFalse((project / "docs" / "delivery").exists())
            self.assertFalse((plugin / WORKSPACE_CONFIG_NAME).exists())
            self.assertFalse((plugin / ".codex").exists())
            self.assertEqual(current["run"]["id"], created["run_id"])
            self.assertEqual(current["run"]["project"]["title"], "TODO Web")

    def test_init_merges_existing_codex_config_without_overwriting_other_settings(self) -> None:
        codex_dir = Path(".codex")
        codex_dir.mkdir(exist_ok=True)
        Path(PROJECT_CODEX_CONFIG).write_text(
            "\n".join(
                [
                    'model = "gpt-5.4"',
                    "",
                    "[features]",
                    "memories = true",
                    "",
                    "[agents]",
                    "max_threads = 3",
                    "",
                    '[agents."existing-reviewer"]',
                    'description = "保留已有评审 Agent"',
                    'config_file = "agents/existing-reviewer.toml"',
                    "",
                    '[agents."product-manager"]',
                    'description = "保留产品经理自定义描述"',
                    'nickname_candidates = ["产品经理", "需求经理", "产品负责人", "产品策划"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        initialized = initialize_project_workspace(overwrite_config=True, overwrite_agents=True)

        self.assertEqual(Path(initialized["codex_config_path"]), Path(PROJECT_CODEX_CONFIG))
        self.assertEqual(initialized["codex_config"]["status"], "merged")
        text = Path(PROJECT_CODEX_CONFIG).read_text(encoding="utf-8")
        codex_config = tomllib.loads(text)
        self.assertEqual(codex_config["model"], "gpt-5.4")
        self.assertTrue(codex_config["features"]["memories"])
        self.assertTrue(codex_config["features"]["multi_agent"])
        self.assertEqual(codex_config["agents"]["max_threads"], 3)
        self.assertEqual(codex_config["agents"]["max_depth"], 1)
        self.assertEqual(codex_config["agents"]["existing-reviewer"]["config_file"], "agents/existing-reviewer.toml")
        self.assertEqual(codex_config["agents"]["product-manager"]["description"], "保留产品经理自定义描述")
        self.assertEqual(codex_config["agents"]["product-manager"]["config_file"], "agents/product-manager.toml")
        product_profile = tomllib.loads((ROOT / "agents" / "product-manager.toml").read_text(encoding="utf-8"))
        self.assertIn(f'nickname_candidates = {_toml_array(product_profile["nickname_candidates"])}', text)
        self.assertIn('[agents."project-manager"]', text)

    def test_existing_project_agent_removes_legacy_agent_type_without_overwriting_other_content(self) -> None:
        agent_dir = Path(".codex/agents")
        agent_dir.mkdir(parents=True)
        target = agent_dir / "product-manager.toml"
        target.write_text(
            'name = "product-manager"\nagent_type = "worker"\n'
            'nickname_candidates = ["PM 01", "PM 02", "PM 03", "PM 04"]\n'
            'description = "保留项目自定义描述"\n',
            encoding="utf-8",
        )

        result = materialize_project_agents(overwrite=False)

        migrated = target.read_text(encoding="utf-8")
        self.assertNotIn("agent_type", migrated)
        self.assertIn("保留项目自定义描述", migrated)
        product_profile = tomllib.loads((ROOT / "agents" / "product-manager.toml").read_text(encoding="utf-8"))
        self.assertIn(f'nickname_candidates = {_toml_array(product_profile["nickname_candidates"])}', migrated)
        self.assertEqual(next(item["status"] for item in result if item["agent"] == "product-manager"), "migrated")

    def test_no_claude_code_config_or_old_codex_workflow_name_remains(self) -> None:
        self.assertFalse((ROOT / ".claude").exists())
        self.assertFalse((ROOT / ".claude-plugin").exists())
        self.assertFalse((ROOT / "hooks.json").exists())
        self.assertFalse((ROOT / "hooks").exists())
        self.assertFalse((ROOT / "workflow" / "codex-workflow.toml").exists())
        self.assertFalse((ROOT / "skills" / "codex-workflow").exists())
        self.assertFalse((ROOT / "codex-workflow.config.json").exists())
        self.assertFalse((ROOT / "workflow.config.json").exists())

    def test_create_project_prepares_custom_agent_spawn_without_claiming_job(self) -> None:
        created = create_project(requirement="做一个极简 TODO Web 应用", title="TODO Web")
        run_id = created["run_id"]

        current = status(run_id)
        self.assertEqual(current["run"]["workflow_id"], PLUGIN_NAME)
        self.assertEqual(created["route_plan"]["loop_kind"], "multi_agent_delivery_loop")
        self.assertEqual(created["route_plan"]["goal"], "做一个极简 TODO Web 应用")
        self.assertEqual(created["route_plan"]["current_iteration"], 1)
        self.assertEqual(created["route_plan"]["max_iterations"], 3)
        self.assertIn("等待用户确认", " ".join(created["route_plan"]["exit_conditions"]))
        self.assertEqual(created["loop"]["loop_kind"], "multi_agent_delivery_loop")
        self.assertEqual(created["execution_policy"]["mode"], "custom_agent_spawn")
        self.assertNotIn("enable_agent_cli", created["execution_policy"])
        handoff = created["next_handoff"]
        self.assertTrue(handoff["ok"])
        self.assertFalse(handoff["idle"])
        self.assertEqual(handoff["run_id"], run_id)
        self.assertEqual(handoff["step_id"], "product-manager")
        self.assertEqual(handoff["agent"], "product-manager")
        self.assertEqual(handoff["agent_type"], "product-manager")
        self.assertEqual(handoff["mention"], "@product-manager")
        self.assertTrue(handoff["auto_spawn_allowed"])
        self.assertEqual(handoff["manager_next_action"], "spawn_custom_agent")
        self.assertEqual(handoff["claim_tool"], "codex_multi_agents_loop_dispatch_next")
        self.assertEqual(handoff["complete_tool"], "codex_multi_agents_loop_complete_agent_step")
        self.assertIn("@product-manager", handoff["handoff_message"])
        self.assertIn("codex_multi_agents_loop_dispatch_next", handoff["handoff_message"])
        self.assertIn('spawn_agent(agent_type="product-manager"', handoff["handoff_message"])
        self.assertIn(run_id, handoff["handoff_message"])
        self.assertNotIn("runtime", handoff)
        self.assertEqual(current["run"]["status"], "prd_draft")
        self.assertEqual(current["run"]["current_step"], "product-manager")
        self.assertEqual(current["run"]["prd_version"], 0)
        self.assertEqual(current["step_runs"], [])
        self.assertEqual(len(current["jobs"]), 1)
        self.assertEqual(current["jobs"][0]["step_id"], "product-manager")
        self.assertEqual(current["jobs"][0]["status"], "pending")
        self.assertEqual(current["gates"], [])

        artifact_names = {artifact["name"] for artifact in current["artifacts"]}
        self.assertEqual({"raw_requirement", "project_context", "route_plan", "product-manager_agent_task"}, artifact_names)

        self.assertTrue(hasattr(engine, "prepare_agent_handoff"))
        self.assertTrue(hasattr(engine, "dispatch_next_agent_task"))
        self.assertTrue(engine.dispatch_next_agent_task(run_id=run_id, agent="ui-designer")["idle"])
        still_pending = status(run_id)
        self.assertEqual(still_pending["jobs"][0]["status"], "pending")
        self.assertEqual(still_pending["step_runs"], [])
        dispatch = engine.dispatch_next_agent_task(run_id=run_id, agent="product-manager", invocation_mode="manager_spawn")
        self.assertTrue(dispatch["ok"])
        self.assertFalse(dispatch["idle"])
        self.assertEqual(dispatch["run_id"], run_id)
        self.assertEqual(dispatch["step_id"], "product-manager")
        self.assertEqual(dispatch["agent"], "product-manager")
        self.assertEqual(dispatch["agent_type"], "product-manager")
        self.assertEqual(dispatch["model"], "gpt-5.5")
        self.assertEqual(dispatch["model_reasoning_effort"], "high")
        self.assertEqual(dispatch["skills"], ["product-manager"])
        self.assertEqual(dispatch["claim_mode"], "custom_project_agent")
        self.assertEqual(dispatch["invocation_mode"], "manager_spawn")
        self.assertNotIn("runtime", dispatch)
        self.assertIn("product-manager", dispatch["task_message"])
        self.assertIn("做一个极简 TODO Web 应用", dispatch["task_message"])
        self.assertIn("产品交接说明 V1", dispatch["task_message"])
        self.assertIn("## 闭环目标", dispatch["task_message"])
        self.assertIn("exit_conditions", dispatch["task_message"])
        self.assertIn("自评是否满足退出条件", dispatch["task_message"])
        self.assertTrue((Path(".codex") / "agents" / "project-manager.toml").exists())
        self.assertTrue((Path(".codex") / "multi-agents-loop" / "memory" / "product-manager.md").exists())

        after_dispatch = status(run_id)
        self.assertEqual(after_dispatch["jobs"][0]["status"], "running")
        self.assertEqual(after_dispatch["jobs"][0]["claimed_agent"], "product-manager")
        self.assertEqual(after_dispatch["jobs"][0]["invocation_mode"], "manager_spawn")
        self.assertEqual(after_dispatch["jobs"][0]["attempts"], 1)
        self.assertEqual(after_dispatch["step_runs"][0]["step_id"], "product-manager")
        self.assertEqual(after_dispatch["step_runs"][0]["status"], "running")

        duplicate = engine.dispatch_next_agent_task(run_id=run_id, agent="product-manager", invocation_mode="explicit_at")
        self.assertTrue(duplicate["idle"])
        self.assertEqual(status(run_id)["jobs"][0]["attempts"], 1)

    def test_manager_continue_next_step_spawns_named_custom_agent(self) -> None:
        created = create_project(requirement="做一个极简 TODO Web 应用", title="TODO Web")
        run_id = created["run_id"]
        handoff = created["next_handoff"]

        self.assertTrue(handoff["auto_spawn_allowed"])
        self.assertEqual(handoff["manager_next_action"], "spawn_custom_agent")
        self.assertIn('spawn_agent(agent_type="product-manager"', handoff["handoff_message"])
        self.assertIn("不要传 model 或 reasoning_effort", handoff["handoff_message"])
        self.assertIn("继续下一步", handoff["handoff_message"])
        self.assertIn("@product-manager", handoff["handoff_message"])

        task_package = read_artifact(run_id, "product-manager_agent_task")["content"]
        self.assertIn("@product-manager", task_package)
        self.assertIn('spawn_agent(agent_type="product-manager"', task_package)
        self.assertIn("只有 product-manager 类型的 Agent", task_package)
        self.assertIn("## 闭环目标", task_package)

        skill = (ROOT / "skills" / PLUGIN_NAME / "SKILL.md").read_text(encoding="utf-8")
        manager = (ROOT / "agents" / "project-manager.toml").read_text(encoding="utf-8")
        for text in (skill, manager):
            self.assertIn("继续下一步", text)
            self.assertIn('spawn_agent(agent_type="<agent-name>"', text)
            self.assertIn("不要传 `model` 或 `reasoning_effort`", text)

    def test_explicit_at_and_manager_spawn_share_memory_by_agent_name(self) -> None:
        first = create_project(requirement="第一个产品需求", title="需求一")
        first_dispatch = engine.dispatch_next_agent_task(
            run_id=first["run_id"], agent="product-manager", invocation_mode="manager_spawn"
        )
        engine.complete_agent_step(run_id=first["run_id"], job_id=first_dispatch["job_id"], output="# PRD 一\n")

        second = create_project(requirement="第二个产品需求", title="需求二")
        second_dispatch = engine.dispatch_next_agent_task(
            run_id=second["run_id"], agent="product-manager", invocation_mode="explicit_at"
        )
        engine.complete_agent_step(run_id=second["run_id"], job_id=second_dispatch["job_id"], output="# PRD 二\n")

        memory_path = Path(".codex/multi-agents-loop/memory/product-manager.md")
        memory_text = memory_path.read_text(encoding="utf-8")
        self.assertIn(first["run_id"], memory_text)
        self.assertIn(second["run_id"], memory_text)
        first_memory = status(first["run_id"])["agent_memory"]
        second_memory = status(second["run_id"])["agent_memory"]
        self.assertEqual(
            next(item["memory_path"] for item in first_memory if item["agent_name"] == "product-manager"),
            next(item["memory_path"] for item in second_memory if item["agent_name"] == "product-manager"),
        )

    def test_global_memory_sync_pack_and_pull_use_compact_cards(self) -> None:
        config = deepcopy(DEFAULT_CONFIG_TEMPLATE)
        config["storage"]["global_memory_root"] = ".codex/test-global-memory"
        path = Path(WORKSPACE_CONFIG_NAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

        created = create_project(requirement="需要一个登录页", title="登录需求")
        dispatch = engine.dispatch_next_agent_task(run_id=created["run_id"], agent="product-manager")
        engine.complete_agent_step(
            run_id=created["run_id"],
            job_id=dispatch["job_id"],
            output="# 产品交接说明\n\n用户偏好：先沉淀 PRODUCT.md，再进入架构设计。",
        )

        preview = engine.sync_global_memory(run_id=created["run_id"], agent="product-manager", dry_run=True)
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["written"], 0)
        self.assertEqual(len(preview["cards"]), 1)
        self.assertEqual(preview["cards"][0]["agent"], "product-manager")
        self.assertIn("项目 Agent 负责执行", preview["relationship"])

        written = engine.sync_global_memory(run_id=created["run_id"], agent="product-manager")
        self.assertEqual(written["written"], 1)
        pack = engine.memory_pack(agent="product-manager", limit=3, max_chars=1200)
        self.assertEqual(len(pack["cards"]), 1)
        self.assertIn("memory cards", pack["token_strategy"])
        pulled = engine.pull_global_memory(agent="product-manager", limit=3)
        self.assertEqual(pulled["updated"][0]["agent"], "product-manager")
        self.assertIn("全局经验拉取", Path(".codex/multi-agents-loop/memory/product-manager.md").read_text(encoding="utf-8"))

    def test_status_and_manager_summary_keep_large_text_out_of_sqlite_payloads(self) -> None:
        unique_marker = "LONG_REQUIREMENT_MARKER_7f65b2b8"
        long_requirement = "做一个极简 TODO Web 应用。\n" + "\n".join(
            f"- {unique_marker} 第 {index} 条很长的需求细节，用于确认 SQLite 状态接口不会携带完整正文。"
            for index in range(120)
        )

        created = create_project(requirement=long_requirement, title="长需求验证")
        run_id = created["run_id"]

        current = status(run_id)
        status_text = json.dumps(current, ensure_ascii=False)
        project_requirement = current["run"]["project"]["requirement"]

        self.assertLessEqual(len(project_requirement), 260)
        self.assertNotIn(unique_marker, status_text)

        raw_requirement = read_artifact(run_id, "raw_requirement")
        self.assertIn(unique_marker, raw_requirement["content"])

        summary_text = json.dumps(engine.manager_summary(run_id=run_id), ensure_ascii=False)
        self.assertLess(len(summary_text), 12000)
        self.assertNotIn(unique_marker, summary_text)

    def test_prd_v1_waits_for_owner_then_confirm_enters_delivery_chain(self) -> None:
        created = create_project(requirement="做一个极简 TODO Web 应用", title="TODO Web")
        run_id = created["run_id"]
        self.assertTrue(hasattr(engine, "dispatch_next_agent_task"))
        self.assertTrue(hasattr(engine, "complete_agent_step"))
        dispatch = engine.dispatch_next_agent_task(run_id=run_id)

        completed = engine.complete_agent_step(
            run_id=run_id,
            job_id=dispatch["job_id"],
            output="# PRD\n\n这是 product-manager 子 Agent 的真实输出。",
            spawned_agent_id="agent_product_manager_001",
        )

        self.assertTrue(completed["ok"])
        self.assertEqual(completed["step_id"], "product-manager")
        self.assertEqual(completed["status"], "completed")
        self.assertIsNone(completed["next_step"])
        self.assertEqual(completed["outputs"][0]["name"], "prd")

        current = status(run_id)
        self.assertEqual(current["run"]["status"], "waiting_owner_review")
        self.assertEqual(current["run"]["current_step"], "owner-review")
        self.assertEqual(current["run"]["prd_version"], 1)
        jobs_by_step = {job["step_id"]: job for job in current["jobs"]}
        self.assertEqual(jobs_by_step["product-manager"]["status"], "done")
        self.assertNotIn("ui-designer", jobs_by_step)
        self.assertEqual(current["step_runs"][0]["status"], "completed")
        prd = read_artifact(run_id, "prd")
        self.assertIn("真实输出", prd["content"])

        self.assertTrue(hasattr(engine, "manager_summary"))
        summary = engine.manager_summary(run_id=run_id)
        self.assertEqual(summary["manager_agent"], "project-manager")
        self.assertEqual(summary["loop"]["loop_kind"], "multi_agent_delivery_loop")
        self.assertEqual(summary["goal"], "做一个极简 TODO Web 应用")
        self.assertIn("等待用户确认", " ".join(summary["exit_conditions"]))
        self.assertEqual(summary["loop_progress"]["exit_condition_status"], "met")
        self.assertEqual(summary["current_step"], "owner-review")
        self.assertEqual(summary["prd_version"], 1)
        self.assertEqual(summary["completed_steps"], ["product-manager"])
        self.assertEqual(summary["pending_jobs"], [])
        self.assertIn("确认产品交接说明 V1", summary["next_action"])
        self.assertIn("prd", [artifact["name"] for artifact in summary["artifacts"]])
        self.assertIn("product-manager", summary["last_update"])

        confirmed = confirm_prd(run_id)
        self.assertTrue(confirmed["ok"])
        self.assertEqual(confirmed["next_step"], "software-architect")
        after_confirm = status(run_id)
        self.assertEqual(after_confirm["run"]["status"], "running")
        self.assertEqual(after_confirm["run"]["current_step"], "software-architect")
        self.assertIn("software-architect", {job["step_id"] for job in after_confirm["jobs"] if job["status"] == "pending"})

        serialized_status = json.dumps(current, ensure_ascii=False).lower()
        self.assertNotIn("lark", serialized_status)
        self.assertNotIn("feishu", serialized_status)
        self.assertNotIn("飞书", serialized_status)

    def test_docs_maintenance_routes_targets_to_owner_agents_and_completes_without_prd_flow(self) -> None:
        created = create_project(requirement="优化当前项目的 PRODUCT.md 和 AGENTS.md", title="文档优化")
        run_id = created["run_id"]

        self.assertEqual(created["task_type"], "multi_agent_task")
        self.assertEqual(created["route_plan"]["mode"], "multi_agent_task")
        self.assertEqual(created["route_plan"]["loop_kind"], "multi_agent_collaboration_loop")
        self.assertIn("优化当前项目的 PRODUCT.md 和 AGENTS.md", created["route_plan"]["goal"])
        self.assertIn("所有目标均已完成", " ".join(created["route_plan"]["exit_conditions"]))
        self.assertEqual(created["next_handoff"]["agent"], "product-manager")
        current = status(run_id)
        self.assertEqual(current["run"]["status"], "running")
        self.assertEqual(current["run"]["current_step"], "multi-agent-task")
        jobs_by_step = {job["step_id"]: job for job in current["jobs"]}
        self.assertEqual(set(jobs_by_step), {"docs-product-md", "docs-agents-md"})
        self.assertEqual(jobs_by_step["docs-product-md"]["status"], "pending")
        self.assertEqual(jobs_by_step["docs-agents-md"]["status"], "pending")

        product_dispatch = engine.dispatch_next_agent_task(run_id=run_id, agent="product-manager")
        self.assertEqual(product_dispatch["agent"], "product-manager")
        self.assertEqual(product_dispatch["step_id"], "docs-product-md")
        self.assertEqual(product_dispatch["required_outputs"], ["PRODUCT.md"])
        self.assertIn("PRODUCT.md", product_dispatch["task_message"])
        self.assertIn("multi_agent_collaboration_loop", product_dispatch["task_message"])
        self.assertIn("自评是否满足退出条件", product_dispatch["task_message"])
        product_done = engine.complete_agent_step(
            run_id=run_id,
            job_id=product_dispatch["job_id"],
            output="# PRODUCT.md 更新报告\n\n已优化产品功能列表。",
        )
        self.assertTrue(product_done["ok"])
        self.assertIsNone(product_done["next_step"])
        after_product = status(run_id)
        self.assertEqual(after_product["run"]["status"], "running")
        self.assertEqual(after_product["run"]["current_step"], "targeted-agent-task")

        architect_dispatch = engine.dispatch_next_agent_task(run_id=run_id, agent="software-architect")
        self.assertEqual(architect_dispatch["agent"], "software-architect")
        self.assertEqual(architect_dispatch["step_id"], "docs-agents-md")
        self.assertEqual(architect_dispatch["required_outputs"], ["AGENTS.md"])
        self.assertIn("AGENTS.md", architect_dispatch["task_message"])
        architect_done = engine.complete_agent_step(
            run_id=run_id,
            job_id=architect_dispatch["job_id"],
            output="# AGENTS.md 更新报告\n\n已优化 AI 协作规则。",
        )
        self.assertTrue(architect_done["ok"])
        self.assertIsNone(architect_done["next_step"])
        self.assertEqual(architect_done["transition"]["event_type"], "workflow.completed")

        final = status(run_id)
        self.assertEqual(final["run"]["status"], "completed")
        self.assertEqual(final["run"]["current_step"], "")
        self.assertEqual(final["run"]["prd_version"], 0)
        self.assertFalse([job for job in final["jobs"] if job["status"] in {"pending", "running"}])
        self.assertIn("PRODUCT.md", {artifact["name"] for artifact in final["artifacts"]})
        self.assertIn("AGENTS.md", {artifact["name"] for artifact in final["artifacts"]})

        summary = engine.manager_summary(run_id=run_id)
        self.assertEqual(summary["run_status"], "completed")
        self.assertEqual(summary["loop"]["loop_kind"], "multi_agent_collaboration_loop")
        self.assertEqual(summary["loop_progress"]["exit_condition_status"], "met")
        self.assertIn("已完成", summary["next_action"])
        self.assertNotIn("确认产品交接说明", summary["next_action"])

    def test_direct_agent_task_runs_single_agent_and_completes_without_manager_flow(self) -> None:
        created = engine.create_agent_task(
            requirement="修复登录页验证码刷新后按钮状态不更新的 bug",
            agent="development-engineer",
            title="登录页验证码 bug",
        )
        run_id = created["run_id"]

        self.assertEqual(created["task_type"], "single_agent_task")
        self.assertEqual(created["route_plan"]["requested_agents"], ["development-engineer"])
        self.assertEqual(created["route_plan"]["loop_kind"], "single_agent_loop")
        self.assertIn("修复登录页验证码刷新后按钮状态不更新", created["route_plan"]["goal"])
        current = status(run_id)
        self.assertEqual(current["run"]["status"], "running")
        self.assertEqual(current["run"]["current_step"], "single-agent-task")
        jobs = [job for job in current["jobs"] if job["status"] == "pending"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["step_id"], "task-development-engineer-1")

        dispatch = engine.dispatch_next_agent_task(run_id=run_id, agent="development-engineer")
        self.assertEqual(dispatch["agent"], "development-engineer")
        self.assertEqual(dispatch["required_outputs"], ["development_report"])
        self.assertIn("定向 Agent 任务", dispatch["task_message"])
        self.assertIn("single_agent_loop", dispatch["task_message"])
        self.assertIn("exit_conditions", dispatch["task_message"])
        done = engine.complete_agent_step(
            run_id=run_id,
            job_id=dispatch["job_id"],
            output="# 修复报告\n\n已修复验证码按钮状态问题。",
        )
        self.assertTrue(done["ok"])
        self.assertEqual(done["transition"]["event_type"], "workflow.completed")

        final = status(run_id)
        self.assertEqual(final["run"]["status"], "completed")
        self.assertEqual(final["run"]["prd_version"], 0)
        self.assertFalse([job for job in final["jobs"] if job["status"] in {"pending", "running"}])
        self.assertIn("development_report", {artifact["name"] for artifact in final["artifacts"]})

    def test_loop_evaluation_can_enqueue_next_iteration_and_finalize_learning(self) -> None:
        created = engine.create_agent_task(
            requirement="修复登录页验证码刷新后按钮状态不更新的 bug",
            agent="development-engineer",
            title="登录页验证码 bug",
        )
        run_id = created["run_id"]

        first = engine.dispatch_next_agent_task(run_id=run_id, agent="development-engineer")
        first_done = engine.complete_agent_step(
            run_id=run_id,
            job_id=first["job_id"],
            output="# 修复报告\n\n已修复代码，但缺少 QA 证据。",
            metadata={
                "loop_evaluation": {
                    "exit_conditions_met": False,
                    "missing_evidence": ["缺少验证码刷新后的按钮状态验证结果"],
                    "next_agent": "qa-engineer",
                    "next_target": "验证验证码刷新后按钮状态",
                    "reason": "修复已完成但还没有测试证据",
                }
            },
        )

        self.assertEqual(first_done["transition"]["event_type"], "loop.iteration_enqueued")
        self.assertIn("loop-i2-qa-engineer", first_done["next_step"])
        after_first = status(run_id)
        self.assertEqual(after_first["run"]["status"], "running")
        qa_jobs = [job for job in after_first["jobs"] if job["status"] == "pending"]
        self.assertEqual(len(qa_jobs), 1)
        self.assertIn("loop-i2-qa-engineer", qa_jobs[0]["step_id"])
        evaluation = read_artifact(run_id, "loop_evaluation")
        evaluation_json = json.loads(evaluation["content"])
        self.assertEqual(evaluation_json["decision"], "continue_iteration")
        self.assertEqual(evaluation_json["iteration"], 1)
        self.assertEqual(evaluation_json["next_agent"], "qa-engineer")

        qa = engine.dispatch_next_agent_task(run_id=run_id, agent="qa-engineer")
        self.assertEqual(qa["required_outputs"], ["qa_report"])
        self.assertIn("验证验证码刷新后按钮状态", qa["task_message"])
        qa_done = engine.complete_agent_step(
            run_id=run_id,
            job_id=qa["job_id"],
            output="# QA 报告\n\n验证码刷新后按钮状态验证通过。",
            metadata={"loop_evaluation": {"exit_conditions_met": True, "reason": "QA 证据已补齐"}},
        )

        self.assertEqual(qa_done["transition"]["event_type"], "workflow.completed")
        final = status(run_id)
        self.assertEqual(final["run"]["status"], "completed")
        names = {artifact["name"] for artifact in final["artifacts"]}
        self.assertIn("loop_evaluation", names)
        self.assertIn("loop_learning", names)
        learning = json.loads(read_artifact(run_id, "loop_learning")["content"])
        self.assertEqual(learning["final_decision"], "complete_or_continue")
        self.assertIn("qa-engineer", learning["participants"])
        summary = engine.manager_summary(run_id=run_id)
        self.assertEqual(summary["latest_evaluation"]["iteration"], 2)
        self.assertEqual(summary["loop_progress"]["exit_condition_status"], "met")

    def test_loop_evaluation_blocks_when_next_agent_is_missing(self) -> None:
        created = engine.create_agent_task(
            requirement="修复登录页验证码刷新后按钮状态不更新的 bug",
            agent="development-engineer",
            title="登录页验证码 bug",
        )
        run_id = created["run_id"]
        dispatch = engine.dispatch_next_agent_task(run_id=run_id, agent="development-engineer")

        done = engine.complete_agent_step(
            run_id=run_id,
            job_id=dispatch["job_id"],
            output="# 修复报告\n\n缺少验证证据。",
            metadata={
                "loop_evaluation": {
                    "exit_conditions_met": False,
                    "missing_evidence": ["缺少验证码刷新后的按钮状态验证结果"],
                    "reason": "不知道下一步应该交给谁",
                }
            },
        )

        self.assertEqual(done["transition"]["event_type"], "loop.blocked")
        current = status(run_id)
        self.assertEqual(current["run"]["status"], "blocked")
        self.assertFalse([job for job in current["jobs"] if job["status"] in {"pending", "running"}])
        summary = engine.manager_summary(run_id=run_id)
        self.assertEqual(summary["latest_evaluation"]["decision"], "blocked_missing_next_agent")
        self.assertIn("缺少验证码刷新后的按钮状态验证结果", summary["loop_progress"]["missing_evidence"])
        self.assertIn("闭环已阻塞", summary["next_action"])

    def test_loop_evaluation_blocks_at_max_iterations(self) -> None:
        created = engine.create_agent_task(
            requirement="持续修复一个仍然失败的 bug",
            agent="development-engineer",
            title="循环上限验证",
        )
        run_id = created["run_id"]

        for expected_iteration in [1, 2, 3]:
            dispatch = engine.dispatch_next_agent_task(run_id=run_id, agent="development-engineer")
            done = engine.complete_agent_step(
                run_id=run_id,
                job_id=dispatch["job_id"],
                output=f"# 第 {expected_iteration} 轮修复\n\n仍缺少通过证据。",
                metadata={
                    "loop_evaluation": {
                        "exit_conditions_met": False,
                        "missing_evidence": [f"第 {expected_iteration} 轮仍未通过"],
                        "next_agent": "development-engineer",
                        "next_target": "继续修复失败用例",
                        "reason": "仍需继续修复",
                    }
                },
            )
            if expected_iteration < 3:
                self.assertEqual(done["transition"]["event_type"], "loop.iteration_enqueued")
            else:
                self.assertEqual(done["transition"]["event_type"], "loop.blocked")

        current = status(run_id)
        self.assertEqual(current["run"]["status"], "blocked")
        evaluation = json.loads(read_artifact(run_id, "loop_evaluation")["content"])
        self.assertEqual(evaluation["iteration"], 3)
        self.assertEqual(evaluation["decision"], "blocked_max_iterations")

    def test_pm_can_create_explicit_multi_agent_task_without_full_workflow(self) -> None:
        created = create_project(
            requirement="让产品和架构一起评估当前需求说明",
            title="产品架构定向评估",
            mode="multi_agent_task",
            requested_agents=["product-manager", "software-architect"],
            targets=[
                {"agent": "product-manager", "target": "补充产品验收口径", "outputs": ["product_review"]},
                {"agent": "software-architect", "target": "补充技术边界和风险", "outputs": ["architecture_review"]},
            ],
        )
        run_id = created["run_id"]

        self.assertEqual(created["route_plan"]["mode"], "multi_agent_task")
        self.assertEqual({target["agent"] for target in created["route_plan"]["targets"]}, {"product-manager", "software-architect"})
        product = engine.dispatch_next_agent_task(run_id=run_id, agent="product-manager")
        self.assertEqual(product["required_outputs"], ["product_review"])
        engine.complete_agent_step(run_id=run_id, job_id=product["job_id"], output="# 产品评估\n\n已补充验收口径。")
        self.assertEqual(status(run_id)["run"]["status"], "running")

        architect = engine.dispatch_next_agent_task(run_id=run_id, agent="software-architect")
        self.assertEqual(architect["required_outputs"], ["architecture_review"])
        engine.complete_agent_step(run_id=run_id, job_id=architect["job_id"], output="# 架构评估\n\n已补充边界和风险。")

        final = status(run_id)
        self.assertEqual(final["run"]["status"], "completed")
        self.assertEqual({artifact["name"] for artifact in final["artifacts"] if artifact["created_by"] != "workflow"} & {"product_review", "architecture_review"}, {"product_review", "architecture_review"})

    def test_prd_review_loop_generates_v2_before_delivery_chain(self) -> None:
        created = create_project(requirement="做一个极简 TODO Web 应用", title="TODO Web")
        run_id = created["run_id"]
        dispatch = engine.dispatch_next_agent_task(run_id=run_id)
        engine.complete_agent_step(run_id=run_id, job_id=dispatch["job_id"], output="# PRD V1\n\n初版。")

        review = request_prd_review(run_id, note="请多角色评审一下，输出 V2")
        self.assertTrue(review["ok"])
        self.assertEqual(review["prd_version"], 1)
        self.assertEqual(len(review["review_jobs"]), 4)
        self.assertEqual(status(run_id)["run"]["status"], "reviewing")

        for reviewer in ["software-architect", "ui-designer", "development-engineer", "qa-engineer"]:
            review_dispatch = engine.dispatch_next_agent_task(run_id=run_id, agent=reviewer)
            self.assertEqual(review_dispatch["agent"], reviewer)
            engine.complete_agent_step(
                run_id=run_id,
                job_id=review_dispatch["job_id"],
                output=f"# {reviewer} 评审\n\n建议补充 {reviewer} 关注点。",
            )

        after_reviews = status(run_id)
        self.assertEqual(after_reviews["run"]["status"], "prd_revision")
        self.assertEqual(len(after_reviews["reviews"]), 4)
        revision_dispatch = engine.dispatch_next_agent_task(run_id=run_id, agent="product-manager")
        self.assertIn("revision", revision_dispatch["step_id"])
        revision = engine.complete_agent_step(run_id=run_id, job_id=revision_dispatch["job_id"], output="# PRD V2\n\n已整合评审意见。")
        self.assertTrue(revision["ok"])
        self.assertIsNone(revision["next_step"])

        current = status(run_id)
        self.assertEqual(current["run"]["status"], "waiting_owner_review")
        self.assertEqual(current["run"]["prd_version"], 2)
        prd = read_artifact(run_id, "prd")
        self.assertEqual(prd["version"], 2)
        self.assertIn("PRD V2", prd["content"])

    def test_mcp_surface_is_minimal_and_uses_codex_multi_agents_loop_prefix(self) -> None:
        tool_names = [tool["name"] for tool in TOOLS]

        self.assertEqual(
            tool_names,
            [
                "codex_multi_agents_loop_init",
                "codex_multi_agents_loop_init_project",
                "codex_multi_agents_loop_create",
                "codex_multi_agents_loop_create_task",
                "codex_multi_agents_loop_status",
                "codex_multi_agents_loop_prepare_handoff",
                "codex_multi_agents_loop_dispatch_next",
                "codex_multi_agents_loop_complete_agent_step",
                "codex_multi_agents_loop_manager_summary",
                "codex_multi_agents_loop_confirm_prd",
                "codex_multi_agents_loop_request_prd_review",
                "codex_multi_agents_loop_memory_pack",
                "codex_multi_agents_loop_memory_pull",
                "codex_multi_agents_loop_memory_sync",
                "codex_multi_agents_loop_list_artifacts",
                "codex_multi_agents_loop_read_artifact",
                "codex_multi_agents_loop_inspect",
            ],
        )
        self.assertFalse(any("lark" in name or "feishu" in name or "bug" in name or "gate" in name for name in tool_names))

    def test_mcp_tool_call_rejects_missing_tool_name(self) -> None:
        response = mcp_server._handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"arguments": {}}}
        )

        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response["error"]["code"], -32000)
        self.assertIn("tool name must be a non-empty string", response["error"]["message"])

    def test_mcp_server_accepts_content_length_input_and_writes_newline_json_response(self) -> None:
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
        }

        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "codex-multi-agents-loop-mcp")],
            input=_mcp_frame(initialize),
            cwd=self.tmp.name,
            capture_output=True,
            timeout=3,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
        self.assertFalse(completed.stdout.startswith(b"Content-Length:"), completed.stdout.decode("utf-8", "replace"))
        response = _read_mcp_json_line(completed.stdout)
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["serverInfo"]["name"], PLUGIN_NAME)
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")

    def test_mcp_server_supports_codex_stdio_newline_json_session(self) -> None:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "codex", "version": "0.142.5"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        stdin = "".join(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n" for message in messages).encode("utf-8")

        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "codex-multi-agents-loop-mcp")],
            input=stdin,
            cwd=self.tmp.name,
            capture_output=True,
            timeout=3,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
        responses = [json.loads(line) for line in completed.stdout.decode("utf-8").splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2])
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], PLUGIN_NAME)
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(responses[1]["result"]["tools"][0]["name"], "codex_multi_agents_loop_init")

    def test_legacy_worker_platform_and_hook_python_files_are_removed(self) -> None:
        legacy_files = [
            ROOT / "agents_workflow" / "platforms.py",
            ROOT / "agents_workflow" / "worker_daemon.py",
            ROOT / "agents_workflow" / "host_hooks.py",
        ]
        for path in legacy_files:
            self.assertFalse(path.exists(), f"legacy python file should be removed: {path}")

        remaining_python = {
            path.name
            for path in (ROOT / "agents_workflow").glob("*.py")
            if path.name != "__init__.py"
        }
        self.assertEqual(
            remaining_python,
            {
                "capabilities.py",
                "cli.py",
                "config.py",
                "definitions.py",
                "engine.py",
                "mcp_server.py",
                "paths.py",
                "storage.py",
                "workflow_log.py",
            },
        )

        checked_files = [
            ROOT / "agents_workflow" / "engine.py",
            ROOT / "agents_workflow" / "cli.py",
            ROOT / "README.md",
            ROOT / "skills" / PLUGIN_NAME / "SKILL.md",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in checked_files)
        for forbidden in [
            "run_worker_once",
            "run_worker_until_blocked",
            "auto_run_to_gate",
            "watch_poll_interval_seconds",
            "build_agent_command",
            "maybe_run_command",
            "host_hook",
            "worker_daemon",
            "runtime_subagent",
            "stop_and_ask_owner_to_at_agent",
            "只能重新输出交接指令",
        ]:
            self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
