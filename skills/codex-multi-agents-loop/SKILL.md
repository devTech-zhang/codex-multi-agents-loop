---
name: codex-multi-agents-loop
description: 当用户要在当前项目初始化、启动、检查或推进 Codex 项目级多 Agent Loop 时使用。
---

# Codex 项目级多 Agent Loop

`codex-multi-agents-loop` 是当前项目的多 Agent 协作入口。它使用项目级 Agent 执行任务，SQLite 只保存结构化状态、版本、路径和短摘要，完整产物保存在 `.codex/multi-agents-loop/runs/`，项目记忆保存在 `.codex/multi-agents-loop/memory/`。

## 角色

| 角色 | 职责 |
| --- | --- |
| 用户 | 用户。提出目标、确认产品交接说明、要求评审、直接点名某个项目 Agent。 |
| PM 项目经理 | `project-manager`。创建 run、准备任务包、调度项目 Agent、汇总状态、暴露阻塞点和用户决策。 |
| 产品经理 | `product-manager`。扫描项目功能、维护 `PRODUCT.md`、输出产品交接说明。 |
| 架构师 | `software-architect`。输出 `feature_xxx.md`、补充 `AGENTS.md`、拆解开发任务，只设计不 coding。 |
| UI 设计师 | `ui-designer`。维护 `DESIGN.md`、读取 Figma 快照、输出 Visual Spec、资源策略和 UI 验收点。 |
| 研发工程师 | `development-engineer`。先识别当前项目技术栈，再按架构与 UI 文档完成最小必要实现和自测。 |
| 测试工程师 | `qa-engineer`。执行基础测试或可选真机/设备测试，输出证据型 QA 报告和准入建议。 |

## 初始化

新项目或已有项目启用时，先调用：

```text
codex_multi_agents_loop_init_project
```

该工具会写入：

```text
.codex/config.toml
.codex/agents/
.codex/multi-agents-loop/config.json
.codex/multi-agents-loop/workflow.sqlite3
.codex/multi-agents-loop/memory/
.codex/multi-agents-loop/runs/
.codex/multi-agents-loop/scratch/
```

`.codex` 是 Loop 控制区，只能保存 Codex 启动配置、Agent 注册、任务状态、运行产物、日志、记忆和临时 scratch。真实项目源码、项目文档和业务配置必须位于项目根目录，也就是 `.codex` 的同级目录。不要把项目源码或业务逻辑写入 `.codex/multi-agents-loop/`。

初始化后，用户可以通过 `@project-manager` 或 `@product-manager` 等方式点名角色。如果当前会话的 `@` 菜单或 `spawn_agent` 类型没有刷新，提示用户信任当前项目，并重启或新开 Codex 会话。

## 新任务主流程

用户说“实现一个 xxx 需求”“创建一个 xxx 新项目”或“优化某些项目文档”时，先识别任务模式：

- `full_workflow`：需求范围大、目标不清、需要产品交接、架构、UI、研发和 QA 串联。
- `single_agent_task`：用户直接点名某个 Agent，且任务明显属于该 Agent 职责，例如 `@development-engineer 修 bug`。
- `multi_agent_task`：只需要两个及以上指定 Agent 协作，例如 PRODUCT.md + AGENTS.md 文档更新，或产品 + 架构联合评审。

每个任务都按闭环工程语义管理。创建 run 后必须关注 `route_plan` 中的：

- `goal`：本次闭环目标。
- `constraints`：本次执行边界。
- `exit_conditions`：可结束条件。
- `current_iteration` / `max_iterations`：当前轮次与自动闭环上限，默认最多 3 轮。

Agent 任务包会包含“闭环目标”。员工 Agent 回填时必须说明执行结果、证据/产物路径、是否满足退出条件、剩余风险和建议下一步。没有验证证据时，不要假装完成，必须在 `codex_multi_agents_loop_complete_agent_step` 的 `metadata.loop_evaluation` 中写明：

```json
{
  "exit_conditions_met": false,
  "missing_evidence": ["缺少具体证据"],
  "next_agent": "qa-engineer",
  "next_target": "补齐验证证据",
  "reason": "当前产物缺少验证"
}
```

PM 汇总时优先读取 `latest_evaluation`。如果状态为 `blocked`，先说明缺口和需要用户决策的点；如果系统已自动入队下一轮，按 pending job 调度对应 Agent。

1. 调用 `codex_multi_agents_loop_init_project` 确认项目结构就绪。
2. PM 创建任务时调用 `codex_multi_agents_loop_create` 创建 run；员工 Agent 被直接 @ 且没有 pending job 时，可调用 `codex_multi_agents_loop_create_task` 创建自己的轻量任务。
3. 读取 `next_handoff`。
4. 如果用户已经显式 @ 某个员工，不要重复 spawn，由该 Agent 使用 `invocation_mode="explicit_at"` 领取自己的 pending job。
5. 如果用户没有显式 @ 员工，或说“继续下一步”“你继续”“往下走”，`project-manager` 按 `next_handoff.agent` 或 pending job 调用 `spawn_agent(agent_type="<agent-name>", message=<spawn_message>)`，不要硬编码为 product-manager。
6. 调用 `spawn_agent` 时不要传 `model` 或 `reasoning_effort`，让 Codex 使用项目 TOML 的配置。
7. 员工 Agent 完成后调用 `codex_multi_agents_loop_manager_summary` 汇总给用户。

