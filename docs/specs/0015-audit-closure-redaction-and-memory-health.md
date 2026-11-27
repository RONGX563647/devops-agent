# SPEC-0015：审计收尾——脱敏精度、记忆失败可见化、轮次边界与 token 口径

- 状态：方案 v1（待评审）
- 日期：2026-11-27
- 基线：`main 260944a`
- 关系：补齐 SPEC-0013 §5.9（上下文可观测）、§8（轨迹可观测）、§5.10（失败模式）的收尾条款；不改变三层记忆模型
- 范围：`memory/{runtime,observer,short_term,events}.py`、`memory/turns.py`（新）、`memory/health.py`（新）、`secrets/{capture,middleware}.py`、`redaction/rules.py`（新）、`input/pipeline/redact.py`、`runtime/scope_runtime.py`、`gateway/routes/{health,memory}.py`、`tests/**`
- 触发来源：一次外部代码审计（4 条观察 + 2 条本规格补录）
- 证据约定：现状结论一律带 `路径:行号`；未运行的验证标 `NOT RUN`

## 1. 设计结论

1. **四条缺陷里三条同源**：C（两套脱敏规则表）、B（两处轮次判定）、A（「失败降级为空」与「失败转事件」两套语义）都是**同一契约存在两份实现**。文档与实现不一致只是表象；修法必须是「单源化 + 契约测试」，不是逐点打补丁。
2. **C 的根因与审计结论不同，修错模块等于没修**：审计指向 `input.pipeline.redact`，实测 **415/425 次误伤来自 `secrets/capture.py` 的高熵兜底**；`input` 侧另有 21 处、但危害形态不同（会吞掉 `continue` 这类语句）。两侧都要改，且必须共用一张规则表。
3. **C 是 P0，因为它伤的是智能体本体**：`SecretInjectionMiddleware` 对**所有**工具输出无条件过脱敏（`secrets/middleware.py:144-146`），所以模型读自己的源码时拿到的是不可编译、不可 grep 的文本；这不是「人看 repo 不方便」，而是「改代码的模型拿到了坏输入」。
4. **A 有两层，不止一个裸 `pass`**：`observer.py:211-212` 吞异常不发事件（审计已指出）；此外 `recall()` 两处裸 `except` 把故障降级成「没命中」（`runtime.py:94-104`），且**回合末写入的帧在流结束后才产生，永远不会被 drain**（`scope_runtime.py:261-266`）——事件发了也可能没人收。
5. **B 的谓词本身可以保留，但必须换锚点**：用 `name` 区分注入与真实输入，依赖「别的模块不乱填 name」。改为 `metadata.injected` 标记，`name` 只作兼容兜底。
6. **D 不是系数错，是系数不可验证**：现行测试带宽是 3000–4500（±25%），任何 20% 的漂移都能通过；docstring 里的校准样本（409 字符/10139 字符）与当前提示词（452 字符/10147 字符）已经对不上，说明「样本」已经过期而没人发现。
7. **顺序 C → A → B → D**：与前次审计建议一致。C 影响面最广且自我强化（错误在上下文里被模型继续消费）；A 只在诊断记忆时咬人；B 是随时间变差的重复；D 是量级问题，加安全垫即可。

## 2. 现状证据

