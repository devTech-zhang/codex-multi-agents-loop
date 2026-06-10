# Delivery Workflow

A software delivery Workflow Worker and multi-agent orchestration plugin. It turns requirements, PRDs, reviews, design specs, development, testing, bug fixes, and final delivery reports into a traceable, recoverable, auditable file-based workflow.

<!-- README-I18N:START -->

[简体中文](./README.md) | **English**

<!-- README-I18N:END -->

> [!NOTE]
> Delivery Workflow currently supports Codex and Claude Code, and is designed to run from a local business project workspace.

## Why It Exists

AI agents are good at generating content, but software delivery also needs deterministic state transitions, approval boundaries, artifact storage, and quality gates. Delivery Workflow lets agents handle thinking and generation while the Workflow Worker owns state and evidence.

- File-based workflow: `delivery_workflow/workflow.yaml` is the single source of process truth.
- Project-level state: SQLite stores runs, jobs, gates, and events; the filesystem stores artifacts.
- Clear boundaries: interactive gates, automated work, and notifications are separate step types.
- Real quality gates: frontend/backend development flows into developer self-test, QA testing, bug-fix, and regression loops until thresholds pass.
- Feishu/Lark approval: PRD v2 can be published as a doc and approved through an interactive card.
- Host hooks: file writes, command execution, and Stop events are recorded as evidence for real self-test verification.

## Workflow Overview

```text
Requirement intake
  -> PRD v1
  -> Multi-role requirement review
  -> PRD v2
  -> Feishu/Lark PRD approval
  -> UI design specification
  -> Frontend/backend technical designs
  -> Technical design review
  -> Development task breakdown
  -> Frontend development
  -> Backend development
  -> Developer self-test
  -> QA system testing
  -> bug-fix <-> QA regression testing
  -> Final delivery report
```

Default quality gate:

| Severity | Default threshold |
| --- | --- |
| Block | 0 |
| Critical | 0 |
| Major | <= 2 |
| Minor | <= 5 |

## Repository Layout

```text
.
├── .codex-plugin/                  # Codex plugin manifest and MCP config
├── .claude-plugin/                 # Claude Code plugin manifest
├── hooks/                          # Claude/Codex host hooks
├── delivery_workflow/              # Python core, CLI, MCP server, workflow definition
├── skills/delivery-workflow/       # Codex skill entrypoint
├── scripts/                        # Local command wrappers
├── tests/                          # Unit tests
├── delivery-workflow.config.json   # Default plugin-level config template
└── pyproject.toml
```

In a business project, initialization creates:

```text
.delivery-workflow/
  delivery.db
  logs/
delivery-project/
source-code/
workflow.config.json
.env
```

## Quick Start

### Install As A Codex Or Claude Code Plugin

#### Codex

##### 1. Codex Desktop

1. Open the Codex desktop plugin page and click **Add marketplace**.
2. Set the Git URL to `https://github.com/devTech-zhang/multi-agent-delivery-workflow.git`.
3. Set the branch to `main`.
4. Leave sparse path empty.
5. Install and enable `delivery-workflow` from the plugin list.

##### 2. Codex CLI

```bash
# 1. Add the plugin marketplace
codex plugin marketplace add https://github.com/devTech-zhang/multi-agent-delivery-workflow.git --ref main

# 2. Install/enable the plugin
codex plugin add delivery-workflow@delivery-workflow-marketplace

# 3. Check installation
codex plugin list | grep delivery-workflow

# 4. Update the plugin
codex plugin marketplace upgrade delivery-workflow-marketplace
codex plugin add delivery-workflow@delivery-workflow-marketplace
```

#### Claude Code CLI

##### 1. Non-interactive commands

```bash
# 1. Add the plugin marketplace
claude plugin marketplace add https://github.com/devTech-zhang/multi-agent-delivery-workflow.git#main

# 2. Install the plugin with default user scope
claude plugin install delivery-workflow@delivery-workflow-marketplace

# 3. Check installation
claude plugin marketplace list
claude plugin list

# 4. Update the marketplace
claude plugin marketplace update delivery-workflow-marketplace

# 5. Update the plugin
claude plugin update delivery-workflow@delivery-workflow-marketplace
```

##### 2. Interactive commands

