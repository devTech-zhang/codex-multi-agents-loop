---
name: codex-delivery-workflow
description: 当用户要启动、检查或推进 Codex 交付工作流时使用；该工作流会按顺序派发 product-manager、ui-designer、frontend-impl、backend-impl 和 qa-tester 五个子 Agent。
---

# Codex 交付工作流

`codex-delivery-workflow` 是轻量交付工作流入口。它只负责状态流转、任务入队、产物归档和子 Agent 派发，不承担审批、外部通知或复杂发布流程。

## 使用场景

当用户提出以下意图时使用本 skill：

- 初始化当前项目目录的工作流环境。
- 基于一段需求创建一次交付工作流运行。
- 查询 run、step、job、event 或 artifact 状态。
- 推进一个或多个待执行 job。
- 读取子 Agent 任务包和输出产物。

## 核心规则

| 规则 | 要求 |
| --- | --- |
| 工作流定义 | 读取 `workflow/codex-delivery-workflow.toml` |
| 子 Agent 定义 | 读取 `agents/*.toml` |
| 运行状态 | SQLite 和日志写入 `.codex-delivery-workflow/` |
| 产物目录 | 输出写入 `delivery-artifacts/` |
| 工作目录 | 需要落代码时使用 `delivery-workspace/` |
| 平台边界 | 只面向 Codex |
| 当前范围 | 不做审批、外部通知、质量门禁循环或跨平台适配 |

## Agent 链路

```text
product-manager
-> ui-designer
-> frontend-impl
-> backend-impl
-> qa-tester
```

子 Agent 不直接相互调用，也不自行推进状态。每个步骤只读取工作流声明的输入产物，只输出工作流声明的结果产物。

## MCP 工具

优先使用 MCP 工具，CLI 只作为排障兜底。

| 意图 | MCP 工具 | CLI 兜底 |
| --- | --- | --- |
| 初始化 | `codex_delivery_workflow_init` | `python3 -m delivery_workflow.cli config init` |
| 创建运行 | `codex_delivery_workflow_create` | `python3 -m delivery_workflow.cli project create --title ... --requirement ...` |
| 查询状态 | `codex_delivery_workflow_status` | `python3 -m delivery_workflow.cli project status` |
| 推进一次 | `codex_delivery_workflow_worker_once` | `python3 -m delivery_workflow.cli worker once --run-id <run_id>` |
| 推进到空闲 | `codex_delivery_workflow_worker_until_idle` | `python3 -m delivery_workflow.cli worker until-idle --run-id <run_id>` |
| 列出产物 | `codex_delivery_workflow_list_artifacts` | `python3 -m delivery_workflow.cli artifact list --run-id <run_id>` |
| 读取产物 | `codex_delivery_workflow_read_artifact` | `python3 -m delivery_workflow.cli artifact read --run-id <run_id> --name <name>` |
| 查看定义 | `codex_delivery_workflow_inspect` | `python3 -m delivery_workflow.cli workflow inspect` |

## 执行模式

`code_platforms.enable_agent_cli=false` 是默认验证模式。此时工作流只写任务包和预备产物，不启动 Codex CLI。

`code_platforms.enable_agent_cli=true` 会让每个 agent step 调用 Codex CLI 执行生成的任务包。建议先完成状态流转 smoke test，再打开真实执行。

## 验证

修改工作流、子 Agent、运行时代码或 skill 文档后，至少运行：

```bash
python3 -m unittest tests.test_core
python3 -m compileall delivery_workflow
git diff --check
```
