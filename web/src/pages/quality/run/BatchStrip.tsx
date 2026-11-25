/** 批次列表：紧凑 chip 展示时间/结论/通过率/模型/用例数，支持结论过滤。 */

import type { OjBatch } from '../../../types';
import { formatPercent } from '../format';
import { verdictClass, verdictText } from '../labels';

const VERDICTS = [
  { value: '', label: '全部结论' },
  { value: 'pass', label: '通过' },
  { value: 'fail', label: '未达标' },
  { value: 'insufficient', label: '样本不足' },
  { value: 'warn', label: '回退' },
  { value: 'cancelled', label: '已取消' },
];

function rateOf(item: OjBatch): number | null {
  const kpi = item.summary?.kpi;
  if (!kpi) return null;
  return kpi.pass_rate ?? (kpi.cases > 0 ? kpi.passed / kpi.cases : 0);
}

function timeOf(item: OjBatch): string {
  return item.finished_at ? item.finished_at.replace('T', ' ').slice(5, 16) : '—';
}

/** 对比下拉的可读文案：选择时就能看出在比哪两批。 */
export function batchOptionLabel(item: OjBatch): string {
  return `#${item.id} · ${timeOf(item)} · ${verdictText(item.verdict)} · ${formatPercent(rateOf(item))}`;
}

interface Props {
  batches: OjBatch[];
  activeId: number | null;
  verdict: string;
  onVerdictChange: (value: string) => void;
  onSelect: (id: number) => void;
}

export function BatchStrip({ batches, activeId, verdict, onVerdictChange, onSelect }: Props) {
  return (
    <div className="panel">
      <div className="tag-row">
        <h2>批次</h2>
        <label>
          结论{' '}
          <select value={verdict} onChange={(event) => onVerdictChange(event.target.value)}>
            {VERDICTS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <span className="muted">共 {batches.length} 批</span>
      </div>
      <div className="batch-strip">
        {batches.map((item) => (
          <button
            key={item.id}
            type="button"
            className={`batch-chip${item.id === activeId ? ' active' : ''}`}
            onClick={() => onSelect(item.id)}
          >
            <strong>#{item.id}</strong>
            <span className="muted">{timeOf(item)}</span>
            <span className={`status-pill ${verdictClass(item.verdict)}`}>
              {verdictText(item.verdict)}
            </span>
            <span>{formatPercent(rateOf(item))}</span>
            <span className="muted">{item.model || '未知模型'}</span>
            <span className="muted">{item.summary?.kpi?.cases ?? 0} 条</span>
          </button>
        ))}
        {batches.length === 0 && <p className="muted">该结论下没有批次</p>}
      </div>
    </div>
  );
}
