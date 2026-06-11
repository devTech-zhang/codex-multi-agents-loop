# QA 系统测试

QA Runner 必须执行真实系统测试，不能只根据开发结果写总结。

执行要求：

- 读取 `prd_v2`、`ui_design_spec`、前后端技术方案、冒烟测试用例、开发自测报告、联调结果和开发结果。
- 前端 Web/H5 必须优先使用 Playwright 或等价浏览器自动化真实打开页面、操作主流程、截图或记录关键断言。
- 后端存在时必须真实启动服务并请求核心接口。
- 如果发现 bug，必须给出等级：Block、Critical、Major、Minor，并提供复现步骤、期望结果、实际结果、责任侧建议。
- 测试结果是开发修复的输入，必须写清每个 bug 的唯一编号、责任侧建议、复现步骤和验证口径。
- 测试结果必须包含可机器识别的 bug 统计，例如：

```json
{
  "quality_gate": {
    "bug_counts": {"block": 0, "critical": 0, "major": 1, "minor": 3}
  }
}
```

当 bug 数量超过 `workflow.config.json` 的 `quality_gate` 阈值时，workflow 会自动回到 `bug-fix`。

输出系统测试结果，必须包含 `commands_run`、`test_cases_executed`、`bugs`、`quality_gate`、`evidence` 和 `conclusion`。
