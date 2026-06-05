# Software Delivery Workflow

通用软件交付 Workflow Worker 与多 Agent 编排插件。

核心原则：

- Agent 只做“大脑”：生成结构化产物、评审意见、任务说明和结果摘要。
- CLI / Workflow 做确定性执行：状态机、Gate 校验、任务入队、产物落盘和流转都在 core 中完成。
- Workflow 是文件化步骤：`delivery_workflow/workflow.yaml` 是流程定义源。
- State 不依赖 Agent 记忆：SQLite 保存 run、job、gate、event，文件系统保存 artifact。
- `interactive` / `automated` / `notification` 三类步骤分离。

## 目录结构

这是单个 Codex 插件项目，不是多插件仓库。

- `.codex-plugin/plugin.json`：Codex 插件 manifest。
- `.mcp.json`：MCP server 配置。
- `skills/delivery-workflow/SKILL.md`：Codex skill 入口。
- `delivery_workflow/`：Python core、CLI、MCP server、workflow 定义和 step references。Python import 不能使用连字符，所以代码包名使用下划线。
- `delivery-workflow.config.json`：插件级配置文件，统一配置飞书、编码平台、长连接事件和 workflow 默认行为；业务项目目录不需要复制此文件。
- `local/`：本地参考方案和说明文档，不打包为插件运行入口。

## 快速使用

```bash
python3 -m delivery_workflow.cli doctor
python3 -m delivery_workflow.cli project create --requirement "实现一个订单审批后台" --title "订单审批"
python3 -m delivery_workflow.cli project list
python3 -m delivery_workflow.cli project status <project_id>
python3 -m delivery_workflow.cli worker once
python3 -m delivery_workflow.cli workflow status --run-id <run_id>
python3 -m delivery_workflow.cli config show
```

默认创建项目后会按 `delivery-workflow.config.json` 的 `workflow.auto_start` 和 `workflow.auto_run_to_gate` 自动启动 worker，直接推进到 `prd-approval`：创建 PRD v1、多 Agent 评审 PRD v1、生成 PRD v2、打开 PRD 审批 Gate。配置了 `lark.chat_id` 时，会以机器人身份发送每个小步骤开始/结束通知，并在 PRD v2 后创建飞书文档和 interactive 审批卡片。

提交 Gate：

```bash
python3 -m delivery_workflow.cli workflow submit-gate \
  --run-id <run_id> \
  --step-id prd-approval \
  --data-json '{"approved":true,"approver":"owner","comment":"通过"}'
```

删除项目需要二次确认，避免误删：

```bash
python3 -m delivery_workflow.cli project delete <project_id> --confirm-project-id <project_id>
```

删除默认会同时删除当前工作区 `delivery-projects/<project_id>` 产物目录；如需保留产物：

```bash
python3 -m delivery_workflow.cli project delete <project_id> \
  --confirm-project-id <project_id> \
  --keep-artifacts
```

Codex/MCP 触发词建议：

- “新建项目 / 创建交付项目 / 开始一个需求”：`delivery_create_project`
- “列出所有项目 / 项目列表”：`delivery_list_projects`
- “查询项目进度 / 查看某项目状态”：`delivery_get_project_status`
- “删除项目 / 清理项目”：`delivery_delete_project`
- “继续推进 / 跑一下 worker”：`delivery_worker_once`
- “飞书审批按钮事件 / 处理审批按钮”：确保 `deliveryflow lark event-consumer` 正在运行，由长连接消费按钮事件。

## 配置文件

所有配置只有一个有效入口：插件目录下的 `delivery-workflow.config.json`。当你在业务项目目录里使用插件时，不需要复制配置文件；插件会读取自身目录的配置，同时把 `.delivery-workflow/` 状态库和 `delivery-projects/` 产物写到业务项目目录。

开发本插件时，如果当前目录本身存在 `delivery-workflow.config.json`，就使用当前目录这份配置；安装后在其他项目里使用时，默认使用插件目录这份配置。

生成配置文件：

```bash
python3 -m delivery_workflow.cli config init
python3 -m delivery_workflow.cli config show
```

常用配置：

