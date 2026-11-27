# SPEC-0018：多网关多模型接入与能力发现

- 状态：待实现
- 日期：2026-11-27
- 基线：`main 27cc2be`
- 关系：扩展 SPEC-0001（模型参数配置）与 ADR-0003；复用 AgentScope 的 `ModelCard` / `CredentialFactory`；沿用 SPEC-0017 §1.5「显式声明替代巧合」
- 范围：`model_catalog/`（新）、`model_settings/{ports,bounds,resolve,payload,params}.py`、`runtime/{chat_model,scope_runtime,assembly}.py`、`model_api.py`、`gateway/routes/health.py`、`config_schema.py`、`config/{providers,models}.yaml`（新）、`web/src/workspace/model-config/**`、`web/src/types/model.ts`、`tests/unit/test_model_catalog*.py`（新）
- 触发来源：单一 `DEVOPS_AGENT_BASE_URL` + 单一 `model.name` 的配置形状无法支撑「接不同网关的模型」，且模型能力（窗口/模态/参数范围）全靠手填与猜测

## 1. 设计结论

1. **要解决的不是「多接几个模型」，是「换个模型时窗口、模态、参数范围自动跟上」**。今天的形状是 `context_size: 128000` + `context_calibrated: false`（`config/model.yaml:19-20`）——后者只是诚实地标注「这是猜的」，没有任何修正机制。换到窗口 1M 的模型仍按 128k 压缩、按 128k 画前端圆环。**能力信息必须成为派生值，而不是又一个要人手填的输入。**

2. **能力信息是四源逐字段合并，不存在「一个接口给全」**。`GET /models` 只保证 `id`，上下文窗口字段各家不同名（vLLM `max_model_len`、OpenRouter `context_length`、Groq `context_window`、Gemini `inputTokenLimit`），**而参数 schema 只有内置模型卡才有**。所以按字段决定来源，每个字段带来源标签，而不是整块覆盖。

3. **匹配用「最长前缀」，不是「任一前缀」**。实测：内置卡有 `gpt-4.1-mini` 与 `gpt-4.1`，网关返回的 `gpt-4.1-mini-2025-04-14` 同时是两者的前缀。**当前这两张卡的 `context_size` / `output_size` 恰好相同（均 1M / 32768），所以今天取错不会改掉窗口** —— 但卡集随框架升级变化，一旦兄弟模型能力分离，静默取错就会改掉压缩阈值与 `max_tokens` 上限，而且没人能看出是哪一步错的。所以按最长前缀取，并记录 `matched_by` 与 `matched_card`：**这条是防御性规则，不是在修一个现有 bug**（§2 有实测数据）。

4. **匹配不上就不猜**。`qwen-max-latest`、`qwen3-max-2025-09-23` 在内置卡里完全不存在。此时回落到「未校准」并提示校准，**禁止**按前缀模糊到 `qwen-plus` —— 错的能力比没有能力更糟，前者静默出错，后者只会显示「未校准」。

5. **发现失败不动对话路径**。`/models` 只在设置面板与健康检查时触发，永不进对话链路。三条降级路径：旧缓存（标 stale）→ 内置卡 → 未校准。任何一种情况下对话都能继续。

6. **凭据仍只在环境变量，注册表只存变量名**。`api_key: env:DEVOPS_AGENT_API_KEY` 这种写法让「一个进程接多个网关」成为可能，同时保住 `bounds.py:32` 已确立的「凭据不经 HTTP 往返」。响应体里只回 `key_fingerprint`（sha256 前 8 位），它能区分「这次用 A key 还是 B key」而不泄露值。

7. **零配置升级必须逐字段等价**。没有 `config/providers.yaml` 时合成一个 `default` provider（取 `DEVOPS_AGENT_BASE_URL` / `DEVOPS_AGENT_API_KEY` / `DEVOPS_AGENT_MODEL`），`resolve()` 的**既有字段**输出与今天完全相同（A35）。这是硬约束：现有部署与全部测试不许因此改动。

8. **模型实例按 wire 指纹缓存，但 `provider` 必须进 `MODEL_WIRE_KEYS`**。切 provider 意味着换 base_url 与凭据，不重建对象就会把 A 网关的 key 发到 B 网关。反之，参数没变时重复构建会重建 HTTP client，连接池白费。

9. **明文 http 只允许回环**。多网关最容易犯的错是把 key 发到 `http://` 公网地址；`base_url` 校验拒绝非 loopback 的 http，也拒绝带 userinfo 的 URL。

10. **不做网关聚合器**。不引 litellm / one-api：这里需要的是「多 base_url + 多模型名 + 能力元信息」，框架已有 11 个 provider 类与 108 张模型卡；再叠一层代理会把凭据、重试、流式、错误语义全部揽进来，收益不抵复杂度。

## 2. 现状证据（改动前）

| 事实 | 位置 | 后果 |
|---|---|---|
| 凭据与地址来自两个固定 env | `runtime/chat_model.py:86-99` | 一个进程只能接一个网关；`missing` 列表写死三个变量名 |
| 模型名是单值字符串 | `config_schema.py:227`、`config/model.yaml:13` | 无「供应商」维度，同名模型分不清来源 |
| 窗口手写 + 校准标志 | `config/model.yaml:19-20` | 换模型不同步窗口；`context_calibrated` 只标注不修正 |
| 明确承认没有模型列表接口 | `model_settings/bounds.py:61-63` | 模型名是自由文本，写错要等下一次调用才失败 |
| `api_key`/`base_url` 列为只读 | `model_settings/bounds.py:32-33` | 正确（凭据不下发），但也没有替代的配置入口 |
| 重建判据不含 provider | `runtime/chat_model.py:21-31` | 只比参数与模型名；将来多 provider 时会漏重建 |
| 热替换只换 model 对象 | `runtime/scope_runtime.py:189,210` | 换 provider 走同一条路，但对象里没有 provider 概念 |
| 未校准只算一个布尔 | `model_settings/payload.py:27` | `not chosen and not context_calibrated`，无法表达「来自模型卡」与「来自网关声明」 |

实测依据（本机 `.venv`，AgentScope 当前版本）：

