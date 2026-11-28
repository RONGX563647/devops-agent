# SPEC-0022：设置页重构 —— 单列模型选择、多网关管理、参数引导

- 状态：已实施（2026-11-28；实施记录与偏差见 §14）
- 日期：2026-11-28
- 基线：`main a6787d3`
- 关系：SPEC-0018（多网关注册表与能力卡）的界面收口；ADR-0005（配置中心）的第二阶段；不改 SPEC-0001 的落盘语义（`model_settings/` 仍是唯一写路径）
- 触发来源：使用截屏反馈——配置中心是一列裸键名、模型参数藏在遮住对话的抽屉里、多网关只能手写 yaml

## 1. 结论

把两件事拆开：

| 场景 | 现在 | 之后 |
|---|---|---|
| 换模型 | 抽屉里「先选网关 → 再选模型」两级联动，只有一个网关时下拉还是禁用的 | 输入栏一个列表，跨网关聚合，选中即落 `(网关, 模型)` |
| 调参数 | 抽屉浮在对话上，不能深链、不能对照 | 设置页第三栏，右侧常驻「这是什么 / 改了会怎样 / 谁在压着它」 |
| 加网关 | 手写 `config/providers.yaml`，`READONLY` 自己写着「新增需重启」 | 设置里增删改，写文件后热加载 |

三条不变：`model_settings/` 仍是参数唯一写路径；`providers.yaml` 仍是网关唯一真相（不进 DB）；每个值仍带来源。

## 2. 现状证据

| 编号 | 现象 | 位置 |
|---|---|---|
| E-1 | 会话里选模型是两级联动，且只有一个网关时下拉被禁用（`fixed = anonymous \|\| items.length <= 1`） | `web/src/workspace/model-config/ProviderSelect.tsx:27,36` |
| E-2 | 模型参数在 `<dialog>` 抽屉里，挂在输入栏齿轮上，覆盖对话、不能深链、Esc 即弃草稿（无确认） | `web/src/workspace/chat/ChatTab.tsx:420`、`model-config/Drawer.tsx:54-116` |
| E-3 | 设置页右侧一次渲染一个域的**全部**叶子键：health 21 / input 17 / memory 39 / sandbox 14 / workspace 9，合计 139 个键平铺 | `workspace/settings/SettingsTab.tsx:284-303`（实测叶子数见 §2.1） |
| E-4 | 行标签只取路径末段，直接从键名渲染：`latency_baseline_ms`、`context_buffer_ratio` | `workspace/settings/SettingsTab.tsx:95-96,161` |
| E-5 | 参数提示只有模型抽屉里的 5 条半句话（「空 = 供应商默认」），配置中心一行都没有 | `model-config/groups.ts:44-96` |
| E-6 | 网关只能读不能写：4 个端点全是 GET + 一个 refresh，没有任何 CRUD | `model_catalog/api.py:39-83` |
| E-7 | 「新增或改端口需重启」写死在只读说明里，界面无法完成这件事 | `model_settings/bounds.py:37-39` |
| E-8 | 七个 tab 平铺：会话级（对话/轨迹）、跨会话观测（指标/质量/成本/密钥）、配置（设置）三种性质挤在一行 | `workspace/Workspace.tsx:16-24` |

### 2.1 叶子键实测

```
health 21   input 17   memory 39   gateway 6
policy 10   model 23   sandbox 14  workspace 9     合计 139
```

一个 `memory` 域就是 39 行同构输入框。这是「不方便 + 丑」的根因：不是样式问题，是**没有信息层次**。

## 3. 目标与非目标

### 3.1 目标

1. 会话内只做一件事：**选模型**。列表跨网关聚合，可搜索，选中原子落 `(provider, model)`。
2. 网关可在界面增删改，写回 `providers.yaml`，保存后不重启即可见（实测后确认，见 §6.4）。
3. 模型参数的唯一入口是设置页；右侧常驻解释与影响，值仍带四层来源链。
4. 139 个键有中文名、有分组、有搜索；常用键有「改了会怎样」，其余有兜底文案且可查。
5. 一级导航按性质分三组，深链兼容旧 `?tab=` / `?domain=`。

