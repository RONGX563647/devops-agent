/** 轨迹步骤列表：轮次为分隔节点，步骤一行一条，异常与 diff 就地展开。
 *
 * 三种排布对应顶部切换：按时长（按发生顺序）、按轮次（可折叠）、按调用（工具聚合）。
 */
import { useState } from 'react';
import type { AgentContext } from '../api/client';
import type { TraceStep, TraceTurn } from '../types';
import { formatDuration } from './time';

export type TraceMode = 'time' | 'turn' | 'call';

interface Props {
  turns: TraceTurn[];
  mode: TraceMode;
  query: string;
  activeStepId: string | null;
  activeTurnId: string | null;
  context: AgentContext | null;
  onSelectStep: (step: TraceStep) => void;
  onSelectTurn: (turn: TraceTurn) => void;
}

const CAPTIONS: Record<TraceMode, string> = {
  time: '按耗时降序：全局最慢的步骤排最前，右侧标出它属于第几轮',
  turn: '按对话轮次分组：点轮次标题折叠整轮，点任一步骤行收起或展开原文',
  call: '按工具聚合：被调用几次、合计多久、最慢一条',
};

const BADGE: Record<TraceStep['kind'], string> = {
  tool: '工具',
  text: '思考',
  model: '模型',
  error: '异常',
  parse: '解析',
};

/** 这几类标题只是类别名，和行首徽标重复，不再单独占一列。 */
const GENERIC_TITLES = new Set(['思考', '模型输出', '输入解析', '模型调用']);

