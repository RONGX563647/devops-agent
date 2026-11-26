# ADR-0005：网关拆子包 —— 三层 + 叶子，边界交给 CI

- 状态：Accepted
- 日期：2026-11-26
- 决策者：项目所有者
- 相关：ADR-0004（网关按变更原因拆分）、`.importlinter`、`docs/wiki/Architecture.md`

## 背景

ADR-0004 之后 `gateway/` 是 18 个文件平铺，职责靠**文件名**区分：`stream.py` 是路由、
`run.py` 是编排、`lease.py` 是资源、`contract.py` 是契约。三个后果：

1. **方向只能靠读代码**：`.importlinter` 里为了表达「契约是叶子」，要把 14 个兄弟模块
   逐个列进 `forbidden_modules`，每加一个文件就要回来补一行 —— 契约本身在跟着代码腐烂。
2. **同名概念互相干扰**：`gateway/run.py`（一轮）与 `runtime/runtime.py`（协议）、
   `gateway/sessions.py`（端点）与 `runtime/sessions.py`（注册表）在读 import 时都得多看一眼。
3. **新增文件的默认位置不明确**：一个「SSE 保活」应该放哪？平铺时答案是「顶层又一个文件」。

## 决策

按**层**分包，叶子留在顶层：

```text
gateway/
├── __init__.py        # 四层说明
├── app.py             # 装配根（uvicorn 目标 devops_agent.gateway.app:app 不变）
├── bootstrap.py       # 启动期自述与告警
├── contract.py        # 叶子：帧与挂起载荷
├── errors.py          # 叶子：错误码与状态码
├── settings.py        # 叶子：env 名 + gateway.yaml 策略
├── turn/              # 一轮 run 的机制
│   ├── orchestrate.py #   原 run.py
│   ├── lease.py
│   ├── ending.py
│   ├── ports.py
│   ├── input_prep.py
│   └── sse.py         #   保活的唯一消费者是编排，归入本轮机制
└── routes/            # HTTP 面
    ├── stream.py
    ├── confirm.py
    ├── sessions.py
    ├── health.py
    ├── schemas.py     #   请求体只服务 HTTP 边界
    └── deps.py        #   依赖取用同理
```

命名取舍：`turn/` 而不是 `runtime/`（与 `devops_agent.runtime` 冲突）；`routes/` 而不是
`http/`（避免与 stdlib `http` 同名带来的阅读噪音）。

### 边界由三条 CI 契约强制

| 契约 | 约束 | 为什么 |
|---|---|---|
| `gateway-contract-is-leaf` | `contract` / `errors` / `settings` 不得依赖 `app` / `bootstrap` / `routes` / `turn` | 叶子是前后端与部署契约，改它不该被锁、编排、持久化牵连 |
| `gateway-turn-no-http` | `turn` 不得依赖 `app` / `routes` | 机制层出现 `Request`/状态码就说明放错了层 |
| `gateway-routes-no-app` | `routes` 不得依赖 `app` | 路由只组装；import 装配根会立刻建出一个 app |

`forbidden_modules` 从「14 个兄弟文件」缩成「2 个包」，新增文件不再需要回来改契约。

## 改名映射（对外可见的唯一变化是 import 路径）

| 旧 | 新 |
|---|---|
| `gateway/run.py` | `gateway/turn/orchestrate.py` |
| `gateway/{lease,ending,ports,input_prep,sse}.py` | `gateway/turn/…` |
| `gateway/{stream,confirm,sessions,health,schemas,deps}.py` | `gateway/routes/…` |
| `gateway/{contract,errors,settings,app,bootstrap}.py` | 不变 |

ASGI 入口、HTTP 路径、SSE 帧、错误码、配置键全部不变；测试只改 import 行，断言零改动。
`git mv` 保留历史，需要时用 `git log --follow -M` 穿过改名。

## 不做什么

- **不把 `contract.py` 拆成 `frames` + `pending`**：它是单一职责（线协议）且只有 142 行，
  拆成包只会多一层 import 跳转。
- **不为 `sse.py`（70 行）单建包**：它跟着唯一的消费者进 `turn/`，而不是自成一个 `transport/`。
- **不引入 `core/`、`common/` 这类名字**：见到这种包名时读者唯一能确定的是「作者没想清楚」。
- **不在包 `__init__` 里 re-export `turn` 的成员**：`turn/__init__` 只写说明，避免包级
  `__init__` 变成隐藏的第二条公开面，也避免 import 顺序带来的环。

## 影响

- 路径变长（`gateway.routes.stream` 比 `gateway.stream` 长 7 个字符），换来「看 import 就知道层」。
- 覆盖率与门禁不变：11 条 import-linter 契约全绿，`gateway/` 覆盖率仍在 94%。
- 需要同步更新：`docs/wiki/Architecture.md` §2/§3、`docs/wiki/Permissions.md`、
  ADR-0004 的目录树（保留原文，加一行指向本 ADR）。
