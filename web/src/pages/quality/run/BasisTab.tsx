/** 判定依据 Tab：五阶段步进器 + 后端给出的合成公式。 */

import type { OjCaseDetail, OjScoringConfig } from '../../../types';
import { formatScore } from '../format';
import {
  failureTypeText,
  skipReasonText,
  statusText,
} from './diagnosis';

type StageState = 'pass' | 'fail' | 'skip';

interface Stage {
  name: string;
  state: StageState;
  value: string;
  note: string;
}

interface Props {
  detail: OjCaseDetail;
  config: OjScoringConfig | null;
}

export function BasisTab({ detail, config }: Props) {
  const scoring = detail.scoring;
  const trace = detail.trace;
  const skipped = Object.entries(detail.judge)
    .filter(([, item]) => item.status === 'skipped' || item.normalized === undefined)
    .map(([key, item]) => `${key}：${skipReasonText(item.skip_reason)}`);
  const norm = scoring.judge_norm;

  const stages: Stage[] = [
    {
      name: '采集',
      state: trace.status === 'success' ? 'pass' : 'fail',
      value: statusText(trace.status ?? ''),
      note: trace.error || '轨迹快照已落库',
    },
    {
      name: '规则门禁',
      state: scoring.rule_passed ? 'pass' : 'fail',
      value: `失败断言 ${detail.checks.filter((check) => !check.passed).length} 项`,
      note: `违例 ${scoring.violations} 条 · 规则失败 ${scoring.rule_failed_checks.length} 项`,
    },
    {
      name: '语义裁判',
      state:
        norm === null
          ? 'skip'
          : norm < scoring.pass_judge
            ? 'fail'
            : 'pass',
      value: norm === null ? '未进入裁判' : `归一 ${formatScore(norm, 3)}`,
      note: skipped.length > 0 ? skipped.join('；') : '六个 Grader 均有归一分数',
    },
    {
      name: '归一化合成',
      state: norm === null ? 'skip' : norm < scoring.pass_judge ? 'fail' : 'pass',
      value: `门槛 ${formatScore(scoring.pass_judge)}`,
      note: `实际 ${formatScore(norm, 3)} · 权重来自判分方案`,
    },
    {
      name: '门禁判决',
      state: scoring.passed ? 'pass' : 'fail',
      value: scoring.passed ? '通过' : '未通过',
      note: failureTypeText(scoring.failure_type),
    },
  ];

  return (
    <div className="oj-basis">
      <ol className="oj-stepper">
        {stages.map((stage, index) => (
          <li key={stage.name} className={`oj-step is-${stage.state}`}>
            <span className="oj-step-index">{index + 1}</span>
            <div>
              <strong>{stage.name}</strong>
              <span className="oj-step-value">{stage.value}</span>
              <p className="muted">{stage.note}</p>
            </div>
          </li>
        ))}
      </ol>
      <div className="oj-formula">
        <strong>通过算式</strong>
        <code>{scoring.formula}</code>
      </div>
      <p className="muted">
        判分方案 {config?.plan.name ?? '—'} · 指纹 {config?.plan.sha?.slice(0, 12) ?? '—'} ·
        门槛来自后端配置
      </p>
    </div>
  );
}
