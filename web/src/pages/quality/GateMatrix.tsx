/**
 * 门禁矩阵：每个赛道对照**自己的**阈值展示实际值。
 *
 * 原来概览在趋势图上画了一条「阈值线」，取的是第一个赛道的 pass_rate ——
 * B 赛道的真实门禁是「零安全违例」，C 赛道是「改动落地」，拿 A 的 60%
 * 当所有人的线，等于给了一张看不出来的错图。现在阈值与实际并排列出来，
 * 阈值全部来自服务端 `track_rules`，前端不自己编数字。
 */

import type { OjTrackStat } from '../../types';
import { formatPercent, formatScore } from './format';
import { TRACK_KEYS, TRACK_LABEL, verdictClass, verdictText } from './labels';

interface Props {
  tracks: Record<string, OjTrackStat>;
  /** 服务端 `/suites` 下发的阈值；键是 pass_rate / judge / min_n / max_violations。 */
  rules: Record<string, Record<string, number>>;
}

/** 只写服务端真给了的阈值：没配 `judge` 的赛道不显示 0.60 假门槛。 */
function ruleText(rule: Record<string, number> | undefined): string {
  if (!rule) return '未配置';
  const parts: string[] = [];
  if (typeof rule.pass_rate === 'number') parts.push(`通过率 ≥${formatPercent(rule.pass_rate)}`);
  if (typeof rule.judge === 'number') parts.push(`质量分 ≥${formatScore(rule.judge)}`);
  if (typeof rule.min_n === 'number') parts.push(`至少 ${rule.min_n} 条`);
  if (typeof rule.max_violations === 'number') parts.push(`违例 ≤${rule.max_violations}`);
  return parts.join(' · ') || '未配置';
}

function actualText(stat: OjTrackStat | undefined): string {
  if (!stat) return '未测';
  const rate = stat.rule_pass_rate ?? stat.pass_rate;
  const parts = [`通过率 ${formatPercent(rate)}`, `质量分 ${formatScore(stat.judge_avg)}`];
  parts.push(`${stat.total ?? stat.n} 条`, `违例 ${stat.violations}`);
  return parts.join(' · ');
}

export function GateMatrix({ tracks, rules }: Props) {
  return (
    <section className="panel">
      <h2>赛道门禁矩阵</h2>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">赛道</th>
              <th scope="col">阈值（服务端下发）</th>
              <th scope="col">最近一批实际</th>
              <th scope="col">结论</th>
            </tr>
          </thead>
          <tbody>
            {TRACK_KEYS.map((track) => {
              const stat = tracks[track];
              return (
                <tr key={track}>
                  <th scope="row">{TRACK_LABEL[track] ?? track}</th>
                  <td className="muted">{ruleText(rules[track])}</td>
                  <td>{actualText(stat)}</td>
                  <td>
                    {stat ? (
                      <span className={`status-pill ${verdictClass(stat.verdict)}`}>
                        {verdictText(stat.verdict)}
                      </span>
                    ) : (
                      <span className="muted">未测</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="hint">
        五个赛道的门槛并不相同，所以趋势图上没有画「一条阈值线」——
        它只能属于某一个赛道，画给所有赛道就是错的。
      </p>
    </section>
  );
}
