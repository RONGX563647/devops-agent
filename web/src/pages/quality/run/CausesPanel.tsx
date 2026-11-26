/**
 * 结论面板：把「结论 / 范围 / 原因 / 证据」四件事分开说。
 *
 * 原来一个 `insufficient` 徽标同时背了三种事实（样本不够、裁判没跑、没校准），
 * 用户只能自己猜下一步该做什么。现在逐项列出来，并且每条原因都写明它是
 * 阻断（挡住发布）还是提示（只影响可信度），点击可跳到对应赛道的失败。
 */

import type { OjBatchSummary, OjCause } from '../../../types';
import { formatCost, formatPercent, formatScore } from '../format';
import {
  JUDGE_LEVEL_LABEL,
  SCOPE_LABEL,
  SEVERITY_LABEL,
  TRACK_LABEL,
  verdictClass,
  verdictText,
} from '../labels';

interface Props {
  summary: OjBatchSummary;
  /** 点原因时跳到该赛道的失败用例。 */
  onTrack?: (track: string) => void;
  /** 点「看证据」时下钻到规则/裁判证据页签。 */
  onInspect?: (kind: 'rule' | 'judge') => void;
}

function CauseList({
  title,
  causes,
  tone,
  onTrack,
}: {
  title: string;
  causes: OjCause[];
  tone: string;
  onTrack?: (track: string) => void;
}) {
  if (causes.length === 0) return null;
  return (
    <div className="oj-cause-group">
      <h3>
        <span className={`status-pill ${tone}`}>{title}</span> {causes.length} 条
      </h3>
      <ul className="oj-causes">
        {causes.map((cause) => (
          <li key={`${cause.track}-${cause.code}-${cause.message}`}>
            <span>{cause.message}</span>
            {onTrack && (
              <button type="button" className="oj-link-btn" onClick={() => onTrack(cause.track)}>
                看 {TRACK_LABEL[cause.track] ?? cause.track} 的失败
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** 多次试验才显示 pass@k / pass^k：k=1 时两列恒等，摆出来只是噪音。 */
function TrialLine({ summary }: { summary: OjBatchSummary }) {
  const kpi = summary.kpi;
  const trials = kpi.trials ?? 1;
  if (trials <= 1) return null;
  return (
    <p className="hint">
      k={trials}：pass@k（至少过一次）{formatPercent(kpi.pass_at_k)} · pass^k（k 次全过）{' '}
      {formatPercent(kpi.pass_hat_k)} —— 两者差距就是抖动。
    </p>
  );
}

export function CausesPanel({ summary, onTrack, onInspect }: Props) {
  const scope = summary.scope;
  const evidence = summary.evidence;
  const causes = summary.causes ?? [];
  const blocking = causes.filter((item) => item.severity === 'blocking');
  const advisory = causes.filter((item) => item.severity !== 'blocking');
  const measured = scope?.measured_tracks ?? Object.keys(summary.tracks);
  const unmeasured = scope?.unmeasured_tracks ?? [];

  return (
    <div className="panel oj-causes-panel">
      <div className="tag-row">
        <h2>结论</h2>
        <span className={`status-pill ${verdictClass(summary.verdict)}`}>
          {verdictText(summary.verdict)}
        </span>
        <strong>{verdictText(summary.verdict, true)}</strong>
      </div>

      <p className="hint">
        范围 {SCOPE_LABEL[scope?.mode ?? ''] ?? scope?.mode ?? '—'}：已测{' '}
        {measured.map((track) => TRACK_LABEL[track] ?? track).join('、') || '无'}
        {unmeasured.length > 0 && (
          <>
            ；未测 {unmeasured.map((track) => TRACK_LABEL[track] ?? track).join('、')}（这些赛道
            不影响结论，但也意味着它们没被验证）
          </>
        )}
      </p>

      <CauseList title={SEVERITY_LABEL.blocking} causes={blocking} tone="is-fail" onTrack={onTrack} />
      <CauseList
        title={SEVERITY_LABEL.advisory}
        causes={advisory}
        tone="is-warn"
        onTrack={onTrack}
      />
      {causes.length === 0 && <p className="muted">没有未达标原因：全部赛道规则与质量门禁均达标。</p>}

      <div className="oj-evidence-line">
        <span>
          裁判档位{' '}
          <strong>{JUDGE_LEVEL_LABEL[evidence?.judge_level ?? 'off'] ?? evidence?.judge_level}</strong>
          {evidence?.judge_model ? ` · ${evidence.judge_model}` : ''}
        </span>
        <span>
          校准 n={evidence?.calibration_n ?? '—'} · ρ={formatScore(evidence?.calibration_spearman)}
        </span>
        <span>
          多次试验 k={summary.kpi.trials ?? 1} · 成本{' '}
          {formatCost(summary.kpi.cost_usd, summary.kpi.cost_note)}
        </span>
      </div>
      <TrialLine summary={summary} />
      {onInspect && (
        <div className="tag-row">
          <button type="button" className="oj-link-btn" onClick={() => onInspect('rule')}>
            看规则证据
          </button>
          <button type="button" className="oj-link-btn" onClick={() => onInspect('judge')}>
            看裁判证据
          </button>
        </div>
      )}
    </div>
  );
}