### 3.2 非目标

- 不做按能力自动选模型 / 自动故障转移 / 多 key 轮询。
- 不做网关级权限（本地单用户，与 ADR-0002 一致）。
- 不把 `providers.yaml` 搬进 `config.db`（理由见 §6.5）。
- 不改 `/workspaces` 页面结构与后端装配逻辑。
- 不引入 UI 组件库；样式继续追加到唯一的 `web/src/styles.css`。

## 4. 信息架构

### 4.1 一级：从七个平铺改成三组

```
[对话] [轨迹]   |   [指标] [质量] [成本] [密钥]   |   [设置]
└──── 会话 ────┘       └──────── 观测 ────────┘      配置
```

- 组标签是 `<span>` 而非按钮，组间用 1px 竖线（`--color-border-tertiary`）分隔。
- 「密钥」留在观测组：它是**工具输出里捕获到的凭据清单**（`web/src/workspace/SecretsTab.tsx:1-5`），属观测而非配置，移动会改变语义。
- `工作区` 仍是 `/workspaces` 路由，保持顶栏 chip 入口不变。

`TABS` 改为带 `group` 字段，`parseTab` 的合法集合不变（7 个 key 全部保留），旧深链零改动。

### 4.2 二级：设置内部两段

```
设置
├── 模型与网关
│   ├── 网关            providers.yaml（新增）
│   └── 模型参数        抽屉迁入
└── 运行策略
    ├── 健康分口径      21 键
    ├── 输入管线        17 键
    ├── 记忆策略        39 键
    ├── 网关策略         6 键
    ├── 沙箱执行面      14 键
    ├── 工作区           9 键
    └── 确认协议        10 键（只读）
```

左树两级：段标题不可点，叶子可点。叶子右侧显示「覆盖 N」与生效时机徽标（沿用 `HOT_LABEL`）。`model` 域从「只读，去模型抽屉改」改为可进（入口即「模型参数」）。

### 4.3 深链

| 链接 | 结果 |
|---|---|
| `?tab=settings&section=model` | 设置 / 模型参数 |
| `?tab=settings&section=providers` | 设置 / 网关 |
| `?tab=settings&section=health` | 设置 / 健康分口径（旧 `&domain=health` 同时接受，`domain` 优先映射到 `section`） |
| `?tab=settings&section=model&field=top_p` | 展开「采样」组、滚动定位、右栏切到该字段 |
| `?tab=secrets` | 行为不变 |

`section` 缺省取 `model`（最高频），不再默认 `health`。

## 5. 会话内模型选择器

### 5.1 交互

输入栏当前是 `PermissionPicker` + 齿轮 + 搜索开关。齿轮换成 **模型 chip**：

```
[模型：qwen3-max · 128k]  [自动批准 ▾]  [联网]
```

点击 chip 打开 popover（`popover` 属性 + 锚定定位，非 `<dialog>`；Esc/外点关闭），内容是**单一模型列表**：

- 按网关分组，组头 sticky：`百炼 · 3 个模型可用`，右侧显示该组状态（`缺 key` / `未取到：401` / `缓存 12 分钟前`）。
- 每项：模型名（主）+ 窗口（次）+ 状态点（`stable` / `deprecated`）。
- 顶部搜索框，跨网关过滤（本地过滤已取到的列表，不逐键请求）。
- 列表底部两项：`刷新模型列表`（对全部网关 `refresh` 一次）、`手动输入模型名…`（走 `ModelCombobox` 的既有手填路径，网关取当前项）。
- 选中 → 一次 `PATCH /api/model-config`，body 同时含 `provider` 与 `model`，作用域固定 `session`。
- 底部一行链接：`模型参数…` → `?tab=settings&section=model`。

