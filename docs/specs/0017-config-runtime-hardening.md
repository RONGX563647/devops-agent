# SPEC-0017：配置与启动层硬化（坏库降级、写事务、失败可见）

- 状态：已实施
- 日期：2026-11-27
- 基线：`main ab23d4c`
- 关系：落实 ADR-0005 §1 的「读降级 / 写权威」，与 SPEC-0013 §18.2 / SPEC-0015 §4 的「失败必须可见」对齐；修正 ADR-0005 未覆盖的三处实现偏差（来源判定已由 #136 修正）
- 范围：`config_schema.py`（新）、`config.py`、`config_store/{store,layers,errors,health,schema,registry,api,__init__}.py`、`gateway/{app,bootstrap}.py`、`gateway/routes/health.py`、`model_settings/resolve.py`、`web/src/types/{config,health}.ts`、`tests/unit/test_config_runtime_hardening.py`（新）
- 触发来源：一次配置与启动层走查（4 个 P0/P1 + 6 个 P2）
- 证据约定：现状结论一律带 `路径:行号`（指向改动前的 `ab23d4c`）；实测结论标明复现方式

## 1. 设计结论

1. **坏库不是「配置问题」，是「可用性问题」**。配置库读不出来时所有域都静默回落代码默认值；但旧实现让 `import devops_agent.gateway.app` 直接抛 `sqlite3.DatabaseError`（`gateway/app.py:64` → `runtime/assembly.py` → `load_memory()` → `store.effective`），端口根本起不来。**读降级、写拒绝**才是正确形状：降级保住可用性，拒绝保住数据。
2. **降级必须留痕**。记忆层已经用 `memory/health.py` 解决了「静默停摆」，配置层当时是唯一例外。这里复用同一形状：按来源分桶计数、`degraded` 只看最近一次、由 `/health` 暴露。
3. **乐观锁不能靠「读一次版本号」**。`patch` 分三次独立连接（`store.py:368` 读 → 校验 → `_write`），SQLite 默认隔离级别下读版本发生在事务之外，十个并发写者全读到 v1。唯一能保证单调的是 `BEGIN IMMEDIATE`：**先拿写锁，再读版本**。
4. **失败语义要按「这句话会不会被当成事实」分类**：`resolved` 可以降级（否则服务不可用）；`audit` / `export` 不能降级成空（空列表 = 「没人改过」，空 yaml = 「没有覆盖」，都是谎报）。
5. **「巧合正确」不算正确**。`policy.yaml` 的段名靠 `raw.get(section, raw)` 兜底才读对（`store.py:112`），文件顶层多一个键就整段失效；`config.py ↔ config_store` 的双向依赖靠 `importlib` 在调用期绕开（`registry.py:116`），静态检查看不见。两处都用显式声明替代。

## 2. 现状证据（改动前）

