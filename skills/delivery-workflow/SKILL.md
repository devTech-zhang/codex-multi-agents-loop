---
name: delivery-workflow
description: 使用通用软件交付 Workflow Worker 编排需求、PRD、评审、设计、开发、测试、上线审批和最终报告。适用于用户要新建交付项目、列出所有项目、查询项目进度、删除项目、启动/推进/调试 delivery workflow 时。
---

# Delivery Workflow

使用本插件时，始终遵守：

- Agent 只负责理解、生成、评审和总结；不要让 Agent 直接改 workflow state。
- 通过 `delivery_*` MCP tools 或 `python3 -m delivery_workflow.cli ...` 推进流程。
- 所有人工输入和审批必须提交结构化 Gate 数据。
- 产物必须写成 artifact；下一步只读取 workflow 定义中声明的 artifact。
- 默认产物目录是当前工作区 `delivery-projects/<project_id>/...`，状态库是当前工作区 `.delivery-workflow/delivery.db`。
- 优先读取插件目录的 `delivery-workflow.config.json`；飞书、前端/后端编码平台、长连接事件都从配置取默认值。业务项目目录不需要复制配置文件或 `scripts/`。
- UI 设计任务默认使用 Figma MCP；开发任务按配置中的 `code_platforms.frontend/backend` 选择 Codex、Claude Code 或 OpenCode。Claude Code 的真实 headless 命令是 `claude -p`，不要调用 `claude-code` 二进制。
- PRD v2 和最终报告需要转为飞书文档；先跑 `delivery_doctor` 或 CLI `doctor` 检测 lark-cli docs 能力。
- 如果 `delivery-workflow.config.json` 配置了 `lark.chat_id`，创建项目后会以机器人身份发送步骤通知，并在 PRD v2 后发送带“通过 / 拒绝”按钮的 interactive 审批卡片。
- 如果未检测到 `lark-cli`，必须按 larksuite/cli README 的 AI Agent 快速开始：先征得用户批准，再执行 `npx @larksuite/cli@latest install`；不要改用全局 `npm install -g` fallback。

## 常用动作

触发条件：

- 用户说“新建项目 / 创建交付项目 / 开始一个需求 / 启动 workflow”：调用 `delivery_create_project`。
- 用户说“列出所有项目 / 项目列表 / 最近项目”：调用 `delivery_list_projects`。
- 用户说“查询项目进度 / 查看某项目状态 / 这个项目到哪一步了”：优先调用 `delivery_get_project_status`。
- 用户说“删除项目 / 清理项目”：必须先拿到准确 `project_id`，再调用 `delivery_delete_project`，且 `confirm_project_id` 必须与 `project_id` 完全一致。
- 用户说“跑一下 worker / 继续推进”：调用 `delivery_worker_once`。
- 用户说“飞书审批按钮 / 处理审批按钮”：优先确认是否已运行长连接 `python3 -m delivery_workflow.cli lark event-consumer`。
- 用户说“查看 / 初始化 delivery workflow 配置”：使用 CLI `config show` / `config init`。

创建项目：

```bash
python3 -m delivery_workflow.cli project create \
  --title "项目标题" \
  --requirement "需求正文" \
  --business-goal "业务目标"
```

创建项目默认会按 `delivery-workflow.config.json` 自动启动 worker，推进到 `prd-approval`：PRD v1、PRD v1 多 Agent 评审、PRD v2、飞书 PRD 文档、飞书审批卡片。

运行一个后台任务：

```bash
python3 -m delivery_workflow.cli worker once
```

查看状态：

```bash
python3 -m delivery_workflow.cli project status <project_id>
python3 -m delivery_workflow.cli workflow status --run-id <run_id>
```

列出项目：

```bash
python3 -m delivery_workflow.cli project list
```

删除项目：

```bash
python3 -m delivery_workflow.cli project delete <project_id> --confirm-project-id <project_id>
```

提交审批：

```bash
python3 -m delivery_workflow.cli workflow submit-gate \
  --run-id <run_id> \
  --step-id prd-approval \
  --data-json '{"approved":true,"approver":"owner","comment":"通过"}'
```

检测并执行 AI Agent 快速开始第 1 步：

```bash
python3 -m delivery_workflow.cli doctor --install-lark-cli
```

随后按命令输出引导用户完成：

```bash
lark-cli config init --new
lark-cli auth login --recommend
lark-cli auth status
```

## 平台规则

- `code_platforms.default=codex`：默认生成 Codex 执行包。
- `code_platforms.frontend=claude-code`：前端开发任务使用 Claude Code，真实执行命令为 `claude -p`。
- `code_platforms.backend=opencode`：后端开发任务使用 OpenCode。
- `openclaw` 项目入口仍兼容旧规则；未单独配置编码平台时按 Claude Code 执行。

默认只生成任务包。只有在 `delivery-workflow.config.json` 设置 `code_platforms.enable_agent_cli=true` 且本机存在对应 CLI 时，worker 才会真实调用外部 Agent CLI。

## 配置文件

插件目录放置唯一配置文件 `delivery-workflow.config.json`。业务项目目录没有配置文件时，会自动读取插件目录这份；状态库和产物仍写入当前业务项目目录。

```json
{
    "code_platforms": {
        "default": "codex",
        "frontend": "codex",
        "backend": "codex",
        "enable_agent_cli": false,
        "executors": {
            "claude-code": {
                "binary_candidates": ["claude"],
                "command": ["{binary}", "-p"],
                "stdin_from_prompt": true
            }
        }
    },
    "lark": {
        "chat_id": "oc_xxx",
        "dry_run": false,
        "sender": {
            "mode": "current_process",
            "on_keychain_error": "host_escalation"
        },
        "sdk": {
            "credential_source": "lark-cli",
            "profile": "",
            "app_id": "",
            "app_secret": "",
            "log_level": "info"
        },
        "event": {
            "transport": "sdk_websocket"
        }
    }
}
```

查看配置：

```bash
python3 -m delivery_workflow.cli config show
```

## 飞书审批卡片

- 发送方式：`lark-cli im +messages-send --as bot --msg-type interactive --content ...`。
- 文档命名：`<项目标题>PRD`，例如 `订单审批PRD`。
- 通知范围：配置 `lark.chat_id` 后，每个步骤开始/结束都会发一条飞书文本通知。
- 事件边界：只使用官方 Python SDK 长连接 `python3 -m delivery_workflow.cli lark event-consumer` 消费 `card.action.trigger`。消费者通过 `lark_oapi.ws.Client` 建立 WebSocket，并用 `register_p2_card_action_trigger` 注册卡片回传交互处理器；不要再使用 `lark-cli event consume`。
- 飞书全链路验收：使用 `scripts/lark-e2e-smoke --live --actions approve,reject`，脚本会创建测试文档和两张审批卡片，等待用户分别点击“通过 / 拒绝”，并校验长连接事件和 Gate 提交。若预检提示 `Python SDK lark-oapi is not installed`，先安装 `uv pip install --python .venv/bin/python lark-oapi`。
- 如果 lark-cli 报 `keychain Get failed: keychain not initialized`，说明沙箱无法访问 macOS Keychain。不要让用户把 appSecret 写进配置；检查事件里的 `host_escalation.command`，由 Agent 请求沙箱外执行该命令，并把 prefix rule 限定为 `<插件目录>/scripts/deliveryflow-host-lark retry-prd-approval`；也可以让常驻原生 worker 负责发送。
