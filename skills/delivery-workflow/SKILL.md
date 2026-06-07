---
name: delivery-workflow
description: 使用通用软件交付 Workflow Worker 编排需求、PRD、评审、设计、开发、测试、上线和最终报告。适用于用户要新建交付项目、列出所有项目、查询项目进度、删除项目、启动/推进/调试 delivery workflow 时。
---

# Delivery Workflow

使用本插件时，始终遵守：

- Agent 只负责理解、生成、评审和总结；不要让 Agent 直接改 workflow state。
- 通过 `delivery_*` MCP tools 或 `python3 -m delivery_workflow.cli ...` 推进流程。
- 所有人工输入和审批必须提交结构化 Gate 数据。
- PRD 审批 Gate 只能由飞书卡片回调或用户明确给出的人工 CLI 兜底操作提交；Agent 不得自己在终端发起“通过 / 拒绝”选择，也不得调用 MCP `delivery_submit_gate` 替人审批。
- 产物必须写成 artifact；下一步只读取 workflow 定义中声明的 artifact。
- Agent 角色说明唯一来源是 `delivery_workflow/references/00-agent-registry.md`；Codex、Claude Code、OpenCode 都引用这一份，不要在平台专属 agents 文件里复制角色定义。
- 默认项目目录就是当前工作区；资料写入 `delivery-project/`，源码写入 `source-code/`，状态库写入 `.delivery-workflow/delivery.db`，日志写入 `.delivery-workflow/logs/`。
- 优先读取当前项目根目录的 `workflow.config.json`；插件目录的 `delivery-workflow.config.json` 只作为默认模板。创建项目时会自动复制默认配置，用户可在项目内覆盖。
- UI 设计任务产出 DESIGN.md 风格设计规范；开发任务按配置中的 `code_platforms.frontend/backend` 自动检测本机可用 CLI（检测优先级：`claude → codex → opencode`），也可显式配置锁定平台。Claude Code 的 headless 命令是 `claude -p`。
- PRD v2、UI 设计规范、前端技术方案、服务端设计方案、测试用例、测试报告和最终报告需要转为飞书文档；先跑 `delivery_doctor` 或 CLI `doctor` 检测 lark-cli docs 能力。
- 如果 `workflow.config.json` 配置了 `lark.chat_id`，创建项目后会以机器人身份发送步骤通知，并在 PRD v2 后发送带“通过 / 拒绝”按钮的 interactive 审批卡片。
- 发送 PRD 审批卡片前，workflow 会按 `lark.event.auto_start_consumer=true` 自动启动 SDK 长连接消费者；进入 `prd-approval` 后必须等待飞书按钮回调，不要改用终端交互。
- 创建项目自动推进到 `prd-approval` 后，不要直接结束。MCP `delivery_create_project` 会在真实发出飞书审批卡片后自动 watch 到后续 worker 稳定状态；如果因为排障或非 MCP 路径没有自动 watch，必须立即调用 `delivery_watch_run`，不要询问用户是否要等待。
- 如果未检测到 `lark-cli`，必须按 larksuite/cli README 的 AI Agent 快速开始：先征得用户批准，再执行 `npx @larksuite/cli@latest install`；不要改用全局 `npm install -g` fallback。

## 常用动作

触发条件：

