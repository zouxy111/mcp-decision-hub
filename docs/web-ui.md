# Web UI（web-ui/）运行与接入说明

- 日期：2026-09-11
- 状态：脚手架就位，未接入后端

## 来源

[satnaing/shadcn-admin](https://github.com/satnaing/shadcn-admin) v2.2.1（MIT，LICENSE 保留在 web-ui/），
技术栈：Vite 8 + React 19 + TypeScript + shadcn/ui + Tailwind + TanStack Router/Query。
已移除上游 .git，作为本仓库普通子目录维护。

## 本地运行

```bash
cd web-ui
pnpm install   # 仅首次
pnpm dev       # http://localhost:5173
```

- pnpm 构建脚本白名单配置在 `web-ui/pnpm-workspace.yaml`（esbuild、@clerk/shared）。
- 当前登录为模板自带 mock 认证（纯前端，任意邮箱密码可登录），默认已内置演示用户。

## 接入 FastAPI 的规划

1. 在 `hub/api/` 服务函数上加一层 JSON 路由（登录/登出、事项 CRUD、拍板、Token 管理），复用现有服务层与 CSRF 逻辑。
2. `pnpm build` 产物由 FastAPI `StaticFiles` 挂载 + SPA fallback，前后端同源，cookie 会话机制不变。
3. 迁移顺序建议：事项工作流 → Token 管理 → 管理页（邀请/审计）；`hub/web/templates` 的 Jinja2+htmx 页面在过渡期并行保留。

## 与后端的关系

- PRD 明确排除 Next.js 等旧平台技术栈；web-ui 为纯 SPA，不引入 SSR，符合该边界。
- 模板内 `clerk/` 路由组为上游可选的 Clerk 认证演示，接入时不使用。