**会话界面不出现「网关」这个词**，只在错误态出现（「2 个网关的模型没取到：dashscope 401」）。用户选的是模型，网关是实现细节。

### 5.2 聚合端点

新增 `GET /api/models?q=&provider=&limit=`：

```jsonc
{
  "items": [{
    "provider": "dashscope", "provider_label": "百炼",
    "model": "qwen3-max", "context": 262144,
    "status": "stable", "capabilities": ["chat"], "source": "discovered"
  }],
  "providers": [{
    "id": "dashscope", "label": "百炼", "code": "ok",
    "total": 53, "stale": false, "missing_key": false
  }],
  "default": {"provider": "dashscope", "model": "qwen3-max"}
}
```

实现要点：

- 逐网关 `asyncio.gather(return_exceptions=True)`，每项 `asyncio.timeout(3.0)`；单项失败只写进 `providers[].code`，不拖垮整体。
- **全部失败才 503**。空列表会被读成「没有模型」，与「读不到」必须分开（沿用 `providers_invalid` 的既有立场）。
- 复用 `catalog.py` 的缓存；`refresh=true` 参数才绕过。
- 结果上限 `limit`（默认 300，硬顶 500），超限时回 `truncated: true` 而不是静默截断。

前端 `useCatalog` 扩展出 `loadAll()`，换网关时不再需要 `resetModels()`——列表本身是跨网关的。

### 5.3 复合键不变式

`provider` 与 `model` 是**一个键**，不是两个字段。服务端在 `PATCH /api/model-config` 加一条校验：

| 请求 | 结果 |
|---|---|
| 只给 `model` | 沿用当前 provider；模型不在其发现列表也允许（手填是合法用法） |
| 只给 `provider` | 当前 model 在**新** provider 的发现列表里 → 保留；否则 400 `{code:"model_not_in_provider", provider, model, available:[…前 20]}` |
| 同时给两者 | 不校验存在性（手填合法），仅校验 `provider` 在注册表里 |

理由：现在的两级下拉允许「换了网关、模型名还指向旧家」，实际表现是请求发到一个不认识的模型名，报 404 或 400，用户看不出是配置错了。`available` 让前端能直接把候选列出来让他改。

## 6. 网关管理

### 6.1 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/providers` | 已有。增加 `version`（文件摘要）与 `kinds` |
| POST | `/api/providers` | 新增，201 |
| PATCH | `/api/providers/{id}` | 改（`id` 不可改），`expected_version` 必填 |
| DELETE | `/api/providers/{id}?reassign_to=` | 删；删的是默认网关时必须带 `reassign_to` |
| POST | `/api/providers/{id}/check` | 探活：拉一次 `/models`，回 `{code, total, latency_ms}`，不写文件 |

请求体：

```jsonc
{
  "expected_version": "1f3a9c2b7e04",
  "provider": {
    "id": "vllm-local", "kind": "openai_chat", "label": "内网 vLLM",
    "base_url": "http://127.0.0.1:8000/v1",
    "api_key_ref": "VLLM_API_KEY",
    "discover": "models"
  },
  "default": "vllm-local"
}
```

`api_key_ref` 只接受**变量名**或 `none`；服务端拼成 `env:NAME` 写盘。

### 6.2 校验与安全边界

1. `api_key_ref` 匹配 `^(sk-|ghp_|gho_|AKIA|xox[baprs]-)` 或长度 > 64 且无 `_` → **400 `plaintext_key`**，文案：「这里填环境变量名，不要粘贴密钥」。这是本次唯一硬拦项，不做「警告后放行」。
2. 复用 `registry.check_url`：明文 http 只允许回环，否则 400（既有规则，不放松）。
3. `id` 唯一、`^[a-z0-9][a-z0-9-]{1,30}$`；`default` 必须存在于 providers 或为空。
4. `kind` 必须命中 `kinds.py` 的协议表。
5. 校验全部在**写盘前**用 `parse_provider` 再跑一遍——注册表解析是唯一裁判，避免「API 校验过、加载器炸」。

