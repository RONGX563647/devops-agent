/** 批次对比面板：base/head 选择、精度上限、指标表与变化用例。 */

import { Fragment } from 'react';
import type { EvalBatch, EvalCompare } from '../../types';
import {
  VERDICT_LABEL,
  deltaTone,
  formatDelta,
  formatNumber,
} from './format';

interface Props {
  batches: EvalBatch[];
  baseId: number | null;
  headId: number | null;
  onSelectBase: (id: number | null) => void;
  onSelectHead: (id: number | null) => void;
  canCompare: boolean;
  data: EvalCompare | undefined;
  isError: boolean;
  isLoading: boolean;
}

export function ComparePanel({
  batches,
  baseId,
  headId,
  onSelectBase,
  onSelectHead,
  canCompare,
  data,
  isError,
  isLoading,
}: Props) {
  const changedCases = data?.cases.filter((item) => item.base_passed !== item.head_passed) ?? [];
  return (
    <section className="panel">
      <h2>批次对比</h2>
      {batches.length === 0 && <p className="muted">暂无可对比的批次。</p>}
      {batches.length > 0 && (
        <div className="filters compare-pickers">
          <label>
            基线 base：
            <select
              aria-label="基线批次"
              value={baseId ?? ''}
              onChange={(event) => onSelectBase(event.target.value ? Number(event.target.value) : null)}
            >
              {batches.map((item) => (
                <option key={item.id} value={item.id}>
                  #{item.id} {item.suite}@{item.suite_version}
                </option>
              ))}
            </select>
          </label>
          <label>
            对比 head：
            <select
              aria-label="对比批次"
              value={headId ?? ''}
              onChange={(event) => onSelectHead(event.target.value ? Number(event.target.value) : null)}
            >
              {batches.map((item) => (
                <option key={item.id} value={item.id}>
                  #{item.id} {item.suite}@{item.suite_version}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}
      {batches.length === 1 && <p className="muted">至少需要两个批次才能对比。</p>}
      {batches.length >= 2 && !canCompare && (
        <p className="muted">请选择两个不同的批次。</p>
      )}

      {canCompare && isError && <p className="query-error">对比加载失败。</p>}
      {canCompare && isLoading && <p className="query-loading">正在加载对比…</p>}
      {data && (
        <Fragment>
          <div className={`compare-banner verdict-${data.verdict}`} role="status">
            对比结论：{VERDICT_LABEL[data.verdict] ?? data.verdict}
          </div>

          <p className="hint">
            精度上限 {((data.mde ?? 0) * 100).toFixed(1)}pp：n=
            {Math.min(data.n, data.base_n)}，小于该值的变化视为抖动。
            {data.compatibility?.level !== 'clean' && (
              <>
                {' '}
                可比性：
                <strong>
                  {data.compatibility?.level === 'blocking'
                    ? '不可比'
                    : data.compatibility?.level === 'confounded'
                      ? '混杂'
                      : '未知'}
                </strong>
                {data.compatibility?.reasons?.[0] ? ` —— ${data.compatibility.reasons[0]}` : ''}
              </>
            )}
          </p>

          <table className="data-table eval-compare-table">
            <thead>
              <tr>
                <th>指标</th>
                <th>base</th>
                <th>head</th>
                <th>delta</th>
                <th>方向</th>
              </tr>
            </thead>
            <tbody>
              {data.metrics.map((metric) => (
                <tr key={metric.key}>
                  <td>{metric.key}</td>
                  <td>{formatNumber(metric.base)}</td>
                  <td>{formatNumber(metric.head)}</td>
                  <td className={`delta-${deltaTone(metric)}`}>{formatDelta(metric.delta)}</td>
                  <td className="muted">{metric.better === 'lower' ? '越低越好' : '越高越好'}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3 className="compare-cases-title">变化用例（{changedCases.length}）</h3>
          <ul className="compare-cases">
            {changedCases.map((item) => (
              <li key={item.case_id} className={item.head_passed ? 'is-fixed' : 'is-regressed'}>
                <code>{item.case_id}</code>
                <span>
                  {item.base_passed ? '通过' : '失败'} → {item.head_passed ? '通过' : '失败'}
                </span>
              </li>
            ))}
            {changedCases.length === 0 && <li className="muted">没有通过状态变化的用例。</li>}
          </ul>
        </Fragment>
      )}
    </section>
  );
}
