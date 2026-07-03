from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from copy import deepcopy
from pathlib import Path

from delivery_workflow import engine
from delivery_workflow.config import DEFAULT_CONFIG_TEMPLATE, WORKSPACE_CONFIG_NAME, initialize_project_workspace, load_config
from delivery_workflow.definitions import load_workflow
from delivery_workflow.engine import confirm_prd, create_project, read_artifact, request_prd_review, status
from delivery_workflow.mcp_server import TOOLS


WORKFLOW_AGENTS = [
    "product-manager",
    "ui-designer",
    "frontend-impl",
    "backend-impl",
    "qa-tester",
]

PROJECT_AGENTS = ["delivery-manager", *WORKFLOW_AGENTS]

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
    "delivery-manager": ["codex-delivery-workflow"],
    "product-manager": ["codex-delivery-prd"],
    "ui-designer": ["codex-delivery-ui-spec"],
    "frontend-impl": ["codex-delivery-frontend-impl"],
    "backend-impl": ["codex-delivery-backend-impl"],
    "qa-tester": ["codex-delivery-qa"],
}

EXPECTED_AGENT_REASONING = {
    "delivery-manager": "medium",
    "product-manager": "high",
    "ui-designer": "high",
    "frontend-impl": "high",
    "backend-impl": "high",
    "qa-tester": "high",
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
        self.assertNotIn("workflow", config)
        self.assertNotIn("code_platforms", config)
        self.assertEqual(config["storage"]["home"], ".codex/delivery-workflow")
        self.assertEqual(config["storage"]["artifact_root"], "docs/delivery")
        self.assertEqual(config["storage"]["memory_root"], ".codex/delivery-workflow/memory")

    def test_workflow_definition_waits_for_owner_after_prd_v1(self) -> None:
        workflow = load_workflow()
        steps = workflow.steps

        self.assertEqual(workflow.workflow_id, PLUGIN_NAME)
        self.assertEqual(workflow.name, "Codex 交付工作流")
        self.assertEqual([step["id"] for step in steps], WORKFLOW_AGENTS)
        self.assertEqual([step["agent"] for step in steps], WORKFLOW_AGENTS)
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
            self.assertIn(profile["agent_type"], {"worker", "explorer"})
            self.assertEqual(profile["model"], "gpt-5.5")
            self.assertEqual(profile["model_reasoning_effort"], EXPECTED_AGENT_REASONING[agent])
            self.assertEqual(profile["sandbox_mode"], "workspace-write")
            self.assertGreaterEqual(len(profile["nickname_candidates"]), 3)
            self.assertEqual(profile["skills"], EXPECTED_AGENT_SKILLS[agent])
            self.assertGreater(len(profile["developer_instructions"]), 800)
            for required in ("职责边界", "规范来源", "交付要求"):
                if agent != "delivery-manager":
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
        self.assertEqual(plugin["mcpServers"], "./.codex-plugin/mcp.json")
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
            self.assertNotIn("enable_agent_cli", text)
            self.assertNotIn("auto_run_to_idle", text)
            self.assertNotIn("code_platforms", text)

    def test_mcp_config_has_single_plugin_source_without_editor_warning_expression(self) -> None:
        plugin = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        mcp_path = ROOT / plugin["mcpServers"]
        mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
        args = mcp["mcpServers"][PLUGIN_NAME]["args"]

        self.assertFalse((ROOT / ".mcp.json").exists())
        self.assertEqual(mcp_path, ROOT / ".codex-plugin" / "mcp.json")
        self.assertIn("scripts/codex-deliveryflow-mcp", args[-1])
        self.assertIn("$CODEX_PLUGIN_ROOT", args[-1])
        self.assertNotIn("${CODEX_PLUGIN_ROOT", args[-1])

    def test_init_materializes_project_agents_and_memory(self) -> None:
        initialized = initialize_project_workspace(overwrite_config=True, overwrite_agents=True)

        self.assertEqual(Path(initialized["agent_dir"]).name, "agents")
        self.assertEqual({item["agent"] for item in initialized["agents"]}, set(PROJECT_AGENTS))
        self.assertEqual({item["agent"] for item in initialized["memories"]}, set(PROJECT_AGENTS))
        self.assertTrue((Path(".codex") / "agents" / "delivery-manager.toml").exists())
        self.assertTrue((Path(".codex") / "agents" / "product-manager.toml").exists())
        self.assertTrue((Path(".codex") / "delivery-workflow" / "memory" / "delivery-manager.md").exists())
        project_agent = tomllib.loads((Path(".codex") / "agents" / "product-manager.toml").read_text(encoding="utf-8"))
        self.assertEqual(project_agent["name"], "product-manager")
        self.assertNotIn("id", project_agent)
        self.assertIn("老板可以直接通过 `@product-manager` 找你", project_agent["developer_instructions"])

    def test_no_claude_code_config_or_old_codex_workflow_name_remains(self) -> None:
        self.assertFalse((ROOT / ".claude").exists())
        self.assertFalse((ROOT / ".claude-plugin").exists())
        self.assertFalse((ROOT / "hooks.json").exists())
        self.assertFalse((ROOT / "hooks").exists())
        self.assertFalse((ROOT / "workflow" / "codex-workflow.toml").exists())
        self.assertFalse((ROOT / "skills" / "codex-workflow").exists())
        self.assertFalse((ROOT / "codex-workflow.config.json").exists())

    def test_create_project_prepares_native_agent_handoff_without_claiming_job(self) -> None:
        created = create_project(requirement="做一个极简 TODO Web 应用", title="TODO Web")
        run_id = created["run_id"]

        current = status(run_id)
        self.assertEqual(current["run"]["workflow_id"], PLUGIN_NAME)
        self.assertEqual(created["execution_policy"]["mode"], "native_agent_handoff")
        self.assertNotIn("enable_agent_cli", created["execution_policy"])
        handoff = created["next_handoff"]
        self.assertTrue(handoff["ok"])
        self.assertFalse(handoff["idle"])
        self.assertEqual(handoff["run_id"], run_id)
        self.assertEqual(handoff["step_id"], "product-manager")
        self.assertEqual(handoff["agent"], "product-manager")
        self.assertEqual(handoff["mention"], "@product-manager")
        self.assertEqual(handoff["claim_tool"], "codex_delivery_workflow_dispatch_next")
        self.assertEqual(handoff["complete_tool"], "codex_delivery_workflow_complete_agent_step")
        self.assertIn("@product-manager", handoff["handoff_message"])
        self.assertIn("codex_delivery_workflow_dispatch_next", handoff["handoff_message"])
        self.assertIn(run_id, handoff["handoff_message"])
        self.assertNotIn("runtime", handoff)
        self.assertNotIn("spawn_agent", json.dumps(handoff, ensure_ascii=False))
        self.assertEqual(current["run"]["status"], "prd_draft")
        self.assertEqual(current["run"]["current_step"], "product-manager")
        self.assertEqual(current["run"]["prd_version"], 0)
        self.assertEqual(current["step_runs"], [])
        self.assertEqual(len(current["jobs"]), 1)
        self.assertEqual(current["jobs"][0]["step_id"], "product-manager")
        self.assertEqual(current["jobs"][0]["status"], "pending")
        self.assertEqual(current["gates"], [])

        artifact_names = {artifact["name"] for artifact in current["artifacts"]}
        self.assertEqual({"raw_requirement", "project_context", "product-manager_agent_task"}, artifact_names)

        self.assertTrue(hasattr(engine, "prepare_agent_handoff"))
        self.assertTrue(hasattr(engine, "dispatch_next_agent_task"))
        self.assertTrue(engine.dispatch_next_agent_task(run_id=run_id, agent="ui-designer")["idle"])
        still_pending = status(run_id)
        self.assertEqual(still_pending["jobs"][0]["status"], "pending")
        self.assertEqual(still_pending["step_runs"], [])
        dispatch = engine.dispatch_next_agent_task(run_id=run_id, agent="product-manager")
        self.assertTrue(dispatch["ok"])
        self.assertFalse(dispatch["idle"])
        self.assertEqual(dispatch["run_id"], run_id)
        self.assertEqual(dispatch["step_id"], "product-manager")
        self.assertEqual(dispatch["agent"], "product-manager")
        self.assertEqual(dispatch["agent_type"], "worker")
        self.assertEqual(dispatch["model"], "gpt-5.5")
        self.assertEqual(dispatch["model_reasoning_effort"], "high")
        self.assertEqual(dispatch["skills"], ["codex-delivery-prd"])
        self.assertEqual(dispatch["claim_mode"], "native_project_agent")
        self.assertNotIn("runtime", dispatch)
        self.assertNotIn("spawn_agent", json.dumps(dispatch, ensure_ascii=False))
        self.assertIn("product-manager", dispatch["task_message"])
        self.assertIn("做一个极简 TODO Web 应用", dispatch["task_message"])
        self.assertIn("PRD V1", dispatch["task_message"])
        self.assertTrue((Path(".codex") / "agents" / "delivery-manager.toml").exists())
        self.assertTrue((Path(".codex") / "delivery-workflow" / "memory" / "product-manager.md").exists())

        after_dispatch = status(run_id)
        self.assertEqual(after_dispatch["jobs"][0]["status"], "running")
        self.assertEqual(after_dispatch["step_runs"][0]["step_id"], "product-manager")
        self.assertEqual(after_dispatch["step_runs"][0]["status"], "running")

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
        self.assertEqual(summary["manager_agent"], "delivery-manager")
        self.assertEqual(summary["current_step"], "owner-review")
        self.assertEqual(summary["prd_version"], 1)
        self.assertEqual(summary["completed_steps"], ["product-manager"])
        self.assertEqual(summary["pending_jobs"], [])
        self.assertIn("确认 PRD V1", summary["next_action"])
        self.assertIn("prd", [artifact["name"] for artifact in summary["artifacts"]])
        self.assertIn("product-manager", summary["last_update"])

        confirmed = confirm_prd(run_id)
        self.assertTrue(confirmed["ok"])
        self.assertEqual(confirmed["next_step"], "ui-designer")
        after_confirm = status(run_id)
        self.assertEqual(after_confirm["run"]["status"], "running")
        self.assertEqual(after_confirm["run"]["current_step"], "ui-designer")
        self.assertIn("ui-designer", {job["step_id"] for job in after_confirm["jobs"] if job["status"] == "pending"})

        serialized_status = json.dumps(current, ensure_ascii=False).lower()
        self.assertNotIn("lark", serialized_status)
        self.assertNotIn("feishu", serialized_status)
        self.assertNotIn("飞书", serialized_status)

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

        for reviewer in ["ui-designer", "frontend-impl", "backend-impl", "qa-tester"]:
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

    def test_mcp_surface_is_minimal_and_uses_codex_delivery_workflow_prefix(self) -> None:
        tool_names = [tool["name"] for tool in TOOLS]

        self.assertEqual(
            tool_names,
            [
                "codex_delivery_workflow_init",
                "codex_delivery_workflow_init_project",
                "codex_delivery_workflow_create",
                "codex_delivery_workflow_status",
                "codex_delivery_workflow_prepare_handoff",
                "codex_delivery_workflow_dispatch_next",
                "codex_delivery_workflow_complete_agent_step",
                "codex_delivery_workflow_manager_summary",
                "codex_delivery_workflow_confirm_prd",
                "codex_delivery_workflow_request_prd_review",
                "codex_delivery_workflow_list_artifacts",
                "codex_delivery_workflow_read_artifact",
                "codex_delivery_workflow_inspect",
            ],
        )
        self.assertFalse(any("lark" in name or "feishu" in name or "bug" in name or "gate" in name for name in tool_names))

    def test_legacy_worker_platform_and_hook_python_files_are_removed(self) -> None:
        legacy_files = [
            ROOT / "delivery_workflow" / "platforms.py",
            ROOT / "delivery_workflow" / "worker_daemon.py",
            ROOT / "delivery_workflow" / "host_hooks.py",
        ]
        for path in legacy_files:
            self.assertFalse(path.exists(), f"legacy python file should be removed: {path}")

        remaining_python = {
            path.name
            for path in (ROOT / "delivery_workflow").glob("*.py")
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
            ROOT / "delivery_workflow" / "engine.py",
            ROOT / "delivery_workflow" / "cli.py",
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
            "spawn_agent",
        ]:
            self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
