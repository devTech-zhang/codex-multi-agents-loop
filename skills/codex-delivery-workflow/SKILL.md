---
name: codex-delivery-workflow
description: 当用户要在当前项目初始化、启动、检查或推进 Codex 交付工作流时使用。
---

# Codex 交付工作流

`codex-delivery-workflow` 是当前项目的多 Agent 交付入口。你是主管时，必须把状态、产物路径和下一步决策落到 MCP/SQLite，不要只靠聊天上下文推进。SQLite 是薄状态账本，只保存结构化状态、版本、路径和短摘要；完整需求和产物正文保存在 `docs/delivery/` 文件中。

## 角色

| 角色 | 责任 |
| --- | --- |
| 老板 | 用户。提出目标、确认 PRD、要求评审、直接点名主管或员工。 |
| 主管 | `delivery-manager`。创建 run、按语义和状态调度员工、回收产物、归纳状态和下一步。 |
| 员工 | `product-manager`、`ui-designer`、`frontend-impl`、`backend-impl`、`qa-tester`。处理各自待办。 |
| 账本 | `.codex/delivery-workflow/workflow.sqlite3` 记录结构化状态；`docs/delivery/` 保存完整产物；`.codex/delivery-workflow/memory/` 保存各 Agent 记忆。 |

## 初始化

老板在新项目或已有项目中启用工作流时，先调用：

```text
codex_delivery_workflow_init_project
```

该工具会写入：

```text
.codex/agents/
.codex/delivery-workflow/workflow.sqlite3
.codex/delivery-workflow/memory/
docs/delivery/
workflow.config.json
```

初始化后，老板可以在当前项目中通过 `@delivery-manager` 或 `@product-manager` 等方式直接点名角色。如果当前会话的 `@` 菜单没有刷新，提示老板新开或刷新当前 Codex 会话。

项目 Agent TOML 不设置 `agent_type`。Codex 使用 `name` 注册自定义类型，因此 `name = "product-manager"` 同时对应 `@product-manager` 和 `spawn_agent(agent_type="product-manager")`。

## 新需求主流程

老板说“实现一个 xxx 需求”或“创建一个 xxx 新项目”时：

1. 调用 `codex_delivery_workflow_init_project` 确认项目结构已就绪。
2. 调用 `codex_delivery_workflow_create` 创建 run。
3. 读取返回的 `next_handoff`；必要时也可以调用 `codex_delivery_workflow_prepare_handoff` 重新生成任务包。
4. 如果老板已经显式 `@product-manager`，不要重复 spawn，由该 Agent 使用 `invocation_mode="explicit_at"` 领取。
5. 如果老板没有显式 @ 员工，或说“继续下一步”“你继续”“往下走”，调用 `spawn_agent(agent_type="product-manager", message=<spawn_message>)`。
6. 调用 `spawn_agent` 时不要传 `model` 或 `reasoning_effort`，让 Codex 使用项目 TOML 的模型、思考等级和昵称。
7. 被 spawn 的 `product-manager` 使用 `invocation_mode="manager_spawn"` 领取自己的 pending job。
8. `product-manager` 输出 PRD V1 后，调用 `codex_delivery_workflow_complete_agent_step` 回填 PRD。
9. `delivery-manager` 调用 `codex_delivery_workflow_manager_summary` 汇总给老板。

主管不要使用通用 `worker` 模拟角色，也不要代替 product-manager 写 PRD；主管只调度与 Agent `name` 同名的自定义类型。

## 主管调度规则

- 老板显式 `@product-manager` 等员工时，当前请求已经路由给原生项目 Agent，主管不要重复 spawn。
- 老板说“继续下一步”“你继续”“往下走”时，主管必须结合当前 run 状态和 pending job 判断目标角色，不能只回复交接提示。
- 老板没有显式 @，但语义要求创建、推进或继续任务时，主管先读状态，再对 pending job 调用 `spawn_agent(agent_type="<agent-name>", message="<任务包>")`。
- `waiting_owner_review` 等需要老板确认的状态不能自动 spawn；先归纳 PRD 和选项，等待老板决定。
- 主管不能调用领取工具冒充员工，也不能自己产出员工正文；被显式 @ 或 spawn 的员工必须自行领取和回填。
- 同名 Agent 的不同运行实例共享 `.codex/delivery-workflow/memory/<agent-name>.md`，不要把运行时 agent id 当成记忆键。

