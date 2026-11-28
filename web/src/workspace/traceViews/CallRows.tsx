/** 调用视图：按 model/tool 聚合，点一行展开每一次调用，并让右侧显示这组统计。 */

import { useState } from 'react';
import type { TraceStep, TraceTurn } from '../../types';
import {
  groupCalls,
  sortGroups,
  type CallGroup,
  type CallRow,
  type CallSort,
} from '../traceData/calls';
import { percentile } from '../traceData/stats';
import { formatDuration } from '../time';
import { BADGE, matchesStep } from './Highlight';

const CALL_SORTS: { key: CallSort; label: string }[] = [
  { key: 'total', label: '合计耗时' },
  { key: 'count', label: '次数' },
  { key: 'fail', label: '失败率' },
];

export function CallRows({
  turns,
  query,
  activeStepId,
  activeGroupKey,
  onSelect,
  onSelectGroup,
}: {
  turns: TraceTurn[];
  query: string;
  activeStepId: string | null;
  /** 右侧正在展示统计的那一组：列表保持高亮，避免「点了哪组」对不上。 */
  activeGroupKey: string | null;
  onSelect: (step: TraceStep) => void;
  onSelectGroup: (group: CallGroup) => void;
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
              className={`trace-step trace-call-head${group.key === activeGroupKey ? ' active' : ''}`}
              aria-expanded={expanded}
              aria-current={group.key === activeGroupKey ? 'true' : undefined}
              onClick={() => {
                setOpen((prev) => ({ ...prev, [group.key]: !expanded }));
                // 点一级节点同时把右侧切到这组统计：展开只是列表动作，统计才是排查入口
                onSelectGroup(group);
              }}
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
                <span className="trace-cache trace-cache-fail">
                  失败 {group.failed} · {Math.round((group.failed / group.calls.length) * 100)}%
                </span>
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
                {group.calls.map((call: CallRow) => (
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
