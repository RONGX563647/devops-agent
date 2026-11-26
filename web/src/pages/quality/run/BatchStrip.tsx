/**
 * 批次列表：紧凑 chip 展示时间/结论/通过率/模型/用例数，支持结论过滤与分页。
 *
 * 「共 N 批」必须是库里的真实条数：原来用 `batches.length` 当总数，
 * 有 200 个批次时界面永远显示「共 20 批」，用户连翻页的念头都不会有。
 */

import type { OjBatch } from '../../../types';
import { formatClock, formatPercent } from '../format';
import { SCOPE_LABEL, VERDICT_OPTIONS, verdictClass, verdictText } from '../labels';

function rateOf(item: OjBatch): number | null {
  const kpi = item.summary?.kpi;
  if (!kpi) return null;
  return kpi.pass_rate ?? (kpi.cases > 0 ? kpi.passed / kpi.cases : 0);
}

/** 范围不写全量：全量是默认值，标出来只增加噪音。 */
function scopeOf(item: OjBatch): string {
  const mode = item.summary?.scope?.mode ?? '';
  return mode && mode !== 'full' ? (SCOPE_LABEL[mode] ?? mode) : '';
}

/** 用例条数：多次试验时 `cases` 是结果行数，`case_count` 才是题目数。 */
function caseCount(item: OjBatch): number {
  const kpi = item.summary?.kpi;
  return kpi?.case_count ?? kpi?.cases ?? 0;
}

/** 对比下拉的可读文案：选择时就能看出在比哪两批。 */
export function batchOptionLabel(item: OjBatch): string {
  return `#${item.id} · ${formatClock(item.finished_at)} · ${verdictText(item.verdict)}`
    + ` · ${formatPercent(rateOf(item))}`;
}

interface Props {
  batches: OjBatch[];
  total: number;
  limit: number;
  offset: number;
  activeId: number | null;
  verdict: string;
  onVerdictChange: (value: string) => void;
  onSelect: (id: number) => void;
  onPage: (offset: number) => void;
}

export function BatchStrip({
  batches,
  total,
  limit,
  offset,
  activeId,
  verdict,
  onVerdictChange,
  onSelect,
  onPage,
}: Props) {
  const pageCount = Math.max(1, Math.ceil(total / Math.max(1, limit)));
  const page = Math.min(pageCount, Math.floor(offset / Math.max(1, limit)) + 1);
  return (
    <div className="panel">
      <div className="tag-row">
        <h2>批次</h2>
        <label>
          结论{' '}
          <select value={verdict} onChange={(event) => onVerdictChange(event.target.value)}>
            {VERDICT_OPTIONS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <span className="muted">
          共 {total} 批 · 第 {page}/{pageCount} 页
        </span>
        <button type="button" disabled={offset <= 0} onClick={() => onPage(0)}>
          最新
        </button>
        <button
          type="button"
          disabled={offset <= 0}
          onClick={() => onPage(Math.max(0, offset - limit))}
        >
          较新
        </button>
        <button
          type="button"
          disabled={offset + limit >= total}
          onClick={() => onPage(offset + limit)}
        >
          更早
        </button>
        <button
          type="button"
          disabled={offset + limit >= total}
          onClick={() => onPage(Math.max(0, (pageCount - 1) * limit))}
        >
          最早
        </button>
      </div>
      <div className="batch-strip">
        {batches.map((item) => (
          <button
            key={item.id}
            type="button"
            className={`batch-chip${item.id === activeId ? ' active' : ''}`}
            aria-current={item.id === activeId ? 'true' : undefined}
            onClick={() => onSelect(item.id)}
          >
            <strong>#{item.id}</strong>
            <span className="muted">{formatClock(item.finished_at)}</span>
            <span className={`status-pill ${verdictClass(item.verdict)}`}>
              {verdictText(item.verdict)}
            </span>
            <span>{formatPercent(rateOf(item))}</span>
            <span className="muted">{item.model || '未知模型'}</span>
            <span className="muted">{caseCount(item)} 条</span>
            {scopeOf(item) && <span className="batch-chip-scope">{scopeOf(item)}</span>}
          </button>
        ))}
        {batches.length === 0 && <p className="muted">该结论下没有批次</p>}
      </div>
    </div>
  );
}
