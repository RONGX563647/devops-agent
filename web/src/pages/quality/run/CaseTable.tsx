/** 用例明细表：sticky 表头由 .table-scroll 提供，行点击打开诊断抽屉。 */

import type { OjCaseRow } from '../../../types';
import { statusText } from './diagnosis';

interface Props {
  rows: OjCaseRow[];
  onOpen: (row: OjCaseRow) => void;
}

export function CaseTable({ rows, onOpen }: Props) {
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>用例</th>
            <th>赛道</th>
            <th>主题</th>
            <th>终态</th>
            <th>耗时</th>
            <th>结论</th>
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
              <td>{row.track}</td>
              <td>{row.subject}</td>
              <td>{statusText(row.status)}</td>
              <td>{row.latency_ms}ms</td>
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
