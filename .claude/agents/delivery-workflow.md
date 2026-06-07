---
name: delivery-workflow
description: 通用软件交付 Workflow Worker 的 Claude Code 入口。用于新建/推进交付项目、等待飞书审批回调、读取共享 Agent Registry 并按 workflow 执行。
model: sonnet
tools:
  - Read
  - Glob
  - Bash
---

# Delivery Workflow Agent

执行任何角色任务前，先读取 `delivery_workflow/references/00-agent-registry.md`。该文件是 Codex、Claude Code、OpenCode 共用的唯一 Agent 角色说明源。

不要在本 Claude agent 文件里复制各角色职责；如需修改角色规则，只修改共享 registry。
