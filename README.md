# Delivery Workflow

通用软件交付 Workflow Worker 与多 Agent 编排插件。它把需求、PRD、评审、设计、开发、测试、缺陷修复和最终交付报告组织成一个可追踪、可恢复、可审计的文件化流程。

<!-- README-I18N:START -->

**简体中文** | [English](./README.en.md)

<!-- README-I18N:END -->

> [!NOTE]
> 当前支持 Codex 和 Claude Code，适合在本地业务项目目录中编排端到端软件交付流程。

## 为什么需要它

AI Agent 很擅长生成内容，但软件交付还需要确定性的状态流转、审批边界、产物归档和质量准出。Delivery Workflow 的目标是让 Agent 负责“思考和生成”，让 Workflow Worker 负责“状态和证据”。

- 文件化流程：`delivery_workflow/workflow.yaml` 是唯一流程定义源。
- 项目级状态：SQLite 保存 run、job、gate、event，文件系统保存 artifact。
- 清晰边界：交互 Gate、自动化步骤、通知步骤分离。
- 真实质量门禁：前后端开发后进入开发自测，QA 系统测试未达标则进入 bug-fix，并循环回归直到满足阈值。
- 飞书审批：PRD v2 可发布为飞书文档，并通过 interactive 卡片完成审批。
- 宿主 hooks：记录文件写入、命令执行和 Stop 事件，为“是否真实执行自测”提供 evidence。

## 工作流概览

```text
需求录入
  -> PRD v1
  -> 多角色需求评审
  -> PRD v2
  -> 飞书 PRD 审批
  -> UI 设计规范
  -> 前端/后端技术方案
  -> 技术方案评审
  -> 开发任务拆解
  -> 前端开发
  -> 后端开发
  -> 开发自测
  -> QA 系统测试
  -> bug-fix <-> QA 回归测试
  -> 最终交付报告
```

默认质量门禁：

| 等级     | 默认阈值 |
| -------- | -------- |
| Block    | 0        |
| Critical | 0        |
| Major    | <= 2     |
| Minor    | <= 5     |

## 项目结构

```text
.
├── .codex-plugin/                  # Codex 插件 manifest 和 MCP 配置
├── .claude-plugin/                 # Claude Code 插件 manifest
├── hooks/                          # Claude/Codex 宿主 hooks
├── delivery_workflow/              # Python core、CLI、MCP server、workflow 定义
├── skills/delivery-workflow/       # Codex skill 入口
├── scripts/                        # 本地命令包装脚本
├── tests/                          # 单元测试
├── delivery-workflow.config.json   # 插件级默认配置模板
└── pyproject.toml
```

在业务项目中，初始化后默认生成：

```text
.delivery-workflow/
  delivery.db
  logs/
delivery-project/
source-code/
workflow.config.json
.env
```

## 快速开始

### 以插件形式安装到 Codex、Claude Code

#### Codex CLI

```bash
# 1. 添加插件市场
codex plugin marketplace add https://github.com/devTech-zhang/multi-agent-delivery-workflow.git --ref main

# 2. 安装/启用插件
codex plugin add delivery-workflow@delivery-workflow-marketplace

# 3. 查询是否安装成功
codex plugin list | grep delivery-workflow

# 4. 更新插件
codex plugin marketplace upgrade delivery-workflow-marketplace
codex plugin add delivery-workflow@delivery-workflow-marketplace
```

#### Claude Code CLI

```bash
# 1. 添加插件市场
claude plugin marketplace add https://github.com/devTech-zhang/multi-agent-delivery-workflow.git#main

# 2. 安装插件，默认 user scope
claude plugin install delivery-workflow@delivery-workflow-marketplace

# 3. 查询
claude plugin marketplace list
claude plugin list

# 4. 更新市场
claude plugin marketplace update delivery-workflow-marketplace

# 5. 更新插件
claude plugin update delivery-workflow@delivery-workflow-marketplace
```

### 初始化并创建项目

进入你的业务项目目录后，可以直接让 AI 调用插件工具完成初始化和创建项目，你可以说：

> "初始化项目"
> 然后
> "新建项目：做一个后台管理系统，具体功能：xxxxxx"

