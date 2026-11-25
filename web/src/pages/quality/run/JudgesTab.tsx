/** 语义裁判 Tab：判分方案里的每个 Grader 一行，含归一与贡献。 */

import type { JudgeEntry, OjCaseDetail, OjScoringConfig } from '../../../types';
import { formatScore } from '../format';
import { skipReasonText } from './diagnosis';

interface JudgeRow {
  key: string;
  grader: string;
  weight: number | null;
  scale: [number, number] | null;
}

interface Props {
  detail: OjCaseDetail;
  config: OjScoringConfig | null;
}

export function JudgesTab({ detail, config }: Props) {
  const rows: JudgeRow[] = config
    ? config.plan.judges.map((spec) => ({
        key: spec.key,
        grader: spec.grader,
        weight: spec.weight,
        scale: spec.scale,
      }))
    : Object.keys(detail.judge).map((key) => ({
        key,
        grader: '—',
        weight: detail.judge[key].weight ?? null,
        scale: null,
      }));
  const known = new Set(rows.map((row) => row.key));
  for (const key of Object.keys(detail.judge)) {
    if (!known.has(key)) {
      rows.push({ key, grader: '—', weight: detail.judge[key].weight ?? null, scale: null });
    }
  }

  return (
    <div className="table-scroll">
      <table className="data-table oj-judge-table">
        <thead>
          <tr>
            <th>裁判</th>
            <th>状态</th>
            <th>原始分</th>
            <th>量纲</th>
            <th>归一分</th>
            <th>权重</th>
            <th>贡献</th>
            <th>意见 / 跳过原因</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const entry: JudgeEntry | undefined = detail.judge[row.key];
            const normalized = entry?.normalized;
            const weight = entry?.weight ?? row.weight;
            const scored =
              entry?.status === 'scored' ||
              (entry?.status === undefined && normalized !== undefined);
            const contribution =
              normalized !== undefined && weight !== null ? normalized * weight : null;
            return (
              <tr key={row.key}>
                <td>
                  <code>{row.key}</code>
                  <div className="muted">{row.grader}</div>
                </td>
                <td>
                  <span className={`status-pill ${scored ? 'is-pass' : 'is-pending'}`}>
                    {scored ? '已判分' : '未判分'}
                  </span>
                </td>
                <td>{formatScore(entry?.score)}</td>
                <td>
                  {row.scale ? `${row.scale[0]} – ${row.scale[1]}` : '—'}
                </td>
                <td>{formatScore(normalized, 3)}</td>
                <td>{formatScore(weight)}</td>
                <td>{formatScore(contribution, 3)}</td>
                <td className="muted">
                  {scored ? (entry?.reason ?? '—') : skipReasonText(entry?.skip_reason)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