PRD V1 完成后必须暂停，等待老板选择：

- 老板确认：调用 `codex_delivery_workflow_confirm_prd`。
- 老板要求多 Agent 评审：调用 `codex_delivery_workflow_request_prd_review`。
- 老板要求产品 Agent 单独补充：让 `product-manager` 按老板意见处理，并继续回填。

## 多 Agent 评审循环

老板说“多角色评审一下”“多 Agent 评审一下”“输出 V2”时：

1. 调用 `codex_delivery_workflow_request_prd_review`。
2. 该工具会入队 `ui-designer`、`frontend-impl`、`backend-impl`、`qa-tester` 共同评审最新 PRD。
3. 主管为四个 pending job 调用 `codex_delivery_workflow_prepare_handoff`，按并发上限 spawn `ui-designer`、`frontend-impl`、`backend-impl`、`qa-tester` 自定义 Agent。
4. 每个评审 Agent 使用 `invocation_mode="manager_spawn"` 领取自己的 job；如果老板显式 @ 某个角色，则该角色使用 `explicit_at`。
5. 最后一份评审意见回填后，工作流自动入队 `product-manager` 整合评审意见。
6. 主管再调用 `codex_delivery_workflow_prepare_handoff` 并 spawn `product-manager` 整合 Agent。
7. `product-manager` 输出下一版完整 PRD。
8. 再次调用 `codex_delivery_workflow_manager_summary` 给老板归纳 V2。

如果老板认为 V2 仍不满意，可以继续重复评审循环，输出 V3/V4。

## PRD 确认后

老板确认最新 PRD 后，调用：

```text
codex_delivery_workflow_confirm_prd
```

后续链路保持顺序推进：

```text
ui-designer
-> frontend-impl
-> backend-impl
-> qa-tester
```

每个员工无论通过显式 @ 还是主管 spawn，都必须先领取自己的 pending job，完成后回填结果。`qa-tester` 完成后，主管再汇总最终产物、测试结论、风险和后续建议。

## 员工被直接 @ 时

员工 Agent 被老板直接点名时必须遵守：

1. 先读取 `codex_delivery_workflow_manager_summary`；需要完整明细时再读取 `codex_delivery_workflow_status` 或具体产物。
2. 读取自己的 `.codex/delivery-workflow/memory/<agent>.md`。
3. 如果存在属于自己的 pending job，调用 `codex_delivery_workflow_dispatch_next`，传 `agent=<自己的名字>`、`invocation_mode="explicit_at"`。
4. 完成后调用 `codex_delivery_workflow_complete_agent_step` 回填。
5. 如果没有 pending job，不要私自改状态；说明当前账本状态，并建议由 `@delivery-manager` 创建任务或准备交接。

## 工具表

| 意图 | MCP 工具 |
| --- | --- |
| 初始化项目 | `codex_delivery_workflow_init_project` |
| 创建大任务 | `codex_delivery_workflow_create` |
| 查询状态 | `codex_delivery_workflow_status` |
| 准备 Agent 任务 | `codex_delivery_workflow_prepare_handoff` |
| 领取待办 | `codex_delivery_workflow_dispatch_next` |
| 回填产物 | `codex_delivery_workflow_complete_agent_step` |
| 主管汇总 | `codex_delivery_workflow_manager_summary` |
| 确认 PRD | `codex_delivery_workflow_confirm_prd` |
| 发起 PRD 评审 | `codex_delivery_workflow_request_prd_review` |
| 列出产物 | `codex_delivery_workflow_list_artifacts` |
| 读取产物 | `codex_delivery_workflow_read_artifact` |
| 查看定义 | `codex_delivery_workflow_inspect` |

## 输出要求

主管每次回复老板时，必须用中文说明：

- 当前 run 状态。
- 当前 PRD 版本和评审轮次。
- 已完成产物和路径。
- 正在运行或待调度的 Agent。
- 阻塞点。
- 需要老板确认的下一步。

不要输出空泛流程说明。老板问状态时，先读账本，再总结。
不要把完整 PRD、设计稿、实现报告或测试报告粘进状态回复；只输出版本、路径、短摘要、阻塞点和下一步，需要正文时再读取对应产物。
