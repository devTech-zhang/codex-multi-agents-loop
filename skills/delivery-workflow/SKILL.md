---
name: delivery-workflow
description: Use when starting, inspecting, advancing, deleting, debugging, or bug-fixing a Delivery Workflow project, especially when PRD approval, Feishu/Lark docs, worker jobs, gates, artifacts, or QA quality gates are involved.
---

# Delivery Workflow

Delivery Workflow 编排需求、PRD、评审、设计、开发、自测、QA、bug-fix 和最终交付报告。Agent 负责生成和执行，Workflow Worker 负责状态、Gate、入队、artifact、日志和质量门禁。

## When To Use

使用本 skill 当用户要：

- 初始化当前项目工作区。
- 新建或推进交付项目。
- 查询此项目状态、artifact、Gate、job 或日志。
- 等待或排查飞书 PRD 审批卡片。
- 删除此项目并备份。
- 根据真人反馈触发 bug-fix。
- 调试前后端开发、自测、QA 回归和质量门禁。

不要用它替用户审批 PRD，也不要绕过 workflow 直接提交 Gate。

## Core Rules

| 规则 | 要求 |
| --- | --- |
| 项目目录 | 当前工作目录就是业务项目目录 |
| 配置优先级 | 项目 `workflow.config.json` 优先，插件 `delivery-workflow.config.json` 只是模板 |
| 状态目录 | `.delivery-workflow/` 存 SQLite、日志、PID |
| 资料目录 | `delivery-project/` 存 Agent 产物 |
| 源码目录 | `source-code/frontend`、`source-code/backend` |
| 平台 | 只支持 Codex 和 Claude Code |
| 审批 | PRD 审批只来自飞书卡片回调或明确的人类 CLI 兜底 |
| 质量门禁 | QA 未达 `quality_gate` 时进入 `bug-fix -> regression-testing` 循环 |
| hooks | 宿主 hooks 只记录 evidence 和做安全拦截，不自动推进 worker |

## Common Actions

在 Codex / Claude Code 会话中，优先使用 MCP 工具。CLI 命令只作为 MCP 不可用时的人类排障兜底；如果必须通过 Bash 执行创建项目或连续 worker，必须设置足够长的超时时间，不能因 30 秒默认超时而重复创建项目。

| 用户意图 | MCP 工具 | CLI |
| --- | --- | --- |
| 初始化项目配置 | `delivery_init_project_config` | `python3 -m delivery_workflow.cli config init` |
| 新建项目 | `delivery_create_project` | `python3 -m delivery_workflow.cli project create --title ... --requirement ...` |
| 查询此项目状态 | `delivery_get_current_project_status` | `python3 -m delivery_workflow.cli project status` |
| 推进一个 job | `delivery_worker_once` | `python3 -m delivery_workflow.cli worker once` |
| 连续推进到稳定状态 | `delivery_worker_until_blocked` | `python3 -m delivery_workflow.cli worker until-blocked --run-id <run_id>` |
| 等待飞书审批 | `delivery_watch_run` | `python3 -m delivery_workflow.cli workflow watch --run-id <run_id>` |
| 触发 bug-fix | `delivery_request_bug_fix` | `python3 -m delivery_workflow.cli project bug-fix --issue ...` |
| 删除此项目 | `delivery_delete_current_project` | `python3 -m delivery_workflow.cli project delete` |
| 检查能力 | `delivery_doctor` | `python3 -m delivery_workflow.cli doctor` |

删除此项目默认会在当前目录备份 zip；用户明确不要备份时才使用 `--no-backup`。

## Project Lifecycle

```text
PRD v1
-> requirement review
-> PRD v2
-> Feishu/Lark approval gate
-> UI design spec
-> frontend/backend technical design
-> technical review
-> development task breakdown
-> frontend development
-> backend development
-> developer self-test
-> QA system test
-> bug-fix <-> QA regression
-> final delivery report
```

开发和 QA 必须真实执行命令。若输出显示“等待批准”“暂未运行测试”“只生成任务包”，步骤必须视为阻断或失败，不能发布伪完成产物。

## Feishu/Lark Notes

- PRD v2、UI 设计规范、技术方案、测试用例、测试报告和最终报告要整理为带标题与表格结构的 XML 后发布飞书文档。
- 当前项目 `.env` 中的 `LARK_APP_ID`、`LARK_APP_SECRET`、`LARK_CHAT_ID` 优先于全局 `lark-cli` 配置和宿主环境变量。
- 不要直接裸跑 `lark-cli ...` 处理项目飞书动作；需要 CLI 兜底时使用 `deliveryflow lark cli -- ...`，该包装命令会自动注入当前项目 `.env`。
- 审批卡片监听 `card.action.trigger`，项目创建由 MCP 工具或 CLI 显式触发。
- 如果卡片发出后停在 `prd-approval`，调用 `delivery_watch_run` 等回调和后台 worker 稳定。
- 如果遇到 `keychain Get failed: keychain not initialized`，使用事件里的 `host_escalation.command` 请求沙箱外执行，不要把 app secret 写入仓库。

## Config And Secrets

项目根目录放：

```text
workflow.config.json
.env
```

敏感值只放 `.env` 或环境变量：

```bash
LARK_APP_ID=...
LARK_APP_SECRET=...
LARK_CHAT_ID=...
```

`workflow.config.json` 可在后续步骤重新读取，但已创建 run 的 `workflow_id`、项目初始平台、创建时的飞书群聊来源和已写出的 artifact 路径不会自动迁移。项目开始后不要随意修改 `storage.*`。

## Evidence And Logs

| 文件 | 内容 |
| --- | --- |
| `.delivery-workflow/logs/workflow.log` | workflow 事件、step、artifact、agent 执行摘要 |
| `.delivery-workflow/logs/host-hooks.jsonl` | 宿主 hooks 记录的文件写入、Bash 命令和 Stop 事件 |
| `.delivery-workflow/lark-event-consumer.log` | 飞书长连接消费者日志 |
| `.delivery-workflow/worker-<run_id>.log` | 后台 worker 推进日志 |

排查“Agent 是否真的自测”时，优先看 `host-hooks.jsonl` 和对应 dev/QA artifact。

## Common Mistakes

- 看到 `prd-approval` 就结束对话：应等待飞书卡片回调或说明当前 Gate。
- 用 MCP 替用户提交 PRD 审批：禁止，除非用户明确要求 CLI 人工兜底。
- 把 `enable_agent_cli=false` 的任务包当成已开发完成：禁止。
- 未检查 QA `quality_gate` 就进入最终报告：必须由 workflow 根据 bug 等级和数量决定。
- 从插件源码目录启动业务项目消费者：业务项目必须在自己的项目目录初始化和运行。
- hooks 只做 evidence 与安全拦截，不负责自动推进 worker。

## Verification

改动 workflow 或 skill 后至少运行：

```bash
python3 -m unittest tests.test_core
git diff --check
```

涉及 Python 模块时补充：

```bash
python3 -m py_compile delivery_workflow/*.py
```