| 编号 | 断言 | 证据 | 复现 |
|---|---|---|---|
| A-1 | 承诺「异常转事件」，实现是裸 `pass` | `memory/runtime.py:4` vs `memory/observer.py:207-212` | 读代码 |
| A-2 | 召回失败被降级成「没命中」，与真空结果不可区分 | `memory/runtime.py:94-104`；`observer.py:118-119` 固定写 `state=miss/reason=no_memory` | 下方脚本输出 `recall -> None []`、`sink frames: []` |
| A-3 | 回合末的写入帧在流结束后产生，没人 drain | `memory/observer.py:204-212` + `runtime/scope_runtime.py:261-266`（drain 只在 `async for event` 循环体内） | `tests/unit/test_memory_middleware.py:148-172` 只能证明「帧进了队列」，不能证明「被转发」 |
| A-4 | `/api/memory/status` 只有开关与后端名，无健康信号 | `gateway/routes/memory.py:31-34`、`memory/runtime.py:247-275` | 关掉 long_term 后端，接口仍返回 `long_term: true` |
| B-1 | 轮次谓词复制两份 | `memory/short_term.py:139-145`、`memory/observer.py:35-39` | 文本相同 |
| B-2 | 锚点是 `name`，注入侧靠约定 | 注入 `observer.py:93`、`runtime/profile_note.py:118,121`；真实输入 `runtime/scope_runtime.py:224-230` 用 `name=session_id` | 改注入命名即静默分叉 |
| C-1 | 高熵兜底把 Python 标识符当凭据 | `secrets/capture.py:104-111`（候选 `[A-Za-z0-9_\-.+/=]{20,}` + ±60 字符内出现 key/token 语义词） | 全库扫描：259 文件 → 111 文件命中、425 条，其中 415 条来自 `entropy`，342 条（80%）是标识符/模块路径 |
| C-2 | 替换用 `str.replace` 全量替换，命中一次坏一整片 | `secrets/capture.py:126-134` | `def estimate_prefix_tokens(...)` → `def {{secret:out_entropy_771a9e4b}}(...)` |
| C-3 | 输入侧赋值规则的 `\s*` 跨行，会吞掉下一条语句 | `input/pipeline/redact.py:40-44` | `if "://" in token:\n            continue` → `continue` 被换成占位符；`token = match.group(0)` → `token = {{secret:token}}(0)` |
| C-4 | 两份规则表已漂移 | `input/pipeline/redact.py:28-46` vs `secrets/capture.py:29-43`：`sk-` 最短 10 vs 20；`Bearer` 只在输入侧；赋值式的键名前缀只有输入侧有 | 对比两处常量 |
| D-1 | 系数是单点校准且样本过期 | `memory/tokens.py:10-16`；当前实测 452 字符系统提示 + 10147 字符工具 schema = 3058 token，docstring 写的是 409/10139 ≈ 3060 | 见 §6.1 |
| D-2 | 测试带宽 ±25%，发现不了漂移 | `tests/unit/test_context_api.py:89-93` | `3000 < prefix_tokens < 4500` |

A-2 复现（`NOT RUN` 之外的实测输出）：

```text
recall -> None []
sink frames after failed recall: []
```

C 的全库扫描（只读，不改文件）：

```bash
.venv/Scripts/python.exe - <<'PY'
import sys, pathlib; sys.path.insert(0, "src")
from devops_agent.secrets.capture import find_secrets
hit = [p for p in pathlib.Path("src").rglob("*.py") if find_secrets(p.read_text(encoding="utf-8"))]
print(len(hit), "files affected")
PY
```

## 3. C（P0）：脱敏精度与规则单源

### 3.1 判定模型：四道闸，按代价递增

候选串必须依次通过，任一不过就放行（宁可漏检一处，不可污染整段上下文）：

| 闸 | 规则 | 拦掉的实例 |
|---|---|---|
| G1 形状 | 长度 8–2048；**同时**含字母与（数字或符号）；拒绝纯点分标识符 `^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$`；拒绝 Python 关键字/内置名 | `estimate_prefix_tokens`、`devops_agent.memory.tokens`、`max_tokens=settings.max_tokens` |
| G2 位置 | 赋值式分隔符只允许 `[ \t]{0,4}[:=][ \t]{0,4}`，**禁止跨行**；值不得以 `.` 开头，不得是点分标识符 | `token:\n continue`、`api_key = os.environ.get` |
| G3 上下文 | 语义词必须是独立词（`(?<![A-Za-z0-9_])token(?![A-Za-z0-9_])`），且出现在同一行；`entropy` 档额外要求候选前后 20 字符内存在 `[:=]`、`:`、`Bearer`、`"`、`'` 之一 | `devops_agent.memory.tokens`（`token` 是下划线前缀的一部分） |
| G4 档案 | `profile="code"`（`Read`/`Grep`/`Glob` 的输出）**关闭 entropy 兜底**，只保留已知前缀 + 赋值式（仍需过 G1–G3） | 源码正文整体豁免高熵误判，`.env`/`config.yaml` 仍靠赋值式命中 |

置信度分层：`known_prefix`（`AKIA…`/`sk-…`/`ghp_…`/`xox…`/JWT/PEM）不过 G3、G4；`assigned` 过 G1–G4；`entropy` 过 G1–G4 且 **只在 `profile="output"` 生效**。

### 3.2 替换算法：边界感知 + 跨度替换

