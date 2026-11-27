# SPEC-0020：短期裁剪的保真性（最新工具结果豁免 / 预算接线 / 估算补漏）

- 状态：已实施（打分口径重标定除外，见 §7.5）
- 日期：2026-11-27
- 基线：`main e1b3f6b`
- 关系：落地 SPEC-0016 §5.2（S0–S5 裁剪算法）与 §5.3（`dedup_reads` 三态）中**已写未实现**的部分，并修正 §5.2 的一处实现级错误；与 SPEC-0015 §3（脱敏档案）为补充关系
- 触发来源：一次针对裁剪器的外部走查（7 条观察：4 条成立、2 条部分成立、1 条不成立，逐条核验见 §2.1）
- 范围：`memory/{short_term,tokens,observer,runtime,long_term,candidate}.py`、`memory_policy.py`、`redaction/rules.py`、`secrets/middleware.py`、四个对应测试文件
- 证据约定：现状结论一律带 `路径:行号`（指向 `e1b3f6b`）；实测结论标明复现方式

## 1. 设计结论

1. **保护方向要按「是否已消化」划，不是按「消息新旧」。** `_truncate_blocks` 无条件作用于全部消息（`short_term.py:167`），最新一轮的原始工具返回与十轮前已被总结成 assistant 文本的内容共用同一个 1600 字符上限。文本消息（非 `tool_result`）根本不裁，于是「已消化的留下、未消化的砍掉」——方向是倒的。docstring `short_term.py:3` 的「最近一轮永远保留」保留的是**消息对象**，不是内容，这句承诺读起来像豁免，实际不豁免。
2. **裁剪应由预算触发，而不是每轮固定触发。** `trim_messages(messages, policy)`（`short_term.py:155`）没有预算形参，所以每个未超预算的轮次都在改写历史。SPEC-0016 §5.2 已经写了 S0 早退与 S1/S3 分级，代码里没有——这是本 spec 的主线。
3. **S0 的前置条件是估算器必须看得见工具结果。** `observer._text_of`（`observer.py:26-35`）只读块的 `.text` 字段（`observer.py:32`），而 `ToolResultBlock` 的字段是 `output`。实测 5000 字符的工具结果估算为 0 token。**不先修这条，S0 会永远早退、裁剪永不发生**——从「乱砍」直接翻到「不砍」，同样是错的。
4. **截断必须可复原，且切在行边界。** `text[:head]`（`short_term.py:53`）常落在行中，模型拿到半行代码会误判语法；标记只写「已截断 N 字」，不给下一步动作。改成行感知切点 + 带原文行号区间的指引。
5. **快照写回是唯一不可逆的丢失路径。** 正常轮次的裁剪只改请求副本（原文仍在 `agent.state.context`），但 `cap_snapshot`（`runtime.py:296`）在 `report.changed` 时把结果**写回** `agent.state.context`（`runtime.py:307-308`），而 `trim_context_for_snapshot` 内部先跑常规 `trim_messages`（`short_term.py:180`），任何 1600 截断都会让 `changed=True`。触发点是切档/重建会话（`runtime/sessions.py:157`、`:200`），不是每轮——但一旦发生，原文永久消失。「临时裁剪」与「因体积而丢」必须是两个不同的信号。
6. **另外三条观察属于别的模块，不混进这次改动。** 脱敏残余误伤（`redaction`）、长期写入噪音与打分饱和（`candidate` / `long_term`）的判据与裁剪正交，混在一起会让回归定位变难，放 §7 另案。

## 2. 现状证据

