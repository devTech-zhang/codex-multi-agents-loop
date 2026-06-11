# QA 回归测试

QA Runner 必须根据 `bug_fix_result` 执行真实回归测试。

执行要求：

- 逐条验证 `bug_fix_result.fixed_bugs`，说明是否修复、是否引入新问题。
- 复用 `smoke_test_cases` 和上一轮 QA 测试结果，覆盖被修复模块的主流程、边界和相关回归范围。
- 前端 Web/H5 必须优先使用 Playwright 或等价浏览器自动化验证。
- 后端存在时必须真实启动服务并请求相关接口。
- 如果发现未修复或新增 bug，必须按 Block、Critical、Major、Minor 标级，并给出复现步骤、期望结果、实际结果、责任侧建议。
- 回归结果必须包含可机器识别的 bug 统计。

当 bug 数量超过 `workflow.config.json` 的 `quality_gate` 阈值时，workflow 会自动回到 `bug-fix`，直到满足准出标准。

输出回归测试结果，必须包含 `commands_run`、`regression_scope`、`verified_fixed_bugs`、`new_or_remaining_bugs`、`quality_gate`、`evidence` 和 `conclusion`。