### 6.3 写入与热加载

写流程（`model_catalog/admin.py`）：

1. 读全文 → `yaml.safe_load`（失败 500，不覆盖）。
2. 比对 `expected_version`（`sha256(summary)` 前 12 位）→ 不一致 409，回当前内容摘要与 `providers` 列表。
3. 改 `providers` / `default` 后 `yaml.safe_dump(allow_unicode=True, sort_keys=False)`，前置固定头部注释。
4. 临时文件 + `os.replace` 原子替换；失败抛 503，原文件不动。
5. `registry.invalidate()` + `catalog.reset()`。

**注释会丢**（`safe_dump` 不保留手写注释）。接受这一代价：写盘时统一重建 `config/providers.yaml` 现有的那段头部说明，并在编辑页明确写一句「保存后文件将按模板重排，手写注释会丢失」。用 `ruamel.yaml` 保注释要新增依赖，为一个注释不值得。

### 6.4 是否需要重启

`load_registry()` 的缓存键含 `mtime`（`registry.py:273-296`），`invalidate()` 可直接清；模型实例缓存键含 `provider`（SPEC-0018 A40）。因此**理论上新增网关后无需重启**。

这条必须**实测验收**（A-12）后再写文案。若某个装配期快照（`config.load_*` 的 `@cache`）挡住了，就在保存成功的提示里如实写「新增网关需重启进程」，不假装热更新。

### 6.5 为什么仍写文件而不是 `config.db`

`providers.yaml` 里存的是**凭据引用**（`env:NAME`），这份文件要能进 code review、要能在没有数据库的环境（CI、`MODE=file`）独立工作、要被 `model_catalog` 在装配期直接读。塞进 SQLite 会让「服务的模型来源」依赖一个运行态二进制文件，与 ADR-0005 §1 对 `gates.yaml` 的处置同一理由。界面只做**带校验的写入口**，不改变真相归属。

## 7. 模型参数页

### 7.1 三栏

```
┌ 分组 ──┬ 表单 ─────────────────────┬ 说明 ──────────────┐
│ 连接   │ 网关     [vllm-local ▾]    │ 上下文窗口          │
│ 采样   │ 模型名   [qwen3-max   ]    │ 是什么：一次请求能… │
│ 执行   │ 窗口跟随 [跟随模型能力 ▾]  │ 改了会：调大更贵…   │
│ 压缩   │ 上下文窗口 [262144 ]       │ 默认 128000         │
│ 高级   │ …                          │ 来源：能力卡 ▸      │
└────────┴────────────────────────────┴────────────────────┘
                                   [未保存 2 项] [放弃] [保存]
```

- 左栏：`GROUPS` 五组，每组右侧显示「已改 N」。点击滚动定位（复用 `highlight` 机制）。
- 中栏：复用 `FieldRow` / `ModelCombobox` / `NumInput`，一行两列栅格（标签+单位 | 控件+来源徽标），hint 只在行内留一句，长解释进右栏。
- 右栏：`position: sticky`，显示聚焦字段的 `ParamDoc` + 实时影响 + 四层来源链（原 `mcd-chain`）。窄屏（<1100px）折叠为行内展开。
- 底部：`position: sticky` 操作条，替代现在埋在长表单末尾的按钮。

### 7.2 保存范围

顶部一个二选一分段控件，替代现在「主按钮全局 + 次按钮会话」的隐藏语义：

```
[本次会话] [全局默认]       落点：config/model.local.yaml
```

- 选「本次会话」时页面顶部常驻警示条：`仅当前会话生效，换会话即回到全局默认`。
- 默认选「全局默认」（与现实现一致：实测 30 个会话只有 2 条会话覆盖）。
- 切换 scope 时草稿**保留**但重新计算脏值与版本（会话层与文件层版本不同）。

