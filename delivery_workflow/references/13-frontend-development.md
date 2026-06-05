# 前端开发执行

执行前端任务。Runner 只拿 workflow 声明的 artifact，不依赖聊天上下文。

执行完成后必须输出 changed_files、commands_run、test_result 和 summary。