| 编号 | 断言 | 证据 | 复现 |
|---|---|---|---|
| E-1 | 最新工具结果被无差别裁剪 | `short_term.py:167` 列表推导覆盖全部消息 | `truncate_output(runtime.py 全文 14356 字符, 377 行, 1600, 0.7)` → 1617 字符：头 1120 字符 / 38 行、尾 480 字符 / 13 行，省略 12756 字 / 326 行 |
| E-2 | 裁剪不看预算 | `trim_messages` 签名只收 `(messages, policy)` | `rg "trim_messages" src/` → 4 处调用全部两参 |
| E-3 | 估算漏算全部工具结果 | `observer.py:32` 只取 `block.text`；`ToolResultBlock.model_fields = {type,id,name,output,state,metadata,created_at,finished_at}` | `ToolResultBlock(output="x"*5000)` → `_text_of` 返回 `""`、`estimate_tokens` 返回 `0`，而 `model_dump` 为 5423 字符 |
| E-4 | 未超预算也改写历史（违反 SPEC-0016 §6 V3） | 同 E-2 + E-1 | 12 轮内的任意会话每轮都进 `_truncate_blocks` |
| E-5 | 同一段内容每轮被切同一刀 | trim 只改副本，`messages` 来自 `agent.state.context` | 对同一 context 连续调两次 `trim_messages`，`reclaimed_chars` 完全相同 |
| E-6 | 切档时裁剪被写回、不可逆 | `runtime.py:307-308`（`if report.changed: agent.state.context = trimmed`）+ `short_term.py:180` | 构造 5000 字符工具结果 → `cap_snapshot()` → `state.context` 变 1617 字符 |
| E-7 | 切点落在行中 | `short_term.py:53` `text[:head]` | 头部末两行为 `def _hit_hash(hits: list[Hit]) -> str:` / `    bod`，函数体被拦腰切断 |
| E-8 | 去重只认完全相同的返回 | `short_term.py:101` 键 `(name, hash(text))` | 全量读一次 + 分段读同一文件 → 两份内容都留着（冗余） |
| E-9 | 脱敏残余误伤 | `rules.py:168` 的标识符豁免加了 `and not has_digit`，`rules.py:163-177` 对含数字的值直接放行 | `token: sha256_checksum_v2_helper_name` → 命中（`confidence=assigned`）；`token = 128000 tiktoken_v2_encoder_helper` → `entropy` 命中 |

### 2.1 对原走查的逐条核验

| 原观察 | 结论 | 依据 |
|---|---|---|
| `head=1120` / `tail=480`，「分毫不差」 | **成立** | E-1（原走查是 297 行版本 → 28 行头；当前 377 行 → 38 行，机制一致） |
| 最新一轮与旧数据共用 1600 上限 | **成立** | E-1 |
| 每轮重裁同一刀、原文仍在 `state.context` | **正常轮次成立，切档时不成立** | E-5 / E-6 |
| 「省 O(n)，付 O(n²)」 | **方向成立，口径需修正**：裁剪不改变「每轮重放历史」这一固有成本。真正的亏空是误伤导致的额外轮次——单轮省 3785 token（实测 4288 → 503），一次返工要重放约 25k token 历史，即倒亏 6 倍 | 实测 |
| 去重键造成「同文件分段读」盲区、丢信息 | **不成立**：全文哈希不会误删任何内容，只是漏合并（残留冗余）。这是优化点，不是 bug | E-8 |
| 高熵误伤函数名/模块路径 | **当前 main 已基本修复**（#132 只把「全小写」认作标识符），残余面是「含数字的小写标识符 + 同行凭据词」，且走 `assigned` 路径而非 `entropy` | E-9 |
| 长期记忆写入噪音、分数恒 1.00 | **成立**：写入侧噪音已在 §7.4 修掉；打分饱和见 §7.5（未做） | §7.4 / §7.5 |

## 3. 裁剪算法（重写 `trim_messages`）

```text
输入：messages, policy, history_limit(tokens) | None

S0  若 history_limit 非空且 tokens(messages) ≤ history_limit
       → 原样返回（元素不重建），report.changed=False
S1  从尾部数出最近 policy.recent_exempt_results 个 tool_result 块 → 豁免集
       （按「块」计数，不按「消息」：一次并行 5 个 Read 是 5 个块）
S2  去重：只在未豁免块之间做（重复读到的是旧副本才该让步）
S3  未豁免块：超 tool_output_chars 时按行边界头尾截断，标记带行号与 offset
S4  淘汰最旧整轮，直到轮数 ≤ max_turns
S5  仍超 history_limit（单轮本身过大）
       → 豁免块按 recent_output_chars 截断，标记写明原文总行数
```

三条不变量：

- **I1 元素不被原地修改，但返回的 list 必须是新 list。** SPEC-0016 §5.2 写「S0 返回同一个 list 对象」是错的：`on_model_call` 会对返回值执行 `trimmed.append(injected_note(...))`（`observer.py:111`），若与 `input_kwargs["messages"]` 同一对象，记忆注脚会写进历史。S0 只保证「不重建元素」，不保证「list 同一」。
- **I2 豁免只作用于内容截断，不作用于整轮淘汰。** 最新一轮本来就不会被 S4 选中，两条规则不冲突。
- **I3「内容被改写」与「整轮被丢」是两个信号。** `report.changed` 只表示前者；快照侧据 `dropped_turns` 判断是否真该写回（见 §5）。

