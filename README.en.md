# Delivery Workflow

A general-purpose software delivery Workflow Worker and multi-agent orchestration plugin. It turns requirements, PRDs, reviews, design, development, testing, bug fixing, and final reports into a traceable, recoverable, auditable local project workflow.

<!-- README-I18N:START -->

[简体中文](./README.md) | **English**

<!-- README-I18N:END -->

> [!NOTE]
> Currently supports Codex and Claude Code. Each business workspace is treated as one independent project; workflow state, artifacts, and source code stay inside that directory.

## Why It Exists

AI agents are good at generating content, but software delivery also needs deterministic state transitions, human confirmation boundaries, artifact archiving, and quality release gates. Delivery Workflow lets agents handle understanding, generation, and execution while the Workflow Worker owns state, evidence, gates, and quality thresholds.

- File-based workflow: `delivery_workflow/workflow.yaml` is the single source of process truth.
- Project-level state: SQLite stores runs, jobs, gates, and events; the filesystem stores artifacts.
- Pre-development confirmation: PRD, UI spec, technical designs, and smoke cases are published to Feishu/Lark before the workflow stops at a human confirmation gate.
- Real quality gates: QA system or regression testing loops through bug-fix until thresholds pass.
- Feishu/Lark archiving: Feishu/Lark fully relies on local `lark-cli` configuration.
- Host hooks: record file writes, command execution, and Stop events as evidence for whether self-tests and QA actually ran.

## Workflow Overview

```text
PRD v1
  -> Multi-role requirement review
  -> Final PRD
  -> UI design specification
  -> Frontend technical design
  -> Backend technical design (skipped when backend is not needed)
  -> QA smoke test cases
  -> Publish Feishu/Lark documents and open pre-development confirmation gate
  -> Frontend development
  -> Backend development (skipped when backend is not needed)
  -> Frontend/backend integration
  -> Developer smoke self-test
  -> QA system testing
  -> bug-fix <-> QA regression testing
  -> QA test report
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
├── .codex-plugin/
├── .claude-plugin/
├── hooks/
├── delivery_workflow/
├── skills/delivery-workflow/
├── tests/
├── delivery-workflow.config.json
└── pyproject.toml
```

After initialization in a business project:

```text
.delivery-workflow/
  delivery.db
  logs/
delivery-project/
source-code/
workflow.config.json
```

## Quick Start

### Codex CLI

```bash
codex plugin marketplace add https://github.com/devTech-zhang/multi-agent-delivery-workflow.git --ref main
codex plugin add delivery-workflow@devTech-Zhang
codex plugin list | grep delivery-workflow
```

Update:

```bash
codex plugin marketplace upgrade delivery-workflow-marketplace
codex plugin add delivery-workflow@devTech-Zhang
```

### Claude Code CLI

```bash
claude plugin marketplace add https://github.com/devTech-zhang/multi-agent-delivery-workflow.git#main
claude plugin install delivery-workflow@devTech-Zhang
claude plugin marketplace list
claude plugin list
```

Update:

```bash
claude plugin marketplace update delivery-workflow-marketplace
claude plugin update delivery-workflow@devTech-Zhang
```

### Initialize And Create A Project

From your business project workspace, ask the AI to call the plugin tools:

```text
Initialize project config.
Create a new project: build a TODO H5 app with add, complete, delete, and local persistence.
```

By default, the workflow advances to the pre-development document confirmation gate. After reviewing the PRD, UI spec, technical designs, and smoke cases in Feishu/Lark documents, ask the AI to continue development. If the documents need changes, describe what to revise and the workflow will loop back through document updates.

## MCP Tools

| User intent                             | MCP tool                                             |
| --------------------------------------- | ---------------------------------------------------- |
| Initialize current project config       | `delivery_init_project_config`                       |
| Create a delivery project               | `delivery_create_project`                            |
| Check current project status            | `delivery_get_current_project_status`                |
| Delete current project with backup      | `delivery_delete_current_project`                    |
| Advance one worker job                  | `delivery_worker_once`                               |
| Advance until blocked, idle, or failed  | `delivery_worker_until_blocked`                      |
| Wait for gate submission and settlement | `delivery_watch_run`                                 |
| Trigger a manual bug-fix workflow       | `delivery_request_bug_fix`                           |
| Inspect artifacts                       | `delivery_list_artifacts` / `delivery_read_artifact` |

## Configuration

The project reads `workflow.config.json` from the current project directory. `config init` copies the plugin default config into the business workspace.

Feishu/Lark config supports:

```json
{
    "lark": {
        "enabled": true,
        "identity": "bot",
        "chat_id": "oc_xxx"
    }
}
```

- `enabled=true`: create Feishu/Lark documents.
- `enabled=false`: skip Feishu/Lark actions and keep only local artifacts.
- `identity`: passed to `lark-cli --as`; use `bot` or `user`.
- `chat_id`: optional project group ID; the default template keeps it empty, and each business project can fill it in when needed.

If `lark_chat_id` / `--lark-chat-id` is also provided at project creation time, the creation parameter takes precedence. CLI example:

```bash
python3 -m delivery_workflow.cli project create \
  --title "TODO H5" \
  --requirement "Build a TODO H5 app" \
  --lark-chat-id "oc_xxx"
```

Feishu/Lark credentials are fully managed by local `lark-cli`:

```bash
npx @larksuite/cli@latest install
lark-cli config init --new
lark-cli auth login --recommend
lark-cli auth status
```

## Feishu/Lark Document Publishing

Before development, the workflow publishes:

- `{Project Name} PRD`
- `{Project Name} UI Design Specification`
- `{Project Name} Frontend Technical Design`
- `{Project Name} Backend Technical Design` (skipped when backend is not needed)
- `{Project Name} Smoke Test Cases`

Later it also publishes:

- `{Project Name} Test Report`
- `{Project Name} Final Delivery Report`

Document content is composed as XML and sent through `lark-cli docs +create --content @file --doc-format xml`, avoiding command-line corruption for long bodies and tables.

Group messages are limited to three stages:

- `{Project Name} project has been created and is in progress.`
- Before development, one message lists PRD, UI spec, technical design, and smoke test case document links.
- After completion, one message lists the test report and final delivery report links.

## Host Hooks

The plugin ships Claude Code / Codex host hooks for execution evidence. Hooks do not automatically advance workflow state.

| Hook                               | Purpose                                                                                            |
| ---------------------------------- | -------------------------------------------------------------------------------------------------- |
| `PreToolUse Bash`                  | Blocks obviously dangerous commands and commands that may expose secrets                           |
| `PostToolUse Write/Edit/MultiEdit` | Records files actually written by the agent                                                        |
| `PostToolUse Bash`                 | Records executed commands and classifies installs, builds, tests, Playwright, API checks, and more |
| `Stop`                             | Records the end of an agent turn                                                                   |

Evidence is written to:

```text
.delivery-workflow/logs/host-hooks.jsonl
.delivery-workflow/logs/workflow.log
```

## Development And Verification

```bash
python3 -m unittest tests.test_core
python3 -m compileall delivery_workflow
git diff --check
```

## Design Principles

- Agents generate content and execute tasks; they do not mutate workflow state directly.
- The Workflow Worker owns the state machine, gates, queues, artifact archiving, and quality gates.
- Each step only reads the input artifacts declared in `workflow.yaml`.
- All important artifacts are written to `delivery-project/`.
- Frontend and backend source code is written to `source-code/` with separate frontend/backend directories.
- Real self-test and QA results must be traceable; a prepared task package is not a completed step.
