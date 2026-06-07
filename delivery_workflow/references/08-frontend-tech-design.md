# 前端技术方案

前端工程师 Agent 基于 PRD v2 和 UI 设计规范输出技术方案。

技术栈默认固定为 Vite + React + TypeScript。是否使用 Tailwind CSS + shadcn/ui 由需求复杂度、组件密度和设计规范决定；如果使用，必须说明原因和组件边界；如果不使用，也必须说明原因。

必须包含：

- 工程结构：代码写入 `source-code/frontend`，说明目录、路由、组件、hooks、services、types、测试文件位置。
- 页面/组件边界：页面组件、业务组件、基础组件、状态组件的职责。
- 状态管理：本地状态、服务端状态、缓存、表单状态、错误状态。
- API 契约需求：请求方法、路径、参数、响应、错误码、重试和 loading 策略。
- UI 实现策略：如何落实 `ui_design_spec` 的颜色、字体、布局、响应式、组件状态。
- 验证方式：必须包含 `npm install`、`npm run build`、`npm run lint` 或等价命令；网页必须包含 Playwright 或等价浏览器自测计划。
- 准出标准：前端自测不得存在运行失败、构建失败、首屏报错、主要流程不可用；发现问题必须先自修。
- Codex/Claude Code 可执行任务边界：明确哪些文件要创建、哪些命令要跑、哪些结果要回填。
