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
- `delivery_workflow/references/00-agent-registry.md`：Codex、Claude Code、OpenCode 共用的 Agent 角色唯一说明源；`.claude/agents/` 只保留薄入口，不复制角色定义。
- `delivery_workflow/`：Python core、CLI、MCP server、workflow 定义和 step references。Python import 不能使用连字符，所以代码包名使用下划线。
- `delivery-workflow.config.json`：插件级默认配置模板；创建项目时会复制为项目根目录的 `workflow.config.json`。
- `local/`：本地参考方案和说明文档，不打包为插件运行入口。

## 快速使用

```bash
python3 -m delivery_workflow.cli doctor
python3 -m delivery_workflow.cli project create --requirement "实现一个订单审批后台" --title "订单审批"
python3 -m delivery_workflow.cli project status
python3 -m delivery_workflow.cli worker once
python3 -m delivery_workflow.cli workflow status --run-id <run_id>
python3 -m delivery_workflow.cli config show
```

默认创建项目后会按项目根目录 `workflow.config.json` 的 `workflow.auto_start` 和 `workflow.auto_run_to_gate` 自动启动 worker，直接推进到 `prd-approval`：创建 PRD v1、多 Agent 评审 PRD v1、生成 PRD v2、打开 PRD 审批 Gate。配置了 `lark.chat_id` 时，会以机器人身份发送每个小步骤开始/结束通知，并在 PRD v2 后创建飞书文档和 interactive 审批卡片。

在 MCP 中，`delivery_create_project` 如果已经真实发出飞书审批卡片并卡在审批 Gate，会自动进入 watch，直到飞书按钮回调和后续 worker 跑到稳定状态。不要让 Agent 在 `prd-approval` 处结束本轮对话，也不要让它询问“是否要等待/是否要继续”。

CLI 排障或手工运行时，可以显式调用前台 watch：

```bash
python3 -m delivery_workflow.cli workflow watch --run-id <run_id>
```

这个命令会阻塞等待飞书按钮回调、Gate 提交或当前 step 变化；事件到来后不会立刻返回，而是继续等到 workflow 没有 pending/running job，或已经到达下一个稳定 Gate/空闲/失败状态。飞书回调自身会按 `workflow.continue_after_gate=true` 自动启动后台 worker，继续推进到下一个 Gate、空闲或失败。

提交 Gate：

PRD 审批 Gate 默认只接受飞书卡片按钮回调。Agent 不应在终端里替人选择“通过 / 拒绝”，也不应通过 MCP 调用提交审批结果；只有明确的人类运维操作才可以用 CLI 兜底提交。

```bash
python3 -m delivery_workflow.cli workflow submit-gate \
  --run-id <run_id> \
  --step-id prd-approval \
  --data-json '{"approved":true,"approver":"owner","comment":"通过"}'
```

删除此项目会彻底清理当前项目的 workflow 状态库、日志、资料目录、源码目录和项目级配置。删除前默认会在当前目录生成 zip 备份：

```bash
python3 -m delivery_workflow.cli project delete
```

确认不需要备份时，可以显式跳过备份：

```bash
python3 -m delivery_workflow.cli project delete --no-backup
```

Codex/MCP 触发词建议：

- “新建项目 / 创建交付项目 / 开始一个需求”：`delivery_create_project`
- “查询此项目状态 / 这个项目到哪一步了”：`delivery_get_current_project_status`
- “删除此项目 / 清理当前项目”：`delivery_delete_current_project`
- “继续推进 / 跑一下 worker”：`delivery_worker_once`
- “飞书审批按钮事件 / 处理审批按钮”：创建 PRD 审批卡片时会按 `lark.event.auto_start_consumer` 自动启动长连接消费者；如需排障，再检查 `deliveryflow lark event-consumer` 是否仍在运行。
- “等待我在飞书审批 / 实时等按钮回调”：`delivery_watch_run`

## 配置文件

项目运行配置优先读取当前项目根目录的 `workflow.config.json`。插件目录的 `delivery-workflow.config.json` 只作为默认模板；创建项目时会自动复制一份到项目目录，之后用户可以在项目内覆盖。状态库写入 `.delivery-workflow/delivery.db`，日志写入 `.delivery-workflow/logs/`，资料写入 `delivery-project/`，源码写入 `source-code/`。

