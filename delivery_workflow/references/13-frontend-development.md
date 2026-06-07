# 前端开发执行

执行前端任务。Runner 只拿 workflow 声明的 artifact，不依赖聊天上下文。

前端工程师必须同时读取并落实：

- `prd_v2`：最终需求，必须掌握每个业务细节和验收标准。
- `ui_design_spec`：设计规范、组件、状态和异常要求。
- `frontend_tech_design` 和 `dev_tasks`：技术边界与任务拆解。

工程要求：

- 在 `source-code/frontend` 创建或修改 Vite + React + TypeScript 工程。
- 根据需求判断是否使用 Tailwind CSS + shadcn/ui；使用时必须保证组件、主题和样式配置完整。
- 严格按 `ui_design_spec` 实现视觉、布局、组件状态和交互细节。
- 完成后必须执行真实自测，至少包括安装依赖、类型检查/构建、核心页面运行验证；网页类项目必须优先用 Playwright 或等价浏览器自动化验证主要流程。
- 自测发现 Block/Critical 问题必须立即修复后再输出结果。

常用命令参考：

```bash
cd source-code
npm create vite@latest frontend -- --template react-ts
cd frontend
npm install
npm run dev -- --host 127.0.0.1
npm run build
```

Tailwind CSS 常用命令：

```bash
npm install -D tailwindcss @tailwindcss/vite
```

`vite.config.ts` 常用配置：

```ts
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
});
```

`src/index.css` 常用入口：

```css
@import "tailwindcss";
```

shadcn/ui 常用命令：

```bash
npx shadcn@latest init
npx shadcn@latest add button input textarea checkbox select dialog dropdown-menu tabs card badge alert form
```

执行完成后必须输出 changed_files、commands_run、self_test_result、known_bugs、quality_gate_risk 和 summary。
