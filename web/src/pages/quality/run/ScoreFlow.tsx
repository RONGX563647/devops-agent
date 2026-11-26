/** 五阶段评分流程 + 三道可信度门禁：流程可见、问题可追溯。 */

import { useState } from 'react';
import type {
  OjBatchSummary,
  OjCalibrationStatus,
  OjGates,
  OjReviewGate,
  OjRubricGate,
  OjScoringConfig,
} from '../../../types';
import { formatPercent, formatScore } from '../format';
import { verdictClass, verdictText } from '../labels';
import { statusText, type DrawerTab } from './diagnosis';

interface Stage {
  name: string;
  value: string;
  note: string;
  action?: DrawerTab | 'reasons';
}

interface GateItem {
  name: string;
  verdict: string;
  label: string;
  value: string;
  note: string;
}

/** 门禁状态用词与批次 verdict 不同：这里说的是「证据齐不齐」，不是「模型好不好」。 */
const GATE_LABEL: Record<string, string> = {
  pass: '达标',
  fail: '未达标',
  insufficient: '未知',
};

interface Props {
  summary: OjBatchSummary;
  config: OjScoringConfig | null;
  gates?: OjGates;
  /** 优先用批次快照；老批次没有时回落到当前状态。 */
  calibration?: OjCalibrationStatus;
  defaultOpen: boolean;
  onInspect: (tab: DrawerTab) => void;
  onReasons: () => void;
}

function reviewGate(gate: OjReviewGate | undefined): GateItem {
  const name = '用例审核';
  if (!gate) {
    return {
      name, verdict: 'insufficient', label: GATE_LABEL.insufficient,
      value: '未知', note: '服务端未下发审核状态',
    };
  }
  const value = `已审 ${gate.approved}/${gate.total}`;
  if (gate.pending > 0) {
    return {
      name, verdict: 'fail', label: GATE_LABEL.fail, value,
      note: `待审 ${gate.pending} 条：${gate.pending_ids.join('、') || '见题库'}`,
    };
  }
  return {
    name, verdict: 'pass', label: GATE_LABEL.pass, value,
    note: gate.legacy > 0
      ? `含 ${gate.legacy} 条历史导入，仍待逐条复核`
      : '全部用例都有审核记录',
  };
}

function rubricGate(gate: OjRubricGate | undefined): GateItem {
  const name = 'rubric 冻结';
  if (!gate || gate.status === 'none') {
    return {
      name, verdict: 'insufficient', label: GATE_LABEL.insufficient,
      value: '未声明', note: '判分方案没有引用 rubric',
    };
  }
  if (!gate.ready) {
    return {
      name, verdict: 'fail', label: GATE_LABEL.fail,
      value: `未冻结（${gate.status}）`,
      note: gate.ref ? `${gate.ref} 未参与判分` : 'rubric 文件缺失',
    };
  }
  return {
    name, verdict: 'pass', label: GATE_LABEL.pass,
    value: `已冻结 ${gate.ref}`,
    note: `签字 ${gate.reviewer} · 指纹 ${gate.sha.slice(0, 12)}`,
  };
}

function calibrationGate(status: OjCalibrationStatus | undefined): GateItem {
  const name = '裁判校准';
  if (!status?.checked) {
    return {
      name, verdict: 'insufficient', label: GATE_LABEL.insufficient,
      value: '未知', note: '批次未记录校准状态，裁判指标不参与门禁',
    };
  }
  if (status.valid) {
    return {
      name, verdict: 'pass', label: GATE_LABEL.pass,
      value: '有效',
      note: `样本与相关性均达标（n≥${status.required_n} · ρ≥${status.required_spearman}）`,
    };
  }
  return {
    name, verdict: 'fail', label: GATE_LABEL.fail,
    value: '未校准',
    note: `缺 ${status.missing.join('、') || '指标'}：裁判分只展示，不进入通过判定`,
  };
}