### 7.3 抽屉下线

- 删除 `web/src/workspace/model-config/Drawer.tsx`、`DrawerFooter.tsx`、`GroupsNav.tsx`（共 655 行）。
- 复用：`FieldRow`、`ModelCombobox`、`NumInput`、`capability.ts`、`validation.tsx`、`groups.ts`、`useCatalog` → 移到 `web/src/workspace/settings/model/`。
- `ChatTab.tsx:420` 的齿轮改为 `Link` 到 `?tab=settings&section=model`，`aria-label="模型参数"`，旁边保留 §5.1 的模型 chip。
- 删除 `web/src/workspace/ModelConfigDrawer.test.tsx`（21 条用例、55 处断言）与 `settings/SettingsTab.test.tsx`（16 处断言）里的相关部分，等量迁到新页面测试。不许「删测试降覆盖」：A-8 / A-9 / A-11 / A-14 必须在新文件中存在，迁移前先列出旧用例 → 新用例的对照表。

### 7.4 预览端点

新增 `POST /api/model-config/preview`：不落库，回 `{effective, sources, derived, warnings, errors}`。

- 前端 300ms debounce 调用，用返回值渲染右栏「改了会怎样」与字段级 400 文案。
- 复用 `resolve` + `payload.derived`，**口径仍只在后端**（前端不重算 `worst_case_attempts`）。这是「改动影响」能说具体数字的前提。

## 8. 参数引导层

### 8.1 文案四段式

```ts
// web/src/workspace/settings/docs.ts
export interface ParamDoc {
  title: string;                          // 中文名：上下文窗口
  what: string;                           // 它是什么（一句话）
  impact: string;                         // 改了会怎样（带方向）
  unit?: string;                          // token / 次 / 比例
  risk?: 'low' | 'mid' | 'high';
}
```

示例（`context_size`）：

> **是什么**：一次请求里模型能看见的最大 token 数，含历史、工具输出与本轮输入。
> **改了会怎样**：调大 → 装得下更多历史，但更贵更慢；调小 → 更早触发压缩，长任务会被截断。开着「窗口跟随」时此值会被模型能力覆盖。
> **风险**：高。

### 8.2 影响实时化

「改动影响」不写空话，直接用 `payload.derived` 的数字：

| 用户改的字段 | 右栏实时文案 | 数据来源 |
|---|---|---|
| `max_iters` / `model_max_retries` / `agent_max_retries` | 「最坏情况下一次对话发 **N** 次模型请求」 | `derived.worst_case_attempts` |
| `trigger_ratio` | 「约占 **7 万** token 时开始压缩」 | `derived.compression_threshold` |
| `context_size` | 「128000 未校准，建议按模型能力核对」/「该数字可能已过期」 | `derived.context_uncalibrated` / `context_card_may_be_stale` |
| `model` | 能力卡：窗口上限、输出上限、模态、生命周期 | `catalog.current` |
| 任意 | 「保存后第 2 轮生效」/「需重启」 | 域 `hot` |

### 8.3 覆盖度与兜底

- **P0 必写**：`model` 域 16 个可编辑字段（用户直接面对的）。
- **P1**：其余 7 个域共 116 个叶子键里，按「出过事故 / 最常改」补 12 个（例：`tool_output_chars`、`max_turns`、`latency_exponent`、`trigger_ratio` 同族）。
- **兜底**：未写文档的键渲染成 `{原键名} · {类型} · 范围 {min–max} · 当前来源 {source}`，不空白、不报错。
- 钉子：`docs.test.ts` 断言 `GROUPS` 每个 key 都命中 `PARAM_DOCS`；`fallback-allowlist.json` 记录尚未写文档的 key，**只许减**（与 `size-baseline.json` 同一棘轮思路）。

### 8.4 首次引导与预设

