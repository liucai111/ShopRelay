# ShopRelay Web

用户客服前端，使用 Vue 3、TypeScript、Vite 和 Element Plus。它连接真实 FastAPI API，不提供本地假答案或伪造运行状态。

## 本地启动

先按[开发环境指南](../docs/getting-started.md)初始化本地演示数据库、种子用户和 FastAPI（`127.0.0.1:8000`）。再从 `frontend/` 执行：

```powershell
npm ci
$env:SHOPRELAY_API_ORIGIN = 'http://127.0.0.1:8000'
npm run dev
```

打开 Vite 输出的本地地址。`SHOPRELAY_API_ORIGIN` 只用于 Vite 开发代理，不含密钥。生产部署应由同源反向代理发布静态构建并转发 `/api`、`/health`；浏览器端不配置 DeepSeek 凭据。

## 对接的 API

- `GET /api/demo/csrf`、`POST /api/demo/login`、`GET /api/demo/me`、`POST /api/demo/logout`
- `GET /api/sessions`、`POST /api/sessions`、`GET /api/sessions/{session_id}`
- `POST /api/chat/stream`，使用服务端 Run 幂等和持久化 SSE 事件
- `GET /api/runs/{run_id}`、`GET /api/runs/{run_id}/events?after_seq=...`

认证只由 HttpOnly 会话 Cookie 承载。CSRF token 仅保存在页面内存；会话 ID 可放在路由和 `localStorage`，不作为身份凭据。未知执行结果不会自动重新 POST；刷新后读取 Run、会话结果和事件游标恢复。

SSE 展示结构化进度事件与最终答案，不模拟逐 Token 输出。
