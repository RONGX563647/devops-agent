"""HTTP 网关层：FastAPI 实例、路由与请求依赖。

ASGI 入口为 `devops_agent.gateway.app:app` —— uvicorn 目标，不随子包调整而变。

四层，依赖只向内：

| 层 | 位置 | 职责 |
|---|---|---|
| 叶子 | `contract.py` / `errors.py` / `settings.py` | 帧与载荷、错误码、部署设置 |
| `turn/` | 编排 / 租约 / 收尾决策 / 端口 / 输入准备 / 保活 | 一轮 run 的机制 |
| `routes/` | 4 个端点 + 请求体 + 依赖取用 | 只做 HTTP 契约映射 |
| 装配 | `app.py` / `bootstrap.py` | `create_app()`：注入、挂载、启动告警 |

`.importlinter` 强制：叶子不依赖兄弟层、`turn` 不依赖 `routes`/`app`、`routes` 不依赖 `app`。
"""