/** 搜索命中高亮：只标出命中片段，不改变行高。 */
function Highlight({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  const lower = text.toLowerCase();
  const needle = query.toLowerCase();
  const parts: (string | { hit: string })[] = [];
  let cursor = 0;
  for (let at = lower.indexOf(needle); at >= 0; at = lower.indexOf(needle, cursor)) {
    if (at > cursor) parts.push(text.slice(cursor, at));
    parts.push({ hit: text.slice(at, at + needle.length) });
    cursor = at + needle.length;
  }
  if (cursor < text.length) parts.push(text.slice(cursor));
  return (
    <>
      {parts.map((part, i) =>
        typeof part === 'string' ? (
          <span key={i}>{part}</span>
        ) : (
          <mark key={i}>{part.hit}</mark>
        ),
      )}
    </>
  );
}

/** 这一条是否命中搜索：标题、入参、返回、正文都算。 */
export function matchesStep(step: TraceStep, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [step.title, step.args, step.result, step.text, step.toolName]
    .join(' ')
    .toLowerCase()
    .includes(needle);
}

function Diff({ step }: { step: TraceStep }) {
  if (!step.diff) return null;
  return (
    <div className="trace-diff">
      {step.diff.oldLines.map((line, i) => (
        <div className="diff-old" key={`o${i}`}>
          - {line || ' '}
        </div>
      ))}
      {step.diff.newLines.map((line, i) => (
        <div className="diff-new" key={`n${i}`}>
          + {line || ' '}
        </div>
      ))}
    </div>
  );
}

/** JSON 入参折成多行再看；不是 JSON 就原样返回，别把日志改样。 */
function pretty(text: string): string {
  const raw = text.trim();
  if (!raw.startsWith('{') && !raw.startsWith('[')) return raw;
  try {
    return JSON.stringify(JSON.parse(raw) as unknown, null, 2);
  } catch {
    return raw;
  }
}

/** 就地展开的原文：入参 / 返回 / 正文，先看关键两句再看全文。 */
function StepDetail({ step }: { step: TraceStep }) {
  const blocks: { label: string; text: string }[] = [];
  if (step.args) blocks.push({ label: '入参', text: pretty(step.args) });
  if (step.result) blocks.push({ label: '返回', text: step.result });
  if (blocks.length === 0 && step.text) {
    blocks.push({ label: step.title === '思考' ? '思考' : '正文', text: step.text });
  }
  if (blocks.length === 0) {
    return <div className="trace-detail"><p className="trace-detail-empty">这一步没有可展开的原文。</p></div>;
  }
  return (
    <div className="trace-detail">
      {blocks.map((block) => (
        <div className="trace-detail-block" key={block.label}>
          <span className="trace-detail-label">{block.label}</span>
          <pre className="trace-detail-code mono">{block.text}</pre>
        </div>
      ))}
    </div>
  );
}

/** 行内摘要已说完的步骤（解析计数、短思考、短输出）不再给箭头，免得点开是同一句话。 */
function expandable(step: TraceStep): boolean {
  if (step.args || step.result) return true;
  return step.kind === 'text' && (step.text ?? '').length > 100;
}

/** 模型这一步的缓存命中率：缓存令牌 ÷ 入参令牌。没有字段时返回 null，不编数。 */
function cacheRate(step: TraceStep): number | null {
  if (!step.cacheTokens || !step.tokensIn) return null;
  return Math.round((step.cacheTokens / step.tokensIn) * 100);
}

/** 令牌数按人眼习惯缩写；只用于估算值，精确值一律来自模型回的 input_tokens。 */
function fmtTok(value: number): string {
  if (value >= 10_000) return `${Math.round(value / 1000)}k`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

/** 上下文构成条：哪一段占了多少，一眼看出「工具 schema 才是大头」。 */
function Composition({
  system,
  tools,
  history,
  input,
  policy,
}: {
  system: number;
  tools: number;
  history: number;
  input: number;
  policy: number;
}) {
  const total = Math.max(1, system + tools + history + input + policy);
  const parts = [
    { key: 'system', label: '系统提示', value: system },
    { key: 'tools', label: '工具 schema', value: tools },
    { key: 'history', label: '历史', value: history },
    { key: 'input', label: '本轮输入', value: input },
    { key: 'policy', label: '档位约束', value: policy },
  ];
  return (
    <div className="ctx-comp">
      <div className="ctx-comp-bar" role="img" aria-label={`上下文构成 ${total} token`}>
        {parts.map((part) =>
          part.value > 0 ? (
            <span
              key={part.key}
              className="ctx-seg"
              data-kind={part.key}
              style={{ flexGrow: part.value }}
              title={`${part.label} ${part.value.toLocaleString()} tok`}
            />
          ) : null,
        )}
      </div>
      <div className="ctx-legend mono">
        {parts.map((part) => (
          <span key={part.key}>
            <i data-kind={part.key} aria-hidden="true" />
            {part.label} {fmtTok(part.value)}
          </span>
        ))}
      </div>
    </div>
  );
}

/** 每轮第 0 步：模型动手前手里有什么——系统提示、工具 schema、历史、这条输入。 */
function ContextRow({ turn, context }: { turn: TraceTurn; context: AgentContext | null }) {
  const [open, setOpen] = useState(false);
  const files = turn.attachments ?? turn.parse?.attachments ?? 0;
  const tools = context?.tools ?? [];
  const prompt = context?.system_prompt ?? '';
  const prefix = context?.prefix_tokens ?? 0;
  // 首枪的输入就是「前缀 + 历史 + 这条输入」，命中量也只有这一枪能证明缓存是否可用
  const firstIn = turn.firstCall?.in ?? 0;
  const hit = turn.firstCall?.cache ?? 0;
  const inputChars = (turn.userText ?? '').length;
  const inputTok = Math.ceil(inputChars / 1.5);
  const policyTok = context?.policy_note_tokens ?? 0;
  const history =
    firstIn > prefix ? Math.max(0, firstIn - prefix - inputTok - policyTok) : 0;
  const hitPct = firstIn > 0 && hit > 0 ? Math.round((hit / firstIn) * 100) : null;
  return (
    <>
      <div className="trace-step-wrap">
        <button
          type="button"
          className="trace-step"
          data-kind="context"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          <span className="trace-idx mono">00</span>
          <span className="trace-badge" data-kind="context">上下文</span>
          <span className="trace-main">
            <span className="trace-summary mono">
              前缀 {fmtTok(prefix)} tok · 工具 {tools.length || turn.tools} 个 · 历史 ≈
              {fmtTok(history)} · 输入 {inputChars} 字
              {hitPct !== null ? ` · 命中 ${hitPct}%` : ''}
              {files ? ` · 附件 ${files}` : ''}
            </span>
          </span>
          <span className="trace-dur mono">—</span>
          <span className="trace-dot" aria-hidden="true" />
          <span className="trace-caret" aria-hidden="true">{open ? '⌄' : '›'}</span>
        </button>
      </div>
      {open && (
        <div className="trace-detail trace-context-body">
          {context ? (
            <>
              <div className="ctx-meta mono">
                <span>{context.profile_label}</span>
                <span>指纹 {context.prefix_fingerprint}</span>
                {firstIn > 0 && (
                  <span>
                    首枪 {hit.toLocaleString()} / {firstIn.toLocaleString()} tok
                  </span>
                )}
              </div>
              <Composition
                system={context.system_prompt_tokens}
                tools={context.tools_tokens}
                history={history}
                input={inputTok}
                policy={policyTok}
              />
            </>
          ) : (
            <p className="trace-detail-empty">上下文接口不可用，读不到这一轮装配了什么。</p>
          )}
          <div className="trace-detail-block">
            <span className="trace-detail-label">用户输入</span>
            <pre className="trace-detail-code mono">
              {turn.userText || '（空输入）'}
              {files ? `\n\n附件 ${files} 个` : ''}
            </pre>
          </div>
          <div className="trace-detail-block">
            <span className="trace-detail-label">
              系统提示词{context ? ` · ${context.system_prompt_chars} 字` : ''}
            </span>
            <pre className="trace-detail-code mono">{prompt || '读不到系统提示词。'}</pre>
          </div>
          {context && (
            <div className="trace-detail-block">
              <span className="trace-detail-label">档位约束 · 追加在消息末尾</span>
              <pre className="trace-detail-code mono">{context.policy_note}</pre>
            </div>
          )}
          <div className="trace-detail-block">
            <span className="trace-detail-label">
              工具{context ? ` · ${context.tools.length} 个 · ${fmtTok(context.tools_tokens)} tok` : ''}
            </span>
            {tools.length > 0 ? (
              <div className="ctx-tools">
                {tools.map((tool) => (
                  <details className="ctx-tool" key={tool.name}>
                    <summary>
                      <code>{tool.name}</code>
                      <span className="ctx-tool-meta mono">
                        {tool.tokens} tok · {tool.schema_chars} 字
                      </span>
                    </summary>
                    <p className="ctx-tool-desc">{tool.description}</p>
                    <pre className="trace-detail-code mono">
                      {JSON.stringify(tool.parameters, null, 2)}
                    </pre>
                  </details>
                ))}
              </div>
            ) : (
              <p className="trace-detail-empty">没有读到工具注册表。</p>
            )}
          </div>
        </div>
      )}
    </>
  );
}

function StepRow({
  step,
  query,
  active,
  expanded,
  turnIndex,
  onSelect,
  onToggle,
}: {
  step: TraceStep;
  query: string;
  active: boolean;
  expanded: boolean;
  /** 平铺视图里标出这一步属于第几轮；分组视图里用不着。 */
  turnIndex?: number;
  onSelect: (step: TraceStep) => void;
  onToggle: () => void;
}) {
  const duration =
    step.durationMs > 0
      ? `${step.measured ? '' : '≈'}${formatDuration(step.durationMs)}`
      : '—';
  const summary = step.argsSummary || step.resultSummary || step.text;
  const canExpand = expandable(step);
  const cache = cacheRate(step);
  return (
    <>
      <div className={`trace-step-wrap${active ? ' active' : ''}`}>
        <button
          type="button"
          className={`trace-step${active ? ' active' : ''}`}
          data-kind={step.kind}
          aria-current={active ? 'true' : undefined}
          aria-expanded={canExpand ? expanded : undefined}
          title={canExpand ? (expanded ? '点击收起原文' : '点击展开原文') : undefined}
          onClick={() => {
            onSelect(step);
            if (canExpand) onToggle();
          }}
        >
          <span className="trace-idx mono">{String(step.index).padStart(2, '0')}</span>
          <span className="trace-badge" data-kind={step.kind}>
            {step.title === '模型输出' ? '输出' : BADGE[step.kind]}
          </span>
          <span className="trace-main">
            {turnIndex !== undefined && (
              <span className="trace-turn-tag mono">第 {turnIndex} 轮</span>
            )}
            {!GENERIC_TITLES.has(step.title) && (
              <span className="trace-title mono">{step.toolName || step.title}</span>
            )}
            {summary && (
              <span className="trace-summary mono">
                <Highlight text={summary} query={query} />
              </span>
            )}
            {step.resultSummary && step.argsSummary && (
              <span className="trace-result">
                <span className="trace-arrow" aria-hidden="true">→</span>
                <Highlight text={step.resultSummary} query={query} />
              </span>
            )}
          </span>
          {cache !== null && (
            <span className="trace-cache" title="缓存命中：缓存令牌 ÷ 入参令牌">
              缓存 {cache}%
            </span>
          )}
          <span className="trace-dur mono" title={step.measured ? '事件时间戳' : '按相邻步骤估算'}>
            {duration}
          </span>
          <span className={`trace-dot ${step.status ?? 'success'}`} aria-hidden="true" />
          <span className="trace-caret" aria-hidden="true">
            {canExpand ? (expanded ? '⌄' : '›') : ''}
          </span>
        </button>
      </div>
      {step.diff && <Diff step={step} />}
      {canExpand && expanded && <StepDetail step={step} />}
    </>
  );
}

function TurnHeader({
  turn,
  collapsed,
  active,
  onToggle,
  onSelect,
}: {
  turn: TraceTurn;
  collapsed: boolean;
  active: boolean;
  onToggle: () => void;
  onSelect: () => void;
}) {
  const duration = formatDuration(turn.endedAt - turn.startedAt);
  // 附件数取用户实际挂上的文件数；历史回放没有消息级附件时回落到解析阶段读到的份数
  const files = turn.attachments ?? turn.parse?.attachments ?? 0;
  // 这一轮模型的缓存命中率：缓存令牌 ÷ 入参令牌，没有字段就不显示
  const cachePct =
    turn.tokens.in > 0 && turn.tokens.cache > 0
      ? Math.round((turn.tokens.cache / turn.tokens.in) * 100)
      : null;
  return (
    <div className={`trace-turn-head${active ? ' active' : ''}`}>
      <button type="button" className="trace-turn-toggle" aria-expanded={!collapsed} onClick={onToggle}>
        <span className="trace-caret" aria-hidden="true">
          {collapsed ? '›' : '⌄'}
        </span>
        <span className="trace-turn-no mono">第 {turn.index} 轮</span>
        <span className="trace-turn-question">
          用户：<Highlight text={turn.question || '（空问题）'} query="" />
        </span>
      </button>
      <button type="button" className="trace-turn-meta mono" onClick={onSelect}>
        {turn.steps} 步 · {turn.tools} 工具 · {duration}
        {turn.errorCount > 0 && <span className="trace-turn-fail"> · {turn.errorCount} 异常</span>}
        {cachePct !== null && <span className="trace-turn-cache"> · 缓存 {cachePct}%</span>}
        {files ? <span> · {files} 附件</span> : null}
        {turn.parse?.refs ? <span> · {turn.parse.refs} 引用</span> : null}
        {turn.repeatOf && <span className="trace-turn-repeat"> · 同第 {turn.repeatOf} 轮</span>}
      </button>
    </div>
  );
}

/** 按调用聚合：哪个工具被喊得最多、总共吃掉多少时间。 */
function CallRows({
  turns,
  query,
  activeStepId,
  onSelect,
}: {
  turns: TraceTurn[];
  query: string;
  activeStepId: string | null;
  onSelect: (step: TraceStep) => void;
}) {
  const groups = new Map<
    string,
    { name: string; count: number; total: number; slowest: TraceStep }
  >();
  for (const turn of turns) {
    for (const step of turn.items) {
      if (step.kind !== 'tool') continue;
      const name = step.toolName || step.title;
      const group = groups.get(name);
      if (!group) {
        groups.set(name, { name, count: 1, total: step.durationMs, slowest: step });
        continue;
      }
      group.count += 1;
      group.total += step.durationMs;
      if (step.durationMs > group.slowest.durationMs) group.slowest = step;
    }
  }
  const rows = [...groups.values()]
    .filter((row) => !query || row.name.toLowerCase().includes(query.toLowerCase()))
    .sort((a, b) => b.total - a.total);
  if (rows.length === 0) return <p className="trace-empty">还没有工具调用。</p>;
  return (
    <div className="trace-calls">
      {rows.map((row) => (
        <button
          type="button"
          key={row.name}
          className={`trace-step${row.slowest.id === activeStepId ? ' active' : ''}`}
          onClick={() => onSelect(row.slowest)}
        >
          <span className="trace-badge" data-kind="tool">工具</span>
          <span className="trace-main">
            <span className="trace-title mono">{row.name}</span>
            <span className="trace-summary mono">
              {row.count} 次 · 合计 {formatDuration(row.total)} · 最慢一条 {formatDuration(row.slowest.durationMs)}
            </span>
          </span>
          <span className="trace-dur mono" title="平均耗时">
            {formatDuration(row.total / row.count)}
          </span>
        </button>
      ))}
    </div>
  );
}

export function TraceList({
  turns,
  mode,
  query,
  activeStepId,
  activeTurnId,
  context,
  onSelectStep,
  onSelectTurn,
}: Props) {
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [visibleTurns, setVisibleTurns] = useState(5);

  /** 默认展开：进来就能读到参数与返回，点整行收起。 */
  const isOpen = (id: string) => expanded[id] ?? true;
  const toggle = (id: string) =>
    setExpanded((prev) => ({ ...prev, [id]: !(prev[id] ?? true) }));

  if (turns.length === 0) {
    return <p className="trace-empty">该会话还没有可展示的轨迹，去对话里发一条消息。</p>;
  }

  if (mode === 'call') {
    return (
      <>
        <p className="trace-caption">{CAPTIONS.call}</p>
        <CallRows
          turns={turns}
          query={query}
          activeStepId={activeStepId}
          onSelect={onSelectStep}
        />
      </>
    );
  }

  // 时长视图：全局按耗时降序，回答「最慢的是哪一步」，不再按轮分组
  if (mode === 'time') {
    const rows = turns
      .flatMap((turn) =>
        turn.items
          .filter((step) => matchesStep(step, query))
          .map((step) => ({ step, turnIndex: turn.index })),
      )
      .sort((a, b) => b.step.durationMs - a.step.durationMs);
    return (
      <div className="trace-list">
        <p className="trace-caption">{CAPTIONS.time}</p>
        {rows.length === 0 ? (
          <p className="trace-empty">没有匹配「{query}」的步骤。</p>
        ) : (
          <div className="trace-steps">
            {rows.map(({ step, turnIndex }) => (
              <StepRow
                key={step.id}
                step={step}
                turnIndex={turnIndex}
                query={query}
                active={step.id === activeStepId}
                expanded={isOpen(step.id)}
                onSelect={onSelectStep}
                onToggle={() => toggle(step.id)}
              />
            ))}
          </div>
        )}
      </div>
    );
  }

  const searching = query.trim().length > 0;
  const shown = searching ? turns : turns.slice(-visibleTurns);
  const hidden = turns.length - shown.length;

  return (
    <div className="trace-list">
      <p className="trace-caption">{CAPTIONS.turn}</p>
      {hidden > 0 && (
        <button
          type="button"
          className="trace-more"
          onClick={() => setVisibleTurns((value) => value + 10)}
        >
          加载更早的 {hidden} 轮
        </button>
      )}
      {shown.map((turn) => {
        const collapsed = overrides[turn.id] ?? false;
        const items = turn.items.filter((step) => matchesStep(step, query));
        return (
          <section className="trace-turn" key={turn.id} data-turn={turn.index}>
            <TurnHeader
              turn={turn}
              collapsed={collapsed}
              active={turn.id === activeTurnId}
              onToggle={() => setOverrides((prev) => ({ ...prev, [turn.id]: !collapsed }))}
              onSelect={() => onSelectTurn(turn)}
            />
            {!collapsed && (
              <div className="trace-steps">
                <ContextRow turn={turn} context={context} />
                {items.length === 0 ? (
                  <p className="trace-empty">
                    {turn.items.length === 0 ? '这一轮还没有过程步骤。' : `没有匹配「${query}」的步骤。`}
                  </p>
                ) : (
                  items.map((step) => (
                    <StepRow
                      key={step.id}
                      step={step}
                      query={query}
                      active={step.id === activeStepId}
                      expanded={isOpen(step.id)}
                      onSelect={onSelectStep}
                      onToggle={() => toggle(step.id)}
                    />
                  ))
                )}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}

