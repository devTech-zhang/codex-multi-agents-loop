# Delivery Workflow

A general-purpose software delivery Workflow Worker and multi-agent orchestration plugin. It organizes requirements, PRDs, reviews, design, development, testing, bug fixes, and final delivery reports into a traceable, recoverable, auditable file-based process.

<!-- README-I18N:START -->

[简体中文](./README.md) | **English**

<!-- README-I18N:END -->

> [!NOTE]
> Currently supports Codex and Claude Code, and is designed for orchestrating end-to-end software delivery from a local business project workspace.

## Why It Exists

AI agents are good at generating content, but software delivery also needs deterministic state transitions, approval boundaries, artifact archiving, and quality release gates. Delivery Workflow lets agents own thinking and generation while the Workflow Worker owns state and evidence.

- File-based workflow: `delivery_workflow/workflow.yaml` is the single source of process truth.
- Project-level state: SQLite stores runs, jobs, gates, and events; the filesystem stores artifacts.
- Clear boundaries: interactive gates, automated steps, and notification steps are separated.
- Real quality gates: frontend/backend development flows into developer self-test, QA system testing, bug-fix, and regression loops until thresholds pass.
- Feishu/Lark approval: PRD v2 can be published as a Feishu/Lark document and approved through an interactive card.
- Host hooks: records file writes, command execution, and Stop events as evidence for whether self-tests really ran.

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
| -------- | ----------------- |
| Block    | 0                 |
| Critical | 0                 |
| Major    | <= 2              |
| Minor    | <= 5              |

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
├── delivery-workflow.config.json   # Plugin-level default config template
└── pyproject.toml
```

After initialization in a business project, the default layout is:

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

#### Codex CLI

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

### Initialize And Create A Project

From your business project workspace, ask the AI to initialize and create a project with the plugin tools. For example:

> "Initialize the project"
> then
> "Create a new project: build an admin system. Detailed features: xxxxxx"

Then wait for the AI agents to complete the workflow.

## MCP Tools

Common MCP tools exposed by the plugin:

| User intent | MCP tool |
| ----------- | -------- |
| Initialize current project config | `delivery_init_project_config` |
| Create a delivery project | `delivery_create_project` |
| Check current project status | `delivery_get_current_project_status` |
| Delete current project | `delivery_delete_current_project` |
| Advance one worker job | `delivery_worker_once` |
| Advance until blocked, idle, or failed | `delivery_worker_until_blocked` |
| Wait for Feishu/Lark approval callback | `delivery_watch_run` |
| Trigger a manual bug-fix workflow | `delivery_request_bug_fix` |
| Inspect artifacts | `delivery_list_artifacts` / `delivery_read_artifact` |

## Configuration

The project reads `workflow.config.json` directly from the current project directory. `config init` copies it into the business project directory.

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

Configure Feishu/Lark bot credentials in `.env`:

```bash
LARK_APP_ID=cli_xxxxxxxxxxxxxxxx
LARK_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LARK_CHAT_ID=oc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## Feishu/Lark Approval

PRD v2 is organized as a Markdown file with a real title and table structure, then published through `lark-cli docs +create --content @file --doc-format markdown` to avoid failures caused by passing long document bodies directly as command arguments.

The approval card includes:

- PRD document link
- Approve button
- Reject button
- Rejection reason input

The consumer only listens for `card.action.trigger`:

```bash
python3 -m delivery_workflow.cli lark event-consumer
```

After a callback arrives, the workflow submits the `prd-approval` gate and automatically starts the background worker when `workflow.continue_after_gate=true`.

## Host Hooks

The plugin ships Claude Code / Codex host hooks for execution evidence. Hooks do not automatically advance workflow state.

| Hook | Purpose |
| ---- | ------- |
| `PreToolUse Bash` | Blocks obviously dangerous commands and commands that may expose `.env` / secrets |
| `PostToolUse Write/Edit/MultiEdit` | Records files actually written by the agent |
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
- The Workflow Worker owns the state machine, gates, queues, artifact archiving, and quality gates.
- Each step only reads the input artifacts declared in `workflow.yaml`.
- All important artifacts are written to `delivery-project/`.
- Frontend and backend source code is written to `source-code/` with separate frontend/backend directories.
- Real self-test and QA results must be traceable; a prepared task package is not a completed step.
