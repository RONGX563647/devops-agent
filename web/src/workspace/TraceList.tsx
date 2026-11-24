/** 轨迹步骤列表：轮次为分隔节点，步骤一行一条，异常与 diff 就地展开。
 *
 * 三种排布共用同一份 TraceStep 与同一份计时：
 * - 时长：全局降序，估算值沉底，行尾给占比条与帕累托合计；
 * - 轮次：按轮分组，可折叠（折叠态由上层持有，便于「全部折叠/展开」与持久化）；
 * - 调用：按 model/tool 聚合，点一行就地展开每一次调用。
 */
import { useState } from 'react';
import type { AgentContext } from '../api/client';
import type { TraceStep, TraceTurn } from '../types';
import { durationRows, percentile, topShare, type TraceRow } from './trace';
import { formatDuration } from './time';

export type TraceMode = 'time' | 'turn' | 'call';

interface Props {
  turns: TraceTurn[];
  mode: TraceMode;
  query: string;
  activeStepId: string | null;
  activeTurnId: string | null;
  context: AgentContext | null;
  /** 轮次折叠态（上层已算好默认值与持久化）：true = 收起。 */
  collapsed: Record<string, boolean>;
  /** 只渲染最近 N 轮；null = 全部轮次。 */
  visibleTurns: number | null;
  onToggleTurn: (turnId: string) => void;
  /** 把「只看最近 N 轮」放开成全部。 */
  onLoadMore: () => void;
  onSelectStep: (step: TraceStep) => void;
  onSelectTurn: (turn: TraceTurn) => void;
}

