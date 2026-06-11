from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from delivery_workflow.config import DEFAULT_CONFIG_TEMPLATE, PLUGIN_ROOT, WORKSPACE_CONFIG_NAME, initialize_project_workspace, lark_chat_id, load_config
from delivery_workflow.definitions import load_workflow
from delivery_workflow.engine import (
    create_project,
    delete_current_project,
    execute_step,
    read_artifact,
    run_worker_once,
    status,
    submit_gate,
    watch_run,
    write_artifact,
)
from delivery_workflow.host_hooks import _blocked_command_reason
from delivery_workflow.lark import create_doc_as_bot, run_project_lark_cli
from delivery_workflow.mcp_server import _call_tool as call_mcp_tool


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
        if overrides:
            config = _merge(config, overrides)
        Path(WORKSPACE_CONFIG_NAME).write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_initialize_project_workspace_creates_runtime_dirs_without_env_template(self) -> None:
        Path(WORKSPACE_CONFIG_NAME).unlink()

        result = initialize_project_workspace()

        self.assertTrue(Path(result["config_path"]).exists())
        self.assertNotIn("env_example_path", result)
        self.assertFalse(Path(".env.example").exists())
        self.assertTrue(Path(result["db_path"]).exists())
        self.assertTrue(Path(".delivery-workflow/logs").is_dir())
        self.assertTrue(Path("delivery-project").is_dir())
        self.assertTrue(Path("source-code").is_dir())

    def test_default_lark_config_only_keeps_enabled_identity_and_chat_id(self) -> None:
        self.assertEqual(set(DEFAULT_CONFIG_TEMPLATE["lark"].keys()), {"enabled", "identity", "chat_id"})

        config = load_config()
        self.assertEqual(set(config["lark"].keys()), {"enabled", "identity", "chat_id"})

    def test_project_lark_chat_id_can_come_from_config_or_create_argument(self) -> None:
        self.assertEqual(lark_chat_id({"lark": {"chat_id": "oc_config"}}, None), "oc_config")
        self.assertEqual(lark_chat_id({"lark": {"chat_id": "oc_config"}}, "oc_project"), "oc_project")

    def test_lark_cli_calls_do_not_inject_project_env_or_home(self) -> None:
        class FakeCompleted:
            returncode = 0
            stdout = json.dumps({"ok": True, "url": "https://example.test/doc"})
            stderr = ""

        with (
            patch.dict(os.environ, {"LARK_APP_ID": "cli_global", "LARK_APP_SECRET": "secret_global"}),
            patch("delivery_workflow.lark.subprocess.run", return_value=FakeCompleted()) as run,
        ):
            result = create_doc_as_bot("测试文档", "<doc><title>测试文档</title><p>正文</p></doc>", dry_run=False, doc_format="xml")

        self.assertTrue(result["ok"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:5], ["lark-cli", "docs", "+create", "--as", "bot"])
        self.assertNotIn("env", run.call_args.kwargs)
        self.assertNotIn("LARK_APP_SECRET", json.dumps(result, ensure_ascii=False))

    def test_lark_cli_wrapper_is_plain_passthrough(self) -> None:
        class FakeCompleted:
            returncode = 0
            stdout = json.dumps({"ok": True})
            stderr = ""

        with patch("delivery_workflow.lark.subprocess.run", return_value=FakeCompleted()) as run:
            result = run_project_lark_cli(["auth", "status"])

        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0], ["lark-cli", "auth", "status"])
        self.assertNotIn("env", run.call_args.kwargs)

    def test_workflow_definition_matches_document_confirmation_flow(self) -> None:
        workflow = load_workflow()
        steps = {step["id"]: step for step in workflow.steps}

        self.assertNotIn("prd-approval", steps)
        self.assertNotIn("tech-review", steps)
        self.assertNotIn("dev-task-breakdown", steps)
        self.assertIn("development-doc-confirmation", steps)
        self.assertIn("smoke-test-case-design", steps)
        self.assertIn("frontend-backend-integration", steps)
        self.assertIn("development-smoke-self-test", steps)
        self.assertIn("qa-system-testing", steps)
        self.assertIn("qa-regression-testing", steps)
        self.assertEqual(steps["review-summary"]["next"]["default"], "ui-design-spec")
        self.assertEqual(steps["smoke-test-case-design"]["next"]["default"], "publish-prd-doc")
        self.assertEqual(steps["publish-smoke-test-cases-doc"]["next"]["default"], "development-doc-confirmation")
        self.assertEqual(steps["development-doc-confirmation"]["next"]["approved"], "frontend-development")
        self.assertEqual(steps["qa-system-testing"]["next"]["failed"], "bug-fix")
        self.assertEqual(steps["bug-fix"]["next"]["default"], "qa-regression-testing")
        self.assertEqual(steps["qa-regression-testing"]["next"]["default"], "qa-test-report")

    def test_create_project_runs_to_development_document_confirmation(self) -> None:
        created = create_project(requirement="做一个 TODO H5", title="TODO H5")
        run_id = created["run_id"]

        current = status(run_id)
        self.assertEqual(current["run"]["current_step"], "development-doc-confirmation")
        self.assertTrue(any(gate["step_id"] == "development-doc-confirmation" and gate["status"] == "open" for gate in current["gates"]))
        for artifact_name in ("prd_v2", "ui_design_spec", "frontend_tech_design", "smoke_test_cases"):
            self.assertTrue(any(item["name"] == artifact_name for item in current["artifacts"]), artifact_name)
        self.assertTrue(run_worker_once(run_id=run_id)["idle"])

    def test_document_confirmation_gate_controls_development_start(self) -> None:
        created = create_project(requirement="做一个 TODO H5", title="TODO H5")
        run_id = created["run_id"]

        result = submit_gate(run_id, "development-doc-confirmation", {"approved": True, "approver": "tester", "comment": "确认开发"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["next_step"], "frontend-development")
        self.assertEqual(status(run_id)["run"]["current_step"], "frontend-development")

    def test_document_confirmation_rejection_loops_to_review_summary(self) -> None:
        created = create_project(requirement="做一个 TODO H5", title="TODO H5")
        run_id = created["run_id"]

        result = submit_gate(run_id, "development-doc-confirmation", {"approved": False, "approver": "tester", "comment": "补充排行榜需求"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["next_step"], "review-summary")
        gate = json.loads(read_artifact(run_id, "development-doc-confirmation_gate")["content"])
        self.assertEqual(gate["comment"], "补充排行榜需求")

    def test_backend_steps_are_skipped_when_project_has_no_backend(self) -> None:
        created = create_project(requirement="做一个纯前端 H5", title="纯前端", requires_backend=False, auto_start=False)
        run_id = created["run_id"]
        write_artifact(run_id, "prd_v2", "# PRD\n\n完整内容", category="prd", created_by="test")
        write_artifact(run_id, "ui_design_spec", "# UI\n\n完整规范", category="ui", created_by="test")

        with patch(
            "delivery_workflow.engine.maybe_run_command",
            return_value={"executed": True, "returncode": 0, "stdout": "# 前端技术方案\n\n方案", "stderr": "", "command": ["codex"]},
        ):
            result = execute_step(run_id, "frontend-tech-design")

        self.assertEqual(result["next_step"], "smoke-test-case-design")

        write_artifact(run_id, "smoke_test_cases", "# 冒烟测试", category="test", created_by="test")
        with patch("delivery_workflow.engine.create_doc_as_bot", return_value={"ok": True, "url": "https://example.test/frontend"}):
            frontend_doc = execute_step(run_id, "publish-frontend-tech-doc")

        self.assertEqual(frontend_doc["next_step"], "publish-smoke-test-cases-doc")

    def test_publish_prd_doc_uses_final_prd_title_and_structured_xml(self) -> None:
        self.write_config({"lark": {"enabled": True, "identity": "bot"}})
        created = create_project(requirement="做 TODO H5", title="TODO H5", auto_start=False)
        run_id = created["run_id"]
        write_artifact(run_id, "prd_v1", "# PRD v1\n\n初稿", category="prd", created_by="test")
        write_artifact(run_id, "requirement_review_report", "# 多角色需求评审报告\n\n- 补充空状态", category="review", created_by="test")
        write_artifact(run_id, "prd_v2", "# TODO H5 PRD\n\n## 未采纳意见\n\n无", category="prd", created_by="test")

        with (
            patch("delivery_workflow.engine.lark_doc_capability", return_value={"ok": True}),
            patch("delivery_workflow.engine.create_doc_as_bot", return_value={"ok": True, "url": "https://example.test/prd"}) as create_doc,
            patch("delivery_workflow.engine.send_text_as_bot", return_value={"ok": True, "message_id": "msg_test"}) as send_text,
        ):
            result = execute_step(run_id, "publish-prd-doc")

        self.assertEqual(result["result"]["doc_url"], "https://example.test/prd")
        args = create_doc.call_args.args
        self.assertEqual(args[0], "TODO H5 PRD")
        self.assertIn("<title>TODO H5 PRD</title>", args[1])
        self.assertIn("<h1>版本变化表格</h1>", args[1])
        self.assertIn("<h1>各 Agent 评审意见汇总</h1>", args[1])
        self.assertIn("补充空状态", args[1])
        send_text.assert_not_called()

    def test_lark_group_notifications_are_limited_to_three_stages(self) -> None:
        self.write_config({"lark": {"enabled": True, "identity": "bot", "chat_id": "oc_config"}})

        with patch("delivery_workflow.engine.send_text_as_bot", return_value={"ok": True, "message_id": "msg_project"}) as send_text:
            created = create_project(requirement="做 TODO H5", title="TODO H5", auto_start=False)

        run_id = created["run_id"]
        self.assertEqual(send_text.call_count, 1)
        self.assertEqual(send_text.call_args.args[0], "oc_config")
        self.assertIn("TODO H5 项目已创建，正在进行中....", send_text.call_args.args[1])

        manifest = {
            "documents": [
                {"source_artifact": "prd_v2", "url": "https://example.test/prd"},
                {"source_artifact": "ui_design_spec", "url": "https://example.test/ui"},
                {"source_artifact": "frontend_tech_design", "url": "https://example.test/frontend"},
                {"source_artifact": "smoke_test_cases", "url": "https://example.test/smoke"},
                {"source_artifact": "test_report", "url": "https://example.test/test-report"},
                {"source_artifact": "final_delivery_report", "url": "https://example.test/final-report"},
            ]
        }
        write_artifact(run_id, "lark_doc_manifest", json.dumps(manifest, ensure_ascii=False), category="lark-doc", created_by="test")

        with patch("delivery_workflow.engine.send_text_as_bot", return_value={"ok": True, "message_id": "msg_docs"}) as send_text:
            execute_step(run_id, "development-doc-confirmation")

        self.assertEqual(send_text.call_count, 1)
        docs_message = send_text.call_args.args[1]
        self.assertIn("各 Agent 已将文档创建完成", docs_message)
        self.assertIn("PRD：https://example.test/prd", docs_message)
        self.assertIn("UI 设计规范：https://example.test/ui", docs_message)
        self.assertIn("冒烟测试用例：https://example.test/smoke", docs_message)

        with patch("delivery_workflow.engine.send_text_as_bot", return_value={"ok": True, "message_id": "msg_final"}) as send_text:
            execute_step(run_id, "delivery-notification")

        self.assertEqual(send_text.call_count, 1)
        final_message = send_text.call_args.args[1]
        self.assertIn("TODO H5 项目进度已全部完成", final_message)
        self.assertIn("测试报告：https://example.test/test-report", final_message)
        self.assertIn("最终交付报告：https://example.test/final-report", final_message)

    def test_qa_quality_gate_loops_bug_fix_then_regression(self) -> None:
        created = create_project(requirement="做 TODO H5", title="TODO H5", auto_start=False)
        run_id = created["run_id"]
        for name in (
            "prd_v2",
            "ui_design_spec",
            "frontend_tech_design",
            "backend_tech_design",
            "smoke_test_cases",
            "development_smoke_self_test_report",
            "frontend_dev_result",
            "backend_dev_result",
            "integration_test_result",
        ):
            write_artifact(run_id, name, f"# {name}\n\n内容", category="test", created_by="test")

        failing_report = json.dumps({"quality_gate": {"bug_counts": {"block": 0, "critical": 0, "major": 3, "minor": 0}}}, ensure_ascii=False)
        with patch(
            "delivery_workflow.engine.maybe_run_command",
            return_value={"executed": True, "returncode": 0, "stdout": failing_report, "stderr": "", "command": ["codex"]},
        ):
            first = execute_step(run_id, "qa-system-testing")

        self.assertEqual(first["next_step"], "bug-fix")

        with patch(
            "delivery_workflow.engine.maybe_run_command",
            return_value={"executed": True, "returncode": 0, "stdout": "# 修复报告\n\nfixed_bugs: BUG-1", "stderr": "", "command": ["codex"]},
        ):
            fix = execute_step(run_id, "bug-fix")

        self.assertEqual(fix["next_step"], "qa-regression-testing")

        passing_report = json.dumps({"quality_gate": {"bug_counts": {"block": 0, "critical": 0, "major": 0, "minor": 1}}}, ensure_ascii=False)
        with patch(
            "delivery_workflow.engine.maybe_run_command",
            return_value={"executed": True, "returncode": 0, "stdout": passing_report, "stderr": "", "command": ["codex"]},
        ):
            second = execute_step(run_id, "qa-regression-testing")

        self.assertEqual(second["next_step"], "qa-test-report")

    def test_watch_run_settles_after_document_confirmation_gate_submission(self) -> None:
        created = create_project(requirement="新增客户退款审批能力", title="客户退款审批")
        run_id = created["run_id"]

        submit_gate(run_id, "development-doc-confirmation", {"approved": True, "approver": "ou_test", "comment": "通过"})
        result = watch_run(run_id, timeout_seconds=0.3, poll_interval_seconds=0.05)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "timeout")

    def test_delete_current_project_backs_up_and_removes_workflow_owned_files(self) -> None:
        create_project(requirement="做 TODO H5", title="TODO H5")
        Path(".env").write_text("LOCAL_ONLY=1\n", encoding="utf-8")
        Path(".gitignore").write_text(".env\n", encoding="utf-8")

        result = delete_current_project()

        self.assertTrue(result["ok"])
        self.assertTrue(Path(result["backup_path"]).exists())
        self.assertFalse(Path(".delivery-workflow").exists())
        self.assertFalse(Path("delivery-project").exists())
        self.assertFalse(Path("source-code").exists())
        self.assertFalse(Path(".env").exists())

    def test_host_hook_blocks_env_reads_and_destructive_commands(self) -> None:
        for command in ("cat .env", "rm -rf ."):
            self.assertIsNotNone(_blocked_command_reason(command))

    def test_mcp_create_project_reports_document_confirmation_state(self) -> None:
        result = call_mcp_tool("delivery_create_project", {"requirement": "做 TODO H5", "title": "TODO H5"})

        self.assertEqual(result["final_state"]["current_step"], "development-doc-confirmation")
        self.assertIn("development-doc-confirmation", result["final_state"]["open_gates"])
        self.assertEqual(result["watch"]["reason"], "workflow stops at human document confirmation")


if __name__ == "__main__":
    unittest.main()