| 编号 | 断言 | 证据 | 复现 |
|---|---|---|---|
| E-1 | 坏库拦住进程启动 | `gateway/app.py:97` 模块级 `create_app()`；`app.py:64` → `load_memory()` → `store.effective` | 把 config.db 写成垃圾字节 → `import devops_agent.gateway.app` 抛 `sqlite3.DatabaseError` |
| E-2 | 坏库时读接口 500 | `store.py:136` 只 catch `OSError`，sqlite 的 `DatabaseError` 继承 `Error` 不继承 `OSError` | 同场景 `GET /api/config/input` → 500 |
| E-3 | 并发写版本号全为 1 | `store.py:368-393` + `store.py:301-322`（`version = before.version + 1`） | 10 线程写 10 个不同键 → 全部返回 v1，库里 10 行 |
| E-4 | 三条读路径三种失败答复 | `store.py:136`（穿透）/ `model_settings/resolve.py:116`（`except Exception` 静默）/ `config_files.py:34`（缺文件返回空） | 并列可见 |
| E-5 | drift 提示永不消除 | `_write_one` 的 upsert（`store.py:344-351`）不更新 `default_at_write` | 写 9 → 篡改 `default_at_write` → drifted 出现 → 写 10 → drifted 仍在 |
| E-6 | `reset` 拼错键静默成功 | `store.py:396-410` 无 `_check_keys`（`patch` 有） | `reset("input", ["max_referances"])` → 版本不变、无异常 |
| E-7 | `/health` 模型名与实际不符 | `bootstrap.py:46` 读 `Settings().model()`（只读 env），实际用 `model_settings.resolve()` | env=env-model、文件层=file-model → 实际用后者，`/health` 报前者 |
| E-8 | 面板首屏 6 次连接 + 6 次 DDL | `store.py:225-243` 每域一次 `_db_layer`；`schema.py:92` 每次 `connect()` 都 `executescript` | `index()` 实测 6 次连接 |
| E-9 | 模式拼错静默当 file | `schema.py:51-53` 不做取值校验 | `DEVOPS_AGENT_CONFIG_MODE=dbb` → 报错文案仍写 `=file` |
| E-10 | `persisted` 在清空后仍为 True | `api.py:111`（`version > 0`） | `patch` 一行 → `reset(all=True)` → 库 0 行，`persisted=true` |
| E-11 | policy 段靠兜底巧合 | `store.py:112`（`raw.get(spec.section, raw)`） | `export_yaml("policy")` 输出 `policy: {}`，贴回会让 `confirm` 整段失效 |
| E-12 | 模型与存取层双向依赖 | `registry.py:110-117`（`importlib.import_module("devops_agent.config")`） | 依赖图上无环，实际是「运行时才连」 |

## 3. 失败语义矩阵（实施后）

| 入口 | 库坏 / 读不到 | 理由 |
|---|---|---|
| `resolved` / `effective`（health、input、memory、gateway 加载器） | **降级为空覆盖**，记 `health("db")` | 否则整个服务不可用；降级后行为等于「从没调过参」 |
| `/api/config`、`/api/config/{domain}` | 200 + `degraded=true` + `db_error` | 面板要能打开并显示「为什么值都是默认」 |
| `patch` / `reset` | 抛 `ConfigUnavailable` → **503** | 写进坏库 = 数据消失且无人知晓 |
| `audit` / `export_yaml` | 抛 `ConfigUnavailable` → **503** | 空列表/空 yaml 会被当成事实 |
| `model_settings` 部署默认 | 落空 + 记 `health("model")` + warning | 回落 yaml / env，与配置库互不牵连 |
| `/health` | 200，`config.degraded=true`、`config.error` | 探针不该因降级重启容器 |
| `create_app` | 不抛；运行时降级 Echo、网关策略用代码默认 | 端口连不上比降级更难查 |
| `DEVOPS_AGENT_CONFIG_MODE` 非法 | **启动即 `ValueError`** | 静态错误，两种模式语义完全不同，猜一个更糟 |

## 4. 写事务（P0）

```python
# config_store/schema.py
@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path(), timeout=10.0, isolation_level=None)
    init_db(conn)  # DDL 在事务外，避免 DDL 隐式提交
    conn.execute("BEGIN IMMEDIATE")  # ← 先拿写锁
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
```

`patch` 的整个「读版本 → 校验 → 归一化 → 写行 → bump」都在这一段里完成：

```text
BEGIN IMMEDIATE
  layer = read_layer(conn, domain)      # 版本号、覆盖行
  if expected != layer.version: raise ConfigConflict   # 409
  normalized = _normalize(floor_flat(spec), candidate)
  build(spec, DbLayer(...))             # pydantic 校验（越界 400）
  version = _next_version(conn, domain) # 事务内读 → +1 → upsert
  for key in changed: _write_one(...)   # 行 + 审计
COMMIT
```

三个直接结果：

- 版本号严格单调：10 并发 → `1..10`，审计里 `version` 各不相同；
- 校验失败 / 冲突时**一行都不写**（ROLLBACK），不再出现「写了 3 个键，第 4 个越界」的半截状态；
- `expected` 冲突时的 `payload` 是事务内读到的真实快照，不是 TTL 缓存。

