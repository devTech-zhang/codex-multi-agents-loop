# 开发前资料确认

Workflow 已完成开发前资料整理，并将以下文档发布到飞书群：

- 最终 PRD
- UI 设计规范
- 前端技术方案
- 后端技术方案（如项目不需要后端则省略）
- 冒烟测试用例

用户检查文档后，如果资料可以进入开发，应提交：

```json
{"approved": true, "approver": "用户姓名", "comment": "确认进入开发"}
```

如果需要修改资料，应提交：

```json
{"approved": false, "approver": "用户姓名", "comment": "需要修改的具体问题"}
```

被拒绝后 workflow 会回到产品经理汇总阶段，相关 Agent 需要根据 `development-doc-confirmation_gate` 中的意见修订文档并重新发布。
