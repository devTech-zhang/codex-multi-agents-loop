# 回归测试执行

QA Agent / 测试 Runner 必须执行真实测试用例，不能只根据开发结果写总结。

执行要求：

- 读取 `prd_v2`、`ui_design_spec`、前后端技术方案、测试用例和开发结果。
- 前端 Web/H5 必须优先使用 Playwright 或等价浏览器自动化真实打开页面、操作主流程、截图或记录关键断言。
- 后端必须真实启动服务并请求核心接口。
- 如果发现 bug，必须给出等级：Block、Critical、Major、Minor，并提供复现步骤、期望结果、实际结果、责任侧建议。
- 测试报告必须包含可机器识别的 bug 统计，例如：

```json
{
  "quality_gate": {
    "bug_counts": {"block": 0, "critical": 0, "major": 1, "minor": 3}
  }
}
```

- 当 bug 数量超过 `workflow.config.json` 的 `quality_gate` 阈值时，workflow 会自动回到 `bug-fix`，再执行二轮回归，直到满足准出标准。

输出测试报告，必须包含 commands_run、test_cases_executed、bugs、quality_gate、conclusion。
