/** 记忆视图：把检索 / 裁剪 / 压缩 / 写入摊成一条独立轨迹。
 *
 * 记忆动作混在轮次里时最容易被忽略（几毫秒一条，夹在几十条工具调用中间），
 * 但它们决定了「模型这一轮到底看见了什么」，所以单独成页。
 */

import type { TraceStep, TraceTurn } from '../../types';
import { formatDuration } from '../time';
import { Highlight, matchesStep } from './Highlight';

interface Row {
  step: TraceStep;
  turnIndex: number;
}

/** 记忆动作按标签归类：检索 / 裁剪 / 压缩 / 写入 / 装配。 */
function kindOf(step: TraceStep): string {
  const [head] = step.title.split(' · ');
  return head || '记忆';
}

function collect(turns: TraceTurn[], query: string): Row[] {
  return turns.flatMap((turn) =>
    turn.items
      .filter((step) => step.kind === 'memory' && matchesStep(step, query))
      .map((step) => ({ step, turnIndex: turn.index })),
  );
}

export function MemoryRows({
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
  const rows = collect(turns, query);
  if (rows.length === 0) {
    return (
      <p className="trace-empty">
        {query.trim()
          ? `没有匹配「${query}」的记忆动作。`
          : '这段会话没有记忆动作（检索 / 裁剪 / 压缩 / 写入都未发生）。'}
      </p>
    );
  }
  const counts = new Map<string, number>();
  for (const row of rows) counts.set(kindOf(row.step), (counts.get(kindOf(row.step)) ?? 0) + 1);
  const summary = [...counts.entries()].map(([kind, count]) => `${kind} ${count}`).join(' · ');
  return (
    <div className="trace-list">
      <p className="trace-caption">
        记忆轨迹：每次检索 / 裁剪 / 压缩 / 写入各一行，点一行看结构化明细（{summary}）
      </p>
      <div className="trace-steps">
        {rows.map(({ step, turnIndex }) => (
          <button
            type="button"
            key={step.id}
            className={`trace-step${step.id === activeStepId ? ' active' : ''}`}
            data-kind="memory"
            aria-current={step.id === activeStepId ? 'true' : undefined}
            onClick={() => onSelect(step)}
          >
            <span className="trace-idx mono">{String(step.index).padStart(2, '0')}</span>
            <span className="trace-badge" data-kind="memory">
              记忆
            </span>
            <span className="trace-main">
              <span className="trace-turn-tag mono">第 {turnIndex} 轮</span>
              <span className="trace-title mono">{step.title}</span>
              {step.text && (
                <span className="trace-summary mono">
                  <Highlight text={step.text} query={query} />
                </span>
              )}
            </span>
            <span className="trace-share" aria-hidden="true" />
            <span className="trace-dur mono">
              {step.durationMs > 0 ? formatDuration(step.durationMs) : '—'}
            </span>
            <span className={`trace-dot ${step.status ?? 'success'}`} aria-hidden="true" />
            <span className="trace-caret" aria-hidden="true">
              ›
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
