from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from delivery_workflow.capabilities import doctor
from delivery_workflow.config import DEFAULT_CONFIG_TEMPLATE, WORKSPACE_CONFIG_NAME, load_config
from delivery_workflow.definitions import load_workflow
from delivery_workflow.engine import create_project, delete_project, execute_step, get_project_status, handle_lark_card_event, list_projects, read_artifact, retry_prd_approval_lark, run_worker_once, status, submit_gate, write_artifact
from delivery_workflow.engine import _host_escalation_payload, _host_lark_retry_command
from delivery_workflow.lark import _is_keychain_unavailable, build_prd_approval_card
from delivery_workflow.lark_events import sdk_event_to_payload
from delivery_workflow.lark_sdk import load_sdk_credentials, sdk_preflight
from delivery_workflow.platforms import build_agent_command, select_dev_executor


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
        if overrides:
            config = _merge(config, overrides)
        Path(WORKSPACE_CONFIG_NAME).write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_workflow_definition_separates_step_categories(self) -> None:
        workflow = load_workflow()
        categories = {step["category"] for step in workflow.steps}
        executors = {step["executor"] for step in workflow.steps}

        self.assertIn("interactive", categories)
        self.assertIn("automated", categories)
        self.assertIn("notification", categories)
        self.assertIn("gate", executors)
        self.assertIn("agent", executors)
        self.assertIn("dev-runner", executors)
        self.assertIn("lark-doc", executors)

    def test_platform_executor_policy(self) -> None:
        self.assertEqual(select_dev_executor("codex"), "codex")
        self.assertEqual(select_dev_executor("claude-code"), "claude")
        self.assertEqual(select_dev_executor("opencode"), "opencode")
        self.assertEqual(select_dev_executor("openclaw"), "claude")
        self.assertEqual(select_dev_executor("something-else"), "codex")

    def test_workspace_config_controls_defaults_and_dev_platforms(self) -> None:
        self.write_config(
            {
                "workflow": {"auto_start": False},
                "code_platforms": {"default": "opencode", "frontend": "opencode", "backend": "claude-code"},
                "lark": {"enabled": True, "dry_run": True, "chat_id": "oc_config", "send_step_notifications": False},
            }
        )

        self.assertEqual(load_config()["code_platforms"]["default"], "opencode")
        created = create_project(requirement="create settings page", title="settings")
        run_id = created["run_id"]
        context = json.loads(read_artifact(run_id, "project_context")["content"])
        self.assertEqual(context["platform"], "opencode")
        self.assertEqual(context["dev_executor"], "opencode")
        self.assertEqual(context["lark_chat_id"], "oc_config")
        self.assertNotIn("auto_run", created)

        write_artifact(run_id, "dev_tasks", "tasks", category="dev", created_by="test")
        write_artifact(run_id, "frontend_tech_design", "frontend", category="tech", created_by="test")
        write_artifact(run_id, "ui_design_spec", "ui", category="ui", created_by="test")
        result = execute_step(run_id, "frontend-development")
        self.assertEqual(result["result"]["executor"], "opencode")
        self.assertTrue(result["result"]["execution"]["command"][0].endswith("opencode"))

    def test_claude_code_uses_claude_print_mode(self) -> None:
        self.write_config({"code_platforms": {"enable_agent_cli": False}})
        prompt = Path("task.md")
        prompt.write_text("请实现这个任务", encoding="utf-8")

        command = build_agent_command("claude-code", prompt)

        self.assertEqual(command.executor, "claude")
        self.assertEqual(command.command[-1], "-p")
        self.assertEqual(command.input_text, "请实现这个任务")
        rendered = json.dumps(command.command, ensure_ascii=False)
        self.assertNotIn("claude-code", rendered)

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
        self.assertIn(f"projects/{project_id}/product-manager/prd/", prd_v2["path"])
        self.assertTrue(run_worker_once(run_id=run_id)["idle"])

    def test_lark_dry_run_prd_doc_and_approval_card(self) -> None:
        self.write_config({"lark": {"enabled": True, "dry_run": True, "send_step_notifications": False, "chat_id": "oc_test"}})
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
        self.assertIn("# 客户退款审批PRD", "\n".join(prd_doc["result"]["command"]))

        card = json.loads(read_artifact(run_id, "prd_approval_card_message")["content"])
        command = card["result"]["command"]
        self.assertIn("--msg-type", command)
        self.assertIn("interactive", command)
        content = json.loads(command[command.index("--content") + 1])
        self.assertNotIn("schema", content)
        self.assertIn("elements", content)
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("通过", rendered)
        self.assertIn("拒绝", rendered)
        self.assertIn(created["run_id"], rendered)

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

    def test_lark_card_event_submits_prd_gate(self) -> None:
        self.write_config({"lark": {"enabled": True, "dry_run": True, "send_step_notifications": False, "chat_id": "oc_test"}})
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
        self.assertIn("来自飞书审批卡片事件", gate["content"])

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

    def test_lark_sdk_credentials_can_use_single_config_explicit_values(self) -> None:
        self.write_config({"lark": {"sdk": {"app_id": "cli_test", "app_secret": "secret_test"}}})
        credentials = load_sdk_credentials(load_config())

        self.assertEqual(credentials.app_id, "cli_test")
        self.assertEqual(credentials.source, "delivery-workflow.config.json")

    def test_lark_sdk_preflight_reports_package_and_websocket_event(self) -> None:
        self.write_config({"lark": {"sdk": {"app_id": "cli_test", "app_secret": "secret_test"}}})
        report = sdk_preflight(load_config())

        self.assertEqual(report["event"], "card.action.trigger")
        self.assertEqual(report["transport"], "sdk_websocket")
        self.assertIn("package", report)
        self.assertEqual(report["package"]["install_command"][:4], ["uv", "pip", "install", "--python"])

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

    def test_default_artifact_root_is_current_workspace_delivery_projects(self) -> None:
        created = create_project(requirement="create a settings page", title="settings page", auto_start=False)
        expected_root = (Path(self.tmp.name) / "delivery-projects").resolve()
        self.assertTrue(Path(created["artifact_dir"]).resolve().is_relative_to(expected_root))
        self.assertIn(created["project_id"], created["artifact_dir"])

    def test_project_list_status_and_confirmed_delete(self) -> None:
        created = create_project(requirement="create audit log", title="audit log", auto_start=False)
        project_id = created["project_id"]

        self.assertTrue(any(project["id"] == project_id for project in list_projects()))
        self.assertEqual(get_project_status(project_id)["run"]["project_id"], project_id)
        with self.assertRaises(Exception):
            delete_project(project_id, confirm_project_id="wrong-id")

        result = delete_project(project_id, confirm_project_id=project_id)
        self.assertTrue(result["ok"])
        self.assertFalse(any(project["id"] == project_id for project in list_projects()))

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


if __name__ == "__main__":
    unittest.main()