- `high` 档：边界感知全局替换 `(?<![A-Za-z0-9_])<value>(?![A-Za-z0-9_])`，保证「同一密钥打印两次都抹掉」，又不切进更长的标识符。
- `entropy` 档：只替换**命中跨度**（`Captured.spans`，右到左应用），不再全量替换——它本来就只是猜测，猜测不该有全局副作用。
- `Captured` 增加 `spans: tuple[tuple[int, int], ...]`；`capture_output` 的返回结构不变，调用方零改动。

### 3.3 规则单源：`devops_agent/redaction/rules.py`

纯 stdlib 叶子模块（不 import `cryptography`、不 import 框架、不 import `input`/`secrets`），供两侧消费：

```python
@dataclass(frozen=True, slots=True)
class Rule:
    name: str                                  # 占位符基底名
    pattern: re.Pattern[str]
    confidence: Literal["known_prefix", "assigned", "entropy"]
    profiles: frozenset[str]                   # {"input"} / {"output"} / {"code"}
```

派生函数：`match(text, profile) -> list[Candidate]`、`shape_ok(value) -> bool`、`replace(text, candidates, namer) -> str`。
`input/pipeline/redact.py` 只保留「槽位保护 + 命名 + 阶段事件」；`secrets/capture.py` 只保留「登记保险箱 + 命名」。两份正则表删除。

> 依赖方向：`input` 与 `secrets` 都指向 `redaction`，不互指，`.importlinter` 现有契约不受影响；新增一条 `redaction` 为叶子的契约（见 §11 未决）。

### 3.4 误报可观测

- `capture_output` 返回的 `Captured` 带 `confidence`，中间件在 `debug` 记账、在 span 里只记计数：`redaction.capture {count, by_confidence}`。
- 新增进程级计数 `redaction.entropy_hits`（供 `/api/memory/status` 之外的诊断页读，见 §7.2）；熵档连续命中同一文本 3 次以上视为规则退化信号，写 `WARNING` 一行（不含原文）。

### 3.5 回归门禁（本条把审计结论固化成测试）

`tests/unit/test_redaction_repo_scan.py`：

1. 遍历 `src/**/*.py`，断言 `redact_preview(src) == src`；
2. 同样断言 `capture_output(src, profile="code")[0] == src`；
3. 例外清单 `tests/fixtures/redaction_allowlist.json` 默认空，新增条目必须在 PR 描述里说明理由。

期望结果：当前 111 个文件命中 → **0**；`tests/` 下真实测试夹具（`sk-abcdef…`）不在此门禁范围（它们是刻意的样本）。

### 3.6 保险箱污染清理

旧规则已把标识符登记成 `source=output` 的「密钥」。新增 `scripts/audit_false_secrets.py`（默认 dry-run，只出报告）：

```bash
python scripts/audit_false_secrets.py            # 只列候选，不删
python scripts/audit_false_secrets.py --purge    # 需人工确认后执行
```

判定：`source='output'` 且值匹配点分标识符、或长度 < 12、或熵 < 3.5。**不做自动清理**——保险箱里有用户真实凭据，误删不可逆。

### 3.7 验收

- [ ] 全库扫描 0 误伤（§3.5 两条断言绿）
- [ ] `Read src/devops_agent/memory/runtime.py` 的工具输出与磁盘字节一致（手工，diff 为空）
- [ ] `.env` 样本 `API_KEY=sk-live-…` 仍被捕获（保留能力，防「修误报修成漏报」）
- [ ] `Bearer`/`sk-`(10–19 字符)/`FOO_TOKEN=` 三类在两侧行为一致（同一张规则表的参数化用例）

## 4. A（P1）：记忆失败可见化

### 4.1 失败语义矩阵（唯一口径）

| 层 | 操作 | 降级值 | 事件 | `state` | `reason` |
|---|---|---|---|---|---|
| episodic | active 读 | `card=None` | `MEMORY_RETRIEVE_END` | `error` | `episodic_unavailable` |
| long_term | search | `hits=[]` | `MEMORY_RETRIEVE_END` | `error` | `long_term_unavailable` |
| long_term | write | `{"stored": false}` | `MEMORY_WRITE_END` | `error` | `write_failed` |
| turn | 回合末写入整体 | 跳过 | `MEMORY_WRITE_END`（`layer="turn"`） | `error` | `turn_write_failed` |
| episodic | save | `None` | `MEMORY_COMPRESS_END` | `error` | 现状已实现（`runtime.py:161-175`） |

