# 前后端开发冒烟自测

前后端开发完成并联调后，开发 Runner 必须带上 QA 产出的 `smoke_test_cases` 做真实冒烟自测。

必须读取：

- `prd_v2`：最终需求和验收标准。
- `ui_design_spec`：视觉、交互和状态要求。
- `frontend_tech_design` / `backend_tech_design`：技术方案和接口契约。
- `smoke_test_cases`：QA 开发前产出的冒烟测试用例。
- `frontend_dev_result` / `backend_dev_result` / `integration_test_result`：开发和联调结果。

执行要求：

- 前端必须在 `source-code/frontend` 执行依赖安装、TypeScript/构建检查、项目启动检查；Web/H5 项目必须优先用 Playwright 或等价浏览器自动化执行冒烟主流程。
- 后端存在时，必须在 `source-code/backend` 执行依赖安装、类型/构建/测试检查、服务启动检查和核心接口请求。
- 必须逐条标记 `smoke_test_cases` 的执行结果：通过、失败、阻断或不适用。
- Block/Critical 问题必须先修复再重新自测，不得把“发现问题待修复”当作自测通过。
- 如果无法真实执行命令，必须返回非成功结果或明确阻断原因。

输出开发冒烟自测报告，必须包含 `commands_run`、`smoke_cases_executed`、`bugs_found_and_fixed`、`known_bugs`、`quality_gate` 和 `conclusion`。