后续待办 Agent 的通用调度格式是 `spawn_agent(agent_type="<agent-name>", message=<任务包>)`；`<agent-name>` 必须替换成当前 pending job 对应的项目 Agent 名称。

## 定向 Agent 任务

当任务只要求一个或少数 Agent 完成时，不进入完整 PRD -> 架构 -> UI -> 研发 -> QA 流程。典型目标按以下规则分配：

- `PRODUCT.md`：`product-manager`
- `AGENTS.md`：`software-architect`
- `DESIGN.md`：`ui-designer`
- Bug 修复、代码改动：`development-engineer`
- 测试、验证、风险分级：`qa-engineer`

所有 route plan 中的 pending/running job 清零后，Loop 直接标记为 completed；不要继续要求确认产品交接说明，也不要自动进入未声明的 Agent。

产品交接说明 V1 完成后必须暂停，等待用户选择：

- 用户确认：调用 `codex_multi_agents_loop_confirm_prd`，进入架构/UI/研发/QA 链路。
- 用户要求多 Agent 评审：调用 `codex_multi_agents_loop_request_prd_review`。
- 用户要求产品经理补充：让 `product-manager` 按意见处理，并继续回填。

## 后续交付链路

用户确认产品交接说明后，顺序推进：

```text
software-architect
-> ui-designer
-> development-engineer
-> qa-engineer
```

每个 Agent 无论通过显式 @ 还是 project-manager spawn，都必须先领取自己的 pending job，完成后回填结果。`qa-engineer` 完成后，`project-manager` 汇总最终产物、测试结论、风险和后续建议。

## 多 Agent 评审循环

用户说“多角色评审一下”“多 Agent 评审一下”“输出 V2”时：

1. 调用 `codex_multi_agents_loop_request_prd_review`。
2. 工具会入队 `software-architect`、`ui-designer`、`development-engineer`、`qa-engineer` 共同评审最新产品交接说明。
3. `project-manager` 为 pending job 调用 `codex_multi_agents_loop_prepare_handoff`，按并发上限 spawn 对应自定义 Agent。
4. 每个评审 Agent 只输出评审意见，不直接改产品交接说明。
5. 最后一份评审意见回填后，Loop 自动入队 `product-manager` 整合下一版产品交接说明。
6. 如果用户仍不满意，可以继续重复评审循环，输出 V3/V4。

## 全局记忆策略

项目 Agent 是唯一执行体；全局 Agent 记忆只是跨项目经验库，不直接接任务。

| 意图 | MCP 工具 |
| --- | --- |
| 读取少量全局经验 | `codex_multi_agents_loop_memory_pack` |
| 拉取经验到当前项目记忆 | `codex_multi_agents_loop_memory_pull` |
| 项目结束后同步经验到全局 | `codex_multi_agents_loop_memory_sync` |

规则：

- 当前用户指令 > 当前项目文档 > 项目记忆 > 全局记忆 > 插件默认规则。
- 全局记忆只保存摘要型 memory cards，不保存完整 PRD、完整代码或完整 QA 报告。
- 每次任务包最多注入少量相关 cards，避免 token 爆炸。
- 如果全局记忆和当前项目证据冲突，当前项目证据优先。

## 员工被直接 @ 时

员工 Agent 被用户直接点名时必须：

1. 先读取 `codex_multi_agents_loop_manager_summary`；需要完整明细时再读取 `codex_multi_agents_loop_status` 或具体产物。
2. 读取自己的 `.codex/multi-agents-loop/memory/<agent>.md`。
3. 如果存在属于自己的 pending job，调用 `codex_multi_agents_loop_dispatch_next`，传 `agent=<自己的名字>`、`invocation_mode="explicit_at"`。
4. 完成后调用 `codex_multi_agents_loop_complete_agent_step` 回填。
5. 如果没有 pending job，不要私自改状态；说明当前账本状态，并建议由 `@project-manager` 创建任务或准备交接。

## 工具表

| 意图 | MCP 工具 |
| --- | --- |
| 初始化项目 | `codex_multi_agents_loop_init_project` |
| 创建大任务 | `codex_multi_agents_loop_create` |
| 创建员工直接任务 | `codex_multi_agents_loop_create_task` |
| 查询状态 | `codex_multi_agents_loop_status` |
| 准备 Agent 任务 | `codex_multi_agents_loop_prepare_handoff` |
| 领取待办 | `codex_multi_agents_loop_dispatch_next` |
| 回填产物 | `codex_multi_agents_loop_complete_agent_step` |
| PM 汇总 | `codex_multi_agents_loop_manager_summary` |
| 确认产品交接说明 | `codex_multi_agents_loop_confirm_prd` |
| 发起多 Agent 评审 | `codex_multi_agents_loop_request_prd_review` |
| 全局记忆打包 | `codex_multi_agents_loop_memory_pack` |
| 全局记忆拉取 | `codex_multi_agents_loop_memory_pull` |
| 全局记忆同步 | `codex_multi_agents_loop_memory_sync` |
| 列出产物 | `codex_multi_agents_loop_list_artifacts` |
| 读取产物 | `codex_multi_agents_loop_read_artifact` |
| 查看定义 | `codex_multi_agents_loop_inspect` |

## 输出要求

`project-manager` 每次回复用户时，必须用中文说明：

- 当前 run 状态。
- 当前产品交接版本和评审轮次。
- 已完成产物和路径。
- 正在运行或待调度的 Agent。
- 阻塞点。
- 需要用户确认的下一步。

不要输出空泛流程说明。用户问状态时，先读账本，再总结。