两条硬约束：

1. **`state=error` 必须带 `reason`**，且 `reason` 是枚举而非自由文本（异常类型与消息进 `memory.exc_type` / `memory.exc_msg` 两个字段，避免把堆栈塞进 `reason` 破坏前端过滤）。
2. **降级与「空结果」必须可区分**：`state=miss / reason=no_memory` 只允许表示「后端正常、确实没命中」。

### 4.2 `recall` 返回结构

```python
@dataclass(frozen=True, slots=True)
class RecallResult:
    card: dict[str, Any] | None
    hits: list[Hit]
    degraded: tuple[str, ...] = ()            # ("episodic",) / ("long_term",) / 两者
    reasons: dict[str, str] = field(default_factory=dict)
```

`_assemble` 据此决定 `state`：`degraded` 非空 → `error`；否则按原逻辑 `hit/miss/skipped`。`meta` 增加 `degraded` 字段，前端可画「记忆降级」角标。

### 4.3 回合末帧滞留（审计未覆盖）

`stream` 的 drain 只发生在循环体内；`on_reply` 的 `finally` 在 `StopAsyncIteration` 之前执行，产出的帧落在队列里：

- 会话继续 → 下一轮第一个事件后被 drain，**记到下一个 run 头上**（归因错）；
- 会话结束 → 丢失，并随 `MemorySink` 的无界 `deque` 一起常驻。

修法（`runtime/scope_runtime.py`）：

```python
async for event in self._agent.reply_stream(self._make_msg(session_id, user_input)):
    yield event_to_dict(event)
    for frame in self._drain_compression():
        yield frame
    for frame in self._drain_memory():
        yield frame
# on_reply 的 finally 在上一行循环结束时才执行完，尾帧在这里补发
for frame in self._drain_memory():
    yield frame
```

`resume` 路径同改。测试：假运行时在 `reply_stream` 结束后往 sink 写帧，断言帧流最后一个元素是该帧。

### 4.4 进程级健康计数：`memory/health.py`

```python
def note_failure(layer: str, op: str, exc: BaseException) -> None: ...
def note_success(layer: str, op: str) -> None: ...
def snapshot() -> dict[str, Any]: ...
```

- 线程安全（`threading.Lock`），不持久化（进程重启即清零，够用且不写库）；
- 只存 `count / last_reason / last_at`，不存异常消息原文（可能带路径与密钥）；
- `MemoryRuntime` 的每个 `except` 都调一次 `note_failure`，与事件发送同点。

### 4.5 接口

`GET /api/memory/status` 增加：

```json
{
  "health": {
    "degraded": true,
    "layers": {
      "long_term": {"failures": 3, "last_reason": "long_term_unavailable", "last_at": "2026-11-27T09:12:31Z"}
    }
  }
}
```

`GET /health` 只增加 `memory.health.degraded: bool`（探针不需要明细，也不该因为记忆降级返回 5xx——延续 `health.py:1-9` 的既定原则）。

### 4.6 验收

- [ ] 注入一个 `search` 抛异常的假后端：`MEMORY_RETRIEVE_END.state == "error"`、`reason == "long_term_unavailable"`、`memory.degraded == ["long_term"]`
- [ ] 同上，`/api/memory/status.health.degraded is True`
- [ ] `write_turn` 内层抛错（monkeypatch `redact_preview`）→ 仍发 `MEMORY_WRITE_END(state=error, layer="turn")`，且**不**向调用方抛出
- [ ] 流结束后补发的尾帧被转发（§4.3 测试）
- [ ] 无故障路径的事件数量不增加（回归：`len(frames)` 与现状一致）

## 5. B（P2）：轮次边界单源

### 5.1 现状

同一谓词两份（`short_term.py:144`、`observer.py:37`），锚点是 `name`；注入侧靠「写死 `name="system"`」维持约定（`observer.py:93`、`profile_note.py:118,121`），真实输入用 `name=session_id`（`scope_runtime.py:224-230`）。

### 5.2 新模块 `memory/turns.py`

```python
INJECTED = "injected"
_LEGACY_INJECTED_NAMES = frozenset({"system", "user"})

def injected_note(text: str, *, kind: str) -> UserMsg:
    """所有 user 角色的注入消息都必须经由此处构造。"""
    return UserMsg(name="system", content=text, metadata={INJECTED: kind})

def is_user_turn(msg: Any) -> bool:
    if getattr(msg, "role", "") != "user":
        return False
    if (getattr(msg, "metadata", None) or {}).get(INJECTED):
        return False
    return getattr(msg, "name", "") not in _LEGACY_INJECTED_NAMES
```

