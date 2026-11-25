/** 题库卡片：每个赛道的规模、门禁阈值与最近一批结论。 */

import { useQuery } from '@tanstack/react-query';
import { fetchOpenJudgeSuites } from '../../../api/client';
import { formatPercent } from '../format';
import { TRACK_LABEL, VERDICT_TEXT, verdictClass } from '../labels';

export function SuitesCard() {
  const query = useQuery({ queryKey: ['oj-suites'], queryFn: fetchOpenJudgeSuites });

  if (query.isLoading) {
    return (
      <div className="panel">
        <h2>题库</h2>
        <p className="muted">加载题库清单…</p>
      </div>
    );
  }
  if (query.isError || !query.data) {
    return (
      <div className="panel">
        <h2>题库</h2>
        <p className="query-error">题库清单加载失败，稍后自动重试。</p>
      </div>
    );
  }

  const { total, suite_sha, tracks } = query.data;
  return (
    <div className="panel">
      <h2>题库</h2>
      <p className="hint">
        共 {total} 条用例 · suite {suite_sha.slice(0, 12)}
      </p>
      {tracks.length === 0 ? (
        <p className="muted">题库里没有任何赛道。</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>赛道</th>
              <th>用例</th>
              <th>通过率阈值</th>
              <th>最近批次</th>
              <th>最近通过率</th>
              <th>结论</th>
            </tr>
          </thead>
          <tbody>
            {tracks.map((item) => (
              <tr key={item.track}>
                <td>{TRACK_LABEL[item.track] ?? item.track}</td>
                <td>{item.cases}</td>
                <td>{formatPercent(item.rules.pass_rate)}</td>
                {item.latest ? (
                  <>
                    <td>#{item.latest.batch_id}</td>
                    <td>{formatPercent(item.latest.pass_rate)}</td>
                    <td>
                      <span className={`status-pill ${verdictClass(item.latest.verdict)}`}>
                        {VERDICT_TEXT[item.latest.verdict] ?? item.latest.verdict}
                      </span>
                    </td>
                  </>
                ) : (
                  <td colSpan={3} className="muted">
                    尚无批次
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