export function ScoreFlow({
  summary,
  config,
  gates,
  calibration,
  defaultOpen,
  onInspect,
  onReasons,
}: Props) {
  const [open, setOpen] = useState(defaultOpen);
  const kpi = summary.kpi;
  const rule = config?.track_rules.B ?? config?.track_rules.A;
  const statuses =
    Object.entries(kpi.status_counts ?? {})
      .map(([key, count]) => `${statusText(key)} ${count}`)
      .join('、') || '—';
  const stages: Stage[] = [
    {
      name: '采集',
      value: `${kpi.cases} 条`,
      note: `终态 ${statuses} · P95 ${kpi.latency_p95_ms ?? '—'}ms`,
    },
    {
      name: '规则门禁',
      value: `失败 ${kpi.rule_failed_cases ?? '—'} 条`,
      note: `违例 ${kpi.violations} 条 · B 赛道上限 ${rule?.max_violations ?? '—'}`,
      action: 'checks',
    },
    {
      name: '语义裁判',
      value: `已判 ${kpi.judge_n} 条`,
      note: `未判 ${kpi.not_judged_cases ?? '—'} 条 · 低分 ${kpi.judge_failed_cases ?? '—'} 条`,
      action: 'judges',
    },
    {
      name: '归一化合成',
      value: `门槛 ${formatScore(config?.pass_judge ?? null)}`,
      note: 'judge_norm = Σ(normalized × weight) / Σweight，权重来自判分方案',
    },
    {
      name: '门禁判决',
      value: verdictText(summary.verdict),
      note: `赛道通过率门槛 ${formatPercent(rule?.pass_rate ?? null)} · 最少 ${rule?.min_n ?? '—'} 条`,
      action: 'reasons',
    },
  ];
  const gateItems = [
    reviewGate(gates?.review),
    rubricGate(gates?.rubric),
    calibrationGate(calibration),
  ];

  return (
    <div className="panel oj-scoreflow">
      <div className="tag-row">
        <h2 id="score-flow-heading" tabIndex={-1}>评分流程</h2>
        <span className={`status-pill ${verdictClass(summary.verdict)}`}>
          {verdictText(summary.verdict)}
        </span>
        <button
          type="button"
          aria-expanded={open}
          aria-controls="score-flow-details"
          onClick={() => setOpen((value) => !value)}
        >
          {open ? '收起依据' : '展开依据'}
        </button>
      </div>
      <div id="score-flow-details" hidden={!open}>
        <div className="oj-flow">
          {stages.map((stage, index) => {
            const action = stage.action;
            return (
              <section key={stage.name} className="oj-flow-stage">
                <span className="oj-flow-index">{index + 1}</span>
                <h3>{stage.name}</h3>
                <strong>{stage.value}</strong>
                <p className="muted">{stage.note}</p>
                {action === 'reasons' && (
                  <button type="button" className="oj-link-btn" onClick={onReasons}>
                    查看赛道依据
                  </button>
                )}
                {action !== undefined && action !== 'reasons' && (
                  <button
                    type="button"
                    className="oj-link-btn"
                    onClick={() => onInspect(action)}
                  >
                    查看证据
                  </button>
                )}
              </section>
            );
          })}
        </div>
        <div className="oj-gate-block">
          <h3>可信度门禁</h3>
          {/* Safari 会把 list-style:none + grid 的 ul 语义抹掉，显式恢复。 */}
          <ul className="oj-gate-list" role="list">
            {gateItems.map((item) => (
              <li key={item.name}>
                <span className={`status-pill ${verdictClass(item.verdict)}`}>
                  {item.label}
                </span>
                <strong>{item.name}</strong>
                <span className="mono">{item.value}</span>
                <span className="muted">{item.note}</span>
              </li>
            ))}
          </ul>
        </div>
        <p className="muted">
          判分方案 {config?.plan.name ?? '—'} · 指纹 {config?.plan.sha?.slice(0, 12) ?? '—'} ·
          阈值与公式均来自服务端
        </p>
      </div>
    </div>
  );
}