1. **三步轻量引导**（无库，纯 CSS 定位 + `focus`/`outline`）：① 换模型在输入栏，配置在设置 ② 来源徽标怎么读 ③ 保存范围（会话 / 全局）。`localStorage: cfg.tour.v1`，可「跳过 / 不再提示」。
2. **场景预设**（引导里最有效的一招）：三个按钮，点了把一组值填进**草稿**，仍需手动保存：

| 预设 | 填什么 | 一句话 |
|---|---|---|
| 省钱 | `max_iters=4`、两个重试=0、`context_buffer_ratio=0.1` | 少试几次、不重试、更早压缩 |
| 长文 | `context_auto=true`、`trigger_ratio=0.85` | 尽量塞满窗口再压缩 |
| 严格 | `temperature=0`、`parallel_tool_calls=false` | 输出更确定、工具串行 |

3. 预设不动 `provider` / `model`——模型是用户显式选的（SPEC-0018 §3.2）。

## 9. 视觉与交互规范

| 现状 | 问题 | 改法 |
|---|---|---|
| 标签是裸键名 | 不认识 | 中文名（14px/500）+ 原键名（12px mono/次要色） |
| 139 个键一次全渲染 | 滚到底也不知道改了什么 | 折叠分组 + 组内搜索 + 左树锚点 + 只显示已改的过滤开关 |
| 输入框与背景同色、1px 淡边 | 看不出可编辑 | `border-secondary` + 2px focus ring；只读用无边框文本 + 「只读」徽标 |
| 来源是 4 色小方块 | 要记颜色 | 文字徽标（代码默认 / yaml 基线 / 配置库 / 环境变量）+ 点开链路 |
| 保存埋在长表单末尾 | 改完找不到 | sticky 操作条 + 设置 tab 脏点 + 离开前 confirm |
| 右侧无解释 | 不敢动 | 三栏，右栏 sticky 说明 |
| 错误只有一行红字 | 像页面坏了 | 骨架屏 + 「重试」按钮 + 保留上次成功数据（stale-while-error） |
| 13px 灰字 | 对比不足 | 正文 13px/1.6；次要文字对比度 ≥ 4.5:1 |
| 抽屉与设置两处讲同一件事 | 口径分裂 | 抽屉下线，唯一入口在设置 |

保留现有 CSS 变量与 `mc-*` / `cfg-*` 命名前缀；新增类进 `styles.css` 对应段，不新建样式文件（项目只有一张样式表）。

## 10. 验收

| 编号 | 用例 | 断言 |
|---|---|---|
| A-1 | 3 个网关、共 9 个模型 | chip 打开后渲染 9 项，按网关分 3 组；搜索 `qwen` 只剩 3 项 |
| A-2 | 选中一项 | 只发一次 `PATCH /api/model-config`，body 同时含 `provider` 与 `model`，`version` 取自当前快照 |
| A-3 | 只改 `provider` 且新网关无该模型 | 400 `model_not_in_provider`，UI 回退到原选择并列出 `available` |
| A-4 | 新增网关 | `providers.yaml` 内容出现该 id；`default` 未变；`registry.invalidate` 被调用（用假文件断言） |
| A-5 | `api_key_ref = "sk-abcdef…"` | 400 `plaintext_key`，文件字节不变 |
| A-6 | 两个 PATCH 用同一 `expected_version` | 后到者 409，回当前 `providers` |
| A-7 | 删除默认网关且未带 `reassign_to` | 400，文件不变 |
| A-8 | `?tab=settings&section=model&field=top_p` | 展开「采样」组、`top_p` 高亮、右栏为 `top_p` 文档 |
| A-9 | `?tab=settings&domain=health`（旧链接） | 落到「健康分口径」，不 404 |
| A-10 | `?tab=secrets` | 渲染行为与改前一致 |
| A-11 | 草稿 `max_iters=5` | preview 返回 `worst_case_attempts=12`，右栏文案含「12」 |
| A-12 | 新增网关后不重启 | `GET /api/models` 出现新网关的模型；若实测不成立，改为「保存提示需重启」并调整本行 |
| A-13 | 全部网关不可达 | `GET /api/models` 回 503（不是空数组），前端显示「读不到」而非「没有模型」 |
| A-14 | 文档覆盖 | `GROUPS` 每个 key 命中 `PARAM_DOCS`；allowlist 只减不增 |
| A-15 | 首次引导 | 首次出现，点「不再提示」后 `localStorage` 置位，刷新不再出现 |
| A-16 | 未保存离开 | 切换 section 或 tab 时出现离开确认，取消则留在原页 |
| A-17 | 只读域 | `policy` 域控件全部 disabled，且显示「只能走 git 评审」 |
| A-18 | 键盘可达 | 所有控件有可访问名；设置左树可用 Tab 遍历、Enter 选中；无 focus trap 残留 |

