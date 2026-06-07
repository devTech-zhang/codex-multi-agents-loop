# 后端开发执行

执行后端任务。Runner 只拿 workflow 声明的 artifact，不依赖聊天上下文。

后端工程师必须读取 `prd_v2`、`backend_tech_design` 和 `dev_tasks`，在 `source-code/backend` 创建或修改 Node.js 工程。

执行要求：

- 服务必须能本地启动，并提供明确启动命令。
- 核心 API 必须根据技术方案真实实现，不能只写接口说明。
- 必须执行真实自测，至少包含依赖安装、启动检查、核心接口请求、异常接口请求；可使用 node:test、vitest、supertest、curl 或等价工具。
- 自测发现 Block/Critical 问题必须立即修复后再输出结果。

执行完成后必须输出 changed_files、commands_run、self_test_result、known_bugs、quality_gate_risk 和 summary。