```json
{
    "storage": {
        "home": ".delivery-workflow",
        "db": ".delivery-workflow/delivery.db",
        "artifact_root": "delivery-projects"
    },
    "workflow": {
        "auto_start": true,
        "auto_run_to_gate": true
    },
    "code_platforms": {
        "default": "codex",
        "frontend": "codex",
        "backend": "codex",
        "other": "codex",
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
        "send_step_notifications": true,
        "send_prd_approval_card": true,
        "prd_doc_title_template": "{project_title}PRD",
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

字段说明：

- `storage.home`：workflow 本地状态目录。
- `storage.db`：SQLite 数据库路径。
- `storage.artifact_root`：产物根目录，默认当前工作区 `delivery-projects/`。
- `workflow.auto_start`：创建项目后是否自动入队第一个步骤。
- `workflow.auto_run_to_gate`：创建项目后是否自动跑到 PRD 审批 Gate。
- `code_platforms.default`：默认编码平台。
- `code_platforms.frontend`：前端开发步骤编码平台。
- `code_platforms.backend`：后端开发步骤编码平台。
- `code_platforms.other`：其他开发步骤编码平台。
- `code_platforms.enable_agent_cli`：是否真实调用 Codex / Claude Code / OpenCode CLI；为 `false` 时只生成任务包。
- `code_platforms.executors.claude-code`：Claude Code 执行模板。平台名仍写 `claude-code`，真实命令按官方 headless 用法执行 `claude -p`，任务包内容从 stdin 传入。
- `lark.enabled`：是否启用飞书动作。
- `lark.chat_id`：飞书群聊 ID，形如 `oc_xxx`。
- `lark.dry_run`：只记录 lark-cli 命令，不真实发消息或创建文档。
- `lark.send_step_notifications`：是否发送每个小步骤开始/结束通知。
- `lark.send_prd_approval_card`：是否发送 PRD 审批卡片。
- `lark.prd_doc_title_template`：PRD v2 飞书文档标题模板。
- `lark.sender.on_keychain_error`：当 Codex 沙箱无法访问 macOS Keychain 时的处理方式；默认返回 `host_escalation` 元信息，让宿主请求沙箱外执行飞书发送动作。
- `lark.sdk.credential_source`：卡片事件消费者的飞书应用凭据来源，默认读取 lark-cli 已初始化的应用配置。
- `lark.sdk.profile`：可选 lark-cli 应用 profile / app_id；为空时使用 lark-cli 配置中的第一个应用。
- `lark.sdk.app_id` / `lark.sdk.app_secret`：SDK 长连接显式凭据兜底项；优先使用 lark-cli 凭据，不建议把 secret 写入仓库。
- `lark.sdk.log_level`：官方 Python SDK 日志级别，支持 `debug` / `info` / `warn` / `error`。
- `lark.event.transport`：卡片按钮事件接收方式，固定为 `sdk_websocket`。插件不再支持 HTTP 回调服务，也不使用 `lark-cli event consume`。

`code_platforms.default/frontend/backend/other` 支持 `codex`、`claude-code`、`opencode`。

## 平台适配

- `code_platforms.default=codex`：默认生成 Codex CLI 执行包。
- `code_platforms.frontend=claude-code`：前端开发任务使用 Claude Code CLI，实际 headless 命令为 `claude -p`。
- `code_platforms.backend=opencode`：后端开发任务使用 OpenCode CLI。
- `platform=openclaw` 仍兼容旧入口；如未配置具体编码平台，OpenClaw 类型开发任务按 Claude Code 执行。
- UI 工作流默认声明 Figma MCP 能力需求，并生成 Figma 设计任务包。

默认不会自动调用外部 Agent CLI。只有 `code_platforms.enable_agent_cli=true` 时，worker 才会在检测到对应 CLI 后执行命令；否则只生成确定性的任务包与待执行说明。

Claude Code 非交互式执行遵循官方 CLI 方式：`claude -p` / `claude --print`。

## 产物规则

默认情况下，workflow 在当前工作区输出：

```text
delivery-projects/
  <project_id>/
    <agent_or_executor>/
      <artifact_category>/
        v1/
          <artifact-name>.md
```

项目 ID 会尽量带上标题 slug，例如 `proj_20260604_abcd-order-approval`。如果标题无法转成 ASCII slug，则使用 `project` 兜底。

例如：

```text
delivery-projects/
  proj_20260604_abcd-order-approval/
    workflow/raw/v1/raw-requirement.md
    workflow/context/v1/project-context.json
    product-manager/prd/v1/prd-v1.md
    product-manager/prd/v1/prd-v2.md
    ui-designer/ui/v1/ui-design-spec.md
    codex-adapter/dev-result/v1/frontend-dev-result.json