- 内置卡规模：`OpenAIChatModel` 17 / `DashScopeChatModel` 19 / `AnthropicChatModel` 15 / `GeminiChatModel` 14 / `XAIChatModel` 11 / `MoonshotChatModel` 10 / `OllamaChatModel` 9 / `DeepSeekChatModel` 5 / `VolcengineChatModel` 5 / `MiniMaxChatModel` 3 / `OpenAIResponseModel` 17，合计 108 张。
- `ModelCard` 字段已覆盖需求：`context_size` / `output_size` / `input_types` / `output_types` / `status` / `deprecated_at` / `release_date` / **`parameter_schema`**（JSON Schema，`max_tokens.maximum` 已按 `output_size` 注入、`thinking_*`/`voice` 已按模型裁剪）。
- `ChatModelBase.list_models(custom_yaml_dir=None)` 是类方法，按 `_models/*.yaml` 加载，返回 `list[ModelCard]`。
- 各 `ChatModel.__init__` 签名同构（`credential, model, parameters, stream, max_retries, retry_delay, context_size, ...`），只有 `Credential` 子类不同；`CredentialFactory` 提供按 `type` 判别的反序列化与 `list_schemas()`。
- 匹配实证：`gpt-4.1-mini-2025-04-14` 对卡名精确匹配失败，前缀候选为 `['gpt-4.1-mini', 'gpt-4.1']`（按长度取前者）；两张卡的 `context_size` 均为 1047576、`output_size` 均为 32768、`max_tokens.maximum` 均为 32768 —— **即当前取错不改变数值**，所以 §6.3 的「最长前缀」是防御性规则。另一侧：`qwen-max-latest`、`qwen3-max-2025-09-23` 对内 108 张卡无任何匹配（不是模糊匹配的候选集内漏掉，而是确实不存在），这是「匹配不上就不猜」的直接证据。

## 3. 目标与非目标

### 3.1 目标

- 一个进程内可配置任意多个网关（同一协议可配多个实例），每个网关下可选任意多个模型。
- 通过上游 `GET /models` 自动获取可用模型列表，带缓存、可强制刷新、失败可见。
- 为每个模型给出一张能力卡：上下文窗口、输出上限、输入/输出模态、生命周期状态、以及**该模型合法的参数 schema（含范围与隐藏项）**。
- 换模型 → 窗口/压缩阈值/参数上限自动跟随；换网关 → 模型对象重建，凭据不串用。
- 上述能力在 `GET /api/model-config`、`/health` 与设置面板三处口径一致，且每个值都带来源。

### 3.2 非目标

- 不做通用 LLM 网关 / 负载均衡 / 故障转移 / 多 key 轮询 / 计费统计。
- 不做「按能力自动选模型」（路由策略）；模型是用户显式选择。
- 不主动探测模态（不发图片/音频试请求），模态只取声明值。
- 不改评测侧选型逻辑；`judge_model` 的 `provider/model` 语法列为 P2（§14）。
- 不把本服务暴露成 OpenAI 兼容网关：不提供 `/v1/models`，避免语义误导（见 §8.4）。

## 4. 供应商注册表（P1）

### 4.1 配置文件 `config/providers.yaml`

```yaml
# 多网关注册表。每一项 = 一个「用户命名的网关实例」，同一 kind 可配多个
# （两个 DashScope 账号、一个内网 vLLM + 一个云网关，都是合法用法）。
providers:
  - id: dashscope-prod            # 唯一标识，出现在 resolve() 的 provider 字段与事件里
    kind: dashscope_chat          # 协议类型，映射到框架类（§4.3）
    label: 百炼（生产）
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: env:DEVOPS_AGENT_API_KEY     # 只写变量名，不写值
    discover: models              # models | manual | off
  - id: vllm-local
    kind: openai_chat
    label: 内网 vLLM
    base_url: http://127.0.0.1:8000/v1
    api_key: env:VLLM_API_KEY
    discover: models
  - id: ollama-local
    kind: ollama_chat
    label: 本机 Ollama
    base_url: http://127.0.0.1:11434
    api_key: none                 # 显式声明无凭据，不是「忘了填」
    discover: models

# 默认选中项。省略时取 providers[0]。
default: dashscope-prod
```

字段规则：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | `^[a-z0-9][a-z0-9_-]{0,31}$`；重复即加载失败（不静默取第一个） |
| `kind` | 是 | 必须是 §4.3 表内的键；未知 kind 加载失败并列出合法值 |
| `base_url` | 是 | 见 §10.2 校验 |
| `api_key` | 是 | `env:NAME` / `none` / 直接值（直接值仅允许测试夹具，加载时 warn） |
| `discover` | 否 | 默认 `models`；`manual` = 只用手工列表；`off` = 只靠内置卡 |
| `label` | 否 | 缺省用 `id` |
| `models` | 否 | `manual` / `off` 模式下的人工模型清单（`[{name, context_size?, label?}]`） |

### 4.2 为什么凭据写 `env:NAME` 而不是值

一个进程接多个网关 → 必须有多个 key。而 `bounds.py:32` 已经把 `api_key` 定为「只读、经环境变量、不进 HTTP 往返」，这个约束不能因为多网关而放弃。`env:NAME` 同时满足两件事：注册表可以进 git（团队共享网关清单），密钥仍留在部署环境。

解析规则：

- `env:NAME` → `os.environ[NAME]`，缺失时该 provider 标 `not_configured`，**不阻止其他 provider 使用**，`/health` 报 `missing_key: ["vllm-local"]`。
- `none` → 传空串（Ollama 这类无需认证的网关显式要求）。
- 其他字面量 → 当作 key 使用，加载时 `logger.warning`（防止有人把 key 提交进仓库）。

### 4.3 `kind → (Credential, ChatModel)` 显式表

刻意写成显式 dict，不用「按名字反射」（对照 SPEC-0017 §1.5：`policy.yaml` 段名靠 `raw.get(section, raw)` 兜底被判定为「巧合正确」）：

