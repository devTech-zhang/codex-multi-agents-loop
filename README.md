# Codex 交付工作流

`codex-delivery-workflow` 是一个面向 Codex 的轻量交付工作流插件。它的目标不是一次性复刻完整研发平台，而是先验证“状态流转 + 多子 Agent 协作 + 本地产物归档”这条最小闭环是否能在插件中稳定跑通。

## 当前范围

本分支只保留必要能力：

- 文件化工作流：从 `workflow/codex-delivery-workflow.toml` 读取步骤顺序。
- 子 Agent 配置：从 `agents/*.toml` 读取角色画像、输入输出、职责边界和可调用 skill。
- 状态持久化：用 SQLite 记录 project、run、step、job、event、artifact。
- 产物串联：每个步骤只读取声明的输入产物，只写出声明的输出产物。
- Codex 优先：默认只生成任务包和预备产物；打开 `enable_agent_cli` 后再调用 Codex CLI。
- 最小 MCP：只提供初始化、创建、查询、推进、读取产物和查看定义等工具。

暂不包含审批、外部通知、质量门禁循环、跨平台适配和复杂发布流程。

## 目录结构

```text
agents/
  product-manager.toml
  ui-designer.toml
  frontend-impl.toml
  backend-impl.toml
  qa-tester.toml
workflow/
  codex-delivery-workflow.toml
skills/
  codex-delivery-workflow/
    SKILL.md
  codex-delivery-prd/
    SKILL.md
  codex-delivery-ui-spec/
    SKILL.md
  codex-delivery-frontend-impl/
    SKILL.md
  codex-delivery-backend-impl/
    SKILL.md
  codex-delivery-qa/
    SKILL.md
```

## 流程

```text
product-manager
-> ui-designer
-> frontend-impl
-> backend-impl
-> qa-tester
```

子 Agent 不直接相互调用。工作流通过 artifact 串联上下文：

- `product-manager` 读取原始需求和项目上下文，输出 `prd`。
- `ui-designer` 读取 `prd`，输出 `design_spec`。
- `frontend-impl` 读取 `prd` 和 `design_spec`，输出 `frontend_result`。
- `backend-impl` 读取 `prd`，输出 `backend_result`。
- `qa-tester` 读取 PRD、设计和实现结果，输出 `qa_report`。

## 运行目录

在业务项目目录中运行后，会生成：

```text
.codex-delivery-workflow/
  workflow.db
  logs/
delivery-artifacts/
delivery-workspace/
workflow.config.json
```

## MCP 工具

```text
codex_delivery_workflow_init
codex_delivery_workflow_create
codex_delivery_workflow_status
codex_delivery_workflow_worker_once
codex_delivery_workflow_worker_until_idle
codex_delivery_workflow_list_artifacts
codex_delivery_workflow_read_artifact
codex_delivery_workflow_inspect
```

## 本地命令

```bash
python3 -m delivery_workflow.cli config init
python3 -m delivery_workflow.cli project create \
  --title "TODO Web" \
  --requirement "做一个极简 TODO Web 应用"
python3 -m delivery_workflow.cli project status
```

默认配置中 `code_platforms.enable_agent_cli=false`，此时工作流只生成任务包和预备产物，不启动 Codex CLI。需要验证真实子 Agent 执行时，再把该配置改成 `true`。

## 验证

```bash
python3 -m unittest tests.test_core
python3 -m compileall delivery_workflow
git diff --check
```
