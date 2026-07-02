from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from copy import deepcopy
from pathlib import Path

from delivery_workflow.config import DEFAULT_CONFIG_TEMPLATE, WORKSPACE_CONFIG_NAME, load_config
from delivery_workflow.definitions import load_workflow
from delivery_workflow.engine import create_project, read_artifact, status
from delivery_workflow.mcp_server import TOOLS


EXPECTED_AGENTS = [
    "product-manager",
    "ui-designer",
    "frontend-impl",
    "backend-impl",
    "qa-tester",
]

PLUGIN_NAME = "codex-delivery-workflow"
ROOT = Path(__file__).resolve().parents[1]

EXPECTED_OUTPUTS = [
    "prd",
    "design_spec",
    "frontend_result",
    "backend_result",
    "qa_report",
]

EXPECTED_AGENT_SKILLS = {
    "product-manager": ["codex-delivery-prd"],
    "ui-designer": ["codex-delivery-ui-spec"],
    "frontend-impl": ["codex-delivery-frontend-impl"],
    "backend-impl": ["codex-delivery-backend-impl"],
    "qa-tester": ["codex-delivery-qa"],
}


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


class CodexDeliveryWorkflowTest(unittest.TestCase):
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
        Path(WORKSPACE_CONFIG_NAME).write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_default_config_is_codex_delivery_only_without_feishu_or_quality_gate(self) -> None:
        config = load_config()

        self.assertNotIn("lark", config)
        self.assertNotIn("quality_gate", config)
        self.assertEqual(config["code_platforms"]["default"], "codex")
        self.assertEqual(set(config["code_platforms"]["executors"].keys()), {"codex"})
        self.assertEqual(config["storage"]["home"], ".codex-delivery-workflow")
        self.assertEqual(config["storage"]["artifact_root"], "delivery-artifacts")

    def test_workflow_definition_is_the_five_agent_codex_delivery_chain(self) -> None:
        workflow = load_workflow()
        steps = workflow.steps

        self.assertEqual(workflow.workflow_id, PLUGIN_NAME)
        self.assertEqual(workflow.name, "Codex 交付工作流")
        self.assertEqual([step["id"] for step in steps], EXPECTED_AGENTS)
        self.assertEqual([step["agent"] for step in steps], EXPECTED_AGENTS)
        self.assertTrue(all(step["executor"] == "agent" for step in steps))
        self.assertNotIn("lark-doc", {step["executor"] for step in steps})
        self.assertNotIn("gate", {step["executor"] for step in steps})
        self.assertEqual([step["outputs"][0] for step in steps], EXPECTED_OUTPUTS)
        self.assertEqual(steps[-1].get("next", {}), {})

    def test_agent_toml_files_are_the_only_declared_child_agents(self) -> None:
        agent_dir = ROOT / "agents"
        files = {path.name for path in agent_dir.glob("*.toml")}

        self.assertEqual(files, {f"{agent}.toml" for agent in EXPECTED_AGENTS})
        for agent in EXPECTED_AGENTS:
            path = agent_dir / f"{agent}.toml"
            profile = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(profile["id"], agent)
            self.assertIn(profile["agent_type"], {"worker", "explorer"})
            self.assertEqual(profile["model"], "gpt-5")
            self.assertEqual(profile["model_reasoning_effort"], "high")
            self.assertEqual(profile["sandbox_mode"], "workspace-write")
            self.assertGreaterEqual(len(profile["nickname_candidates"]), 3)
            self.assertEqual(profile["skills"], EXPECTED_AGENT_SKILLS[agent])
            self.assertGreater(len(profile["developer_instructions"]), 800)
            for required in ("职责边界", "规范来源", "交付要求"):
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

    def test_plugin_names_and_docs_are_chinese(self) -> None:
        plugin = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        skill = (ROOT / "skills" / PLUGIN_NAME / "SKILL.md").read_text(encoding="utf-8")

        self.assertEqual(plugin["name"], PLUGIN_NAME)
        self.assertEqual(pyproject["project"]["name"], PLUGIN_NAME)
        self.assertEqual(marketplace["plugins"][0]["name"], PLUGIN_NAME)
        self.assertIn("Codex 交付工作流", plugin["interface"]["displayName"])
        self.assertTrue(_has_cjk(plugin["description"]))
        self.assertTrue(_has_cjk(plugin["interface"]["shortDescription"]))
        self.assertTrue(_has_cjk(plugin["interface"]["longDescription"]))
        self.assertIn(f"name: {PLUGIN_NAME}", skill)
        for text in (readme, skill):
            self.assertTrue(_has_cjk(text))
            self.assertNotIn("Codex Workflow", text)
            self.assertNotIn("When To Use", text)
            self.assertNotIn("Core Rules", text)

    def test_no_claude_code_config_or_old_codex_workflow_name_remains(self) -> None:
        self.assertFalse((ROOT / ".claude").exists())
        self.assertFalse((ROOT / ".claude-plugin").exists())
        self.assertFalse((ROOT / "hooks.json").exists())
        self.assertFalse((ROOT / "hooks").exists())
        self.assertFalse((ROOT / "workflow" / "codex-workflow.toml").exists())
        self.assertFalse((ROOT / "skills" / "codex-workflow").exists())
        self.assertFalse((ROOT / "codex-workflow.config.json").exists())

    def test_create_project_runs_all_child_agent_steps_to_completion(self) -> None:
        created = create_project(requirement="做一个极简 TODO Web 应用", title="TODO Web")
        run_id = created["run_id"]

        current = status(run_id)
        self.assertEqual(current["run"]["workflow_id"], PLUGIN_NAME)
        self.assertEqual(current["run"]["status"], "completed")
        self.assertEqual(current["run"]["current_step"], "")
        self.assertEqual([step["step_id"] for step in current["step_runs"]], EXPECTED_AGENTS)
        self.assertTrue(all(step["status"] == "completed" for step in current["step_runs"]))
        self.assertEqual(current["gates"], [])

        artifact_names = {artifact["name"] for artifact in current["artifacts"]}
        self.assertTrue({"raw_requirement", "project_context", *EXPECTED_OUTPUTS}.issubset(artifact_names))
        for output in EXPECTED_OUTPUTS:
            artifact = read_artifact(run_id, output)
            self.assertIn("工作流步骤", artifact["content"])

        serialized_status = json.dumps(current, ensure_ascii=False).lower()
        self.assertNotIn("lark", serialized_status)
        self.assertNotIn("feishu", serialized_status)
        self.assertNotIn("飞书", serialized_status)

    def test_mcp_surface_is_minimal_and_uses_codex_delivery_workflow_prefix(self) -> None:
        tool_names = [tool["name"] for tool in TOOLS]

        self.assertEqual(
            tool_names,
            [
                "codex_delivery_workflow_init",
                "codex_delivery_workflow_create",
                "codex_delivery_workflow_status",
                "codex_delivery_workflow_worker_once",
                "codex_delivery_workflow_worker_until_idle",
                "codex_delivery_workflow_list_artifacts",
                "codex_delivery_workflow_read_artifact",
                "codex_delivery_workflow_inspect",
            ],
        )
        self.assertFalse(any("lark" in name or "feishu" in name or "bug" in name or "gate" in name for name in tool_names))


if __name__ == "__main__":
    unittest.main()