```python
# model_catalog/kinds.py
KINDS: dict[str, KindSpec] = {
    "openai_chat":   KindSpec(OpenAIChatModel, OpenAICredential, "openai"),
    "openai_response": KindSpec(OpenAIResponseModel, OpenAICredential, "openai"),
    "dashscope_chat": KindSpec(DashScopeChatModel, DashScopeCredential, "dashscope"),
    "anthropic_chat": KindSpec(AnthropicChatModel, AnthropicCredential, "anthropic"),
    "gemini_chat":   KindSpec(GeminiChatModel, GeminiCredential, "gemini"),
    "deepseek_chat": KindSpec(DeepSeekChatModel, DeepSeekCredential, "deepseek"),
    "moonshot_chat": KindSpec(MoonshotChatModel, MoonshotCredential, "openai"),
    "xai_chat":      KindSpec(XAIChatModel, XAICredential, "openai"),
    "minimax_chat":  KindSpec(MiniMaxChatModel, MiniMaxCredential, "openai"),
    "volcengine_chat": KindSpec(VolcengineChatModel, VolcengineCredential, "openai"),
    "ollama_chat":   KindSpec(OllamaChatModel, OllamaCredential, "ollama"),
}
```

末位是**发现协议族**（§5.2），与 `kind` 分开是因为「怎么发请求」和「怎么列模型」不是同一件事：`deepseek_chat` 用 OpenAI 兼容请求形状，但发现协议也走 `openai` 族；`gemini_chat` 两者都不同。

`Credential` 构造按字段名对齐（各子类字段已在 §2 核实）：

```python
OPENAI_FAMILY = {"openai", "dashscope", "deepseek", "moonshot", "xai", "volcengine", "minimax"}
# base_url 字段名差异：Ollama 用 host，Gemini 无 base_url
fields = {"api_key": key} if family == "gemini" else {"api_key": key, "base_url": url}
if family == "ollama": fields = {"host": url}
```

### 4.4 匿名 `default` provider（零配置兼容）

`config/providers.yaml` 不存在时，注册表合成：

```python
ProviderSpec(
    id="default", kind=_guess_kind(), label="默认网关",
    base_url=os.environ.get("DEVOPS_AGENT_BASE_URL", ""),
    api_key="env:DEVOPS_AGENT_API_KEY", discover="off",
)
```

`_guess_kind()`：显式 env `DEVOPS_AGENT_PROVIDER_KIND` > 由 `base_url` 主机名推断（含 `dashscope`/`aliyuncs` → `dashscope_chat`，含 `anthropic` → `anthropic_chat`，含 `generativelanguage` → `gemini_chat`，含 `11434` → `ollama_chat`，其余 `openai_chat`）。

`discover="off"` 是刻意的：匿名 provider 说明用户没做任何新配置，此时不应因多打一次 `/models` 而引入新的失败面。要自动发现的人会写 `providers.yaml`。

**匿名 provider 下 `context_auto` 默认 `false`**（§6.4 的另一半）。若默认 `true`，未写 `providers.yaml` 的部署会因为「模型名恰好命中内置卡」而突然换掉窗口，等于用一次升级改掉了运行参数 —— 这种「靠匹配不上才不出事」的等价性是巧合，不是设计。所以：**没有 `providers.yaml` = 今天的静态窗口；写了 `providers.yaml` = 能力自动跟随。**

**等价性要求（A35）**：此路径下 `resolve()` 的**改动前已有字段**（`model` / `context_size` / 采样参数 / 重试 / 比例 / `max_iters`）的值与 `sources`，必须与改动前逐项相同（含 `Source.YAML` / `Source.ENV` 的归属）。新增字段（`provider` / `context_auto`）单列断言，不计入等价性比较 —— 否则「新增一个键」会被误判成回归。

## 5. 模型发现（P2）

### 5.1 触发点

只有三处：设置面板打开（`GET /api/providers/{id}/models`）、用户点刷新（`POST .../refresh`）、`/health` 的定时自检（默认关闭，`catalog.health_check: false`）。**对话链路、装配链路、压缩链路都不触发发现**（结论 5）。

### 5.2 协议族适配表

| family | 方法 / 路径 | 认证 | 备注 |
|---|---|---|---|
| `openai` | `GET {base_url}/models` | `Authorization: Bearer` | OpenAI 兼容；vLLM / SGLang / one-api / new-api 同形 |
| `dashscope` | `GET {base_url}/models` | `Authorization: Bearer` | 按 OpenAI 兼容处理；原生模式（非 compatible-mode）不支持 |
| `anthropic` | `GET {base_url}/v1/models` | `x-api-key` + `anthropic-version: 2023-06-01` | 返回 `data[].id` / `display_name` / `created_at` |
| `gemini` | `GET {base_url}/models?key={key}` | query | 返回 `models[].name`（`models/gemini-…`，**必须剥前缀**）/ `inputTokenLimit` / `outputTokenLimit` / `supportedGenerationMethods` |
| `ollama` | `GET {host}/api/tags` | 无 | 返回 `models[].name`；上下文窗口需 `/api/show`，**本期取不到**（标 `unknown`） |

**证据等级**：本表来自各供应商公开文档，**本仓库尚未对任何真实网关实测**（写 spec 时无凭据）。所以实现时一律按 `openai` 族作为默认路径，并遵守两条纪律：

1. 首个真实网关接入后，把实测结论（路径、认证头、响应字段）回填本节，并注明「已实测 + 日期」。
2. 未实测的分支（`volcengine_chat` 的 Ark 前缀、`minimax_chat`、`gemini` 的 `?key=` 是否仍有效）在代码里走「失败即降级」路径，不写成断言——按 §5.6，任何一条走不通都只落 `unsupported` / `malformed`，不会让面板空白或抛异常。

这条纪律的理由：把文档当实测是「编造证据」，而这个仓库的现状证据一律要求 `路径:行号` 或可复现命令。

### 5.3 归一化

上游形状差异全部收在这里，对外只有一个形状：

```json
{
  "id": "qwen3-max",
  "label": "qwen3-max",
  "created_at": null,
  "context_size": null,
  "output_size": null,
  "input_types": [],
  "status": "implicitly_active",
  "origin": "openai"
}
```

窗口字段的同义映射表（**新增网关时只改这一张表**）：