## 5. 其余修复

| 项 | 改动 | 验收 |
|---|---|---|
| drift 可清除 | `_write_one` 的 upsert 增加 `default_at_write=excluded.default_at_write` | `test_drift_clears_after_reconfirming_new_default` |
| `reset` 键校验 | `reset` 对 `keys` 走 `_check_keys` | `test_reset_rejects_unknown_key` |
| `persisted` 语义 | `Snapshot.persisted`，由 `_write` 的 `changed` 决定 | `test_persisted_reflects_actual_write` |
| 面板一次连接 | `layers.db_layers(domains)`：一次 `connect()` 读完所有域 | `test_index_opens_one_connection` |
| 模式校验 | `schema.mode()` 只认 `db` / `file`，非法即 `ValueError`；`create_app` 启动时调用 | `test_invalid_mode_is_rejected` |
| policy 段 | `DomainSpec.root=True`：整份文件即负载，去掉 `raw.get(section, raw)` 兜底 | `test_policy_file_is_read_as_root` |
| 健康页模型名 | `bootstrap._effective_model()` 走 `model_settings.resolve(stored_defaults())` | `test_health_reports_effective_model_not_just_env` |
| 解双向依赖 | 模型下沉 `config_schema.py`；`registry.MODELS` 静态映射，删 `importlib` | `lint-imports` 11 contracts kept |
| 类型契约 | `web/src/types/config.ts` 增 `ConfigHealth` / `persisted`；`health.ts` 增 `ConfigHealth` | frontend typecheck |

`config.py` 对下沉后的模型全部 `from devops_agent.config_schema import X as X` 再导出 —— 旧 import 路径（`from devops_agent.config import GatewayPolicy`）一个都不变。这是硬要求：`gateway/settings.py` 是契约叶子，它的 import 行不该因重构而变化。

## 6. 接口变更

```jsonc
// GET /health
{
  "status": "ok",
  "runtime": { "kind": "agentscope", "model": "file-model" },   // ← 取实际生效值
  "config": { "mode": "db", "degraded": true, "error": "DatabaseError: file is not a database" },
  "memory": { ... }
}

// GET /api/config、GET /api/config/{domain}
{ "mode": "db", "db_path": "...", "degraded": false, "db_error": "", ... }

// PATCH /api/config/{domain}、POST /api/config/{domain}/reset
{ ..., "persisted": true }    // 本次是否真的落库

// 503
{ "detail": { "code": "config_unavailable", "message": "配置库不可写：..." } }
```

新增字段全部是**追加**，前端类型里可选（`degraded?` / `db_error?` / `persisted?`），旧服务端返回不带这些字段也不会炸。

## 7. 验收

```bash
ruff check . && ruff format --check .
mypy src
python -m radon cc src -s -n C        # 无 >10 的块
lint-imports                          # 11 contracts kept
python scripts/check_file_size.py     # 500 行棘轮
pytest -q                             # 覆盖率门禁 80%
```

新增用例 16 条（`tests/unit/test_config_runtime_hardening.py`），逐条对应 §2 的 E-1…E-12，不复述「能不能写进 SQLite」。

## 8. 未做与后续

- **`PatchRequest.by` 仍由客户端自报**（审计可伪造）。要改需先定「谁算操作者」：本机单人场景下 IP / 头都可以伪造，意义有限；多用户部署时应从会话推导。留给「配置中心多用户」这一需求出现时一起做。
- **`db_path` 绝对路径回给前端**：本地工具面板需要它显示「写到哪里」，暂不改；若将来做远程部署，应改为相对路径或只回文件名。
- **`connect()` 仍每次 `executescript` 建表**：`index()` 已从 6 次降到 1 次，剩余频率由 `resolved` 的 2s TTL 决定，实测开销可忽略。若将来读路径进入高频循环，再加「路径 + mtime」的 DDL 缓存。
