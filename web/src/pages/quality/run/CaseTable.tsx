/** 用例明细表：sticky 表头由 .table-scroll 提供，行点击打开诊断抽屉。 */

import type { OjCaseRow } from '../../../types';
import { formatDuration } from '../format';
import { TIER_LABEL, TRACK_LABEL } from '../labels';
import { statusText } from './diagnosis';

interface Props {
  rows: OjCaseRow[];
  onOpen: (row: OjCaseRow) => void;
}

/** 分层列：挑战集失败不该被读成「又坏了一条必过用例」。 */
function tierCell(row: OjCaseRow): string {
  if (!row.tier) return '—';
  return TIER_LABEL[row.tier] ?? row.tier;
}

export function CaseTable({ rows, onOpen }: Props) {
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th scope="col">用例</th>
            <th scope="col">赛道</th>
            <th scope="col">分层</th>
            <th scope="col">主题</th>
            <th scope="col">终态</th>
            <th scope="col">试验</th>
            <th scope="col">耗时</th>
            <th scope="col">结论</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.id}
              className={row.passed ? 'case-row' : 'case-row is-failed'}
              onClick={() => onOpen(row)}
            >
              <td>
                <code>{row.case_id}</code>
              </td>
              <td>{TRACK_LABEL[row.track] ?? row.track}</td>
              <td className="muted">{tierCell(row)}</td>
              <td>{row.subject}</td>
              <td>{statusText(row.status)}</td>
              <td className="muted">{(row.trial ?? 0) + 1}</td>
              <td>{formatDuration(row.latency_ms)}</td>
              <td>
                <span className={`status-pill ${row.passed ? 'is-pass' : 'is-fail'}`}>
                  {row.passed ? '通过' : '失败'}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