| 来源 | 字段路径 |
|---|---|
| vLLM | `max_model_len` |
| OpenRouter | `context_length`，回退 `top_provider.context_length` |
| Together / Fireworks | `context_length` |
| Groq | `context_window` |
| Mistral | `max_context_length` |
| Gemini | `inputTokenLimit`（输出：`outputTokenLimit`） |
| 其它 | 无（`null`） |

模态映射：OpenRouter `architecture.input_modalities`（`text`/`image`/`file` → `text/plain`/`image/*`/`application/pdf`）；Gemini `supportedGenerationMethods` 只用于判定是否 chat 模型，不映射模态。

`status` 一律记 `implicitly_active`（发现接口不保证生命周期），真正的状态以模型卡为准（§6.2）。

### 5.4 发现模式（`discover`）

`manual` 与 `off` 都不发请求，区别在「有没有人工清单」：

| 模式 | 请求上游 | 列表来源 | 用途 |
|---|---|---|---|
| `models` | 是 | `/models` ∪ 人工清单 | 默认 |
| `manual` | 否 | 仅人工清单 | 网关不支持 `/models`（如 `volcengine` 待实测）或运维要限定可选范围 |
| `off` | 否 | 内置卡中该 kind 的全部卡名 | 纯离线 |

`manual` 的清单项同样进 §6 的合并（`context_size` 可省，省了就走卡/校准），所以「手列模型名」与「自动发现」拿到的是同一张能力卡，只是 id 来源不同。

### 5.5 缓存

| 项 | 值 | 理由 |
|---|---|---|
| 成功 TTL | 300s | 面板反复开关不该重复打上游 |
| 失败负缓存 | 30s | 401 连点刷新不该连打 30 次 |
| `unsupported` 缓存 | 3600s | 该 kind 没有 `/models` 是稳定事实 |
| 连接超时 | 2s | 内网网关通常 <100ms；2s 已是异常 |
| 读超时 | 3s | 模型列表在慢网关上可达 2s+ |
| 并发上限 | 4 | 面板首屏并发拉多个 provider |
| 条件请求 | `ETag` / `If-None-Match` | OpenRouter 支持；304 时刷新 `fetched_at` 不动数据 |

缓存键：`(provider_id, base_url, key_fingerprint)` —— 换 key 必须重取（不同 key 可见模型集可能不同）。

落盘：`workspace/model_catalog/<provider_id>.json`（gitignore），让进程重启后首屏立刻有列表，标 `stale: true` 直到首次刷新成功。**只落 `id/label/created_at`，不落窗口与模态**（那些走 §6 合并，落盘会产生第二份真相）。

### 5.6 失败语义矩阵

| 情况 | `code` | 行为 |
|---|---|---|
| 无 `base_url` 或 `env:` 变量缺失 | `not_configured` | 不发请求；回卡；`/health` 列出 `missing_key` |
| 401 / 403 | `unauthorized` | 不重试；负缓存 30s；回卡；面板红字「凭据无效或无权访问该网关」 |
| 404 | `unsupported` | 回卡；缓存 1h；不再请求 |
| 429 | `rate_limited` | 退避 30s；有旧缓存则返回并标 `stale` |
| 5xx / 超时 / 连接失败 | `unavailable` | 回旧缓存（`stale`）；无缓存回卡 |
| 200 但 `data` 为空 | `empty` | 回卡；提示「网关未返回任何模型」 |
| 200 但形状不认识 | `malformed` | 回卡；`warning` 日志带**前 200 字符脱敏后内容** |

所有分支的共同要求：`discover()` **永不抛异常**，返回 `DiscoveryResult(items, code, fetched_at, stale, http_status)`。调用方按 `code` 决定文案，不靠猜 `None`。

## 6. 能力合并（P3）

### 6.1 四源

| 源 | 代号 | 内容 | 权威性 |
|---|---|---|---|
| 本地校准 | `calibrated` | `config/models.yaml`（提交）+ `models.local.yaml`（gitignore，面板写入） | 人填的，最高 |
| 网关声明 | `discovered` | §5 的 `context_size` / `output_size` | 贴近「这个部署实例」 |
| 内置模型卡 | `card` | AgentScope `_models/*.yaml` | 真值声明，但可能滞后于新模型 |
| 内置默认 | `default` | `context_size=128000` | 兜底，标未校准 |

为什么校准排在最前：`qwen-max-latest` 这类名字在卡里不存在（§2 实测），网关又常返回 `null`，此时唯一可行的是人填一次然后团队共享。把校准放最前也让「我认为卡是错的」有表达出口，而不需要改代码。

### 6.2 逐字段合并

| 字段 | 优先级（高 → 低） |
|---|---|
| `context_size` | `calibrated` > `discovered` > `card` > `default` |
| `output_size` | `calibrated` > `card` > `discovered` > `null` |
| `input_types` / `output_types` | `card` > `discovered` > `["text/plain"]` |
| `parameter_schema` | `card`（唯一来源）> `{}` |
| `label` | `discovered.label`（非空且不等于 id 时）> `card.label` > `id` |
| `status` | `card`（`active`/`deprecated`/`sunset`）> `implicitly_active` |

**逐字段而不是整块**的理由：`parameter_schema` 只有卡有，窗口则网关更准；整块取一个源必然丢掉另一半。

### 6.3 模型名匹配

```python
def match(cards: list[ModelCard], model: str) -> tuple[ModelCard | None, str]:
    # 1) 精确
    # 2) 最长前缀：候选 = card.name where model.startswith(card.name)
    # 3) 归一化后相等：小写、去掉 -latest / -preview / 日期后缀
    # 4) 都没有 → (None, "none")
```

返回值第二项是 `matched_by` ∈ `exact | prefix | normalized | none`，进 API 与日志。不匹配时**不许**做编辑距离/模糊匹配（结论 4）。

`-latest` 的处理顺序要固定：先归一化剥离再比，避免 `qwen-plus-latest` 与卡里的 `qwen-plus` 错过。

### 6.4 `context_auto`：让窗口跟随模型

新增可编辑字段 `context_auto: bool`，与 `context_size` 联动。**默认值取决于注册表形态**（§4.4）：

