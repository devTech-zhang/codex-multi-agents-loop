# 开发自测

前后端开发完成后，开发 Runner 必须执行真实自测，不能只汇总开发结果。

必须读取：

- `prd_v2`：最终需求和验收标准。
- `ui_design_spec`：前端视觉、交互和状态要求。
- `frontend_tech_design` / `backend_tech_design`：技术方案和接口契约。
- `dev_tasks`：任务拆解和实现边界。
- `frontend_dev_result` / `backend_dev_result`：前后端开发结果、已执行命令和已知风险。

执行要求：

- 前端必须在 `source-code/frontend` 执行依赖安装、TypeScript/构建检查和核心页面启动检查；Web/H5 项目必须优先使用 Playwright 或等价浏览器自动化打开页面并验证主流程。
- 后端必须在 `source-code/backend` 执行依赖安装、类型/构建或测试检查、服务启动检查，以及核心接口和异常接口请求。
- 如果发现 Block/Critical 问题，必须先修复再重新自测，不得把“发现问题待修复”当作自测通过。
- 如果发现 Major/Minor 遗留问题，必须说明原因、影响范围和是否影响进入 QA。
- 如果无法真实执行命令，必须返回非成功结果或明确阻断原因，不得输出“已准备自测方案”冒充自测报告。

输出开发自测报告，必须包含：

- `commands_run`：实际执行的命令、目录、结果。
- `frontend_self_test`：前端构建、类型检查、页面运行、浏览器验证结果。
- `backend_self_test`：后端构建/测试、服务启动、接口请求结果。
- `bugs_found_and_fixed`：自测发现并已修复的问题。
- `known_bugs`：仍遗留的问题，按 Block、Critical、Major、Minor 标级。
- `conclusion`：是否允许进入 QA 系统测试。
