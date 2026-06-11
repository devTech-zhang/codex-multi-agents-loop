from __future__ import annotations

import json
import io
import os
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from delivery_workflow.capabilities import doctor
from delivery_workflow.config import DEFAULT_CONFIG_TEMPLATE, PLUGIN_ROOT, WORKSPACE_CONFIG_NAME, initialize_project_workspace, lark_chat_id, load_config
from delivery_workflow.definitions import load_workflow
from delivery_workflow.engine import WorkflowError, create_project, current_project_status, delete_current_project, emit_event, enqueue_step, execute_step, handle_lark_card_event, read_artifact, request_bug_fix, retry_prd_approval_lark, run_worker_once, run_worker_until_blocked, status, submit_gate, watch_run, write_artifact
from delivery_workflow.engine import _compose_lark_doc_xml, _compose_prd_v2_lark_markdown, _host_escalation_payload, _host_lark_retry_command
from delivery_workflow.lark import _is_keychain_unavailable, build_prd_approval_card, create_doc_as_bot
from delivery_workflow.lark_daemon import _code_fingerprint, _terminate_process, ensure_lark_event_consumer
from delivery_workflow.host_hooks import main as host_hook_main
from delivery_workflow.lark_events import sdk_event_to_payload
from delivery_workflow.lark_sdk import load_sdk_credentials, sdk_preflight
from delivery_workflow.mcp_server import _call_tool as call_mcp_tool
from delivery_workflow.platforms import build_agent_command, select_dev_executor
from delivery_workflow.storage import connect


def _merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = value
    return base