| 场景 | `context_auto` 默认 | 理由 |
|---|---|---|
| 无 `providers.yaml`（匿名 `default`） | `false` | 升级不改运行参数（A35） |
| 有 `providers.yaml` | `true` | 用户显式进入多网关模式，能力自动跟随 |

| 状态 | `context_size` 取值 | `sources.context_size` |
|---|---|---|
| `context_auto=true` | 合并结果（§6.2） | `calibrated` / `discovered` / `card` / `framework-default`（未匹配） |
| `context_auto=false` | 用户显式填的值；未填则 `config/model.yaml` 的静态值 | `stored` / `session` / `yaml` |

联动规则（后端唯一判定，前端只发动作）：

- `PATCH {context_size: N}` → 同时置 `context_auto=false`。
- `PATCH {context_auto: true}` → 丢弃显式 `context_size`，回落到合并值（等价于「按模型重置」）。
- 切换 `model` / `provider` → `context_auto` 保持不变：若为 `true` 则自动跟随新模型（**A39 的核心**）；若为 `false` 则保留用户值并给 warning（「换了模型但窗口仍是手填值，可能与模型不符」）。

### 6.5 `derived.context_uncalibrated` 重定义

```python
CONTEXT_TRUSTED = {"calibrated", "discovered", "card", "stored", "session"}

def context_uncalibrated(source: str) -> bool:
    trusted = source in CONTEXT_TRUSTED
    # 等价性兜底：匿名 provider 走 yaml 静态值，此时必须沿用旧判据，
    # 否则 context_calibrated=true 的老部署会突然被标成「未校准」（破坏 A35）
    if not trusted and source == Source.YAML.value:
        trusted = load_model().context_calibrated
    return not trusted
```

同时新增两个派生值，让前端能把「信任度」画准确而不是二元：

```python
{
  "context_uncalibrated": False,
  "context_source": "card",          # 直接回 sources 里的值，便于 UI 分支
  "context_card_may_be_stale": True, # source == "card" 时提示「来自模型卡，可能滞后于网关」
}
```

`config/model.yaml` 的 `context_calibrated` 降级为「无人填过且无卡可查」时的人工标记，不再参与主要判定（§14 替代条款）。

### 6.6 校准表 `config/models.yaml`

```yaml
# 团队共识的模型能力校准值。优先级高于网关声明与内置卡。
models:
  - provider: dashscope-prod
    name: qwen3-max
    context_size: 262144
    output_size: 65536
    note: 官方文档 2026-11 口径；卡里还没有这个模型
  - provider: "*"                 # provider 省略 / "*" = 对所有 provider 生效
    name: qwen-max-latest
    context_size: 131072
```

`models.local.yaml`（gitignore）由面板「保存为默认」写入，合并时 `local` 覆盖 `models.yaml`（与 `model.local.yaml` 对 `model.yaml` 的关系一致）。

查找顺序（**四步，逐级回退**，避免「同一个模型两条校准值谁生效」说不清）：

1. `models.local.yaml` 且 `provider` 精确匹配
2. `models.yaml` 且 `provider` 精确匹配
3. `models.local.yaml` 且 `provider` 为 `"*"` 或省略
4. `models.yaml` 且 `provider` 为 `"*"` 或省略

`name` 按 §6.3 的匹配规则比（精确 → 归一化），**不允许前缀匹配**：校准是人填的，允许前缀会让 `qwen-max` 的校准值误套到 `qwen-max-long` 上。

### 6.7 参数 schema 的用法

`card.parameter_schema` 是 JSON Schema，前端据此渲染**采样参数**面板（覆盖 `ModelParams.sampling()` 的全部 6 项，即 `max_tokens` / `temperature` / `top_p` / `reasoning_effort` / `thinking_enable` / `parallel_tool_calls`；不含 `max_iters`、重试、压缩比例 —— 后者是调度参数，与模型能力无关，仍由 `BOUNDS` 单独管）：

- `properties[*].title/description` 作文案，`maximum/minimum/enum/default` 作范围。
- `hidden` 为真的项不渲染（框架已按模型裁掉 `thinking_*`/`voice`，见 §2）。
- `max_tokens.maximum` 直接用卡里注入的值（= `output_size`）。`BOUNDS["max_tokens"]` 只有 `min=1`、无全局上限，所以这里不需要「取交集」——但 `BOUNDS` 的 `min` 仍然生效。

后端校验同步：`validate()` 若拿到 `parameter_schema`，对上述采样字段做二次约束（越界即 400，`detail` 带 `maximum`）；`BOUNDS` 是硬底线，schema 缺失字段时按 `BOUNDS` 放行，不允许「schema 没写就任意填」。

## 7. 解析与热替换（P4）

### 7.1 分层：`model_settings` 不依赖 `model_catalog`

`model_settings` 现在是纯逻辑（不 import agentscope），这个性质要保住。新增端口：

```python
# model_settings/ports.py
class CapabilityLookup(Protocol):
    def capability(self, provider_id: str, model: str) -> "Capability | None": ...
```

`resolve()` 增加可选参数：

```python
def resolve(session=None, stored=None, *, domain="devops",
            capability: CapabilityLookup | None = None) -> dict[str, Resolved]:
```

`capability=None` → 行为与今天完全一致（A35），且此时 `context_auto` **强制为 `false`**（拿不到合并值就只能用静态窗口，不允许「声称跟随但跟随不了」）。装配层（`runtime/assembly.py`、`model_api.py`）注入 `model_catalog.Catalog` 实例。`lint-imports` 契约因此不需要新增边：依赖方向是 `model_catalog → model_settings`。

### 7.2 `Source` 扩展

```python
class Source(StrEnum):
    SESSION = "session"
    STORED = "stored"
    YAML = "yaml"
    ENV = "env"
    CALIBRATED = "calibrated"   # 新增：config/models[.local].yaml
    DISCOVERED = "discovered"   # 新增：网关 /models 声明
    CARD = "card"               # 新增：内置模型卡
    FRAMEWORK = "framework-default"
```