接下来只需要等待 AI Agent 帮你完成整个流程即可。

## MCP 工具

插件暴露的常用 MCP 工具：

| 用户意图              | MCP 工具                                             |
| --------------------- | ---------------------------------------------------- |
| 初始化当前项目配置    | `delivery_init_project_config`                       |
| 新建交付项目          | `delivery_create_project`                            |
| 查询此项目状态        | `delivery_get_current_project_status`                |
| 删除此项目            | `delivery_delete_current_project`                    |
| 推进一个 worker job   | `delivery_worker_once`                               |
| 推进到阻塞/空闲/失败  | `delivery_worker_until_blocked`                      |
| 等待飞书审批回调      | `delivery_watch_run`                                 |
| 触发人工 bug 修复流程 | `delivery_request_bug_fix`                           |
| 查看 artifact         | `delivery_list_artifacts` / `delivery_read_artifact` |

## 配置

项目直接读取当前项目目录的 `workflow.config.json`。`config init` 会把它复制到业务项目目录。

常用配置项：

```json
{
    "quality_gate": {
        "block": 0,
        "critical": 0,
        "major": 2,
        "minor": 5
    },
    "workflow": {
        "auto_start": true,
        "auto_run_to_gate": true,
        "continue_after_gate": true
    },
    "code_platforms": {
        "default": "codex",
        "frontend": "codex",
        "backend": "claude-code",
        "enable_agent_cli": false
    },
    "lark": {
        "dry_run": false,
        "send_step_notifications": true,
        "send_prd_approval_card": true
    }
}
```

配置飞书机器人相关 `.env`：

```bash
LARK_APP_ID=cli_xxxxxxxxxxxxxxxx
LARK_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LARK_CHAT_ID=oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 飞书审批

PRD v2 会被整理成带标题和表格结构的 Markdown，再通过 `lark-cli docs +create --title --markdown` 创建飞书文档，避免文档显示 `Untitled` 或表格渲染成纯文本。

审批卡片包含：

- PRD 文档链接
- “通过”按钮
- “拒绝”按钮
- 拒绝理由输入框

消费者只监听 `card.action.trigger`：

```bash
python3 -m delivery_workflow.cli lark event-consumer
```

收到审批回调后，workflow 会提交 `prd-approval` Gate，并按 `workflow.continue_after_gate=true` 自动启动后台 worker 继续推进。

## 宿主 Hooks

插件提供 Claude Code / Codex 宿主 hooks，用于记录真实执行证据，不自动推进 workflow：

| Hook                            | 作用                                                                 |
| ------------------------------- | -------------------------------------------------------------------- |
| `PreToolUse Bash`               | 拦截明显危险命令和可能泄露 `.env` / secret 的命令                    |
| `PostToolUse Write/Edit/MultiEdit` | 记录 Agent 实际写入的文件                                         |
| `PostToolUse Bash`              | 记录实际执行的命令，并归类为安装、构建、测试、Playwright、API 检查等 |
| `Stop`                          | 记录一轮 Agent 响应结束                                              |

Evidence 写入：

```text
.delivery-workflow/logs/host-hooks.jsonl
.delivery-workflow/logs/workflow.log
```

## 开发与验证

运行测试：

```bash
python3 -m unittest tests.test_core
python3 -m py_compile delivery_workflow/*.py
git diff --check
```

飞书链路 smoke 测试：

```bash
scripts/lark-e2e-smoke --live --actions approve,reject
```

缺少 `lark-cli` 时，按官方 AI Agent 快速开始安装：

```bash
npx @larksuite/cli@latest install
lark-cli config init --new
lark-cli auth login --recommend
lark-cli auth status
```

## 设计原则

- Agent 只生成内容和执行任务，不直接改 workflow state。
- Workflow Worker 负责状态机、Gate、入队、产物归档和质量门禁。
- 每个步骤只读取 `workflow.yaml` 声明的输入 artifact。
- 所有关键产物都写入 `delivery-project/`。
- 前后端源码写入 `source-code/`，前后端分离。
- 真实自测和 QA 结果必须可追踪，不把“任务包已准备”伪装成“已完成”。