class SoftwareDeliveryWorkflowTest(unittest.TestCase):
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
        config["lark"]["enabled"] = False
        config["workflow"]["continue_after_gate"] = False
        env_values: dict[str, str] = {}
        lark_override = (overrides or {}).get("lark") if isinstance(overrides, dict) else None
        if isinstance(lark_override, dict):
            if lark_override.get("chat_id"):
                env_values["LARK_CHAT_ID"] = str(lark_override["chat_id"])
            sdk_override = lark_override.get("sdk")
            if isinstance(sdk_override, dict):
                if sdk_override.get("app_id"):
                    env_values["LARK_APP_ID"] = str(sdk_override["app_id"])
                if sdk_override.get("app_secret"):
                    env_values["LARK_APP_SECRET"] = str(sdk_override["app_secret"])
        if overrides:
            config = _merge(config, overrides)
        Path(WORKSPACE_CONFIG_NAME).write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        env_path = Path(".env")
        if env_values:
            env_path.write_text("".join(f"{key}={value}\n" for key, value in env_values.items()), encoding="utf-8")
        elif env_path.exists():
            env_path.unlink()

    def test_initialize_project_workspace_creates_config_db_and_directories(self) -> None:
        Path(WORKSPACE_CONFIG_NAME).unlink()

        result = initialize_project_workspace()

        self.assertTrue(Path(result["config_path"]).exists())
        self.assertTrue(Path(result["db_path"]).exists())
        self.assertTrue(Path(".delivery-workflow").is_dir())
        self.assertTrue(Path(".delivery-workflow/logs").is_dir())
        self.assertTrue(Path("delivery-project").is_dir())
        self.assertTrue(Path("source-code").is_dir())
        workflow_log = Path(".delivery-workflow/logs/workflow.log").read_text(encoding="utf-8")
        self.assertIn("project_workspace.init.started", workflow_log)
        self.assertIn("project_workspace.init.completed", workflow_log)

    def test_lark_sensitive_values_are_loaded_only_from_env(self) -> None:
        self.write_config(
            {
                "lark": {
                    "chat_id": "oc_config",
                    "sdk": {"app_id": "cli_config", "app_secret": "secret_config"},
                }
            }
        )
        Path(".env").write_text(
            "LARK_CHAT_ID=oc_env\nLARK_APP_ID=cli_env\nLARK_APP_SECRET=secret_env\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
            credentials = load_sdk_credentials(config)

        self.assertEqual(lark_chat_id(config), "oc_env")
        self.assertEqual(credentials.app_id, "cli_env")
        self.assertEqual(credentials.app_secret, "secret_env")
        self.assertEqual(credentials.source, ".env")

    def test_lark_cli_subprocess_uses_project_env_credentials(self) -> None:
        Path(".env").write_text(
            "LARK_CHAT_ID=oc_env\nLARK_APP_ID=cli_project\nLARK_APP_SECRET=secret_project\n",
            encoding="utf-8",
        )

        class FakeCompleted:
            returncode = 0
            stdout = json.dumps({"ok": True})
            stderr = ""

        with (
            patch.dict(os.environ, {"LARK_APP_ID": "cli_global", "LARK_APP_SECRET": "secret_global"}),
            patch("delivery_workflow.lark.subprocess.run", return_value=FakeCompleted()) as run,
        ):
            result = create_doc_as_bot("测试文档", "正文", dry_run=False)

        self.assertTrue(result["ok"])
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["LARK_APP_ID"], "cli_project")
        self.assertEqual(env["LARK_APP_SECRET"], "secret_project")
        self.assertNotIn("secret_project", json.dumps(result, ensure_ascii=False))

    def test_workflow_definition_separates_step_categories(self) -> None:
        workflow = load_workflow()
        categories = {step["category"] for step in workflow.steps}
        executors = {step["executor"] for step in workflow.steps}
        steps = {step["id"]: step for step in workflow.steps}

        self.assertIn("interactive", categories)
        self.assertIn("automated", categories)
        self.assertIn("notification", categories)
        self.assertIn("gate", executors)
        self.assertIn("agent", executors)
        self.assertIn("dev-runner", executors)
        self.assertIn("lark-doc", executors)
        self.assertNotIn("release-approval", steps)
        self.assertEqual(steps["regression-testing"]["next"]["default"], "publish-test-report-doc")
        self.assertEqual(steps["regression-testing"]["next"]["failed"], "bug-fix")
        self.assertEqual(steps["frontend-tech-design"]["next"]["default"], "backend-tech-design")
        self.assertEqual(steps["backend-tech-design"]["next"]["default"], "tech-review")
        self.assertEqual(steps["tech-review"]["next"]["default"], "publish-frontend-tech-doc")
        self.assertEqual(steps["backend-development"]["next"]["default"], "development-self-test")
        self.assertEqual(steps["development-self-test"]["next"]["default"], "test-case-design")
        self.assertIn("development_self_test_report", steps["test-case-design"]["inputs"])
        self.assertIn("development_self_test_report", steps["regression-testing"]["inputs"])
        self.assertIn("bug_fix_result", steps["regression-testing"]["inputs"])
        self.assertIn("test_report", steps["bug-fix"]["inputs"])
        self.assertIn("bug_fix_result", steps["bug-fix"]["inputs"])
        self.assertNotIn("ui-high-fidelity-design", steps)
        self.assertNotIn("release-approval_gate", steps["final-report"].get("inputs", []))

    def test_shared_agent_registry_covers_workflow_agents(self) -> None:
        workflow = load_workflow()
        agents = {step["agent"] for step in workflow.steps if step.get("executor") == "agent"}
        registry = (PLUGIN_ROOT / "delivery_workflow" / "references" / "00-agent-registry.md").read_text(encoding="utf-8")
        missing = sorted(agent for agent in agents if f"### {agent}" not in registry)
        self.assertEqual(missing, [])

        manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["agents"], ["./.claude/agents/delivery-workflow.md"])

    def test_host_hook_commands_include_plugin_pythonpath(self) -> None:
        for rel_path in ("hooks.json", "hooks/hooks.json"):
            config = json.loads((PLUGIN_ROOT / rel_path).read_text(encoding="utf-8"))
            rendered = json.dumps(config, ensure_ascii=False)
            for hook_group in config["hooks"].values():
                for entry in hook_group:
                    for hook in entry["hooks"]:
                        command = hook["command"]
                        self.assertIn('PYTHONPATH="${CLAUDE_PLUGIN_ROOT}:${PYTHONPATH}"', command)
                        self.assertIn("python3 -m delivery_workflow.host_hooks", command)
            self.assertNotIn('"command": "python3 -m delivery_workflow.host_hooks', rendered)

    def test_platform_executor_policy(self) -> None:
        self.assertEqual(select_dev_executor("codex"), "codex")
        self.assertEqual(select_dev_executor("claude-code"), "claude")
        self.assertEqual(select_dev_executor("openclaw"), "claude")
        # unknown platform falls back to auto-detect; CI 环境可能没有 claude/codex
        detected = select_dev_executor("something-else")
        self.assertIn(detected, {"claude", "codex"})

    def test_workspace_config_controls_defaults_and_dev_platforms(self) -> None:
        self.write_config(
            {
                "workflow": {"auto_start": False},
                "code_platforms": {"default": "codex", "frontend": "codex", "backend": "claude-code"},
                "lark": {"enabled": True, "dry_run": True, "chat_id": "oc_config", "send_step_notifications": False},
            }
        )

        self.assertEqual(load_config()["code_platforms"]["default"], "codex")
        created = create_project(requirement="create settings page", title="settings")
        run_id = created["run_id"]
        self.assertEqual(created["execution_policy"]["mode"], "prepared_only")
        self.assertFalse(created["execution_policy"]["enable_agent_cli"])
        context = json.loads(read_artifact(run_id, "project_context")["content"])
        self.assertEqual(context["platform"], "codex")
        self.assertEqual(context["dev_executor"], "codex")
        self.assertEqual(context["lark_chat_id"], "oc_config")
        self.assertNotIn("auto_run", created)

        write_artifact(run_id, "dev_tasks", "tasks", category="dev", created_by="test")
        write_artifact(run_id, "frontend_tech_design", "frontend", category="tech", created_by="test")
        write_artifact(run_id, "ui_design_spec", "ui", category="ui", created_by="test")
        result = execute_step(run_id, "frontend-development")
        self.assertEqual(result["result"]["executor"], "codex")
        self.assertIn("codex", result["result"]["execution"]["command"][0])
        workflow_log = Path(".delivery-workflow/logs/workflow.log").read_text(encoding="utf-8")
        self.assertIn("project.create.started", workflow_log)
        self.assertIn("step.started", workflow_log)
        self.assertIn("agent.execution", workflow_log)
        self.assertIn("frontend-development", workflow_log)

    def test_claude_code_uses_claude_print_mode(self) -> None:
        self.write_config({"code_platforms": {"enable_agent_cli": False}})
        prompt = Path("task.md")
        prompt.write_text("请实现这个任务", encoding="utf-8")

        command = build_agent_command("claude-code", prompt)

        self.assertEqual(command.executor, "claude")
        self.assertEqual(command.command[-1], "-p")
        self.assertIn("--disable-slash-commands", command.command)
        self.assertIn("--permission-mode", command.command)
        self.assertIn("acceptEdits", command.command)
        self.assertEqual(command.input_text, "请实现这个任务")
        rendered = json.dumps(command.command, ensure_ascii=False)
        self.assertNotIn("claude-code", rendered)

    def test_agent_stdout_becomes_real_artifact_content(self) -> None:
        self.write_config({"code_platforms": {"enable_agent_cli": True}})
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批", auto_start=False)
        run_id = created["run_id"]

        with patch(
            "delivery_workflow.engine.maybe_run_command",
            return_value={"executed": True, "returncode": 0, "stdout": "# PRD v1\n\n真实 PRD 内容", "stderr": "", "command": ["claude", "-p"]},
        ):
            execute_step(run_id, "prd-v1")

        self.assertIn("真实 PRD 内容", read_artifact(run_id, "prd_v1")["content"])

    def test_agent_permission_prompt_does_not_complete_step(self) -> None:
        self.write_config({"code_platforms": {"enable_agent_cli": True}})
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批", auto_start=False)

        with (
            patch(
                "delivery_workflow.engine.maybe_run_command",
                return_value={"executed": True, "returncode": 0, "stdout": "I need your approval to write these files.", "stderr": "", "command": ["claude", "-p"]},
            ),
            self.assertRaises(WorkflowError) as context,
        ):
            execute_step(created["run_id"], "prd-v1")

        self.assertIn("permission approval", str(context.exception))
        current = status(created["run_id"])
        self.assertEqual(current["run"]["status"], "failed")
        step_run = next(item for item in current["step_runs"] if item["step_id"] == "prd-v1")
        self.assertEqual(step_run["status"], "failed")
        with self.assertRaises(WorkflowError):
            enqueue_step(created["run_id"], "prd-validation")

    def test_ui_design_spec_prefers_written_design_md_over_stdout_summary(self) -> None:
        self.write_config({"code_platforms": {"enable_agent_cli": True}})
        created = create_project(requirement="做 TODO H5", title="TODO H5", auto_start=False)
        run_id = created["run_id"]
        write_artifact(run_id, "prd_v2", "# PRD v2\n\n完整需求", category="prd", created_by="test")
        Path("delivery-project/ui_design_spec.md").write_text("# DESIGN.md\n\n详尽设计规范正文", encoding="utf-8")

        with patch(
            "delivery_workflow.engine.maybe_run_command",
            return_value={"executed": True, "returncode": 0, "stdout": "# Summary\n\n只是一段摘要", "stderr": "", "command": ["claude", "-p"]},
        ):
            execute_step(run_id, "ui-design-spec")

        content = read_artifact(run_id, "ui_design_spec")["content"]
        self.assertIn("详尽设计规范正文", content)
        self.assertNotIn("只是一段摘要", content)

    def test_prd_v2_prefers_written_full_file_over_stdout_summary(self) -> None:
        self.write_config({"code_platforms": {"enable_agent_cli": True}})
        created = create_project(requirement="做 TODO H5", title="TODO H5", auto_start=False)
        run_id = created["run_id"]
        write_artifact(run_id, "prd_v1", "# PRD v1\n\n初始需求", category="prd", created_by="test")
        write_artifact(run_id, "requirement_review_report", "# 评审\n\n补充验收标准", category="review", created_by="test")
        Path("delivery-project/prd_v2.md").write_text("# PRD v2\n\n最终完整 PRD 正文", encoding="utf-8")

        with patch(
            "delivery_workflow.engine.maybe_run_command",
            return_value={"executed": True, "returncode": 0, "stdout": "# Summary\n\n只是一段 PRD 摘要", "stderr": "", "command": ["claude", "-p"]},
        ):
            execute_step(run_id, "review-summary")

        content = read_artifact(run_id, "prd_v2")["content"]
        self.assertIn("最终完整 PRD 正文", content)
        self.assertNotIn("只是一段 PRD 摘要", content)

    def test_gate_and_worker_progress_to_prd_approval(self) -> None:
        created = create_project(requirement="新增后台 order approval 功能", title="order approval")
        run_id = created["run_id"]
        project_id = created["project_id"]
        self.assertIn("order-approval", project_id)
        self.assertIn("auto_run", created)

        current = status(run_id)
        self.assertEqual(current["run"]["current_step"], "prd-approval")
        self.assertTrue(any(item["name"] == "prd_v2" for item in current["artifacts"]))
        self.assertTrue(any(gate["step_id"] == "prd-approval" and gate["status"] == "open" for gate in current["gates"]))
        self.assertTrue(any(item["name"] == "requirement-intake_gate" for item in current["artifacts"]))
        prd_v2 = next(item for item in current["artifacts"] if item["name"] == "prd_v2")
        self.assertIn("/delivery-project/product-manager/prd/", prd_v2["path"])
        self.assertNotIn(project_id, Path(prd_v2["path"]).parts)
        self.assertTrue(run_worker_once(run_id=run_id)["idle"])

    def test_lark_dry_run_prd_doc_and_approval_card(self) -> None:
        self.write_config({"lark": {"enabled": True, "dry_run": True, "send_step_notifications": True, "chat_id": "oc_test"}})
        created = create_project(
            requirement="新增客户退款审批能力",
            title="客户退款审批",
            business_goal="降低退款处理风险",
        )
        run_id = created["run_id"]

        prd_doc = json.loads(read_artifact(run_id, "prd_v2_lark_doc")["content"])
        self.assertEqual(prd_doc["title"], "客户退款审批PRD")
        self.assertEqual(prd_doc["url"], f"dry-run://lark-doc/{created['project_id']}/prd-v2")
        self.assertEqual(prd_doc["result"]["command"][:5], ["lark-cli", "docs", "+create", "--as", "bot"])
        with connect() as conn:
            project_created_notice = conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND event_type = 'lark.notify.sent' AND message = ?",
                (run_id, "客户退款审批 项目已创建。"),
            ).fetchone()
        self.assertIsNotNone(project_created_notice)
        prd_doc_markdown = read_artifact(run_id, "prd_v2_lark_markdown")["content"]
        for heading in ("版本变化表格", "各 Agent 评审意见汇总", "相比 v1 变更点", "未采纳意见", "最终完整 PRD 内容"):
            self.assertIn(heading, prd_doc_markdown)
        self.assertIn("| 版本 | 来源 | 主要变化 |\n| --- | --- | --- |\n| v1", prd_doc_markdown)
        command_text = "\n".join(prd_doc["result"]["command"])
        self.assertIn("--doc-format\nmarkdown", command_text)
        self.assertIn("--content\n@", command_text)
        self.assertNotIn("--title", command_text)
        self.assertNotIn("<doc>", command_text)
        content_arg = prd_doc["result"]["command"][prd_doc["result"]["command"].index("--content") + 1]
        self.assertFalse(Path(content_arg[1:]).is_absolute())
        content_file = Path(content_arg[1:])
        self.assertTrue(content_file.exists())
        self.assertTrue(content_file.read_text(encoding="utf-8").startswith("# 客户退款审批PRD\n"))

        card = json.loads(read_artifact(run_id, "prd_approval_card_message")["content"])
        command = card["result"]["command"]
        self.assertIn("--msg-type", command)
        self.assertIn("interactive", command)
        content = json.loads(command[command.index("--content") + 1])
        self.assertNotIn("schema", content)
        self.assertIn("elements", content)
        self.assertIn("form", json.dumps(content, ensure_ascii=False))
        self.assertIn("reject_reason", json.dumps(content, ensure_ascii=False))
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("通过", rendered)
        self.assertIn("拒绝", rendered)
        self.assertIn(created["run_id"], rendered)
        lark_payload = created["auto_run"]["results"][-1]["result"]["gate"]["lark"]
        self.assertEqual(lark_payload["listener"]["reason"], "lark dry_run is true")

    def test_prd_v2_lark_markdown_normalizes_table_and_review_heading(self) -> None:
        created = create_project(requirement="做 TODO H5", title="TODO H5", auto_start=False)
        run_id = created["run_id"]
        write_artifact(run_id, "prd_v1", "# TODO H5 PRD\n\nv1", category="prd", created_by="test")
        write_artifact(
            run_id,
            "requirement_review_report",
            "# 多角色需求评审报告\n\n- 产品：补充空状态。\n- QA：补充验收标准。",
            category="review",
            created_by="test",
        )

        markdown = _compose_prd_v2_lark_markdown(run_id, "# TODO H5 PRD\n\n## 未采纳意见\n\n无")

        self.assertIn("| 版本 | 来源 | 主要变化 |\n| --- | --- | --- |\n| v1", markdown)
        review_section = markdown.split("## 各 Agent 评审意见汇总", 1)[1].split("## 相比 v1 变更点", 1)[0]
        self.assertNotIn("多角色需求评审报告", review_section)
        self.assertIn("产品：补充空状态", review_section)

    def test_generic_lark_doc_xml_has_title_and_table(self) -> None:
        xml = _compose_lark_doc_xml("TODO H5 项目 UI 设计规范", "# DESIGN.md\n\n| 项 | 值 |\n| --- | --- |\n| 色彩 | 暖色 |")

        self.assertIn("<title>TODO H5 项目 UI 设计规范</title>", xml)
        self.assertIn("<table>", xml)
        self.assertIn("<td>色彩</td>", xml)

    def test_prd_approval_card_uses_legacy_compatible_shape(self) -> None:
        card = build_prd_approval_card(
            project_title="TODO H5 应用",
            project_id="proj_test",
            run_id="run_test",
            step_id="prd-approval",
            doc_url="https://example.com/doc",
        )

        self.assertNotIn("schema", card)
        self.assertIn("elements", card)
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("approve_prd", rendered)
        self.assertIn("reject_prd", rendered)
        self.assertIn("reject_reason", rendered)

    def test_lark_card_event_submits_prd_gate(self) -> None:
        self.write_config({"lark": {"enabled": True, "dry_run": True, "send_step_notifications": True, "chat_id": "oc_test"}})
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批")
        run_id = created["run_id"]

        result = handle_lark_card_event(
            {
                "action": {
                    "value": {
                        "action": "approve_prd",
                        "approved": True,
                        "project_id": created["project_id"],
                        "run_id": run_id,
                        "step_id": "prd-approval",
                    }
                },
                "operator": {"open_id": "ou_test"},
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(status(run_id)["run"]["current_step"], "ui-design-spec")
        gate = read_artifact(run_id, "prd-approval_gate")
        self.assertIn('"approver": "ou_test"', gate["content"])
        self.assertIn('"comment": "通过"', gate["content"])
        self.assertIn("response_card", result)
        self.assertIn("该审批已通过", json.dumps(result["response_card"], ensure_ascii=False))
        self.assertEqual(result["continuation"]["reason"], "workflow.continue_after_gate is false")

    def test_lark_card_event_starts_worker_continuation_when_enabled(self) -> None:
        self.write_config({"workflow": {"continue_after_gate": True}, "lark": {"enabled": True, "dry_run": True, "send_step_notifications": False, "chat_id": "oc_test"}})
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批")
        run_id = created["run_id"]

        class FakeProcess:
            pid = 54321

        with patch("delivery_workflow.worker_daemon.subprocess.Popen", return_value=FakeProcess()) as popen:
            result = handle_lark_card_event(
                {
                    "action": {
                        "value": {
                            "action": "approve_prd",
                            "approved": True,
                            "project_id": created["project_id"],
                            "run_id": run_id,
                            "step_id": "prd-approval",
                        }
                    },
                    "operator": {"open_id": "ou_test"},
                }
            )

        self.assertTrue(result["continuation"]["started"])
        self.assertEqual(result["continuation"]["pid"], 54321)
        command = result["continuation"]["command"]
        self.assertEqual(command[-4:], ["--run-id", run_id, "--max-jobs", "50"])
        self.assertEqual(Path(popen.call_args.kwargs["cwd"]).resolve(), Path(self.tmp.name).resolve())

    def test_lark_card_reject_requires_and_records_reason(self) -> None:
        self.write_config({"lark": {"enabled": True, "dry_run": True, "send_step_notifications": False, "chat_id": "oc_test"}})
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批")
        run_id = created["run_id"]

        with self.assertRaises(Exception):
            handle_lark_card_event(
                {
                    "action": {
                        "value": {
                            "action": "reject_prd",
                            "approved": False,
                            "project_id": created["project_id"],
                            "run_id": run_id,
                            "step_id": "prd-approval",
                        },
                        "form_value": {},
                    },
                    "operator": {"open_id": "ou_test"},
                }
            )

        result = handle_lark_card_event(
            {
                "action": {
                    "value": {
                        "action": "reject_prd",
                        "approved": False,
                        "project_id": created["project_id"],
                        "run_id": run_id,
                        "step_id": "prd-approval",
                    },
                    "form_value": {"reject_reason": "PRD 缺少验收标准"},
                },
                "operator": {"open_id": "ou_test"},
            }
        )

        self.assertFalse(result["approved"])
        self.assertEqual(status(run_id)["run"]["current_step"], "review-summary")
        gate = read_artifact(run_id, "prd-approval_gate")
        self.assertIn("PRD 缺少验收标准", gate["content"])
        self.assertIn("该审批已拒绝", json.dumps(result["response_card"], ensure_ascii=False))

    def test_prd_approval_rejection_reopens_gate_and_sends_new_card_round(self) -> None:
        self.write_config({"lark": {"enabled": True, "dry_run": True, "send_step_notifications": True, "chat_id": "oc_test"}})
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批")
        run_id = created["run_id"]

        submit_gate(
            run_id,
            "prd-approval",
            {"approved": False, "approver": "ou_test", "comment": "补充一个说明"},
        )
        result = run_worker_until_blocked(run_id=run_id, stop_steps={"prd-approval"}, max_jobs=10)

        self.assertEqual(result["stopped"], "blocked")
        current = status(run_id)
        gate = next(item for item in current["gates"] if item["step_id"] == "prd-approval")
        self.assertEqual(gate["status"], "open")
        self.assertIsNone(gate["data_json"])
        self.assertEqual(read_artifact(run_id, "prd_v2")["version"], 2)
        card = json.loads(read_artifact(run_id, "prd_approval_card_message")["content"])
        command = card["result"]["command"]
        self.assertEqual(command[-1], f"pa-{run_id.rsplit('_', 1)[-1]}-p2-d2")
        content = json.loads(command[command.index("--content") + 1])
        rendered = json.dumps(content, ensure_ascii=False)
        self.assertNotIn("approval_round", rendered)
        self.assertIn("客户退款审批 PRD 第 2 轮复审", rendered)
        with connect() as conn:
            review_notice = conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND event_type = 'lark.notify.sent' AND message LIKE ?",
                (run_id, "%PRD v2 已按照拒绝原因：补充一个说明 修改，准备发起第 2 轮复审。%"),
            ).fetchone()
        self.assertIsNotNone(review_notice)

    def test_submit_gate_requires_open_gate(self) -> None:
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批")
        run_id = created["run_id"]

        submit_gate(run_id, "prd-approval", {"approved": True, "approver": "ou_test", "comment": "通过"})

        with self.assertRaises(Exception) as context:
            submit_gate(run_id, "prd-approval", {"approved": True, "approver": "ou_test", "comment": "重复提交"})

        self.assertIn("gate is not open", str(context.exception))

    def test_lark_card_rejects_stale_document_url(self) -> None:
        self.write_config({"lark": {"enabled": True, "dry_run": True, "send_step_notifications": False, "chat_id": "oc_test"}})
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批")
        run_id = created["run_id"]
        submit_gate(run_id, "prd-approval", {"approved": False, "approver": "ou_test", "comment": "补充说明"})
        run_worker_until_blocked(run_id=run_id, stop_steps={"prd-approval"}, max_jobs=10)
        latest_doc = json.loads(read_artifact(run_id, "prd_v2_lark_doc")["content"])

        with self.assertRaises(Exception) as context:
            handle_lark_card_event(
                {
                    "action": {
                        "value": {
                            "action": "approve_prd",
                            "approved": True,
                            "project_id": created["project_id"],
                            "run_id": run_id,
                            "step_id": "prd-approval",
                            "doc_url": f"{latest_doc['url']}/stale-card",
                        }
                    },
                    "operator": {"open_id": "ou_test"},
                }
            )

        self.assertIn("stale PRD approval card", str(context.exception))

    def test_prd_approval_approved_runs_to_completion_without_release_gate(self) -> None:
        self.write_config({"lark": {"enabled": True, "dry_run": True, "send_step_notifications": False, "chat_id": "oc_test"}})
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批")
        run_id = created["run_id"]

        handle_lark_card_event(
            {
                "action": {
                    "value": {
                        "action": "approve_prd",
                        "approved": True,
                        "project_id": created["project_id"],
                        "run_id": run_id,
                        "step_id": "prd-approval",
                    }
                },
                "operator": {"open_id": "ou_test"},
            }
        )
        qa_pass_report = json.dumps({"quality_gate": {"bug_counts": {"block": 0, "critical": 0, "major": 0, "minor": 0}}})
        with patch("delivery_workflow.engine.maybe_run_command", return_value={"executed": True, "returncode": 0, "stdout": qa_pass_report, "stderr": "", "command": ["agent"]}):
            result = run_worker_until_blocked(run_id=run_id, max_jobs=80)
        current = status(run_id)

        self.assertEqual(result["stopped"], "idle")
        self.assertEqual(current["run"]["status"], "completed")
        self.assertFalse([gate for gate in current["gates"] if gate["status"] == "open"])
        self.assertFalse([step for step in current["step_runs"] if step["step_id"] == "release-approval"])
        self.assertTrue(any(step["step_id"] == "delivery-notification" and step["status"] == "completed" for step in current["step_runs"]))

    def test_final_report_is_published_to_lark_with_project_title_and_group_notice(self) -> None:
        self.write_config({"lark": {"enabled": True, "dry_run": True, "send_step_notifications": True, "chat_id": "oc_test"}})
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批")
        run_id = created["run_id"]

        handle_lark_card_event(
            {
                "action": {
                    "value": {
                        "action": "approve_prd",
                        "approved": True,
                        "project_id": created["project_id"],
                        "run_id": run_id,
                        "step_id": "prd-approval",
                    }
                },
                "operator": {"open_id": "ou_test"},
            }
        )
        qa_pass_report = json.dumps({"quality_gate": {"bug_counts": {"block": 0, "critical": 0, "major": 0, "minor": 0}}})
        with patch("delivery_workflow.engine.maybe_run_command", return_value={"executed": True, "returncode": 0, "stdout": qa_pass_report, "stderr": "", "command": ["agent"]}):
            run_worker_until_blocked(run_id=run_id, max_jobs=80)

        final_doc = json.loads(read_artifact(run_id, "final_report_lark_doc")["content"])
        command = final_doc["command"]
        command_text = "\n".join(command)
        self.assertIn("--doc-format\nmarkdown", command_text)
        self.assertIn("--content\n@", command_text)
        self.assertNotIn("--title", command_text)
        content_arg = command[command.index("--content") + 1]
        self.assertFalse(Path(content_arg[1:]).is_absolute())
        final_doc_markdown = read_artifact(run_id, "final_report_lark_doc_markdown")["content"]
        self.assertIn("最终交付报告", final_doc_markdown)
        with connect() as conn:
            notice = conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND event_type = 'lark.notify.sent' AND message LIKE ?",
                (run_id, "%最终交付报告飞书文档已发布：%"),
            ).fetchone()
        self.assertIsNotNone(notice)

    def test_regression_quality_gate_routes_to_bug_fix_when_threshold_exceeded(self) -> None:
        created = create_project(requirement="create todo h5", title="todo h5", auto_start=False)
        run_id = created["run_id"]
        report = json.dumps({"quality_gate": {"bug_counts": {"block": 0, "critical": 1, "major": 0, "minor": 0}}})

        with patch("delivery_workflow.engine.maybe_run_command", return_value={"executed": True, "returncode": 0, "stdout": report, "stderr": "", "command": ["qa"]}):
            result = execute_step(run_id, "regression-testing")

        self.assertFalse(result["result"]["quality_gate"]["passed"])
        self.assertEqual(result["next_step"], "bug-fix")

    def test_qa_bugfix_regression_loop_hands_reports_between_steps(self) -> None:
        self.write_config({"code_platforms": {"enable_agent_cli": True}})
        created = create_project(requirement="create todo h5", title="todo h5", auto_start=False)
        run_id = created["run_id"]
        for name, category in [
            ("prd_v2", "prd"),
            ("ui_design_spec", "ui"),
            ("frontend_tech_design", "tech"),
            ("backend_tech_design", "tech"),
            ("test_cases", "test"),
            ("development_self_test_report", "dev-result"),
            ("frontend_dev_result", "dev-result"),
            ("backend_dev_result", "dev-result"),
        ]:
            write_artifact(run_id, name, f"{name} content", category=category, created_by="test")

        qa_fail_report = json.dumps(
            {
                "bugs": [{"id": "BUG-1", "severity": "critical", "steps": "open page"}],
                "quality_gate": {"bug_counts": {"block": 0, "critical": 1, "major": 0, "minor": 0}},
            },
            ensure_ascii=False,
        )
        with patch("delivery_workflow.engine.maybe_run_command", return_value={"executed": True, "returncode": 0, "stdout": qa_fail_report, "stderr": "", "command": ["qa"]}):
            first_qa = execute_step(run_id, "regression-testing")

        self.assertEqual(first_qa["next_step"], "bug-fix")
        self.assertIn("BUG-1", read_artifact(run_id, "test_report")["content"])

        fix_report = "# 修复报告\n\nfixed_bugs: BUG-1\ncommands_run: npm run build, npm test\nself_test_result: pass"
        with patch("delivery_workflow.engine.maybe_run_command", return_value={"executed": True, "returncode": 0, "stdout": fix_report, "stderr": "", "command": ["fix"]}):
            fix = execute_step(run_id, "bug-fix")

        self.assertEqual(fix["next_step"], "regression-testing")
        self.assertIn("BUG-1", read_artifact(run_id, "bug_fix_result")["content"])

        qa_pass_report = json.dumps({"quality_gate": {"bug_counts": {"block": 0, "critical": 0, "major": 0, "minor": 0}}, "regression_verified": ["BUG-1"]}, ensure_ascii=False)
        with patch("delivery_workflow.engine.maybe_run_command", return_value={"executed": True, "returncode": 0, "stdout": qa_pass_report, "stderr": "", "command": ["qa"]}):
            second_qa = execute_step(run_id, "regression-testing")

        self.assertEqual(second_qa["next_step"], "publish-test-report-doc")
        self.assertIn("BUG-1", read_artifact(run_id, "test_report")["content"])

    def test_regression_testing_requires_real_execution(self) -> None:
        self.write_config({"code_platforms": {"enable_agent_cli": False}})
        created = create_project(requirement="create todo h5", title="todo h5", auto_start=False)

        with self.assertRaises(WorkflowError) as context:
            execute_step(created["run_id"], "regression-testing")

        self.assertIn("requires real QA execution", str(context.exception))

    def test_dev_step_blocks_when_self_test_was_not_run(self) -> None:
        self.write_config({"code_platforms": {"enable_agent_cli": True}})
        created = create_project(requirement="create todo h5", title="todo h5", auto_start=False)
        run_id = created["run_id"]
        write_artifact(run_id, "prd_v2", "prd", category="prd", created_by="test")
        write_artifact(run_id, "dev_tasks", "tasks", category="dev", created_by="test")
        write_artifact(run_id, "frontend_tech_design", "frontend tech", category="tech", created_by="test")
        write_artifact(run_id, "ui_design_spec", "ui", category="ui", created_by="test")

        with (
            patch(
                "delivery_workflow.engine.maybe_run_command",
                return_value={"executed": True, "returncode": 0, "stdout": "实现已完成，但暂未运行 npm install / npm run build。", "stderr": "", "command": ["claude"]},
            ),
            self.assertRaises(WorkflowError) as context,
        ):
            execute_step(run_id, "frontend-development")

        self.assertIn("did not complete required self-test", str(context.exception))

    def test_manual_bug_fix_request_enqueues_bug_fix_step(self) -> None:
        created = create_project(requirement="create todo h5", title="todo h5", auto_start=False)

        result = request_bug_fix(issue="修复新增待办后页面空白的问题", project_id=created["project_id"], reporter="tester")

        self.assertTrue(result["ok"])
        self.assertEqual(result["job"]["step_id"], "bug-fix")
        self.assertIn("页面空白", read_artifact(created["run_id"], "manual_bug_fix_request")["content"])

    def test_lark_sdk_payload_converter_uses_official_json_marshal_shape(self) -> None:
        class FakeJSON:
            @staticmethod
            def marshal(data: dict, indent: int | None = None) -> str:
                return json.dumps(data, ensure_ascii=False)

        class FakeLark:
            JSON = FakeJSON

        payload = sdk_event_to_payload(
            FakeLark,
            {
                "schema": "2.0",
                "event": {
                    "operator": {"open_id": "ou_test"},
                    "action": {"value": {"action": "approve_prd", "run_id": "run_test"}},
                },
            },
        )

        self.assertEqual(payload["event"]["action"]["value"]["action"], "approve_prd")

    def test_lark_sdk_credentials_can_use_env_values(self) -> None:
        Path(".env").write_text("LARK_APP_ID=cli_test\nLARK_APP_SECRET=secret_test\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            credentials = load_sdk_credentials(load_config())

        self.assertEqual(credentials.app_id, "cli_test")
        self.assertEqual(credentials.source, ".env")

    def test_lark_sdk_preflight_reports_package_and_websocket_event(self) -> None:
        Path(".env").write_text("LARK_APP_ID=cli_test\nLARK_APP_SECRET=secret_test\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            report = sdk_preflight(load_config())

        self.assertEqual(report["event"], "card.action.trigger")
        self.assertEqual(report["transport"], "sdk_websocket")
        self.assertIn("package", report)
        self.assertEqual(report["package"]["install_command"][:4], ["uv", "pip", "install", "--python"])

    def test_watch_run_returns_when_workflow_event_arrives(self) -> None:
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批", auto_start=False)
        run_id = created["run_id"]
        result_box: dict[str, dict] = {}

        def wait_for_event() -> None:
            result_box["result"] = watch_run(run_id, timeout_seconds=2, poll_interval_seconds=0.05)

        thread = threading.Thread(target=wait_for_event)
        thread.start()
        time.sleep(0.1)
        emit_event(run_id, "gate.submitted", "test gate submitted", {"step_id": "prd-approval"})
        thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        result = result_box["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "workflow settled after event")
        self.assertEqual(result["events"][0]["event_type"], "gate.submitted")

    def test_watch_run_waits_until_pending_jobs_are_finished(self) -> None:
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批", auto_start=False)
        run_id = created["run_id"]
        enqueue_step(run_id, "prd-v1")
        result_box: dict[str, dict] = {}

        def wait_for_settled() -> None:
            result_box["result"] = watch_run(run_id, timeout_seconds=4, poll_interval_seconds=0.05)

        thread = threading.Thread(target=wait_for_settled)
        thread.start()
        time.sleep(0.1)
        emit_event(run_id, "gate.submitted", "test gate submitted", {"step_id": "prd-approval"})
        time.sleep(0.2)
        self.assertTrue(thread.is_alive())

        run_worker_until_blocked(run_id=run_id, stop_steps={"prd-approval"}, max_jobs=20)
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        result = result_box["result"]
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "workflow settled after event")
        self.assertFalse([job for job in result["status"]["jobs"] if job["status"] in {"pending", "running"}])

    def test_watch_run_keeps_waiting_after_prd_rejection_reopens_approval_gate(self) -> None:
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批")
        run_id = created["run_id"]
        result_box: dict[str, dict] = {}

        def wait_for_settled() -> None:
            result_box["result"] = watch_run(run_id, timeout_seconds=1.2, poll_interval_seconds=0.05)

        thread = threading.Thread(target=wait_for_settled)
        thread.start()
        time.sleep(0.1)
        submit_gate(run_id, "prd-approval", {"approved": False, "approver": "ou_test", "comment": "继续补充背景"})
        run_worker_until_blocked(run_id=run_id, stop_steps={"prd-approval"}, max_jobs=10)
        time.sleep(0.2)
        self.assertTrue(thread.is_alive())
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertFalse(result_box["result"]["ok"])
        self.assertEqual(result_box["result"]["reason"], "timeout")

    def test_lark_event_consumer_can_autostart_in_workspace(self) -> None:
        self.write_config({"lark": {"enabled": True, "dry_run": False, "sdk": {"app_id": "cli_test", "app_secret": "secret_test"}}})

        class FakeProcess:
            pid = 43210
            returncode = None

            def poll(self) -> None:
                return None

        with (
            patch("delivery_workflow.lark_daemon.sdk_preflight", return_value={"ok": True, "credentials": {"source": "test"}}),
            patch("delivery_workflow.lark_daemon.subprocess.Popen", return_value=FakeProcess()) as popen,
            patch("delivery_workflow.lark_daemon.time.sleep", return_value=None),
        ):
            result = ensure_lark_event_consumer("run_test")

        self.assertTrue(result["ok"])
        self.assertTrue(result["started"])
        self.assertEqual(result["pid"], 43210)
        self.assertEqual(result["code_fingerprint"], _code_fingerprint())
        self.assertEqual(Path(result["workspace"]).resolve(), Path(self.tmp.name).resolve())
        self.assertTrue((Path(self.tmp.name) / ".delivery-workflow" / "lark-event-consumer.pid.json").exists())
        self.assertEqual(Path(popen.call_args.kwargs["cwd"]).resolve(), Path(self.tmp.name).resolve())

    def test_lark_event_consumer_restarts_stale_process_after_code_changes(self) -> None:
        self.write_config({"lark": {"enabled": True, "dry_run": False, "sdk": {"app_id": "cli_test", "app_secret": "secret_test"}}})
        home = Path(self.tmp.name) / ".delivery-workflow"
        home.mkdir()
        (home / "lark-event-consumer.pid.json").write_text(
            json.dumps({"pid": 11111, "workspace": self.tmp.name, "code_fingerprint": "old-code"}, ensure_ascii=False),
            encoding="utf-8",
        )

        class FakeProcess:
            pid = 22222
            returncode = None

            def poll(self) -> None:
                return None

        with (
            patch("delivery_workflow.lark_daemon._pid_is_alive", side_effect=[True, False, False]),
            patch("delivery_workflow.lark_daemon._terminate_process", return_value=True) as terminate,
            patch("delivery_workflow.lark_daemon.sdk_preflight", return_value={"ok": True, "credentials": {"source": "test"}}),
            patch("delivery_workflow.lark_daemon.subprocess.Popen", return_value=FakeProcess()),
            patch("delivery_workflow.lark_daemon.time.sleep", return_value=None),
        ):
            result = ensure_lark_event_consumer("run_new")

        self.assertTrue(result["ok"])
        self.assertTrue(result["started"])
        self.assertEqual(result["pid"], 22222)
        terminate.assert_called_once_with(11111, expected_command=None)
        state = json.loads((home / "lark-event-consumer.pid.json").read_text(encoding="utf-8"))
        self.assertEqual(state["pid"], 22222)
        self.assertEqual(state["code_fingerprint"], _code_fingerprint())

    def test_lark_event_consumer_can_stop_pid_file_confirmed_consumer(self) -> None:
        class FakeCompleted:
            returncode = 0
            stdout = "/usr/bin/python unrelated.py"

        with (
            patch("delivery_workflow.lark_daemon.subprocess.run", return_value=FakeCompleted()),
            patch("delivery_workflow.lark_daemon.os.kill") as kill,
        ):
            self.assertTrue(_terminate_process(12345, expected_command=["/plugin/scripts/deliveryflow", "lark", "event-consumer"]))

        kill.assert_called_once()

    def test_lark_event_consumer_treats_zombie_pid_as_not_alive(self) -> None:
        from delivery_workflow.lark_daemon import _pid_is_alive

        class FakeCompleted:
            returncode = 0
            stdout = "Z+"

        with (
            patch("delivery_workflow.lark_daemon.subprocess.run", return_value=FakeCompleted()),
            patch("delivery_workflow.lark_daemon.os.kill") as kill,
        ):
            self.assertFalse(_pid_is_alive(12345))

        kill.assert_not_called()

    def test_lark_event_consumer_does_not_stop_unrelated_reused_pid(self) -> None:
        class FakeCompleted:
            returncode = 0
            stdout = "/usr/bin/python unrelated.py"

        with (
            patch("delivery_workflow.lark_daemon.subprocess.run", return_value=FakeCompleted()),
            patch("delivery_workflow.lark_daemon.os.kill") as kill,
        ):
            self.assertFalse(_terminate_process(12345))

        kill.assert_not_called()

    def test_lark_keychain_failure_detection_and_retry_entrypoint(self) -> None:
        self.assertTrue(_is_keychain_unavailable({"stderr": "keychain Get failed: keychain not initialized"}))
        self.write_config({"lark": {"enabled": True, "dry_run": True, "send_step_notifications": False, "chat_id": "oc_test"}})
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批")
        result = retry_prd_approval_lark(created["run_id"])
        self.assertTrue(result["ok"])
        self.assertIn("doc_url", result["result"])

    def test_host_lark_escalation_uses_narrow_runner_prefix(self) -> None:
        command = _host_lark_retry_command("run_test")
        payload = _host_escalation_payload(command)

        self.assertIn("scripts/deliveryflow-host-lark retry-prd-approval --workspace", command)
        self.assertIn("--run-id run_test", command)
        self.assertEqual(payload["persistent_prefix_rule"][-1], "retry-prd-approval")
        self.assertNotIn("python3 -m", command)

    def test_default_artifact_root_is_current_workspace_delivery_project(self) -> None:
        created = create_project(requirement="create a settings page", title="settings page", auto_start=False)
        expected_root = (Path(self.tmp.name) / "delivery-project").resolve()
        self.assertEqual(Path(created["artifact_dir"]).resolve(), expected_root)
        self.assertEqual(Path(created["source_dir"]).resolve(), (Path(self.tmp.name) / "source-code").resolve())
        self.assertNotIn(created["project_id"], created["artifact_dir"])

    def test_current_project_status_and_delete_backup(self) -> None:
        created = create_project(requirement="create audit log", title="audit log", auto_start=False)
        project_id = created["project_id"]
        Path("source-code/frontend").mkdir(parents=True)
        Path("source-code/frontend/package.json").write_text("{}", encoding="utf-8")
        Path(".claude").mkdir()
        Path(".claude/settings.local.json").write_text("{}", encoding="utf-8")
        Path(".env.example").write_text("LARK_APP_ID=\n", encoding="utf-8")
        Path(".gitignore").write_text(".env\n", encoding="utf-8")

        self.assertEqual(current_project_status()["run"]["project_id"], project_id)
        result = delete_current_project()

        self.assertTrue(result["ok"])
        backup = Path(result["backup_path"])
        self.assertTrue(backup.exists())
        self.assertFalse(Path(".delivery-workflow").exists())
        self.assertFalse(Path("delivery-project").exists())
        self.assertFalse(Path("source-code").exists())
        self.assertFalse(Path(WORKSPACE_CONFIG_NAME).exists())
        self.assertFalse(Path(".claude").exists())
        self.assertFalse(Path(".env.example").exists())
        self.assertFalse(Path(".gitignore").exists())
        with zipfile.ZipFile(backup) as archive:
            self.assertIn("source-code/frontend/package.json", archive.namelist())
            self.assertIn(".claude/settings.local.json", archive.namelist())
            self.assertIn(".env.example", archive.namelist())
        with self.assertRaises(WorkflowError):
            current_project_status()

    def test_doctor_reports_lark_doc_shape(self) -> None:
        report = doctor()
        self.assertIn("lark_doc", report)
        self.assertIn("ok", report["lark_doc"])
        self.assertIn("install_hint", report["lark_doc"])
        self.assertEqual(report["lark_doc"]["install_command"], ["npx", "@larksuite/cli@latest", "install"])
        rendered = json.dumps(report["lark_doc"], ensure_ascii=False)
        self.assertIn("config init --new", rendered)
        self.assertIn("auth login --recommend", rendered)
        self.assertNotIn("npm install -g", rendered)

    def test_host_hooks_record_command_evidence(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "npm run build"}, "session_id": "sess_test"}

        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            exit_code = host_hook_main(["record-command"])

        self.assertEqual(exit_code, 0)
        hook_log = Path(".delivery-workflow/logs/host-hooks.jsonl").read_text(encoding="utf-8")
        self.assertIn("host_hook.command_executed", hook_log)
        self.assertIn("node-verification", hook_log)
        workflow_log = Path(".delivery-workflow/logs/workflow.log").read_text(encoding="utf-8")
        self.assertIn("host_hook.command_executed", workflow_log)

    def test_host_hook_safety_guard_blocks_secret_reads(self) -> None:
        payload = {"tool_name": "Bash", "tool_input": {"command": "cat .env"}}

        with (
            patch("sys.stdin", io.StringIO(json.dumps(payload))),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            exit_code = host_hook_main(["safety-guard"])

        self.assertEqual(exit_code, 0)
        decision = json.loads(stdout.getvalue())
        output = decision["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("environment secrets", output["permissionDecisionReason"])

    def test_host_hook_safety_guard_allows_code_search_and_specific_cleanup(self) -> None:
        for command in ('rg -n ".env" delivery_workflow tests', "rm -rf .claude .delivery-workflow"):
            with (
                patch("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))),
                patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                exit_code = host_hook_main(["safety-guard"])

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue(), "")

    def test_mcp_config_uses_portable_wrapper_entrypoint(self) -> None:
        self.assertFalse((PLUGIN_ROOT / ".mcp.json").exists())
        manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["mcpServers"], "./.codex-plugin/mcp.json")
        config = json.loads((PLUGIN_ROOT / ".codex-plugin" / "mcp.json").read_text(encoding="utf-8"))
        server = config["mcpServers"]["delivery-workflow"]

        rendered = json.dumps(server, ensure_ascii=False)
        self.assertEqual(server["command"], "${CLAUDE_PLUGIN_ROOT}/scripts/deliveryflow-mcp")
        self.assertIn("scripts/deliveryflow-mcp", rendered)
        self.assertIn("CLAUDE_PLUGIN_ROOT", rendered)
        self.assertNotIn(str(PLUGIN_ROOT), rendered)

    def test_mcp_server_uses_newline_delimited_jsonrpc(self) -> None:
        process = subprocess.Popen(
            [str(PLUGIN_ROOT / "scripts" / "deliveryflow-mcp")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
        }
        process.stdin.write(json.dumps(initialize, ensure_ascii=False) + "\n")
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, ensure_ascii=False) + "\n")
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, ensure_ascii=False) + "\n")
        process.stdin.flush()

        first = process.stdout.readline().strip()
        second = process.stdout.readline().strip()
        process.stdin.close()
        process.terminate()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()

        self.assertTrue(first.startswith("{"), first)
        self.assertNotIn("Content-Length", first)
        self.assertEqual(json.loads(first)["result"]["serverInfo"]["name"], "delivery-workflow")
        tools = json.loads(second)["result"]["tools"]
        self.assertTrue(any(tool["name"] == "delivery_create_project" for tool in tools))
        self.assertTrue(any(tool["name"] == "delivery_init_project_config" for tool in tools))
        self.assertTrue(any(tool["name"] == "delivery_watch_run" for tool in tools))

    def test_mcp_cannot_submit_approval_gates(self) -> None:
        with self.assertRaises(Exception) as context:
            call_mcp_tool(
                "delivery_submit_gate",
                {
                    "run_id": "run_test",
                    "step_id": "prd-approval",
                    "data": {"approved": True, "approver": "claude", "comment": "terminal choice"},
                },
            )

        self.assertIn("must wait for the Feishu/Lark approval card callback", str(context.exception))

    def test_mcp_create_project_auto_watches_real_lark_approval(self) -> None:
        self.write_config({"lark": {"enabled": True, "dry_run": False, "chat_id": "oc_test"}})
        created = {
            "project_id": "proj_test",
            "run_id": "run_test",
            "auto_run": {
                "results": [
                    {
                        "result": {
                            "gate": {
                                "lark": {
                                    "card_result": {"ok": True}
                                }
                            }
                        }
                    }
                ]
            },
        }
        current = {
            "run": {"id": "run_test", "project_id": "proj_test", "current_step": "prd-approval", "status": "running"},
            "gates": [{"step_id": "prd-approval", "status": "open"}],
            "jobs": [],
        }
        completed = {
            "run": {"id": "run_test", "project_id": "proj_test", "current_step": "", "status": "completed"},
            "gates": [{"step_id": "prd-approval", "status": "submitted"}],
            "jobs": [],
        }

        with (
            patch("delivery_workflow.mcp_server.create_project", return_value=created),
            patch("delivery_workflow.mcp_server.status", return_value=current),
            patch("delivery_workflow.mcp_server.watch_run", return_value={"ok": True, "reason": "workflow settled after event", "status": completed}) as watch,
        ):
            result = call_mcp_tool("delivery_create_project", {"requirement": "做一个 TODO H5"})

        self.assertEqual(result["project"]["run_id"], "run_test")
        self.assertEqual(result["watch"]["reason"], "workflow settled after event")
        self.assertEqual(result["final_state"]["status"], "completed")
        self.assertEqual(result["status"]["run"]["status"], "completed")
        watch.assert_called_once_with("run_test")

    def test_mcp_create_project_does_not_watch_without_real_lark_card(self) -> None:
        self.write_config({"lark": {"enabled": True, "dry_run": True, "chat_id": "oc_test"}})
        created = {"project_id": "proj_test", "run_id": "run_test", "auto_run": {"results": []}}
        current = {
            "run": {"id": "run_test", "project_id": "proj_test", "current_step": "prd-approval", "status": "running"},
            "gates": [{"step_id": "prd-approval", "status": "open"}],
            "jobs": [],
        }

        with (
            patch("delivery_workflow.mcp_server.create_project", return_value=created),
            patch("delivery_workflow.mcp_server.status", return_value=current),
            patch("delivery_workflow.mcp_server.watch_run") as watch,
        ):
            result = call_mcp_tool("delivery_create_project", {"requirement": "做一个 TODO H5"})

        self.assertEqual(result["watch"]["reason"], "no real Feishu/Lark approval card was sent")
        watch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
