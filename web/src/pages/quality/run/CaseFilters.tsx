/** 用例明细筛选条：赛道/状态/主题/每页条数/只看失败，条件写入 URL。 */

import { TRACK_LABEL } from '../labels';
import { statusText } from './diagnosis';

const PAGE_SIZES = [10, 20, 50, 100];

interface Props {
  track: string;
  status: string;
  subject: string;
  pageSize: number;
  onlyFailed: boolean;
  trackOptions: string[];
  statusOptions: string[];
  total: number;
  pageCount: number;
  currentPage: number;
  onChange: (changes: Record<string, string>) => void;
  onPage: (index: number) => void;
}

export function CaseFilters({
  track,
  status,
  subject,
  pageSize,
  onlyFailed,
  trackOptions,
  statusOptions,
  total,
  pageCount,
  currentPage,
  onChange,
  onPage,
}: Props) {
  return (
    <div className="tag-row">
      <label>
        赛道{' '}
        <select value={track} onChange={(event) => onChange({ track: event.target.value })}>
          <option value="">全部赛道</option>
          {trackOptions.map((key) => (
            <option key={key} value={key}>
              {TRACK_LABEL[key] ?? key}
            </option>
          ))}
        </select>
      </label>
      <label>
        状态{' '}
        <select value={status} onChange={(event) => onChange({ status: event.target.value })}>
          <option value="">全部状态</option>
          {statusOptions.map((key) => (
            <option key={key} value={key}>
              {statusText(key)}
            </option>
          ))}
        </select>
      </label>
      <label>
        主题{' '}
        <input
          type="search"
          aria-label="按主题筛选"
          value={subject}
          onChange={(event) => onChange({ subject: event.target.value })}
        />
      </label>
      <label>
        每页{' '}
        <select
          value={String(pageSize)}
          onChange={(event) => onChange({ pageSize: event.target.value })}
        >
          {PAGE_SIZES.map((size) => (
            <option key={size} value={size}>
              {size} 条
            </option>
          ))}
        </select>
      </label>
      <label>
        <input
          type="checkbox"
          checked={onlyFailed}
          onChange={(event) => onChange({ failed: event.target.checked ? '1' : '' })}
        />{' '}
        只看失败
      </label>
      <span className="muted">
        共 {total} 条 · 第 {currentPage + 1}/{pageCount} 页
      </span>
      <button type="button" disabled={currentPage === 0} onClick={() => onPage(0)}>
        首页
      </button>
      <button
        type="button"
        disabled={currentPage === 0}
        onClick={() => onPage(currentPage - 1)}
      >
        上一页
      </button>
      <button
        type="button"
        disabled={currentPage >= pageCount - 1}
        onClick={() => onPage(currentPage + 1)}
      >
        下一页
      </button>
      <button
        type="button"
        disabled={currentPage >= pageCount - 1}
        onClick={() => onPage(pageCount - 1)}
      >
        末页
      </button>
    </div>
  );
}