`history_limit` 取自现有预算：`budget.short_term + budget.headroom`（SPEC-0016 §5.1 口径）。`on_model_call` 里已经为 `CONTEXT_ASSEMBLED` 算过一次 `estimate_prefix_tokens`（`observer.py:291`），把它提前到裁剪之前复用，不增加估算次数。

## 4. token 估算补漏（S0 的前置，P0）

`observer._text_of` 改成「任意内容块 → 文本」：

```python
def _block_text(block: Any) -> str:
    if isinstance(block, ToolResultBlock):  # 字段是 output，不是 text
        raw = getattr(block, "output", "")
        return raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
    text = getattr(block, "text", None)  # TextBlock / ThinkingBlock
    return text if isinstance(text, str) else ""
```

顺带修好两处被同一个 bug 影响的地方：`_emit_assembled` 的 `history.chars`（`observer.py:300`）与 `CONTEXT_ESTIMATE_DELTA` 的 `ratio`（`observer.py:196-198`）。后者的语义是「估算 / 实测」，工具结果漏算时该比值系统性偏大，等于把一个可用的对账信号变成了噪音。

## 5. 截断形态与快照写回

### 5.1 行感知截断

```python
def truncate_output(text: str, limit: int, head_ratio: float) -> tuple[str, int]:
    """返回 (截断后文本, 丢弃行数)；切点回退到行边界，标记带原文行号与 offset。"""
```

切点回退规则：若 `text[:head]` 的最后一个换行距 head 不超过 `head * 0.2`，就退到该换行；否则保持原样（避免超长单行文本退化成空）。标记形如：

```text
…[已截断 12756 字 / 326 行（原第 39–364 行）：用 Read(offset=39, limit=60) 或更窄的 grep 取中段]…
```

标记里必须写「原文行号区间」而不是只写字数：模型能据此直接算出 `offset`，不必整份重读——这是把返工成本从「4 次全量 Read」降到「1 次定向 Read」的关键。

### 5.2 快照写回（P1）

`cap_snapshot` 当前把两种丢失混成一种（`runtime.py:307-308`）。改法：

- `trim_context_for_snapshot` 只做「体积超限丢整轮」，不再复跑常规 `trim_messages`；常规裁剪是请求期行为，不该被固化；
- `cap_snapshot` 的写回条件从 `report.changed` 收窄为 `report.dropped_turns > 0`；
- 发 `MEMORY_TRIM(reason=snapshot_cap)` 的判定同步收窄。

## 6. 策略、报告与接口

`ShortTermPolicy` 新增字段（全有默认值，不改现有 yaml）：

| 字段 | 默认 | 含义 |
|---|---|---|
| `recent_exempt_results` | 4 | 尾部 N 个工具结果不参与内容截断 |
| `recent_output_chars` | 6000 | S5 时最新工具结果的上限（比旧轮次的 1600 宽 3.75 倍） |
| `trim_on_budget` | True | 关掉即回到旧的无条件裁剪，用于评测对照 |

`TrimReport` 新增 `exempt_results: int`、`truncated_lines: int`、`stage: str`（`s0`/`s2`/`s3`/`s4`/`s5`）。`MEMORY_TRIM` 帧带上 `exempt_results`、`truncated_lines` 与 `stage`。

`/health.memory.last_trim` **未做**：事件流里已经有每帧的完整报告，再加一个「最近一次」的快照等于把同一份数据存两处，收益只是少翻一次事件；等面板确实需要「不展开事件流也能看到裁剪」时再加。

## 7. 相邻修复（原 P2，本次已做）与未做项

