/** 结果区附加视图：批次对比、题库覆盖与 rubric 工件。 */

import type { OjBatch, OjCompare, OjCoverage, OjRubric } from '../../../types';
import { formatPercent as percent } from '../format';
import { TRACK_LABEL } from '../labels';
import { batchOptionLabel } from './BatchStrip';

interface Props {
  batches: OjBatch[];
  base: number | null;
  head: number | null;
  onBase: (value: number | null) => void;
  onHead: (value: number | null) => void;
  compareEnabled: boolean;
  compare: OjCompare | undefined;
  coverage: OjCoverage | undefined;
}

export function ResultsExtras({
  batches,
  base,
  head,
  onBase,
  onHead,
  compareEnabled,
  compare,
  coverage,
}: Props) {
  return (
    <div className="panel-grid">
      <div className="panel">
        <h2>批次对比</h2>
        <div className="tag-row">
          <select value={base ?? ''} onChange={(event) => onBase(Number(event.target.value) || null)}>
            <option value="">基线批次</option>
            {batches.map((item) => (
              <option key={item.id} value={item.id}>
                {batchOptionLabel(item)}
              </option>
            ))}
          </select>
          <select value={head ?? ''} onChange={(event) => onHead(Number(event.target.value) || null)}>
            <option value="">对比批次</option>
            {batches.map((item) => (
              <option key={item.id} value={item.id}>
                {batchOptionLabel(item)}
              </option>
            ))}
          </select>
        </div>
        {!compareEnabled && <p className="muted">选两个不同批次查看变化</p>}
        {compare && (
          <>
            {!compare.comparable && <p className="muted">题库或判分方案已变化，结果仅供参考</p>}
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>赛道</th><th>基线</th><th>对比</th><th>变化</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(compare.tracks).map(([key, stat]) => (
                    <tr key={key}>
                      <td>{TRACK_LABEL[key] ?? key}</td>
                      <td>{percent(stat.base)}</td>
                      <td>{percent(stat.head)}</td>
                      <td>{stat.delta === 0 ? '—' : `${(stat.delta * 100).toFixed(1)}pp`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {compare.flips.length > 0 && (
              <p className="muted">
                翻转用例：{compare.flips.map((flip) => flip.case_id).join('、')}
              </p>
            )}
          </>
        )}
      </div>

      <div className="panel">
        <h2>题库覆盖</h2>
        {coverage ? (
          <>
            <p className="muted">
              共 {coverage.cases} 条 · suite {coverage.suite_sha.slice(0, 12)}
            </p>
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr><th>赛道</th><th>用例</th></tr>
                </thead>
                <tbody>
                  {Object.entries(coverage.coverage.tracks ?? {}).map(([key, count]) => (
                    <tr key={key}>
                      <td>{TRACK_LABEL[key] ?? key}</td>
                      <td>{count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {coverage.problems.length > 0 && (
              <p className="muted">校验问题：{coverage.problems.join('；')}</p>
            )}
          </>
        ) : (
          <p className="muted">加载中…</p>
        )}
      </div>
    </div>
  );
}

export function RubricPanel({ rubrics }: { rubrics: OjRubric[] }) {
  return (
    <div className="panel">
      <h2>Rubric 工件</h2>
      {rubrics.length === 0 ? (
        <p className="muted">
          还没有 rubric。运行 <code>python -m devops_agent.openjudge rubric generate</code>
        </p>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr><th>名称</th><th>版本</th><th>状态</th><th>审阅人</th></tr>
            </thead>
            <tbody>
              {rubrics.map((item) => (
                <tr key={`${item.name}@${item.version}`}>
                  <td>{item.name}</td>
                  <td>{item.version}</td>
                  <td>{item.status}</td>
                  <td>{item.reviewer || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
