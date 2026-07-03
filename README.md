# Codex 交付工作流

`codex-delivery-workflow` 是一个面向 Codex 的轻量交付工作流插件。它把当前项目初始化成一个多 Agent 协作工作台：老板可以找主管推进完整任务，也可以直接 `@product-manager`、`@ui-designer`、`@frontend-impl`、`@backend-impl`、`@qa-tester` 找对应员工处理自己的部分。

## 当前范围

本分支只保留最小闭环：

- 项目级 Agent：init 后写入 `.codex/agents/*.toml`，让 Codex 可以在当前项目里识别主管和员工。
- 薄状态账本：SQLite 只记录 project、run、job、step、artifact、review、event 和 agent memory 的状态、版本、路径和短摘要；完整需求、PRD、设计、实现和测试报告都放在文件产物里。
- PRD 审核循环：PRD V1 先给老板确认；老板可要求多 Agent 评审，由产品 Agent 整合为 V2/V3。
- 产物归档：所有产物写入 `docs/delivery/`，按 run、Agent、类别和版本归档。
- 主管汇总：`delivery-manager` 负责状态总结、产物归纳、阻塞点和下一步建议。

暂不包含审批、外部通知、飞书、质量门禁循环、跨平台适配和复杂发布流程。

## 协作模型

```text
老板：用户
主管：delivery-manager
员工：product-manager / ui-designer / frontend-impl / backend-impl / qa-tester
账本：.codex/delivery-workflow/workflow.sqlite3（只放结构化状态和路径）
产物：docs/delivery/
记忆：.codex/delivery-workflow/memory/
```

老板可以这样使用：

```text
@delivery-manager 实现一个会员积分兑换需求
@product-manager 这个 PRD 再补一下异常流程
@ui-designer 评审下这个 PRD 的交互风险
@backend-impl 看下数据模型和接口风险
```

## 初始化后的项目结构

```text
.codex/
  agents/
    delivery-manager.toml
    product-manager.toml
    ui-designer.toml
    frontend-impl.toml
    backend-impl.toml
    qa-tester.toml
  delivery-workflow/
    workflow.sqlite3
    logs/
    memory/
      delivery-manager.md
      product-manager.md
      ui-designer.md
      frontend-impl.md
      backend-impl.md
      qa-tester.md
docs/
  delivery/
workflow.config.json
```

插件仓库里的 `agents/*.toml` 是项目级 Agent 模板源。真正让 `@product-manager` 出现在当前项目里的，是 init 把模板写入业务项目的 `.codex/agents/`。

## 主流程

```text
老板提出需求
-> delivery-manager 创建 run
-> product-manager 输出 PRD V1
-> delivery-manager 汇总 V1 给老板
-> 老板确认 PRD 或要求多 Agent 评审
```

老板确认 PRD 后进入交付链路：

```text
ui-designer
-> frontend-impl
-> backend-impl
-> qa-tester
-> delivery-manager 汇总最终状态和产物
```

老板要求评审时进入循环：

```text
ui-designer / frontend-impl / backend-impl / qa-tester 并行评审最新 PRD
-> product-manager 整合意见输出下一版 PRD
-> delivery-manager 再次归纳给老板
-> 老板确认或继续评审
```

## MCP 工具

```text
codex_delivery_workflow_init
codex_delivery_workflow_init_project
codex_delivery_workflow_create
codex_delivery_workflow_status
codex_delivery_workflow_dispatch_next
codex_delivery_workflow_complete_agent_step
codex_delivery_workflow_manager_summary
codex_delivery_workflow_confirm_prd
codex_delivery_workflow_request_prd_review
codex_delivery_workflow_list_artifacts
codex_delivery_workflow_read_artifact
codex_delivery_workflow_inspect
```

## 主管执行约定

`delivery-manager` 不亲自写 PRD、设计、前端、后端或 QA 报告。它只做调度和归纳：

1. 创建或读取 run。
2. 派发对应 Agent。
3. 等待 Agent 输出。
4. 回填产物和状态。
5. 更新或读取 Agent memory。
6. 汇总当前状态、产物路径、阻塞点和下一步。

员工被老板直接 `@` 时，也必须先读取状态账本和自己的 memory。若有属于自己的 pending job，就领取并回填；若没有待办，要说明当前状态并建议由 `@delivery-manager` 派发。需要查看完整需求或产物正文时，通过产物路径或读取产物工具获取，不要把完整正文长期塞进状态总结。

## 本地命令

```bash
python3 -m delivery_workflow.cli config init
python3 -m delivery_workflow.cli project create \
  --title "TODO Web" \
  --requirement "做一个极简 TODO Web 应用"
python3 -m delivery_workflow.cli project status
```

本地命令只用于初始化、排障和查看状态。日常协作以 Codex 当前会话中的主管、员工 Agent 和 MCP 工具为准。

## 验证

```bash
python3 -m unittest tests.test_core
python3 -m compileall delivery_workflow
git diff --check
```
