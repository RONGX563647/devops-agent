# ADR-0005：运行期配置集中到 SQLite，yaml 退为仓库基线

- 状态：Accepted
- 日期：2026-11-27
- 相关：`src/devops_agent/config_store/`、`src/devops_agent/config_files.py`、
  `config/{health,input,memory,gateway}.yaml`、`src/devops_agent/config.py`、
  SPEC-0001（模型参数）、ADR-0003（模型参数落盘）

## 背景

`config/` 下并排躺着 7 份 yaml、约 100 个叶子键：`health` / `input` / `memory` /
`gateway` / `model` / `policy` / `gates`。它们由 `config.py` 的 `@cache` 加载器
统一读取，问题有三个，且互相放大：

1. **改一个阈值要重启进程**。`load_health()` 等函数是 `@cache` 的，运行中改文件
   不生效；不看代码的人会以为「保存了」。
2. **没有写入路径**。yaml 在仓库里，属于源码；服务跑在容器里时，改它等于改镜像。
3. **找不到、也说不清来源**。同一个键可能来自 yaml / 环境变量 / 代码默认，
   界面上没有任何地方能回答「现在这个值是哪来的」。

## 决定

### 1. 只收「运行期部署策略」，三类值明确不动

| 类别 | 例子 | 处置 |
|---|---|---|
| 运行期部署策略 | health 权重、input 体积上限、memory 分层预算、gateway 限额 | 进库、可在线改 |
| 安全域知识 | `policy.yaml` 的高危正则、永不沉淀名单 | 进库但不给写：改它等于给自己发权限 |
| 构建期 / 契约 | `gates.yaml`、`size-baseline.json`、HTTP 状态码、SSE 帧名、RunStatus 枚举 | 不进库 |

`gates.yaml` 必须留文件：CI 在 checkout 上跑 `check_gates.py`，那里没有数据库；
而且门禁阈值恰恰是最需要在 PR 里被 diff 审查的内容。把它塞进 SQLite 会让
「CI 能不能跑」依赖一个运行态文件，属于倒置依赖。

### 2. 只存差异，默认值留在代码里

表里出现的每一行都是「有人刻意改成与下层不同」。写进来的值若等于下层，
直接删行 —— 等价于「恢复默认」，不需要 `is_default` 之类的标记列。

默认层就是 pydantic 字段默认值，不抄一份到 yaml 或数据库：升级代码改默认值时
不需要迁移，也不会出现「代码默认 12、yaml 也写 12、库里还是 12」三份同值。

> 实施期遗留：`health / input / memory / gateway` 四份 yaml 曾把默认值又抄了一遍，
> 与本条相违。已于 2026-11-27 删除，见文末「后续修订」。

### 3. 四层优先级与环境变量锁定

```
环境变量白名单 > config.db 覆盖 > config/*.yaml 基线 > 代码默认
```

env 放在最高并**在界面上锁定**，是为了避免容器注入 `MEMORY_BACKEND=mem0`、
界面改成 `local` 后静默无效 —— 那是最难查的一类偏差。被锁的键拒绝写入并回报原因，
而不是写进去等它不生效。

yaml 仍然参与合流（而不是「首次导入后就丢掉」）：它可评审、可 diff，在没有数据库
的环境（CI、单测、`MODE=file`）能独立工作；新增的键在 yaml 里写一次就能生效，
不需要为老部署补迁移脚本。

### 4. 生效时机如实上报

注册表给每个域标 `hot`：`health` / `input` 是读取时读配置，`immediate`；
`memory` 在装配期读，`next_turn`；`gateway` 的 `Settings` 在装配期固定，`restart`。
面板直接显示这三个词，不做「假装热更新」的乐观提示。

### 5. 模型参数不并入

`model` 域已有自己的一套落盘（`model.local.yaml`）与乐观锁（`settings.version`），
再由本模块写一条路径只会制造第二个真相。注册表把它标成 `managed_by=model_settings`，
面板显示「去模型抽屉改」。

## 后果

- 新增：`workspace/config.db`（不在仓库内，需随部署备份）、`GET/PATCH /api/config`、
  `config_audit` 表（每次改动一行，含 old/new/version/by）。
- 代价：配置有了第二个存放位置。缓解手段是「只存差异 + 导出 yaml + 漂移提示」：
  每行记下写入时的默认值，若代码默认后来变了，接口返回 `drifted`，界面提示
  「默认值已升级，当前仍生效旧覆盖」。
- 测试与 CI 用 `DEVOPS_AGENT_CONFIG_MODE=file` 或把 `DEVOPS_AGENT_CONFIG_DB`
  指到 tmp：读路径在库不存在时不会创建文件，不会污染工作区。

## 备选方案

- **全部塞进一份 yaml + 文件锁**：可 diff，但并发写与逐键审计要自己实现，
  且容器里改文件仍然不落地。
- **复用 `observability.db` 的 `settings` 表**：改动最小，但清理运行记录 / 重建
  观测库会连带配置；配置的寿命明显长于轨迹。
- **每个域一张表**：结构更显式，代价是每加一个域都要建表 + 迁移。
  KV + 点号路径能在不改 DDL 的前提下新增键。

## 后续修订（2026-11-27）：删除四份同值 yaml

`config/health.yaml`、`config/input.yaml`、`config/memory.yaml`、`config/gateway.yaml`
已删除。删除前逐项比对过：四份文件共 76 个键，与对应 pydantic 模型的字段默认值
**完全相同（0 处差异）**——它们只是把 §2 里说的「默认层」抄了第二遍，正是本条
决策要消灭的「代码默认 12、yaml 也写 12」。

删除不改变任何行为：`config_store` 的 yaml 层在文件缺失时返回空 dict，合流退化为
「代码默认 ← db 覆盖 ← env」，`load_health / load_input / load_memory / load_gateway`
的返回值与删除前一致，由 `test_*_loads_after_deployment_yaml_removed` 与
`test_memory_defaults_load_without_yaml` 钉住。部署者调参走配置面板（写 config.db），
不再需要「改文件 + 重启」这条与面板并行的旧路径。

仍留在仓库文件里的四份，各自理由未变：

| 文件 | 为什么不能进库 |
|---|---|
| `policy.yaml` | 安全域知识（高危正则、永不沉淀名单），改它等于给自己发权限 |
| `model.yaml` | 写入口在模型抽屉（`model_settings/`），再开一条会制造第二个真相 |
| `gates.yaml` | CI 在干净 checkout 上跑 `check_gates.py`，那里没有数据库 |
| `size-baseline.json` | 同上，构建期契约，必须在 PR 里被 diff 看见 |

「yaml 作为可评审基线」的能力没有取消：`config_store` 仍读同段名的 yaml，
无数据库环境（CI / 测试）依旧只靠代码默认 + 可选 yaml 工作。
除本节外，ADR-0003 / 0004 / 0005-gateway-subpackages 与早期 spec 里对这几份
yaml 的引用属历史记录，按 ADR 惯例不回改。
