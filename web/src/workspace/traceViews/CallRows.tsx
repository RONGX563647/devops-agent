/** 调用视图：按 model/tool 聚合，点一行展开每一次调用。 */

import { useState } from 'react';
import type { TraceStep, TraceTurn } from '../../types';
import { percentile, type TraceRow } from '../trace';
import { formatDuration } from '../time';
import { BADGE, matchesStep } from './Highlight';

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

export function CallRows({
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
