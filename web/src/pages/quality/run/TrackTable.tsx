/**
 * 赛道结论表：永远五行。
 *
 * 原来只渲染 `summary.tracks` 里出现过的赛道，于是「只跑了 A」看起来像
 * 「五个赛道都通过了」—— 少画的不是一行，是「还有四个没测」这个事实。
 * 没测的赛道显式写「未测」，并且不带结论色块，避免被当成通过。
 */

import { useState } from 'react';
import type { OjFailure, OjTrackStat } from '../../../types';
import { formatPercent, formatScore } from '../format';
import { SEVERITY_LABEL, TRACK_KEYS, TRACK_LABEL, verdictClass, verdictText } from '../labels';
import { failuresByTrack } from './select';

/** 列数：赛道 / 用例 / 规则通过率 / 区间 / 质量分 / 违规 / 失败 / 结论 / 挑战集。 */
const COLUMNS = 9;

interface Props {
  tracks: Record<string, OjTrackStat>;
  failures: OjFailure[];
  /** 从别处点进来时高亮的赛道。 */
  highlight?: string | null;
}

/** 规则通过率优先：裁判没跑不该被打成规则失败。 */
function ruleRate(stat: OjTrackStat): number {
  return stat.rule_pass_rate ?? stat.pass_rate;
}

function interval(stat: OjTrackStat): string {
  const ci = stat.ci;
  return ci ? `${formatPercent(ci[0])}–${formatPercent(ci[1])}` : '';
}

function TrackRow({
  track,
  stat,
  failed,
  highlight,
}: {
  track: string;
  stat: OjTrackStat | undefined;
  failed: number;
  highlight: boolean;
}) {
  const [open, setOpen] = useState(false);
  const reasons = stat?.reasons ?? [];
  const blocking = reasons.some((item) => item.severity === 'blocking');
  return (
    <>
      <tr className={[highlight ? 'is-highlight' : '', blocking ? 'is-blocking' : '']
        .filter(Boolean)
        .join(' ') || undefined}
      >
        <th scope="row">{TRACK_LABEL[track] ?? track}</th>
        {stat ? (
          <>
            <td>{stat.total ?? stat.n}</td>
            <td>{formatPercent(ruleRate(stat))}</td>
            <td className="muted">{interval(stat)}</td>
            <td>{formatScore(stat.judge_avg)}</td>
            <td>{stat.violations}</td>
            <td>{failed}</td>
            <td>
              <span className={`status-pill ${verdictClass(stat.verdict)}`}>
                {verdictText(stat.verdict)}
              </span>
              {reasons.length > 0 && (
                <button type="button" className="oj-link-btn" onClick={() => setOpen((v) => !v)}>
                  {open ? '收起' : `依据 ${reasons.length}`}
                </button>
              )}
            </td>
            <td>{stat.challenge?.total ? `${stat.challenge.passed}/${stat.challenge.total}` : '—'}</td>
          </>
        ) : (
          <>
            <td colSpan={COLUMNS - 2} className="muted">
              本批次未测此赛道
            </td>
            <td>—</td>
          </>
        )}
      </tr>
      {open && (
        <tr className="oj-reason-row">
          <td colSpan={COLUMNS}>
            <ul>
              {reasons.map((reason) => (
                <li key={`${reason.code}-${reason.message}`}>
                  <span
                    className={`status-pill ${reason.severity === 'blocking' ? 'is-fail' : 'is-warn'}`}
                  >
                    {SEVERITY_LABEL[reason.severity ?? 'advisory']}
                  </span>{' '}
                  {reason.message}
                </li>
              ))}
            </ul>
          </td>
        </tr>
      )}
    </>
  );
}

export function TrackTable({ tracks, failures, highlight }: Props) {
  const perTrack = failuresByTrack(failures);
  return (
    <div className="panel">
      <h2>赛道结论</h2>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">赛道</th>
              <th scope="col">用例</th>
              <th scope="col">规则通过率</th>
              <th scope="col">95% 区间</th>
              <th scope="col">质量分</th>
              <th scope="col">违规</th>
              <th scope="col">失败</th>
              <th scope="col">结论</th>
              <th scope="col">挑战集</th>
            </tr>
          </thead>
          <tbody>
            {TRACK_KEYS.map((track) => (
              <TrackRow
                key={track}
                track={track}
                stat={tracks[track]}
                failed={perTrack[track] ?? 0}
                highlight={highlight === track}
              />
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted">
        「未测」表示本批次没有跑这个赛道，不是通过；挑战集分数只进挑战分，不参与发布门禁。
      </p>
    </div>
  );
}
