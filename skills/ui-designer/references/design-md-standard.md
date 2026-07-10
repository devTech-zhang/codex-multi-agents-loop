# DESIGN.md 输出标准

## 核心原则

1. `DESIGN.md` 是项目视觉设计系统，不是工程流程、任务计划或实现日志。
2. 视觉 token 必须有代码、平台规范、设计稿、快照或用户确认作为证据；缺失项写“待确认”，不编造数值。
3. frontmatter 只保存稳定、机器可读的 token；正文写可执行的应用规则。
4. 不静默覆盖已有 `DESIGN.md`；先读取现有文件并说明合并、刷新或覆盖范围。

## 文件结构

~~~md
---
name: Project Name
description: Visual design system for this project.
colors: {}
typography: {}
rounded: {}
spacing: {}
components: {}
---

# Design System: Project Name

## Overview

## Colors

## Typography

## Elevation

## Components

## Do's and Don'ts
~~~

frontmatter 可使用 `name`、`description`、`colors`、`typography`、`rounded`、`spacing` 和 `components`。组件 token 只描述基础视觉属性；阴影、动效、断点和复杂状态写入正文。

## 证据与 seed

- **locked**：材料明确给出的颜色、尺寸、组件状态或平台规则。
- **inferred**：从需求、交互稿或现有代码合理推导出的信息密度、层级和状态覆盖。
- **open**：设计或代码证据不足、实现前必须确认的问题。

没有完整 UI 代码时，文件顶部写明 seed 证据来源，并在首次 UI 实现后建议基于真实代码刷新。设计稿或快照只作为视觉证据，落地前仍需映射为项目语义 token。

## 六段式正文

### Overview

说明产品语境、视觉目标、平台约束、证据来源、层级范围和待确认项，不写构建、路由、请求或测试流程。

### Colors

说明颜色角色、使用位置、主题策略、禁用态、风险色和对比度要求。

### Typography

说明字体、字号角色、字重、行高、截断、数字样式和无障碍适配。

### Elevation

说明页面背景、卡片、弹层、遮罩、描边、圆角、阴影与层级策略。

### Components

说明按钮、输入、卡片、导航、弹层、列表、标签、图标、图片等组件的视觉职责、状态、资源策略和适配规则。

### Do's and Don'ts

写可以直接指导实现和审查的红线，避免“保持美观”“注意一致性”这类不可执行表述。