const CAPTIONS: Record<TraceMode, string> = {
  time: '按耗时降序（模型、思考、工具、解析全都在内）：估算值沉底，最慢的排最前',
  turn: '按对话轮次分组：点轮次标题折叠整轮，点任一步骤行收起或展开原文',
  call: '按调用聚合：模型与工具分开，点一行展开每一次调用',
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
          {/* 缓存 / 占比两列：占位也要渲染，否则整行会错位 */}
          <span className="trace-cache-slot" aria-hidden="true" />
          <span className="trace-share" aria-hidden="true" />
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

/** 占比条：宽度就是这个步骤吃掉全会话实测耗时的比例，比只看毫秒直观。 */
function ShareCell({ step }: { step: TraceStep }) {
  if (step.timing.source === 'estimated') {
    return (
      <span className="trace-share" title="按相邻步骤估算，不参与占比统计">
        —
      </span>
    );
  }
  const pct = Math.round(step.share * 100);
  return (
    <span className="trace-share" title={`占全会话实测 ${pct}%`}>
      <span className="trace-share-track" aria-hidden="true">
        <span className="trace-share-fill" style={{ width: `${Math.min(100, pct)}%` }} />
      </span>
      <span className="mono">{pct}%</span>
    </span>
  );
}

function StepRow({
  step,
  query,
  active,
  expanded,
  turnIndex,
  showShare,
  onSelect,
  onToggle,
}: {
  step: TraceStep;
  query: string;
  active: boolean;
  expanded: boolean;
  /** 平铺视图里标出这一步属于第几轮；分组视图里用不着。 */
  turnIndex?: number;
  showShare: boolean;
  onSelect: (step: TraceStep) => void;
  onToggle: () => void;
}) {
  const duration =
    step.durationMs > 0
      ? `${step.timing.source === 'estimated' ? '≈' : ''}${formatDuration(step.durationMs)}`
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
          {cache !== null ? (
            <span className="trace-cache" title="缓存命中：缓存令牌 ÷ 入参令牌">
              缓存 {cache}%
            </span>
          ) : (
            <span className="trace-cache-slot" aria-hidden="true" />
          )}
          {showShare ? <ShareCell step={step} /> : <span className="trace-share" aria-hidden="true" />}
          <span
            className="trace-dur mono"
            title={step.timing.source === 'estimated' ? '按相邻步骤估算' : '真实起止'}
          >
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

/** 折叠态也要能判断「值不值得展开」：步数、工具数、耗时、最慢一步、异常、缓存。 */
function turnSlowest(turn: TraceTurn): TraceStep | null {
  let best: TraceStep | null = null;
  for (const step of turn.items) {
    if (step.synthetic || step.timing.source === 'estimated') continue;
    if (!best || step.durationMs > best.durationMs) best = step;
  }
  return best;
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
  // 附件数取用户实际挂上的文件数；历史回放没有消息级附件时回落到解析阶段读到的份数
  const files = turn.attachments ?? turn.parse?.attachments ?? 0;
  // 这一轮模型的缓存命中率：缓存令牌 ÷ 入参令牌，没有字段就不显示
  const cachePct =
    turn.tokens.in > 0 && turn.tokens.cache > 0
      ? Math.round((turn.tokens.cache / turn.tokens.in) * 100)
      : null;
  const slowest = turnSlowest(turn);
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
        {turn.steps} 步 · {turn.tools} 工具 · {formatDuration(turn.measuredMs)}
        {slowest && (
          <span className="trace-turn-slowest">
            {' '}· 最慢 {slowest.toolName || slowest.title} {formatDuration(slowest.durationMs)}
          </span>
        )}
        {turn.errorCount > 0 && <span className="trace-turn-fail"> · {turn.errorCount} 异常</span>}
        {cachePct !== null && <span className="trace-turn-cache"> · 缓存 {cachePct}%</span>}
        {files ? <span> · {files} 附件</span> : null}
        {turn.parse?.refs ? <span> · {turn.parse.refs} 引用</span> : null}
        {turn.repeatOf && <span className="trace-turn-repeat"> · 同第 {turn.repeatOf} 轮</span>}
      </button>
    </div>
  );
}

/** 调用分组：模型与工具分开聚合，避免「模型调用」盖住真正要看的工具。 */
interface CallGroup {
  key: string;
  type: 'model' | 'tool';
  name: string;
  calls: TraceRow[];
  total: number;
  failed: number;
  slowest: TraceStep;
}

type CallSort = 'total' | 'count' | 'fail';

const CALL_SORTS: { key: CallSort; label: string }[] = [
  { key: 'total', label: '合计耗时' },
  { key: 'count', label: '次数' },
  { key: 'fail', label: '失败率' },
];

function groupCalls(turns: TraceTurn[]): CallGroup[] {
  const groups = new Map<string, CallGroup>();
  for (const turn of turns) {
    for (const step of turn.items) {
      if (step.synthetic) continue;
      if (step.kind !== 'tool' && step.kind !== 'model') continue;
      const type = step.kind;
      const name = step.toolName || step.title || (type === 'model' ? '模型' : '工具');
      const key = `${type}:${name}`;
      const group = groups.get(key);
      const row: TraceRow = { step, turnIndex: turn.index };
      if (!group) {
        groups.set(key, {
          key,
          type,
          name,
          calls: [row],
          total: step.durationMs,
          failed: step.status === 'error' ? 1 : 0,
          slowest: step,
        });
        continue;
      }
      group.calls.push(row);
      group.total += step.durationMs;
      if (step.status === 'error') group.failed += 1;
      if (step.durationMs > group.slowest.durationMs) group.slowest = step;
    }
  }
  return [...groups.values()];
}

function sortGroups(groups: CallGroup[], sort: CallSort): CallGroup[] {
  return [...groups].sort((a, b) => {
    if (sort === 'count') return b.calls.length - a.calls.length || b.total - a.total;
    if (sort === 'fail') {
      const rate = (group: CallGroup) => (group.calls.length ? group.failed / group.calls.length : 0);
      return rate(b) - rate(a) || b.total - a.total;
    }
    return b.total - a.total;
  });
}

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
  const [sort, setSort] = useState<CallSort>('total');
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const needle = query.trim().toLowerCase();
  const groups = sortGroups(
    groupCalls(turns).filter(
      (group) =>
        !needle ||
        group.name.toLowerCase().includes(needle) ||
        group.calls.some((call) => matchesStep(call.step, query)),
    ),
    sort,
  );
  if (groups.length === 0) {
    return (
      <p className="trace-empty">
        {needle ? `没有匹配「${query}」的调用。` : '还没有模型或工具调用。'}
      </p>
    );
  }
  return (
    <div className="trace-calls">
      <div className="trace-call-sort" role="group" aria-label="调用排序">
        {CALL_SORTS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={sort === item.key ? 'active' : ''}
            aria-pressed={sort === item.key}
            onClick={() => setSort(item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>
      {groups.map((group) => {
        const durations = group.calls.map((call) => call.step.durationMs);
        const avg = group.total / group.calls.length;
        const p95 = percentile(durations, 0.95);
        const expanded = open[group.key] ?? false;
        return (
          <div className="trace-call-group" key={group.key}>
            <button
              type="button"
              className="trace-step trace-call-head"
              aria-expanded={expanded}
              onClick={() => setOpen((prev) => ({ ...prev, [group.key]: !expanded }))}
            >
              <span className="trace-badge" data-kind={group.type}>
                {BADGE[group.type]}
              </span>
              <span className="trace-main">
                <span className="trace-title mono">{group.name}</span>
                <span className="trace-summary mono">
                  {group.calls.length} 次 · 合计 {formatDuration(group.total)} · 均值{' '}
                  {formatDuration(avg)}
                  {p95 !== null && ` · p95 ${formatDuration(p95)}`} · 最慢{' '}
                  {formatDuration(group.slowest.durationMs)}
                </span>
              </span>
              {group.failed > 0 ? (
                <span className="trace-cache trace-cache-fail">失败 {group.failed}</span>
              ) : (
                <span className="trace-cache-slot" aria-hidden="true" />
              )}
              <span className="trace-dur mono" title="均值">
                {formatDuration(avg)}
              </span>
              <span className="trace-caret" aria-hidden="true">
                {expanded ? '⌄' : '›'}
              </span>
            </button>
            {expanded && (
              <div className="trace-call-items">
                {group.calls.map((call) => (
                  <button
                    type="button"
                    key={call.step.id}
                    className={`trace-call-item${call.step.id === activeStepId ? ' active' : ''}`}
                    onClick={() => onSelect(call.step)}
                  >
                    <span className="trace-turn-tag mono">第 {call.turnIndex} 轮</span>
                    <span className="trace-summary mono">
                      {call.step.resultSummary || call.step.argsSummary || call.step.text || '—'}
                    </span>
                    <span className={`trace-dot ${call.step.status ?? 'success'}`} aria-hidden="true" />
                    <span className="trace-dur mono">
                      {formatDuration(call.step.durationMs)}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
        );
      })}
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
  collapsed,
  visibleTurns,
  onToggleTurn,
  onLoadMore,
  onSelectStep,
  onSelectTurn,
}: Props) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

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
    const rows = durationRows(turns, (step) => matchesStep(step, query));
    const measured = rows.filter((row) => row.step.timing.source !== 'estimated');
    return (
      <div className="trace-list">
        <p className="trace-caption">{CAPTIONS.time}</p>
        {rows.length === 0 ? (
          <p className="trace-empty">没有匹配「{query}」的步骤。</p>
        ) : (
          <>
            <div className="trace-steps">
              {rows.map(({ step, turnIndex }) => (
                <StepRow
                  key={step.id}
                  step={step}
                  turnIndex={turnIndex}
                  query={query}
                  active={step.id === activeStepId}
                  expanded={isOpen(step.id)}
                  showShare
                  onSelect={onSelectStep}
                  onToggle={() => toggle(step.id)}
                />
              ))}
            </div>
            <p className="trace-foot mono">
              共 {measured.length} 步计入实测 · 前 {Math.min(3, measured.length)} 步占{' '}
              {Math.round(topShare(rows, 3) * 100)}%
              {rows.length > measured.length && ` · ${rows.length - measured.length} 条估算未计入`}
            </p>
          </>
        )}
      </div>
    );
  }

  const searching = query.trim().length > 0;
  // 搜索时必须放开截断：否则命中的那一轮可能正好在「更早」里没渲染
  const shown = searching || visibleTurns === null ? turns : turns.slice(-visibleTurns);
  const hidden = turns.length - shown.length;

  return (
    <div className="trace-list">
      <p className="trace-caption">{CAPTIONS.turn}</p>
      {hidden > 0 && (
        <button type="button" className="trace-more" onClick={onLoadMore}>
          加载更早的 {hidden} 轮
        </button>
      )}
      {shown.map((turn) => {
        const isCollapsed = collapsed[turn.id] ?? false;
        const items = turn.items.filter((step) => matchesStep(step, query));
        return (
          <section className="trace-turn" key={turn.id} data-turn={turn.index}>
            <TurnHeader
              turn={turn}
              collapsed={isCollapsed}
              active={turn.id === activeTurnId}
              onToggle={() => onToggleTurn(turn.id)}
              onSelect={() => onSelectTurn(turn)}
            />
            {!isCollapsed && (
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
                      showShare={false}
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