前端 `validation.tsx` / `Drawer.tsx` 里按字符串映射徽标的位置要补三个标签，未识别的 source 渲染为 `未知来源`（不是崩溃）——A44 断言这一点。

### 7.3 `MODEL_WIRE_KEYS` 增加 `provider`

```python
MODEL_WIRE_KEYS = ("provider", "name", "context_size", ...)
```

`build_chat_model` 改按注册表取 `(Credential, ChatModel)`：

```python
spec = registry.get(active.provider or registry.default_id())
cls, cred_cls = KINDS[spec.kind]
model = cls(credential=cred_cls(**cred_fields), model=active.name, ...)
```

`extra_body` 的联网字段只对 `openai` / `dashscope` 族发送；其它 kind 传 `None`（框架不接受未知 `extra_body` 会 400，这是真实风险）。

### 7.4 实例缓存

```python
_cache: dict[tuple[str, str, str], ChatModelBase] = {}   # (provider, model, wire_sha)

def wire_sha(active: ModelParams) -> str:
    """只对 MODEL_WIRE_KEYS 的当前值做 sha256。

    刻意不用 `params_sha()`：那个基于 `record()`（只记非默认值），
    两个 provider 下同参数同模型算出的 sha 会相同 —— 这正是要避免的复用。
    """
    payload = {k: getattr(active, k, None) for k in MODEL_WIRE_KEYS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
```

键里必须含 `provider`：否则换 provider 但模型同名（很常见，别名都在两个网关上）会复用错对象，把 A 的 key 发到 B。

`MODEL_WIRE_KEYS` 里有 `name` 与 `context_size`，所以「换模型」与「窗口被重新校准」都会自然触发重建 —— 后者是必须的：`context_size` 是构造参数，不是可热改属性。

淘汰：`len(_cache) > 32` 时按创建时间清到 16。不做 LRU 计数器——这个规模不需要。

### 7.5 新增可编辑字段

`bounds.py` 的增量为：

```python
EDITABLE = ("provider", "model", ...)          # provider 排在最前：选它才能选模型
BOUNDS |= {
    "provider": Bound("str", nullable=True),   # null = 用注册表 default
    "context_auto": Bound("bool", nullable=False),
}
READONLY["providers_file"] = "config/providers.yaml（网关清单与 key 变量名）"
```

两条约定：

- `provider` 用 `Bound("str")` 而不是 `enum`：`BOUNDS` 是模块常量，写死 id 列表会在加载 `providers.yaml` 后失效。合法性由 `validate()` 在运行期用注册表检查（未知 id → 400，`reason` 列出可用 id）。
- `model` 仍不设白名单（发现结果可手填），但 `validate()` 增加**软校验**：`matched_by == "none"` 时不报错，只进 `warnings`（「未在内置卡中找到该模型，窗口按未校准处理」）。

`MODEL_WIRE_KEYS` 增 `provider`（§7.3）。`ModelParams` 增 `provider: str = ""` 与 `context_auto: bool = False`，`record()` 里只在非空/非默认时记录，避免无网关部署的 run 记录里多出噪声字段。

会话级隔离直接复用既有链路：`/api/session/{id}/model-config` 的 PATCH 走同一个 `apply_patch` → `_checked_values` → `apply_model_params`，所以「A 会话用百炼的 qwen3-max、B 会话用内网 vLLM 的 qwen2.5-72b」不需要新端点，只需 `provider` 进 `EDITABLE`。这是 §3.1「每个网关下可选任意多个模型」在会话维度的落地方式。

### 7.6 装配与自述

- `gateway/bootstrap.py::_effective_model` 改为返回 `"<provider>/<model>"`（无 provider 时退回今天的裸模型名，保持 `/health.runtime.model` 形状兼容）。
- `/health` 增：

```json
{"model": {"provider": "dashscope-prod", "kind": "dashscope_chat",
           "catalog": {"providers": 2, "missing_key": ["vllm-local"],
                       "last_refresh": "2026-11-27T14:20:11Z",
                       "stale": false, "codes": {"vllm-local": "unauthorized"}}}}
```

## 8. API（P5）

### 8.1 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/providers` | 已配置网关清单（脱敏） |
| GET | `/api/providers/{provider_id}/models` | 发现的模型列表（缓存优先） |
| POST | `/api/providers/{provider_id}/models/refresh` | 强制绕过缓存 |
| GET | `/api/providers/{provider_id}/models/{model_id:path}` | 单模型能力卡（四源合并 + 逐字段来源） |

`{model_id:path}` 是必须的：Gemini 的 id 是 `models/gemini-2.5-pro`、OpenRouter 是 `anthropic/claude-sonnet-4`，普通路径参数会在 `/` 处截断。

### 8.2 `GET /api/providers`

```json
{
  "default": "dashscope-prod",
  "items": [
    {"id": "dashscope-prod", "kind": "dashscope_chat", "label": "百炼（生产）",
     "base_url_host": "dashscope.aliyuncs.com", "discover": "models",
     "has_key": true, "key_fingerprint": "a91f3c72",
     "models": 42, "last_check": {"at": "…", "code": "ok"}}
  ]
}
```

约束：**只回 host，不回完整 URL**（URL 里可能带 token 参数）；`key_fingerprint` 是 `sha256(key)[:8]`，同进程内稳定、跨进程不可反查；不回 key 尾号（尾号也是信息）。

### 8.3 `GET /api/providers/{id}/models`

```json
{
  "provider": "dashscope-prod",
  "fetched_at": "2026-11-27T14:20:11Z",
  "stale": false, "code": "ok", "total": 42,
  "items": [{"id": "qwen3-max", "label": "qwen3-max", "status": "active",
             "context_size": 262144, "input_types": ["text/plain"],
             "matched_by": "prefix"}]
}
```

`items[].context_size` 已是四源合并结果（面板要能直接显示窗口，不必再逐个请求能力卡）。`q` / `limit`（默认 200）/ `offset` 支持；`total` 是未截断总数。

**性能要求**：卡片集按 `kind` 在进程内缓存（`list_models()` 每次都 glob + 解析全部 yaml，42 个模型逐个调用就是 42 次重复 IO）。合并只做内存匹配，所以 200 条列表的耗时应在毫秒级。缓存失效条件：进程启动后首次访问，或 `ModelCard` 目录 mtime 变化（框架升级时）。