## 11. 实施顺序与文件预算

| 阶段 | 内容 | 主要文件 | 预算（行） |
|---|---|---|---|
| P0 | IA 分组 + 设置二级导航 + 模型参数页（迁移，无新能力） | `Workspace.tsx`、`settings/{SettingsTab,settings.css}`、`settings/model/*` | +420 −655 |
| P1 | 网关 CRUD + 写 yaml + 摘要版本 | `model_catalog/{admin,registry}.py`、`api.py` | +260 |
| P2 | 网关管理 UI（列表 / 表单 / 探活） | `settings/providers/*` | +380 |
| P3 | 聚合模型端点 + 会话 ModelPicker | `model_catalog/aggregate.py`、`chat/ModelPicker.tsx` | +230 + 260 |
| P4 | preview 端点 + 引导层（文案 / 影响 / 首次 / 预设） | `model_api.py`、`settings/docs.ts`、`settings/tour.tsx` | +90 + 340 |
| P5 | 视觉收口 + 测试补齐 | `styles.css`、`*.test.tsx` | +200 |

单文件上限 500 行不变。`SettingsTab.tsx` 现 385 行，P0 必须按「域树 / 域编辑器 / 字段行」拆成三个文件，不能继续堆。

## 12. 风险与回滚

| 风险 | 表现 | 缓解 |
|---|---|---|
| 保存网关丢注释 | 手写的说明被 `safe_dump` 抹掉 | 统一重建模板头 + 页面明示；不接受引入 `ruamel.yaml` |
| 热加载不彻底 | 新增网关后模型列表仍为空 | A-12 先实测；不成立就如实提示重启，不假装 |
| 聚合端点变慢 | N 个网关 × 3s | 并发 + 逐项超时 + 缓存优先；`limit` 硬顶 |
| 139 键文案工作量大 | 一半是兜底 | 分期（16 + 12）+ 兜底 + allowlist 棘轮 |
| 抽屉下线打断肌肉记忆 | 用户找不到保存 | 输入栏模型 chip + 齿轮改为「模型参数」链接，一步到位 |
| 明文 key 落盘 | 凭据进 git | 硬拦（A-5）+ 日志不回显请求体 |
| 旧深链失效 | 收藏的设置页 404 | `parseTab` 合法集合不变，`domain` 继续接受（A-9/A-10） |

回滚：后端 5 个端点纯新增，不碰旧路径；前端可整块 revert；`providers.yaml` 写入走临时文件 + `os.replace`，失败不破坏原文件；参数写入路径未改，`model_settings/` 无需回滚。

## 13. 实施前置

按 AGENTS.md §3.8，任何 `web/**` 改动开工前必须先跑 `modern-web-guidance` 检索并按其指南落地，本方案涉及的主题至少包括：`popover`（模型 chip 的轻层）、`<dialog>` 移除后的焦点归还、`:has()`（行内错误态）、container queries（三栏在窄屏的折叠）。交付回复需附 `### Modern Web Guidance` 段逐条披露。