```bash
# 1. Add the plugin marketplace inside a Claude Code session:
/plugin marketplace add https://github.com/devTech-zhang/multi-agent-delivery-workflow.git#main

# 2. Install/enable the plugin
/plugin install delivery-workflow@delivery-workflow-marketplace

# 3. Check installation
/plugin marketplace list
/plugin list

# 4. Reload plugins for the current session
/reload-plugins

# 5. Update the plugin
/plugin marketplace update delivery-workflow-marketplace
/plugin update delivery-workflow@delivery-workflow-marketplace
/reload-plugins
```

### Initialize And Create A Project

From your business project workspace, ask the AI to initialize/create the project with the plugin tools, or use the CLI directly:

```bash
python3 -m delivery_workflow.cli doctor
python3 -m delivery_workflow.cli config init
python3 -m delivery_workflow.cli project create \
  --title "Order Approval" \
  --requirement "Build an order approval admin system"
python3 -m delivery_workflow.cli project status
```

Manually advance worker jobs when needed:

```bash
python3 -m delivery_workflow.cli worker once
python3 -m delivery_workflow.cli worker until-blocked --run-id <run_id>
```

Wait for a Feishu/Lark approval callback:

```bash
python3 -m delivery_workflow.cli workflow watch --run-id <run_id>
```

> [!IMPORTANT]
> The PRD approval gate is expected to be submitted by the Feishu/Lark card callback. Agents should not approve or reject on behalf of the user from the terminal or through MCP.

## MCP Tools

Common MCP tools exposed by the plugin:

| User intent | MCP tool |
| --- | --- |
| Initialize the current project workspace | `delivery_init_project_config` |
| Create a delivery project | `delivery_create_project` |
| Check current project status | `delivery_get_current_project_status` |
| Delete the current project | `delivery_delete_current_project` |
| Run one worker job | `delivery_worker_once` |
| Run until blocked, idle, or failed | `delivery_worker_until_blocked` |
| Wait for Feishu/Lark approval callback | `delivery_watch_run` |
| Trigger a real bug-fix workflow | `delivery_request_bug_fix` |
| Inspect artifacts | `delivery_list_artifacts` / `delivery_read_artifact` |

## Configuration

Runtime config is read from the current workspace's `workflow.config.json`. The plugin-level `delivery-workflow.config.json` is only a template copied during `config init`.

Common config:

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

Keep secrets in `.env` or environment variables, not in `workflow.config.json`:

```bash
LARK_APP_ID=cli_xxxxxxxxxxxxxxxx
LARK_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LARK_CHAT_ID=oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## Feishu/Lark Approval

PRD v2 is converted into XML with a real title and table structure before `lark-cli docs +create` publishes it. This avoids `Untitled` documents and Markdown tables rendered as plain text.

The approval card includes:

- PRD document link
- Approve button
- Reject button
- Rejection reason input

The consumer only listens for `card.action.trigger`:

```bash
python3 -m delivery_workflow.cli lark event-consumer
```

After a callback arrives, the workflow submits the `prd-approval` gate and starts a background worker when `workflow.continue_after_gate=true`.

## Host Hooks

The plugin ships Claude Code / Codex host hooks for execution evidence. Hooks do not automatically advance workflow state.

| Hook | Purpose |
| --- | --- |
| `PreToolUse Bash` | Blocks obviously destructive commands and commands that may expose `.env` / secrets |
| `PostToolUse Write|Edit|MultiEdit` | Records files actually written by the agent |
| `PostToolUse Bash` | Records executed commands and classifies installs, builds, tests, Playwright, API checks, and more |
| `Stop` | Records the end of an agent turn |

Evidence is written to:

```text
.delivery-workflow/logs/host-hooks.jsonl
.delivery-workflow/logs/workflow.log
```

## Development And Verification

Run tests:

```bash
python3 -m unittest tests.test_core
python3 -m py_compile delivery_workflow/*.py
git diff --check
```

Run the Feishu/Lark smoke test:

```bash
scripts/lark-e2e-smoke --live --actions approve,reject
```

Install `lark-cli` through the official AI Agent quick start when missing:

```bash
npx @larksuite/cli@latest install
lark-cli config init --new
lark-cli auth login --recommend
lark-cli auth status
```

## Design Principles

- Agents generate content and execute tasks; they do not mutate workflow state directly.
- The Workflow Worker owns the state machine, gates, queues, artifact storage, and quality gates.
- Each step only reads the input artifacts declared in `workflow.yaml`.
- Important documents are stored under `delivery-project/`.
- Frontend and backend source code live under `source-code/`.
- Real self-test and QA results must be traceable; a prepared task package is not a completed step.