开发本插件时，根目录的 `delivery-workflow.config.json` 是默认模板；业务项目目录的 `workflow.config.json` 始终优先。

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
        "default": "codex",
        "frontend": "codex",
        "backend": "codex",
        "other": "codex",
        "enable_agent_cli": false,
        "executors": {
            "claude-code": {
                "binary_candidates": ["claude"],
                "command": ["{binary}", "--permission-mode", "acceptEdits", "-p"],
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
            "transport": "sdk_websocket",
            "auto_start_consumer": true
        },
        "bot_commands": {
            "enabled": true
        }
    }
}
```

字段说明：

- `storage.home`：workflow 本地状态目录。
- `storage.db`：SQLite 数据库路径。
- `storage.artifact_root`：资料产物目录，默认当前项目 `delivery-project/`。
- `storage.source_root`：前后端源码目录，默认当前项目 `source-code/`。
- `storage.logs`：运行日志目录，默认当前项目 `.delivery-workflow/logs/`。
- `quality_gate`：QA 准出标准，默认 Block=0、Critical=0、Major<=2、Minor<=5。
- `workflow.auto_start`：创建项目后是否自动入队第一个步骤。
- `workflow.auto_run_to_gate`：创建项目后是否自动跑到 PRD 审批 Gate。
- `workflow.continue_after_gate`：飞书审批回调提交 Gate 后，是否自动启动后台 worker 继续推进。
- `workflow.continue_after_gate_max_jobs`：回调后后台 worker 单次最多执行的 job 数。
- `workflow.mcp_auto_watch_after_create`：MCP `delivery_create_project` 在真实发出飞书审批卡片后是否自动 watch 到稳定状态，默认 `true`。
- `workflow.watch_timeout_seconds`：`workflow watch` / `delivery_watch_run` 默认等待秒数。默认 7200 秒；超时后前台 AI 可以暂时停止，飞书长连接消费者仍会接收后续按钮事件，用户再询问 AI “继续”即可查询并接续推进。
- `workflow.watch_poll_interval_seconds`：前台 watch 轮询间隔秒数。
- `code_platforms.default`：默认编码平台。
- `code_platforms.frontend`：前端开发步骤编码平台。
- `code_platforms.backend`：后端开发步骤编码平台。
- `code_platforms.other`：其他开发步骤编码平台。
- `code_platforms.enable_agent_cli`：是否真实调用 Codex / Claude Code / OpenCode CLI；为 `false` 时只生成任务包，当前 AI Agent 不得自行接管前后端实现。
- `code_platforms.executors.claude-code`：Claude Code 执行模板。平台名仍写 `claude-code`，真实命令按官方 headless 用法执行 `claude -p`，默认附加 `--permission-mode acceptEdits`，任务包内容从 stdin 传入。
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
- `lark.event.auto_start_consumer`：发送 PRD 审批卡片前是否自动在当前业务工作区启动 SDK 长连接消费者；默认 `true`。消费者 PID 和日志写入当前工作区 `.delivery-workflow/`。
- `lark.bot_commands.enabled`：是否允许群聊/单聊消息触发 workflow 命令。

`code_platforms.default/frontend/backend/other` 支持 `codex`、`claude-code`、`opencode`。

## 平台适配

- `code_platforms.default=codex`：默认生成 Codex CLI 执行包。
- `code_platforms.frontend=claude-code`：前端开发任务使用 Claude Code CLI，实际 headless 命令为 `claude --permission-mode acceptEdits -p`。
- `code_platforms.backend=opencode`：后端开发任务使用 OpenCode CLI。
- `platform=openclaw` 仍兼容旧入口；如未配置具体编码平台，OpenClaw 类型开发任务按 Claude Code 执行。
- UI 工作流产出 DESIGN.md 风格设计规范，前端开发按设计规范实现界面。

默认不会自动调用外部 Agent CLI。只有 `code_platforms.enable_agent_cli=true` 时，worker 才会在检测到对应 CLI 后执行命令；否则只生成确定性的任务包与待执行说明。此模式下 Claude / Codex 只能报告任务包位置和当前 Gate 状态，不能脱离 workflow 去直接实现项目代码。

Claude Code 非交互式执行遵循官方 CLI 方式：`claude -p` / `claude --print`。自动化 worker 默认使用 `--permission-mode acceptEdits`，避免 headless 运行时只在 stdout 里提示“等待写入审批”却没有真实落盘；如果 stdout/stderr 仍出现交互审批提示，workflow 会把该 step 标记失败，不再发布伪产物。

## 产物规则

默认情况下，workflow 在当前工作区输出：

```text
delivery-project/
  <agent_or_executor>/
    <artifact_category>/
      v1/
        <artifact-name>.md