- `short_term._turn_starts` 与 `observer._last_user_text` 都改调 `is_user_turn`，删掉本地实现；
- `profile_note.py:118,121` 改用 `injected_note(..., kind="profile")`；
- `_LEGACY_INJECTED_NAMES` 保留 `name` 兜底一个版本，供旧快照恢复的历史消息使用（旧消息没有 metadata，否则会被算成真实轮次，导致裁剪方向反转）。

### 5.3 架构守卫

`tests/unit/test_turn_boundary_single_source.py`：

1. 扫描 `src/**/*.py`，除 `memory/turns.py` 外不得出现 `UserMsg(name="system"`；
2. 构造「真实输入 + profile note + memory note」的 messages，断言 `len(_turn_starts(...)) == 1`；
3. 构造旧快照（无 metadata、`name="system"`），断言仍被判为注入（兼容）。

### 5.4 验收

- [ ] 两处谓词删除，仅剩 `turns.is_user_turn`
- [ ] 上述三条测试绿
- [ ] `trim_messages` 与 `_last_user_text` 在混合消息序列上的行为与现状逐条一致（用现有用例回归，不改断言）

## 6. D（P3）：token 口径可校准

### 6.1 现状量化

```text
工具数 4；系统提示 452 字符 → 240 token；工具 schema 10147 字符 → 2818 token
固定前缀 3058 token；窗口 128000 × trigger 0.8 − 4096 → history_max 95246
再加一个 1.8KB schema 的工具：固定前缀 3591（+533），history_max 94713（−0.56%）
```

单点漂移不致命，问题是**没有东西能告诉你有漂移**：docstring 的 409/10139 已被现实超过 43 字符，而测试仍绿。

### 6.2 校准 fixture + 误差断言

`tests/fixtures/token_calibration.json`：

```json
{
  "model": "qwen-max",
  "measured_at": "2026-11-27",
  "system_chars": 452,
  "tools_chars": 10147,
  "expected_tokens": 3060,
  "source": "线上单次调用的 usage.prompt_tokens − 历史/输入估算（人工核对）"
}
```

测试三条：

1. `|estimate(system) + estimate(tools) − expected_tokens| / expected_tokens ≤ 0.08`；
2. 当前实时提示词的估算与该 fixture 的偏差 ≤ 0.15，超阈值直接失败并提示「提示词/工具集已变更，请重校准」——把「样本过期」变成红灯，而不是靠人读 docstring；
3. 分类型系数单测：纯中文、纯 ASCII、JSON schema、混合四类各一条，钉住 1.4 / 3.6 的边界（改系数必须同时改 fixture）。

### 6.3 估算—实测对账（可见化，不自动调参）

每个 run 的**首次**模型调用后发一帧：

```json
{"type": "CONTEXT_ESTIMATE_DELTA", "estimated": 12043, "actual": 13088, "ratio": 1.087}
```

- `estimated` 用同一份 `estimate_tokens` 对真正下发的 `system + tools + messages` 求和；`actual` 取响应 `usage.input_tokens`；
- 每 run 最多一帧（`context.estimate_audit: true` 时才发），只在首调用采样，避免多轮累计误差污染；
- 落到 `runs` 侧栏即可，不做告警阈值（阈值留给评测阶段定）。

### 6.4 安全垫

`compute_budget` 增加 `estimate_margin_ratio: float = 0.1`：

```python
fixed = int(prefix_tokens * (1 + estimate_margin_ratio))
history_max = max(0, threshold - fixed - output)
```

估算低估时，先吃掉安全垫，而不是吃掉模型窗口。副作用是 `history_max` 约少 300 token（0.3%），可接受。配置项进 `config/memory.yaml` 的 `budget` 段，默认 0.1。

### 6.5 验收

- [ ] 上述三条测试
- [ ] `estimated`/`actual` 在真实模型下偏差 < 12%（一次手工 run，记入 PR 描述）
- [ ] `estimate_margin_ratio=0` 时 `Budget` 与现状逐字段相等（向后兼容）

## 7. 事件与接口变更汇总

### 7.1 事件

