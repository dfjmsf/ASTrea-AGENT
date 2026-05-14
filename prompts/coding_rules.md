# ASTrea 编码环境约束（静态规则）

> 以下是 ASTrea 系统特有的环境约束，模型训练数据中不包含这些信息。
> 违反这些规则将导致项目无法正确运行。

## 端口规则

- **严禁使用 8000 端口** — 8000 已被 ASTrea 系统后端占用，生成的项目使用 8000 会启动失败
- 后端服务必须使用 `port=5001`

## 后端路由规则（FastAPI）

- `routes.py` 必须使用 `APIRouter()`，严禁使用 `FastAPI()` 实例 — 否则 `include_router` 无法注册路由，所有 API 返回 405
- 每个处理 HTTP 请求的函数必须有 `@router.post` / `@router.get` 装饰器 — 没有装饰器的函数不会被注册为端点
- POST/PUT 必须用 Pydantic `BaseModel` 接收 JSON Body — 直接写参数会被解析为 query parameter，导致 422

## 静态文件挂载规则

- 路由注册必须在 `app.mount("/", StaticFiles(...))` 之前 — 否则 mount 会吞噬所有 `/api` 请求导致 404
- 后端入口文件必须配置 CORS 中间件

## 前端路径规则

- 前后端分离时，`fetch` 调用禁止使用相对路径 `/api/xxx` — 用户通过 `file://` 打开时会失败
- 必须定义 `const API_BASE = 'http://localhost:5001'`，所有请求使用 `` `${API_BASE}/api/xxx` ``
- `API_BASE` 的端口必须与后端 `port=5001` 一致，严禁写 8000

## 前端 CDN 引入规则

- 使用 CDN 方式引入前端框架时，`<script src>` 标签必须在 `<head>` 或 `<body>` 顶部，且在应用代码 `<script>` 之前
- Vue 3 CDN：`<script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>`
- React CDN：需同时引入 `react.production.min.js` + `react-dom.production.min.js` + `babel-standalone`
- Tailwind CSS CDN：`<script src="https://cdn.tailwindcss.com"></script>`
- **绝对不能遗漏框架 CDN 引入** — 漏掉 script 标签会导致整个页面白屏

