# QA 生成测试报告

QA Agent 在系统测试或回归测试达到质量门禁后生成最终测试报告。

必须读取：

- `prd_v2`：最终需求和验收标准。
- `smoke_test_cases`：冒烟用例设计。
- `development_smoke_self_test_report`：开发冒烟自测报告。
- `qa_system_test_result`：第一轮系统测试结果。
- `qa_regression_test_result`：回归测试结果；如没有回归轮次，应说明未发生 bug-fix 回归。
- `bug_fix_result`：开发修复报告；如没有修复轮次，应说明无修复项。

报告必须包含：

- 测试范围与环境。
- 实际执行命令和证据。
- 冒烟、系统测试、回归测试执行摘要。
- 缺陷列表与最终状态。
- 质量门禁统计：Block、Critical、Major、Minor 数量和阈值。
- 准出结论与遗留风险。

输出完整 Markdown 测试报告，不得只输出摘要。
