/** 五阶段评分流程：采集 → 规则门禁 → 语义裁判 → 归一化合成 → 门禁判决。 */

import { useState } from 'react';
import type { OjBatchSummary, OjScoringConfig } from '../../../types';
import { formatPercent, formatScore } from '../format';
import { verdictClass, verdictText } from '../labels';
import { statusText, type DrawerTab } from './diagnosis';

interface Stage {
  name: string;
  value: string;
  note: string;
  action?: DrawerTab | 'reasons';
}

interface Props {
  summary: OjBatchSummary;
  config: OjScoringConfig | null;
  defaultOpen: boolean;
  onInspect: (tab: DrawerTab) => void;
  onReasons: () => void;
}

export function ScoreFlow({ summary, config, defaultOpen, onInspect, onReasons }: Props) {
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

  return (
    <div className="panel oj-scoreflow">
      <div className="tag-row">
        <h2>评分流程</h2>
        <span className={`status-pill ${verdictClass(summary.verdict)}`}>
          {verdictText(summary.verdict)}
        </span>
        <button type="button" onClick={() => setOpen((value) => !value)}>
          {open ? '收起依据' : '展开依据'}
        </button>
      </div>
      {open && (
        <>
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
          <p className="muted">
            判分方案 {config?.plan.name ?? '—'} · 指纹 {config?.plan.sha?.slice(0, 12) ?? '—'} ·
            阈值与公式均来自服务端
          </p>
        </>
      )}
    </div>
  );
}