- 用户说“新建项目 / 创建交付项目 / 开始一个需求 / 启动 workflow”：调用 `delivery_create_project`。
- 用户说“列出所有项目 / 项目列表 / 最近项目”：调用 `delivery_list_projects`。
- 用户说“查询项目进度 / 查看某项目状态 / 这个项目到哪一步了”：优先调用 `delivery_get_project_status`。
- 用户说“删除项目 / 清理项目”：必须先拿到准确 `project_id`，再调用 `delivery_delete_project`，且 `confirm_project_id` 必须与 `project_id` 完全一致。
- 用户说“跑一下 worker / 继续推进”：调用 `delivery_worker_once`。
- 用户说“修复 bug / 修复问题 / 处理报错 / 解决缺陷”：使用 CLI `project bug-fix --issue ...` 或 MCP 对应工具触发 `bug-fix`，不要直接绕过 workflow 修改代码。
- 用户说“飞书审批按钮 / 处理审批按钮”：先查看 workflow 返回的 `listener` 状态；如监听未启动再排查 `python3 -m delivery_workflow.cli lark event-consumer`。
- 用户说“等待飞书审批 / 实时等按钮 / 点完按钮继续”：调用 `delivery_watch_run`，不要轮询式反复查询项目状态。
- 用户说“飞书群里 @机器人新建项目”：确认飞书后台已订阅 `im.message.receive_v1`，并让 `event-consumer` 在业务项目目录运行。
- 用户说“查看 / 初始化 delivery workflow 配置”：使用 CLI `config show` / `config init`。

创建项目：

```bash
python3 -m delivery_workflow.cli project create \
  --title "项目标题" \
  --requirement "需求正文" \
  --business-goal "业务目标"
```

创建项目默认会按项目 `workflow.config.json` 自动启动 worker，推进到 `prd-approval`：PRD v1、PRD v1 多 Agent 评审、PRD v2、飞书 PRD 文档、飞书审批卡片。

如果创建项目结果显示当前停在 `prd-approval`，下一步是调用：

```bash
python3 -m delivery_workflow.cli workflow watch --run-id <run_id>
```

在 MCP 中，`delivery_create_project` 已内置自动 watch：真实发出飞书审批卡片并卡在审批 Gate 时，会等待飞书按钮事件，并继续等到后续 worker 没有 pending/running job 或到达下一个稳定 Gate、空闲或失败。只有在排障或非创建项目路径中才单独使用 `delivery_watch_run`；不要看到队列里有 pending step 就询问用户是否继续。

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

PRD 审批：

正常路径是等待飞书审批卡片按钮回调。不要让 Agent 在终端询问通过/拒绝，也不要用 MCP 工具提交 `prd-approval`。下面的 CLI 只作为明确人工兜底操作示例：

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

## 平台检测规则

平台自动检测按以下优先级：

1. 配置显式指定（`default` / `frontend` / `backend` / `other` 设为具体值如 `claude-code` / `codex` / `opencode`）
2. `auto_detect: true` 时运行时自动检测本机可用 CLI：`claude → codex → opencode`
3. 默认回退为只生成任务包，不真实调用外部 CLI

Claude Code 的 headless 命令是 `claude -p`，不要调用 `claude-code` 二进制。自动化 worker 默认附加 `--permission-mode acceptEdits`；如果执行输出仍提示等待写入/权限审批，必须视为 step 阻断或失败，不能把 stdout 当成已完成产物。

只有在 `code_platforms.enable_agent_cli=true` 且本机存在对应 CLI 时，worker 才会真实调用外部 Agent CLI；否则只生成确定性的任务包与待执行说明。`enable_agent_cli=false` 时，Claude / Codex 不得自行实现前后端代码，只能报告任务包、产物路径和当前 Gate 状态；等待人工审批时使用 `delivery_watch_run` 保持前台交互。

## 配置文件

项目目录放置 `workflow.config.json`，插件目录的 `delivery-workflow.config.json` 是默认模板。创建项目时自动复制模板；状态库、日志、资料和源码都写入当前项目目录。

敏感配置（飞书 app_id、app_secret、chat_id）不要写在 `workflow.config.json` 中。可在插件目录或项目目录创建 `.env` 文件（已加入 `.gitignore`）：

