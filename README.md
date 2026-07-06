# Codex 交付工作流

`codex-delivery-workflow` 是一个面向 Codex 的轻量交付工作流插件。它把当前项目初始化成一个多 Agent 协作工作台：老板可以找主管推进完整任务，也可以直接 `@product-manager`、`@ui-designer`、`@frontend-impl`、`@backend-impl`、`@qa-tester` 找对应员工处理自己的部分。

## 当前范围

本分支只保留最小闭环：

- 项目级 Agent：init 后写入 `.codex/agents/*.toml`，让 Codex 可以在当前项目里识别主管和员工。
- 薄状态账本：SQLite 只记录 project、run、job、step、artifact、review、event 和 agent memory 的状态、版本、路径和短摘要；完整需求、PRD、设计、实现和测试报告都放在文件产物里。
- PRD 审核循环：PRD V1 先给老板确认；老板可要求多 Agent 评审，由产品 Agent 整合为 V2/V3。
- 产物归档：所有产物写入 `docs/delivery/`，按 run、Agent、类别和版本归档。
- 主管调度与汇总：`delivery-manager` 负责创建 run、按语义和 pending job 主动 spawn 自定义 Agent、状态总结、产物归纳、阻塞点和下一步建议。

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
  config.toml
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

插件仓库里的 `agents/*.toml` 是项目级 Agent 模板源。init 会把模板写入业务项目的 `.codex/agents/`，同时 merge/create `.codex/config.toml`：

```toml
[features]
multi_agent = true

[agents]
max_threads = 6
max_depth = 1

[agents."product-manager"]
description = "产品经理 Agent。负责把用户原始需求转成可交付 PRD，明确目标、范围、流程、验收标准、依赖和风险。"
config_file = "agents/product-manager.toml"
nickname_candidates = ["Product Manager", "Requirements Lead", "Product Owner", "PRD Owner"]
```

`config_file` 路径相对 `.codex/config.toml`，所以写 `agents/product-manager.toml`。这层配置用于让 Codex 的 `spawn_agent` role registry 稳定识别 `product-manager` 等自定义 Agent；`.codex/agents/*.toml` 里的 `name` 仍然是该 Agent 的真实身份。TOML 不设置 `agent_type`。

`description` 和 `developer_instructions` 保持中文，便于 Agent 理解和输出；`nickname_candidates` 必须使用英文 ASCII，中文昵称会导致 Codex Agent 加载或注册异常。

初始化后要信任当前项目，并完全重启或新开 Codex 会话，新的 `@` 菜单和 `spawn_agent(agent_type="product-manager")` 类型注册才会刷新。

已有项目再次执行 init 时，会自动移除旧版生成的 `agent_type = "worker"` 或 `explorer`，并把旧版默认昵称或中文昵称迁移为英文；项目自行修改的英文 Agent 昵称不会被覆盖。已有 `.codex/config.toml` 也不会整文件覆盖，只会写入/校正 `features.multi_agent`，在缺失时补 `agents.max_threads`、`agents.max_depth`，并补齐各 workflow Agent 的 `config_file`、`description`、`nickname_candidates` 注册项；已有的 model、features、其他 Agent、已有 `max_threads`、同名 Agent 的自定义 description 和英文 nickname 会保留。

`workflow/codex-delivery-workflow.toml` 中每个步骤的中文 `name` 只是展示名，例如“产品需求整理 V1”；真正关联 Agent 的字段是 `agent = "product-manager"`。中文展示名不会影响 `@product-manager` 或 `spawn_agent(agent_type="product-manager")` 的调度。

## 主流程

```text
老板提出需求
-> delivery-manager 创建 run
-> 老板显式 @product-manager 时直接命中原生项目 Agent
-> 否则 delivery-manager 主动 spawn product-manager 自定义 Agent
-> product-manager 领取任务并输出 PRD V1
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
-> delivery-manager 并行 spawn 对应自定义 Agent；老板也可以显式 @ 其中任一角色
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
codex_delivery_workflow_prepare_handoff
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
2. 调用 `codex_delivery_workflow_prepare_handoff` 准备对应 Agent 的任务包。
3. 老板已显式 `@agent` 时不重复调度；否则调用 `spawn_agent(agent_type="<agent-name>", message="<任务包>")`。
4. 调用时不要传 `model` 或 `reasoning_effort`，由项目 Agent TOML 决定模型、思考等级和昵称。
5. 等待员工自行领取、执行和回填，再读取状态与 Agent memory。
6. 汇总当前状态、产物路径、阻塞点和下一步。

员工被老板直接 `@` 时，使用 `invocation_mode="explicit_at"` 领取；被主管 spawn 时，使用 `invocation_mode="manager_spawn"` 领取。两者是不同运行实例，但按同一个 `agent_name` 读取和更新 `.codex/delivery-workflow/memory/<agent-name>.md`。同一 job 的领取是原子的，后到的实例不会重复执行。若没有待办，要说明当前状态并建议由 `@delivery-manager` 创建任务或准备调度。

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
