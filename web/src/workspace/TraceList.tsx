/** 轨迹步骤列表：轮次为分隔节点，步骤一行一条，异常与 diff 就地展开。
 *
 * 三种排布对应顶部切换：按时长（按发生顺序）、按轮次（可折叠）、按调用（工具聚合）。
 */
import { useState } from 'react';
import type { TraceStep, TraceTurn } from '../types';
import { formatDuration } from './time';

export type TraceMode = 'time' | 'turn' | 'call';

interface Props {
  turns: TraceTurn[];
  mode: TraceMode;
  query: string;
  activeStepId: string | null;
  activeTurnId: string | null;
  onSelectStep: (step: TraceStep) => void;
  onSelectTurn: (turn: TraceTurn) => void;
}

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

function StepRow({
  step,
  query,
  active,
  expanded,
  onSelect,
  onToggle,
}: {
  step: TraceStep;
  query: string;
  active: boolean;
  expanded: boolean;
  onSelect: (step: TraceStep) => void;
  onToggle: () => void;
}) {
  const duration =
    step.durationMs > 0
      ? `${step.measured ? '' : '≈'}${formatDuration(step.durationMs)}`
      : '—';
  const summary = step.argsSummary || step.resultSummary || step.text;
  const canExpand = expandable(step);
  const label = step.toolName || (GENERIC_TITLES.has(step.title) ? BADGE[step.kind] : step.title);
  return (
    <>
      <div className={`trace-step-wrap${active ? ' active' : ''}`}>
        <button
          type="button"
          className={`trace-step${active ? ' active' : ''}`}
          data-kind={step.kind}
          aria-current={active ? 'true' : undefined}
          onClick={() => onSelect(step)}
        >
          <span className="trace-idx mono">{String(step.index).padStart(2, '0')}</span>
          <span className="trace-badge" data-kind={step.kind}>
            {step.title === '模型输出' ? '输出' : BADGE[step.kind]}
          </span>
          <span className="trace-main">
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
          <span className="trace-dur mono" title={step.measured ? '事件时间戳' : '按相邻步骤估算'}>
            {duration}
          </span>
          <span className={`trace-dot ${step.status ?? 'success'}`} aria-hidden="true" />
        </button>
        {canExpand && (
          <button
            type="button"
            className="trace-expand"
            aria-expanded={expanded}
            aria-label={`${expanded ? '收起' : '展开'} ${label} 原文`}
            title={expanded ? '收起原文' : '展开原文'}
            onClick={onToggle}
          >
            <span className="trace-caret" aria-hidden="true">{expanded ? '⌄' : '›'}</span>
          </button>
        )}
      </div>
      {step.diff && <Diff step={step} />}
      {expanded && <StepDetail step={step} />}
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
  onSelectStep,
  onSelectTurn,
}: Props) {
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [visibleTurns, setVisibleTurns] = useState(5);

  if (mode === 'call') {
    return (
      <CallRows
        turns={turns}
        query={query}
        activeStepId={activeStepId}
        onSelect={onSelectStep}
      />
    );
  }

  const searching = query.trim().length > 0;
  const shown = searching ? turns : turns.slice(-visibleTurns);
  const hidden = turns.length - shown.length;

  if (turns.length === 0) {
    return <p className="trace-empty">该会话还没有可展示的轨迹，去对话里发一条消息。</p>;
  }

  return (
    <div className="trace-list">
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
        const collapsed = overrides[turn.id] ?? (mode === 'turn' && turn.index !== turns.length);
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
            {!collapsed &&
              (items.length === 0 ? (
                <p className="trace-empty">
                  {turn.items.length === 0 ? '这一轮还没有过程步骤。' : `没有匹配「${query}」的步骤。`}
                </p>
              ) : (
                <div className="trace-steps">
                  {items.map((step) => (
                    <StepRow
                      key={step.id}
                      step={step}
                      query={query}
                      active={step.id === activeStepId}
                      expanded={Boolean(expanded[step.id])}
                      onSelect={onSelectStep}
                      onToggle={() =>
                        setExpanded((prev) => ({ ...prev, [step.id]: !prev[step.id] }))
                      }
                    />
                  ))}
                </div>
              ))}
          </section>
        );
      })}
    </div>
  );
}