### 8.4 单模型能力卡

```json
{
  "provider": "dashscope-prod", "model": "qwen3-max",
  "capability": {
    "context_size": 262144, "output_size": 65536,
    "input_types": ["text/plain"], "output_types": ["text/plain"],
    "status": "active", "label": "qwen3-max",
    "parameter_schema": {"type": "object", "properties": {"max_tokens": {"maximum": 65536}}}
  },
  "sources": {"context_size": "calibrated", "output_size": "card",
              "input_types": "card", "status": "card", "label": "discovered"},
  "matched_by": "prefix",
  "matched_card": "qwen3-max"   // 实际命中的卡名，便于解释「为什么是这个窗口」
}
```

**不提供 `/v1/models`**：本服务的语义是「一个自带配置面板的 agent」，不是模型网关。挂一个 OpenAI 兼容路径会让调用方以为可以改写模型或做多租户分流，而那些都不支持。

### 8.5 `/api/model-config` 增量字段

`effective` 增 `provider` / `context_auto`；`sources` 增 `provider`；新增 `catalog` 段：

```json
{"catalog": {"providers_configured": 2, "current": {
   "matched_by": "prefix", "context_source": "card",
   "card_may_be_stale": true, "status": "active",
   "deprecated_at": null, "label": "qwen3-max"}}}
```

`derived` 增 `context_source` / `context_card_may_be_stale`（§6.5）。

## 9. 前端（P6）

`web/src/workspace/model-config/`：

| 文件 | 改动 |
|---|---|
| `ProviderSelect.tsx`（新） | 网关下拉；显示 `has_key=false` 的项为禁用 + 提示「未配置 KEY」 |
| `ModelCombobox.tsx`（新） | 可搜索组合框；**允许手填**（网关 `/models` 常不全）；deprecated 黄标、sunset 红标并二次确认 |
| `capability.ts`（新） | 把 `parameter_schema` 映射成面板字段（`hidden` 跳过、`maximum` 生效） |
| `Drawer.tsx` | 模型名输入换成 `ModelCombobox`；`context_size` 旁加「跟随模型」开关与来源徽标 |
| `types/model.ts` | 增 `provider` / `context_auto` / `catalog` 类型 |

交互约束：

- 打开抽屉**只**发 `GET /api/providers`（无上游请求）；选模型时才发一次 `/models`（走缓存）。避免「每次开面板都打网关」。
- 点「按模型重置」→ `PATCH {context_auto: true}`，成功后立刻用返回值刷新窗口与阈值，不做本地推算（口径只有一处）。
- 分类着色：`deprecated` 用警示色但不阻止；`sunset` 阻止保存，除非勾选「我知道该模型已下线」。

## 10. 安全与凭据

### 10.1 凭据

- 注册表只存 `env:NAME`；`/api/providers` 只回 `has_key` + `key_fingerprint`。
- `key_fingerprint` 计算用 `hashlib.sha256(key.encode()).hexdigest()[:8]`，**不加盐也不加主机名**：它的唯一用途是「两次配置是不是同一把 key」，泄露 8 位哈希不构成暴力可逆风险（key 长度 ≥32）。
- 错误日志与 `malformed` 的响应片段一律过 `redaction`（已有 `devops_agent/redaction/`），不允许出现 `sk-…` 形态的串。

### 10.2 `base_url` 校验

```python
def check_url(url: str) -> None:
    # 1) scheme ∈ {http, https}
    # 2) 不含 userinfo（`user:pass@`）—— 凭据不该藏在 URL 里
    # 3) http 且 host 不是 loopback（127.0.0.1 / ::1 / localhost）→ 拒绝
    # 4) https 不限制
```

拒绝的理由是「明文发 key」不是「不安全站点」：内网 http 网关必须可用，所以放行回环，其余一律要求 https。UI 与加载期共用这个函数，错误信息给出改法（「改成 https，或让网关监听 127.0.0.1」）。

### 10.3 发现请求

- 不跟随跨主机重定向（`follow_redirects=False`）；3xx 视为 `malformed` 并提示「网关返回了重定向，请直接配置最终地址」——跟随重定向会把 key 带到另一个主机。
- 不复用对话用的 HTTP client（生命周期不同：发现是短请求，对话是长连接）。

## 11. 验收标准

| 编号 | 断言 | 方式 |
|---|---|---|
| A35 | 零配置等价：无 `providers.yaml` 时，`resolve()` 的**既有字段**值与 `sources` 与改动前逐项相同，且 `context_auto` 解析为 `false` | 快照对比（用 `main` 的 `resolve` 输出做基准） |
| A36 | 发现成功：`/models` 返回 3 个模型 → `items` 含 id/label，第二次调用**不发 HTTP**（缓存命中） | 假 HTTP + 计数断言 |
| A37 | 401 → `code=unauthorized`，不重试（请求计数 = 1），退到卡；30s 内再请求仍不发出 | 假 HTTP |
| A38 | 四源合并：校准 262144 / 发现 131072 / 卡 1000000 三种组合下 `context_size` 与 `sources` 正确 | 三组参数化用例 |
| A39 | 换模型窗口跟随：配了 `providers.yaml` 时 `context_auto` 默认 `true`；把 `model` 从 A（1M）切到 B（32k）后 `resolve().context_size == 32768` 且 `sources=card`；`context_auto=false` 时保持用户值并产出 warning | 纯函数用例 |
| A40 | 换网关不串凭据：同模型名在两个 provider 下，`build_chat_model` 返回的对象 `credential.api_key` 各自正确；`MODEL_WIRE_KEYS` 含 `provider` 触发重建 | 构造断言 |
| A41 | 凭据不泄露：`GET /api/providers` 响应体不包含任何 key 明文或后缀片段（`key_fingerprint` 是哈希前缀，允许出现）；`key_fingerprint` 在 key 变化时变化、不变时稳定 | 字符串扫描 + 两把假 key |
| A42 | URL 校验：`http://1.2.3.4/v1`、`https://u:p@host/v1`、`file:///x` 全部拒绝；`http://127.0.0.1:8000/v1` 通过 | 参数化用例 |
| A43 | 超时不阻塞：把假网关置为 hang → `/models` 3s 内返回 `code=unavailable` 并回卡；对话路径（`build_chat_model`）耗时不受影响 | 计时断言 |
| A44 | 未知 source 不崩：前端收到 `sources.context_size="card"` 与未知值 `"future"` 时都渲染出徽标 | 前端单测（`Drawer.test`） |
| A45 | 匹配规则：`gpt-4.1-mini-2025-04-14` → `matched_by=prefix` 且命中 `gpt-4.1-mini`（不是 `gpt-4.1`）；`qwen-max-latest` → `none` 且 `context_size=128000`、`uncalibrated=true` | 纯函数用例 |
| A46 | 参数 schema 生效：`max_tokens=99999` 在 `qwen-flash`（上限 32768）下 400，文案带上限；`BOUNDS` 硬底线不被 schema 放宽 | API 用例 |
| A47 | `/health` 暴露 `model.provider` / `catalog.codes` / `missing_key`，且缺 key 的 provider 不阻止其他 provider 使用 | 契约测试 |

