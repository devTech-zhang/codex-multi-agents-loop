---
name: delivery-workflow
description: Use when starting, inspecting, advancing, deleting, debugging, or bug-fixing a Delivery Workflow project, especially when Feishu/Lark docs, worker jobs, gates, artifacts, or QA quality gates are involved.
---

# Delivery Workflow

Delivery Workflow 编排需求、PRD、评审、设计、开发、自测、QA、bug-fix 和最终交付报告。Agent 负责生成和执行，Workflow Worker 负责状态、Gate、入队、artifact、日志和质量门禁。

## When To Use

使用本 skill 当用户要：

- 初始化当前项目工作区。
- 新建或推进交付项目。
- 查询此项目状态、artifact、Gate、job 或日志。
- 排查飞书文档发布。
- 删除此项目并备份。
- 根据真人反馈触发 bug-fix。
- 调试前后端开发、自测、QA 回归和质量门禁。

不要替用户提交开发前资料确认 Gate；只有用户明确说文档可以继续开发时才提交通过。

## Core Rules

| 规则 | 要求 |
| --- | --- |
| 项目目录 | 当前工作目录就是业务项目目录 |
| 配置优先级 | 项目 `workflow.config.json` 优先，插件 `delivery-workflow.config.json` 只是模板 |
| 状态目录 | `.delivery-workflow/` 存 SQLite、日志、PID |
| 资料目录 | `delivery-project/` 存 Agent 产物 |
| 源码目录 | `source-code/frontend`、`source-code/backend` |
| 平台 | 只支持 Codex 和 Claude Code |
| 飞书 | 只依赖本机 `lark-cli` 配置；workflow 不读取 `.env` |
| 开发前确认 | 文档发布后停在 `development-doc-confirmation` Gate |
| 质量门禁 | QA 未达 `quality_gate` 时进入 `bug-fix -> qa-regression-testing` 循环 |
| hooks | 宿主 hooks 只记录 evidence 和做安全拦截，不自动推进 worker |

## Common Actions

在 Codex / Claude Code 会话中，优先使用 MCP 工具。CLI 命令只作为 MCP 不可用时的人类排障兜底；如果必须通过 Bash 执行创建项目或连续 worker，必须设置足够长的超时时间，不能因 30 秒默认超时而重复创建项目。

| 用户意图 | MCP 工具 | CLI |
| --- | --- | --- |
| 初始化项目配置 | `delivery_init_project_config` | `python3 -m delivery_workflow.cli config init` |
| 新建项目 | `delivery_create_project` | `python3 -m delivery_workflow.cli project create --title ... --requirement ... [--lark-chat-id oc_xxx]` |
| 查询此项目状态 | `delivery_get_current_project_status` | `python3 -m delivery_workflow.cli project status` |
| 推进一个 job | `delivery_worker_once` | `python3 -m delivery_workflow.cli worker once` |
| 连续推进到稳定状态 | `delivery_worker_until_blocked` | `python3 -m delivery_workflow.cli worker until-blocked --run-id <run_id>` |
| 等待 Gate 提交后稳定 | `delivery_watch_run` | `python3 -m delivery_workflow.cli workflow watch --run-id <run_id>` |
| 触发 bug-fix | `delivery_request_bug_fix` | `python3 -m delivery_workflow.cli project bug-fix --issue ...` |
| 删除此项目 | `delivery_delete_current_project` | `python3 -m delivery_workflow.cli project delete` |
| 检查能力 | `delivery_doctor` | `python3 -m delivery_workflow.cli doctor` |

删除此项目默认会在当前目录备份 zip；用户明确不要备份时才使用 `--no-backup`。

## Project Lifecycle

```text
PRD v1
-> requirement review
-> final PRD
-> UI design spec
-> frontend technical design
-> backend technical design (skip when not needed)
-> QA smoke test cases
-> publish Feishu/Lark documents
-> development-doc-confirmation gate
-> frontend development
-> backend development (skip when not needed)
-> frontend/backend integration
-> developer smoke self-test
-> QA system test
-> bug-fix <-> QA regression
-> QA test report
-> final delivery report
```

`enable_agent_cli=true` 时，开发和 QA 必须真实执行命令；若输出显示“等待批准”“暂未运行测试”等未执行信号，步骤必须视为阻断或失败。`enable_agent_cli=false` 是 prepared-only 演示/任务包模式，Workflow 可生成结构化占位产物并继续推进全链路，但这些产物只能标记为 prepared，不能对外声称已真实开发或真实测试。

## Feishu/Lark Notes

- 飞书凭证完全由 `lark-cli` 管理；先完成 `lark-cli config init --new` 和 `lark-cli auth login --recommend`。
- `workflow.config.json` 的 `lark` 支持 `enabled`、`identity`，项目级配置还可以写 `chat_id`。
- 需要把文档链接和阶段消息发到项目群时，可在项目 `workflow.config.json` 写 `lark.chat_id`，或创建项目时传 `lark_chat_id`；创建参数优先。
- 群消息只发送三个阶段：项目创建、开发前文档汇总、最终测试/交付报告汇总；不要为每个 step 单独发群通知。
- PRD、UI 设计规范、前后端技术方案、冒烟测试用例、测试报告和最终报告会整理为带标题与表格结构的 XML 后发布飞书文档。
- `identity` 会作为 `lark-cli --as` 传入，支持 `bot` 或 `user`。
- 如果缺少 `lark-cli`，执行 `delivery_doctor` 查看安装提示。

## Config

项目根目录放：

```text
workflow.config.json
```

飞书配置示例：

```json
{
  "lark": {
    "enabled": true,
    "identity": "bot",
    "chat_id": "oc_xxx"
  }
}
```

`workflow.config.json` 可在后续步骤重新读取，但已创建 run 的 `workflow_id`、项目初始平台和已写出的 artifact 路径不会自动迁移。项目开始后不要随意修改 `storage.*`。

## Evidence And Logs

| 文件 | 内容 |
| --- | --- |
| `.delivery-workflow/logs/workflow.log` | workflow 事件、step、artifact、agent 执行摘要 |
| `.delivery-workflow/logs/host-hooks.jsonl` | 宿主 hooks 记录的文件写入、Bash 命令和 Stop 事件 |
| `.delivery-workflow/worker-<run_id>.log` | 后台 worker 推进日志 |

排查“Agent 是否真的自测”时，优先看 `host-hooks.jsonl` 和对应 dev/QA artifact。

## Common Mistakes

- 看到 `development-doc-confirmation` 就直接继续：必须先让用户确认文档是否可进入开发。
- 把 `enable_agent_cli=false` 的任务包/占位测试报告当成已真实开发、真实测试完成：禁止。
- 未检查 QA `quality_gate` 就进入最终报告：必须由 workflow 根据 bug 等级和数量决定。
- 从插件源码目录启动业务项目流程：业务项目必须在自己的项目目录初始化和运行。
- hooks 只做 evidence 与安全拦截，不负责自动推进 worker。

## Verification

改动 workflow 或 skill 后至少运行：

```bash
python3 -m unittest tests.test_core
git diff --check
```

涉及 Python 模块时补充：

```bash
python3 -m compileall delivery_workflow
```