1. **区间去重（已做）**：`ToolCallBlock.input` 在框架里是 **JSON 字符串**而不是 dict（实施时才发现），`_call_inputs` 需要解析。去重键从 `(工具名, 全文哈希)` 扩成「同目标、后读区间覆盖前读区间」，全文哈希保留为拿不到入参时的兜底。豁免集内的块不参与替换（内容重复也不动），因为它们是下一步推理的依据。
2. **Bash 读源码的档案（已做，收益面比预期窄）**：`_profile_for` 现在按命令判 `cat` / `sed -n` / `grep` 等只读命令走 `code` 档案，出现重定向、`sed -i`、`tee` 一律不降档。实施时确认：`code` 与 `output` 的差异**只在熵兜底**，而 E-9 的误伤走的是 `assigned` 路径——所以真正修掉它的是 §7.4 的标识符闸，不是档案降档。降档的价值在于收窄「纯熵命中」那一小块，且不能反向放宽：下划线分段的真 key（`sk_live_...`）由前缀表兜住。
3. **标识符闸（已做）**：`token: sha256_checksum_v2_helper_name` 这类「含数字的小写标识符 + 同行凭据词」不再被判为凭据。新判据按 `_` 分段看「每段是否像英文词」，单段小写 alnum 串（`abcdefghij1234567890`）仍按随机串处理。
4. **长期写入去噪（已做）**：`is_candidate` 新增 `meta_preamble`——无技术信号 + 元话语开头（「我先看一下……」）+ 短，三者同时成立才拒；带路径/命令/后缀的回合照常写入。
5. **打分口径重标定（未做）**：`score = overlap / sqrt(len(query_tokens))` 饱和快是事实，但评测阈值 `EVAL_MIN_SCORE=0.7` 正是照这个口径标定的（对 8 个 2-gram 的查询等价于「≥2 个命中」）。实测改成线性归一会让 `mt-001` 的召回分从 1.00 掉到 0.375、评测直接转红——换口径必须同轮重标定整条评测基线，属于独立一轮，不混进裁剪修复。

## 8. 验收（实施后核对）

| 编号 | 用例（`tests/unit/`） | 断言 | 结果 |
|---|---|---|---|
| T-1 | `test_recent_tool_results_are_exempt_from_truncation` | 最后 2 个（`recent_exempt_results=2`）逐字节不变，旧的两条被截断且带标记 | 通过 |
| T-2 | `test_untrimmed_when_under_budget` | `changed is False`、`stage=="s0"`、元素同一对象、列表是新对象 | 通过 |
| T-3 | `test_over_budget_truncates_only_old_results` | `stage=="s3"`，最新结果完整 | 通过 |
| T-4 | `test_single_turn_over_budget_falls_back_to_recent_cap` | `stage=="s5"`，输出落在 `recent_output_chars` 与含标记区间内 | 通过 |
| T-5/T-6 | `test_truncate_output_marks_original_line_range` | 标记前最后一行是完整行；能解析出「原第 X–Y 行」 | 通过 |
| T-7 | `test_estimate_includes_tool_output` | 5000 字符工具结果估算 > 1000 token | 通过 |
| T-8 | `test_snapshot_trim_never_truncates_content`、`test_cap_snapshot_does_not_bake_request_trim` | 快照只丢整轮；`state.context` 原文不被改写 | 通过 |
| T-9 | `test_zero_exempt_and_off_budget_matches_old_behaviour` | `trim_on_budget=False` + `recent_exempt_results=0` 回到旧行为 | 通过 |
| T-10 | `test_dedupe_by_read_span_supersedes_covered_read` | 被后读区间覆盖的旧读取被省略，豁免集内不动 | 通过 |
| T-11 | `test_identifier_with_digits_is_not_a_secret` 等 3 条 | 含数字标识符不误伤；单段随机串仍命中；下划线 key 由前缀兜住 | 通过 |
| T-12 | `test_readonly_shell_command_uses_code_profile` | 只读命令降档，写盘与 `printenv` 不降档 | 通过 |
| T-13 | `test_meta_preamble_is_not_candidate` 等 2 条 | 元话语被拒；带技术信号的保留 | 通过 |

既有用例改动：`test_trim_truncates_long_tool_result_and_keeps_current_input` 原来断言「唯一的、最新的工具结果被截断」，与 T-1 直接冲突；已由 T-1 与 `test_over_budget_truncates_only_old_results` 取代（前者把「当前用户输入不裁」并入断言）。

## 9. 回滚

`trim_on_budget=False` + `recent_exempt_results=0` 即恢复旧行为（除了 §4 的估算修复，那是纯 bug 修复）。开关保留到评测对比跑完，之后按 SPEC-0016 §8 的方式删掉。