```

SQLite 状态和产物路径由 `delivery-workflow.config.json` 的 `storage` 配置决定，默认写在当前工作区：

```text
.delivery-workflow/delivery.db
delivery-projects/
```

## 飞书文档与审批卡片

PRD v2 和最终报告发布步骤依赖 `lark-cli docs` 能力。卡片按钮事件接收依赖飞书官方 Python SDK `lark-oapi`。`doctor` 和 smoke 预检会检测：

- `lark-cli` 是否在 PATH 中；
- `lark-cli docs --help` 是否可用；
- 本机是否存在 `lark-doc` skill 提示文件。
- Python 是否能导入 `lark_oapi`；
- 是否能从 `delivery-workflow.config.json` 或 lark-cli 应用配置中取得 SDK 长连接所需的 `app_id` / `app_secret`。

PRD v2 文档标题为 `<中文项目名>PRD`，例如 `订单审批PRD`。如果配置了飞书群聊，workflow 会用：

```bash
lark-cli docs +create --as bot --api-version v2 --doc-format markdown --content ...
lark-cli im +messages-send --as bot --chat-id oc_xxx --msg-type interactive --content ...
```

interactive 卡片包含“查看 PRD 文档”“通过”“拒绝”按钮。按钮点击会产生 `card.action.trigger`，本插件只通过官方 SDK 长连接接收该事件：

启动长连接消费者：

```bash
python3 -m delivery_workflow.cli lark event-consumer
```

这个进程使用 `lark_oapi.ws.Client(app_id, app_secret, event_handler=...)` 建立 WebSocket 长连接，并通过 `register_p2_card_action_trigger` 注册卡片回传交互处理器。收到按钮事件后，消费者把官方 SDK 事件对象序列化为 `card.action.trigger` payload，再提交 `prd-approval` Gate。

飞书后台需要在同一个自建应用里开启机器人能力，回调订阅方式选择“使用长连接接收回调”，并订阅卡片回传交互 `card.action.trigger`。如果点击“通过 / 拒绝”弹出“完成配置”，点“去配置”又提示“该应用不存在”，说明发卡片的应用和你当前能配置的开发者应用不是同一个，或当前账号无权管理这个应用；处理方式是确认 `lark-cli config show` 里的 `appId` 与开放平台后台应用一致，并在这个应用中启用长连接和发布版本。

### 飞书链路验收脚本

不想让 AI 手工试链路时，直接运行：

```bash
scripts/lark-e2e-smoke --live --actions approve,reject
```

脚本会真实执行完整链路：预检 lark-cli/docs 能力、预检 `lark-oapi` SDK、启动 SDK 长连接、创建测试 PRD 飞书文档、发送审批卡片、等待你点击“通过”和“拒绝”、接收 `card.action.trigger` 事件、提交 `prd-approval` Gate 并校验结果。

默认不加 `--live` 时只做预检，不会发飞书消息。测试结束后如需自动删除本地 workflow 测试项目和产物，可加 `--cleanup`。

如果预检输出：

```text
Python SDK lark-oapi is not installed
```

先安装官方 Python SDK 到插件自己的虚拟环境：

```bash
uv venv .venv
uv pip install --python .venv/bin/python lark-oapi
```

如果预检输出 `cannot find lark-cli app config` 或 `missing readable appSecret`，先运行 `lark-cli config init --new` 完成自建应用配置；如果 lark-cli 把 secret 存在系统 Keychain 而当前 Python 进程读不到，可以在 `delivery-workflow.config.json` 的 `lark.sdk.app_id` / `lark.sdk.app_secret` 中显式配置兜底，但不建议把包含 secret 的配置提交到仓库。

### macOS Keychain 沙箱问题

Codex 沙箱内运行 `lark-cli` 时，可能出现：

```text
keychain Get failed: keychain not initialized
```

这表示当前沙箱进程访问不到 macOS Keychain 中的 lark-cli 凭证。不要把 appSecret 或 token 写入仓库配置文件，也不要把 workflow 改成“用户手工补脚本”。

处理策略是自动化的：

- 在 Codex 宿主可请求提权时，workflow 事件会带 `host_escalation.command`，由 Codex 请求沙箱外执行同一飞书动作。
- 第一次授权时选择持久允许这个窄前缀即可一劳永逸：`<插件目录>/scripts/deliveryflow-host-lark retry-prd-approval`。后续不同项目只会变化 `--workspace` 和 `--run-id`，不需要反复发送“我确认允许...”。
- 在长期运行场景，建议把 `worker start` / `lark event-consumer` 作为本机原生常驻进程启动；它们读取同一个 `delivery-workflow.config.json`，其中 SDK 消费器通过 lark-cli 应用配置或显式 SDK 凭据建立长连接。

缺少能力时，飞书动作会在事件里记录 skipped/failed，并提示安装/认证建议。
按 larksuite/cli `README.zh.md` 的 AI Agent 快速开始，不需要改走全局 `npm install -g` fallback；第 1 步就是由用户明确授权后执行：

```bash
npx @larksuite/cli@latest install
```

后续步骤是：

```bash
lark-cli config init --new
lark-cli auth login --recommend
lark-cli auth status
```

CLI 也提供显式入口执行第 1 步：

```bash
python3 -m delivery_workflow.cli doctor --install-lark-cli
```