| 事件 | 变更 | 兼容性 |
|---|---|---|
| `MEMORY_RETRIEVE_END` | `reason` 枚举化，新增 `memory.degraded` | 新增字段，前端忽略即兼容 |
| `MEMORY_WRITE_END` | 新增 `layer="turn"` 用法；`reason` 枚举化 | 同上 |
| `CONTEXT_ESTIMATE_DELTA` | 新增 | 前端未消费前无影响 |
| `redaction.capture` | 新增 span（计数级） | 新增 |

枚举常量统一放 `memory/events.py`（`REASON_*`），避免字符串散落。

### 7.2 接口

- `GET /api/memory/status` → `+health`
- `GET /health` → `+memory.health.degraded`
- 其余不变；`/api/memory/session/{id}` 的 `active` 预览逻辑不动

## 8. 实施顺序（4 个 PR，各自独立可回滚）

| PR | 内容 | 关键门禁 |
|---|---|---|
| 1（C） | `redaction/rules.py` 单源 + 四道闸 + 跨度替换 + code 档案 + 全库扫描测试 + 保险箱审计脚本 | 全库扫描 0 误伤；`.env` 仍命中 |
| 2（A） | `RecallResult` + 事件枚举 + 尾帧补发 + `memory/health.py` + 两个接口 | 5 条验收用例；事件数无回归 |
| 3（B） | `memory/turns.py` + 两处改调 + 架构守卫测试 | 行为等价回归 |
| 4（D） | fixture + 三条测试 + `CONTEXT_ESTIMATE_DELTA` + `estimate_margin_ratio` | 兼容性：margin=0 时逐字段相等 |

每个 PR 按 AGENTS.md §2 在 worktree 分支开发，本地跑齐 ruff / mypy / vulture / pytest（`--cov-fail-under=80`）/ `check_file_size.py`，CI `quality` 绿后 squash 合并。

## 9. 验收（端到端，一次手工 + 全部自动）

自动：`pytest tests/unit/test_redaction_repo_scan.py tests/unit/test_memory_failure_events.py tests/unit/test_turn_boundary_single_source.py tests/unit/test_memory_tokens_calibration.py -q`

手工（一次性，结果贴 PR）：

1. 让智能体执行 `Read src/devops_agent/memory/observer.py`，比对其输出与磁盘文件：**逐字节一致**；
2. 停掉长期后端（改配置指向不存在的路径），发一轮对话：
   - `/api/memory/status.health.degraded == true`；
   - 轨迹页出现 `memory.retrieve` 且 `status=error`、`error=long_term_unavailable`；
   - 对话正常完成（不阻断）；
3. 同一会话再发一轮：上一轮的 `memory.write` span 落在**上一轮**的 run 下（不是新一轮）。

## 10. 风险与决策点

| 风险 | 处置 |
|---|---|
| 收紧规则导致漏检真密钥 | 已知前缀档不过闸（`AKIA`/`sk-`/`ghp_`/PEM）；赋值式档保留；`profile="output"` 的熵兜底仍开。漏检代价是「明文进上下文」，误检代价是「代码不可用」——在代码档案里明确选择前者 |
| 已污染的保险箱条目被 `inject` 回填 | 审计脚本默认 dry-run；`--purge` 前打印全部候选；不做自动删除 |
| 事件变多拖慢 SSE | 新增事件全部 `debug` 或每 run 一次；`redaction.capture` 只记计数 |
| `metadata.injected` 在旧快照里缺失 | `_LEGACY_INJECTED_NAMES` 兜底一个版本，下个版本删除 |
| 安全垫让预算变小，压缩更早触发 | 0.3% 量级，可接受；`estimate_margin_ratio` 可配 0 回退 |

## 11. 未决

1. `redaction` 是否提升为顶层包并加 `.importlinter` 叶子契约（倾向：是，两个消费方 + 未来 `gateway` 的输入回显也要用）。
2. `health` 计数是否需要持久化到 `runs` 表以便跨重启诊断（倾向：首版不持久化，进程内存够回答「现在坏没坏」）。
3. `profile="code"` 的判定是按工具名硬编码，还是让工具自己在 schema 里声明 `output_profile`（倾向：先硬编码在 `secrets/middleware.py` 的一张映射表里，MCP 工具接入时再改为声明式）。
4. `CONTEXT_ESTIMATE_DELTA` 是否进前端上下文面板（依赖前端排期，不阻塞本规格）。