## 12. 实施顺序与文件预算

| 阶段 | 内容 | 文件 | 预算（行） |
|---|---|---|---|
| P1 | 注册表 + kind 表 + 匿名 default | `model_catalog/{schema,registry,kinds}.py`、`config/providers.yaml` | 120 + 150 + 60 |
| P1' | `resolve` 端口注入 + `Source` 扩展 + `provider`/`context_auto` 字段 | `model_settings/{ports,bounds,resolve}.py`、`config_schema.py` | +90 |
| P2 | 发现 + 归一化 + 缓存 | `model_catalog/{discover,normalize,cache}.py` | 200 + 120 + 80 |
| P3 | 卡查询 + 匹配 + 合并 + 校准表 | `model_catalog/{cards,match,merge}.py`、`config/models.yaml` | 80 + 70 + 140 |
| P4 | 模型构造改按 kind + 实例缓存 + health | `runtime/{chat_model,scope_runtime}.py`、`gateway/routes/health.py` | +120 |
| P5 | API 四个端点 + payload 增量 | `model_catalog/api.py`、`model_api.py`、`model_settings/payload.py` | 200 + +60 |
| P6 | 前端三件 + 类型 | `web/src/workspace/model-config/*`、`web/src/types/model.ts` | +400 |

单文件硬上限 500 行不变；`model_catalog/api.py` 若超限，按「providers 端点 / models 端点」拆两个 router。

## 13. 风险与回滚

| 风险 | 表现 | 缓解 |
|---|---|---|
| 内置卡滞后 | 新模型（`qwen3-max`）匹配不上，显示 128k 未校准 | 校准表 + `context_card_may_be_stale` 提示；不模糊匹配（A45） |
| 网关 `/models` 返回几百条 | 面板卡顿、API 响应巨大 | `limit=200` + `q` 搜索 + `total`；前端虚拟滚动 |
| 多网关 key 配错 | 401 沉默，用户以为网关坏了 | `/api/providers` 的 `last_check.code` + `missing_key` 直接显示 |
| `extra_body` 发给不支持的 kind | 供应商 400 | 只对 `openai`/`dashscope` 族发送（§7.3） |
| 明文 http 发 key | 凭据泄露 | §10.2 校验，回环例外 |
| 换 provider 复用旧实例 | key 串用 | 实例缓存键含 provider + `MODEL_WIRE_KEYS` 含 provider（A40） |

回滚开关（三档，都在 `config/providers.yaml` 顶层，都不改代码）：

1. `catalog.enabled: false` → 关闭能力合并，`context_size` 回静态 yaml 值（等于今天）。
2. `providers.yaml` 删掉或 `providers: []` → 合成匿名 `default`，回到单网关单模型。
3. `discover: off` → 保留多 provider 选型，但不打 `/models`（纯离线）。

## 14. 不做的事与后续

**本期不做**（明确划界，避免 review 反复）：

- 负载均衡 / 故障转移 / 重试换 provider：一个 provider 失败就是失败，不静默换家（会掩盖配置错误）。
- 模型能力自动探测（发真实多模态请求）：只取声明值。
- 定价与成本估算：`/models` 无统一价格字段，OpenRouter 有但只覆盖自家聚合，做出来是半张表。
- `/v1/models` 兼容端点：语义误导（§8.4）。
- 评测侧 `judge_model` 的 `provider/model` 语法：P2 后续，本期 `DEVOPS_AGENT_JUDGE_MODEL` 仍只接受模型名，provider 沿用主模型。

**被替代条款**：

- `config/model.yaml` 的 `context_calibrated` 不再是主判据（§6.5 接管）；字段保留，只在「匿名 provider + `context_auto=false`」这条静态路径上兜底（`source=yaml` 时用它决定是否标未校准）。
- `bounds.py:61-63` 的注释「当前没有 provider 的模型列表接口」随本 spec 失效，改为「模型名接受任意字符串，但面板提供发现列表；未知名会标 `matched_by=none`」。
- `bounds.py:32-33` 的「`base_url` 环境变量（重启生效）」改为「注册表 `providers.yaml`，或匿名 provider 的 `DEVOPS_AGENT_BASE_URL`」。

## 15. 依赖与前置

- 无新增 pip 依赖：HTTP 用已有的 `httpx`，YAML 用已有的 `pyyaml`，模型卡来自已装的 `agentscope`。
- 前置：SPEC-0017 已合入（`config_schema.py` 存在，`ModelSettings` 在 `config_schema.py:218`），`config/memory.yaml` 已被删除（#136），所以本 spec 只新增 yaml，不修改被删文件。
- 与 SPEC-0016 的交界：`context_size` 变化会改变记忆层 `history_limit`（`budget.py` 由 `context_size` 派生），这是**期望行为**（窗口变大 → 历史预算变大），但会让 `test_memory_budget_execution.py` 里的固定期望值失效。实施时应改为按 `resolve()` 结果计算而非硬编码，避免第二个真相。
