/** 单条用例行：状态、规则检查、裁判分与展开明细。 */

import { Fragment } from 'react';
import type { EvalCaseItem } from '../../types';
import { HARD_KIND_META } from '../../types';
import { formatDuration } from '../../workspace/time';
import { formatScore } from './format';

export function CaseRow({
  item,
  expanded,
  onToggle,
}: {
  item: EvalCaseItem;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <Fragment>
      <tr className={item.passed ? 'case-row' : 'case-row is-failed'}>
        <td>
          {!item.passed && (
            <button
              type="button"
              className="case-expand"
              aria-label={`展开 ${item.case_id}`}
              onClick={onToggle}
            >
              {expanded ? '▾' : '▸'}
            </button>
          )}
          <code className="case-id">{item.case_id}</code>
        </td>
        <td className="case-tags">
          {item.tags.map((tag) => (
            <span className="tag-chip" key={tag}>{tag}</span>
          ))}
          {item.tags.length === 0 && <span className="muted">—</span>}
        </td>
        <td>
          <span className={`status-pill ${item.passed ? 'is-pass' : 'is-fail'}`}>
            {item.status || (item.passed ? 'passed' : 'failed')}
          </span>
          {/* 三档轨迹：模型没试 / 试了但被拒 / 真的跑了，三者含义完全不同 */}
          {item.denied_tools.length > 0 && (
            <span className="tier-chip is-denied" title={`被权限拒绝：${item.denied_tools.join(', ')}`}>
              被拒 {item.denied_tools.join(',')}
            </span>
          )}
          {item.denied_tools.length === 0 && item.executed_tools.length > 0 && (
            <span
              className="tier-chip is-executed"
              title={`真的执行了：${item.executed_tools.join(', ')}`}
            >
              已执行
            </span>
          )}
          {item.status === 'pending' && <span className="tier-chip is-pending">待确认</span>}
        </td>
        <td className="case-checks">
          {item.hard_failures.map((kind) => {
            const meta = HARD_KIND_META[kind];
            return (
              <span
                key={kind}
                className={`check-chip ${meta?.severity === 'blocking' ? 'bad' : 'warn'}`}
                title={`${meta?.label ?? kind}（${meta?.severity === 'blocking' ? '阻断' : '计分'}）`}
              >
                {meta?.label ?? kind}
              </span>
            );
          })}
          {item.hard_failures.length === 0 &&
            item.checks.map((check) => (
              <span key={check.name} className={`check-chip ${check.ok ? 'ok' : 'bad'}`}>
                {check.ok ? '✓' : '✗'} {check.name}
              </span>
            ))}
          {item.hard_failures.length === 0 && item.checks.length === 0 && (
            <span className="muted">—</span>
          )}
        </td>
        <td>{formatScore(item.trajectory_score) ?? '—'}</td>
        <td className="judge-cell">
          {Object.keys(item.judge).length === 0 && <span className="muted">—</span>}
          {Object.entries(item.judge).map(([dimension, score]) => (
            <span className="judge-mini" key={dimension} title={`${dimension}: ${score}`}>
              <span className="judge-mini-label">{dimension}</span>
              <span className="judge-mini-track">
                <span
                  className="judge-mini-fill"
                  style={{ width: `${Math.max(0, Math.min(1, (score - 1) / 4)) * 100}%` }}
                />
              </span>
            </span>
          ))}
        </td>
        <td>{formatDuration(item.latency_ms)}</td>
        <td>{item.tokens}</td>
      </tr>
      {expanded && (
        <tr className="case-detail-row">
          <td colSpan={8}>
            <ul className="case-check-list">
              {item.checks.map((check, index) => (
                <li key={`${check.name}-${index}`} className={check.ok ? 'ok' : 'bad'}>
                  <span>{check.ok ? '✓' : '✗'}</span>
                  <strong>{check.name}</strong>
                  <span className="muted">{check.detail}</span>
                </li>
              ))}
              {item.checks.length === 0 && <li className="muted">没有检查明细。</li>}
            </ul>
          </td>
        </tr>
      )}
    </Fragment>
  );
}