```

项目 ID 仍保存在数据库中用于状态查询，但不再作为资料目录层级。

例如：

```text
delivery-project/
    workflow/raw/v1/raw-requirement.md
    workflow/context/v1/project-context.json
    product-manager/prd/v1/prd-v1.md
    product-manager/prd/v1/prd-v2.md
    ui-designer/ui/v1/ui-design-spec.md
    codex-adapter/dev-result/v1/frontend-dev-result.json
```

SQLite 状态、资料和源码路径由项目 `workflow.config.json` 的 `storage` 配置决定，默认写在当前工作区：

```text
.delivery-workflow/delivery.db
delivery-project/
source-code/
```

## 飞书文档与审批卡片

PRD v2、UI 设计规范、前端技术方案、服务端设计方案、测试用例、测试报告和最终报告发布步骤依赖 `lark-cli docs` 能力。卡片按钮事件接收依赖飞书官方 Python SDK `lark-oapi`。`doctor` 和 smoke 预检会检测：

- `lark-cli` 是否在 PATH 中；
- `lark-cli docs --help` 是否可用；
- 本机是否存在 `lark-doc` skill 提示文件。
- Python 是否能导入 `lark_oapi`；
- 是否能从 `workflow.config.json` 或 lark-cli 应用配置中取得 SDK 长连接所需的 `app_id` / `app_secret`。

PRD v2 文档标题为 `<中文项目名>PRD`，例如 `订单审批PRD`。如果配置了飞书群聊，workflow 会用：

```bash
lark-cli docs +create --as bot --api-version v2 --doc-format markdown --content ...
lark-cli im +messages-send --as bot --chat-id oc_xxx --msg-type interactive --content ...
```

interactive 卡片包含“查看 PRD 文档”、拒绝理由输入框，以及“通过 / 拒绝”提交按钮。拒绝时必须填写理由；通过时理由可为空。按钮点击会产生 `card.action.trigger`，本插件只通过官方 SDK 长连接接收该事件：

通常不需要手动启动长连接消费者：发送 PRD 审批卡片前，workflow 会按 `lark.event.auto_start_consumer=true` 在当前业务工作区后台启动消费者，并把 PID 与日志写到 `.delivery-workflow/lark-event-consumer.pid.json` 和 `.delivery-workflow/lark-event-consumer.log`。PID 文件会记录当前插件代码指纹；如果插件升级后旧消费者仍在运行，下一次自动启动检查会先停止旧消费者，再用新代码启动，避免出现“回调收到了，但没有触发后续 worker”的状态漂移。

每一轮 PRD v2 审批都会使用不同的短卡片幂等 key，例如 `pa-94de4a69-p2-d6`。拒绝后重新生成 PRD v2 时，同一个 `prd-approval` Gate 会被重新打开，旧提交数据会清空；新卡片会引用最新 PRD 文档链接，旧卡片或重复点击不会再提交到新的审批轮次。

二轮及后续复审会先发送提示消息：`PRD v2 已按照拒绝原因：<原因> 修改，准备发起第 <N> 轮复审。` 随后审批卡片标题会显示为 `<项目名> PRD 第 <N> 轮复审`。

手动排障时可以启动长连接消费者：

```bash
python3 -m delivery_workflow.cli lark event-consumer
```

这个进程使用 `lark_oapi.ws.Client(app_id, app_secret, event_handler=...)` 建立 WebSocket 长连接，并通过 `register_p2_card_action_trigger` 注册卡片回传交互处理器。收到按钮事件后，消费者把官方 SDK 事件对象序列化为 `card.action.trigger` payload，再提交 `prd-approval` Gate。

审批提交成功后，回调会同步返回一张替换卡片，原“通过 / 拒绝”按钮会消失，卡片显示“该审批已通过”或“该审批已拒绝”，并展示理由和操作人。这样同一张卡不会被反复点击。

回调提交 Gate 后还会自动启动一次后台 worker：

```bash
python3 -m delivery_workflow.cli worker until-blocked --run-id <run_id> --max-jobs <continue_after_gate_max_jobs>
```

后台 worker 的 PID 和日志写入 `.delivery-workflow/worker-<run_id>.pid.json` 与 `.delivery-workflow/worker-<run_id>.log`。这解决“点了飞书按钮但项目不继续推进”的问题；前台 `workflow watch` 会等后台 worker 跑到稳定状态再返回，解决“AI 终端没有实时反馈”和“AI 看到 pending job 又问要不要继续”的问题。

飞书后台需要在同一个自建应用里开启机器人能力，回调订阅方式选择“使用长连接接收回调”，并订阅卡片回传交互 `card.action.trigger`。如果点击“通过 / 拒绝”弹出“完成配置”，点“去配置”又提示“该应用不存在”，说明发卡片的应用和你当前能配置的开发者应用不是同一个，或当前账号无权管理这个应用；处理方式是确认 `lark-cli config show` 里的 `appId` 与开放平台后台应用一致，并在这个应用中启用长连接和发布版本。

### 群聊 @机器人创建项目

同一个长连接消费者也会注册 `im.message.receive_v1`。飞书后台需要在事件配置里订阅“接收消息”事件，并确保机器人具备接收和发送群消息的权限。

在业务项目目录启动消费者：

```bash
./scripts/deliveryflow lark event-consumer
```

然后在配置的群里发送类似：

```text
@codex 飞书机器人 新建项目：TODO H5 应用，支持新增、完成和删除待办。
```

消费者会在当前业务项目目录创建 `.delivery-workflow/`、`delivery-project/` 和 `source-code/`，并自动推进到 PRD 审批节点。不要从插件源码目录启动消费者创建业务项目；源码目录运行时会拒绝创建并提示到项目目录启动。

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

如果预检输出 `cannot find lark-cli app config` 或 `missing readable appSecret`，先运行 `lark-cli config init --new` 完成自建应用配置；如果 lark-cli 把 secret 存在系统 Keychain 而当前 Python 进程读不到，可以在 `workflow.config.json` 的 `lark.sdk.app_id` / `lark.sdk.app_secret` 中显式配置兜底，但不建议把包含 secret 的配置提交到仓库。

### macOS Keychain 沙箱问题

Codex 沙箱内运行 `lark-cli` 时，可能出现：

```text
keychain Get failed: keychain not initialized
```

这表示当前沙箱进程访问不到 macOS Keychain 中的 lark-cli 凭证。不要把 appSecret 或 token 写入仓库配置文件，也不要把 workflow 改成“用户手工补脚本”。

处理策略是自动化的：

- 在 Codex 宿主可请求提权时，workflow 事件会带 `host_escalation.command`，由 Codex 请求沙箱外执行同一飞书动作。
- 第一次授权时选择持久允许这个窄前缀即可一劳永逸：`<插件目录>/scripts/deliveryflow-host-lark retry-prd-approval`。后续不同项目只会变化 `--workspace` 和 `--run-id`，不需要反复发送“我确认允许...”。
- 在长期运行场景，建议把 `worker start` / `lark event-consumer` 作为本机原生常驻进程启动；它们读取同一个 `workflow.config.json`，其中 SDK 消费器通过 lark-cli 应用配置或显式 SDK 凭据建立长连接。审批卡片会携带创建项目时的业务工作区路径，所以卡片事件可以从任意目录启动的消费者回到正确项目目录提交 Gate；群聊新建项目则以消费者当前目录为准。

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
