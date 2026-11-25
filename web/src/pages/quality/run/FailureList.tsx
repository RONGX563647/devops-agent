/** 失败用例与归因列表：每条可定位到抽屉、复制复现命令。 */

import type { OjFailure } from '../../../types';
import { statusText } from './diagnosis';

interface Props {
  failures: OjFailure[];
  pareto: { check: string; count: number }[];
  onLocate: (caseId: string) => void;
  onCopy: (text: string) => void;
}

export function FailureList({ failures, pareto, onLocate, onCopy }: Props) {
  return (
    <div className="panel-grid">
      <div className="panel">
        <h2>失败用例</h2>
        {failures.length === 0 ? (
          <p className="muted">本批次没有失败用例</p>
        ) : (
          <ul className="oj-failures">
            {failures.map((failure) => (
              <li key={failure.case_id}>
                <div>
                  <code>{failure.case_id}</code> · {failure.track} ·{' '}
                  {statusText(failure.status)}{' '}
                  <span className="muted">{failure.failed_checks.join('、')}</span>
                </div>
                <div className="tag-row">
                  <code className="muted">{failure.repro}</code>
                  <button type="button" onClick={() => onLocate(failure.case_id)}>
                    定位
                  </button>
                  <button type="button" onClick={() => onCopy(failure.repro)}>
                    复制复现命令
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="panel">
        <h2>失败归因</h2>
        {pareto.length === 0 ? (
          <p className="muted">无</p>
        ) : (
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>检查项</th>
                  <th>命中次数</th>
                </tr>
              </thead>
              <tbody>
                {pareto.map((item) => (
                  <tr key={item.check}>
                    <td>
                      <code>{item.check}</code>
                    </td>
                    <td>{item.count}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