```bash
LARK_APP_ID=cli_xxxxxxxxxxxxxxxx
LARK_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LARK_CHAT_ID=oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

也可以直接设置同名环境变量。`.env` 和 `os.environ` 的值会覆盖 配置文件中的对应字段。

```json
{
    "storage": {
        "home": ".delivery-workflow",
        "db": ".delivery-workflow/delivery.db",
        "artifact_root": "delivery-project",
        "source_root": "source-code",
        "logs": ".delivery-workflow/logs"
    },
    "quality_gate": {
        "block": 0,
        "critical": 0,
        "major": 2,
        "minor": 5
    },
    "workflow": {
        "auto_start": true,
        "auto_run_to_gate": true,
        "continue_after_gate": true,
        "continue_after_gate_max_jobs": 50,
        "mcp_auto_watch_after_create": true,
        "watch_timeout_seconds": 7200,
        "watch_poll_interval_seconds": 2.0
    },
    "code_platforms": {
        "auto_detect": true,
        "default": "auto",
        "frontend": "auto",
        "backend": "auto",
        "other": "auto",
        "enable_agent_cli": false,
        "executors": {
            "codex": {
                "binary_candidates": ["codex"],
                "command": ["{binary}", "exec", "--file", "{prompt_path}"]
            },
            "claude-code": {
                "binary_candidates": ["claude"],
                "command": ["{binary}", "--permission-mode", "acceptEdits", "-p"],
                "stdin_from_prompt": true
            },
            "opencode": {
                "binary_candidates": ["opencode"],
                "command": ["{binary}", "run", "--file", "{prompt_path}"]
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
            "transport": "sdk_websocket",
            "auto_start_consumer": true
        },
        "bot_commands": {
            "enabled": true
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
- 自动监听：`lark.event.auto_start_consumer=true` 时，发送 PRD 审批卡片前会在当前业务工作区后台启动消费者，PID 和日志写入 `.delivery-workflow/`。PID 文件包含插件代码指纹；如果插件升级后旧消费者仍在运行，自动启动检查必须重启旧消费者，确保回调处理逻辑和当前 workflow 代码一致。
- 自动续跑：`workflow.continue_after_gate=true` 时，飞书回调提交 Gate 后会启动后台 worker 继续推进；前台 AI 要实时响应时必须同时调用 `delivery_watch_run` 等 worker 稳定后再返回。
- PRD 被拒绝后会回到 `review-summary` 重新生成 PRD v2，并把拒绝理由作为 `prd-approval_gate` 输入；随后会重新打开同一个 `prd-approval` Gate 并发送下一轮审批卡片。此时仍显示 `prd-approval` 是正常的下一轮审批等待，不代表拒绝没有生效；要查看最新 `prd_v2` / `prd_approval_card_message` artifact 版本和事件流。
- 每一轮 PRD v2 审批卡片必须使用短幂等 key，例如 `pa-94de4a69-p2-d6`；旧卡片用文档链接识别，旧文档链接不得提交到新的审批轮次。
- 二轮及后续复审必须先发飞书提示：`PRD v2 已按照拒绝原因：<原因> 修改，准备发起第 <N> 轮复审。`，审批卡片标题使用 `<项目名> PRD 第 <N> 轮复审`。
- 审批卡片包含拒绝理由输入框。拒绝时必须填写理由；提交后回调同步返回替换卡片，隐藏“通过 / 拒绝”按钮，并显示“该审批已通过 / 已拒绝”和理由。
- 群聊 @机器人新建项目依赖 `im.message.receive_v1`。消息包含“新建项目 / 创建项目 / 开始项目”才会触发，产物写入消费者当前项目目录；不要从插件源码目录启动消费者创建业务项目。
- 飞书全链路验收：使用 `scripts/lark-e2e-smoke --live --actions approve,reject`，脚本会创建测试文档和两张审批卡片，等待用户分别点击“通过 / 拒绝”，并校验长连接事件和 Gate 提交。若预检提示 `Python SDK lark-oapi is not installed`，先安装 `uv pip install --python .venv/bin/python lark-oapi`。
- 如果 lark-cli 报 `keychain Get failed: keychain not initialized`，说明沙箱无法访问 macOS Keychain。不要让用户把 appSecret 写进配置；检查事件里的 `host_escalation.command`，由 Agent 请求沙箱外执行该命令，并把 prefix rule 限定为 `<插件目录>/scripts/deliveryflow-host-lark retry-prd-approval`；也可以让常驻原生 worker 负责发送。