## 14. 实施记录（2026-11-28）

### 14.1 实测数字

| 项 | 结果 | 判定 |
|---|---|---|
| 后端测试 | `1141 passed, 1 skipped`；总覆盖率 **89%**（`check_gates.py` 口径 91.5% ≥ 80） | 过 |
| 前端测试 | 43 个文件 / **483 条**全绿；语句覆盖 **81.3%**（门禁 65） | 过 |
| 圈复杂度 | `radon cc -n C` 无输出（拆完 `aggregate` 20→8、`admin.delete` 15→6、`admin._clean` 14→9） | 过 |
| 单文件行数 | `check_file_size.py` ok（`model_api.py 440`、`ChatTab.tsx 499`） | 过 |
| A-12 免重启 | `test_provider_admin.py::test_create_writes_file_and_is_visible_without_restart`：同一进程内新建网关后 `get_catalog()` 立刻可见 → `reset_catalog()` 生效 | **成立**，文案保持「保存后立即读到，运行中的会话下次装配生效」 |
| A-5 明文 key | 前端先提示、后端 `400 plaintext_key`，且断言文件字节不变 | 过 |
| A-14 文案棘轮 | `GROUPS` 16 键全命中 `PARAM_DOCS`；`fallback-allowlist.json` 记 101 个未写键，`budget === keys.length` | 过 |

### 14.2 与方案的偏差（都是实现时才发现的）

1. **分区只认 URL**。原方案让 `Workspace` 把 `?domain=` 折算成 `initialSection` 传下来，结果是「点左树有没有反应」取决于父组件有没有跟着重渲染。改为 `SettingsTab` 自己读 `section`／`domain` 参数，`initialSection` 只用于首帧。
2. **运行策略的文案键要带域前缀**。`DomainEditor` 原来用 `docFor(path)` 查表，而表里记的是 `health.latency_baseline_ms`；不修就是「写了 12 段文案一条也用不上」。现在传 `docKey = 域.路径`。
3. **`popover` 属性必须做能力检测**。`[popover]` 的 UA 样式是 `display:none`，`:popover-open` 匹配不上时面板永远隐形 —— 旧代码的 `data-open` 兜底救不回来。改为渲染前判断 `showPopover` 是否存在，不支持就不挂属性（jsdom 与老浏览器都走这条路）。
4. **危险确认闸门保留**。旧抽屉要求「换模型/改窗口/放大重试」先勾「我了解上面的影响」，方案 §7 没写这条。迁移时补回（`mp-danger` 块 + `boundError` 本地拦截），否则是一次安全降级。
5. **面板角色**：`role="listbox"` 里塞搜索框不合法，面板改 `role="dialog"`、列表单独 `role="listbox"`。
6. **`model_api.py` 拆出 `model_catalog_view.py`**：`capability_of / registry_view / current_view / catalog_block / soft_warnings` 是纯视图（不读库、不写文件、不抛 400），拆出去后 505 → 440 行且无循环依赖。
7. **`GearIcon` 移到 `chat/icons.tsx`**：`ChatTab.tsx` 496 → 514 行触顶，图标本就和其它图标同类。

### 14.3 已知缺口（不假装已解决）

- **会话覆盖只能整体清空**：单键「撤销」按钮只在草稿里改过该键时出现，所以「删掉本会话某一条已有覆盖」目前只能先改一次再重置，或点「清空本会话覆盖」。要彻底解决需给 `FieldRow` 加 `overridden` 语义（本次范围外）。
- **跨进程热加载未验证**：A-12 只在同进程内断言（单进程部署成立）。多 worker 部署下另一个进程仍要等自己的 `providers.yaml` mtime 检查，页面文案已写「运行中的会话下次装配生效」，不承诺跨进程立刻可见。
- **`providers.yaml` 手写注释会丢**：保存按模板重建文件头，页面已明示；不引入 `ruamel.yaml`。
