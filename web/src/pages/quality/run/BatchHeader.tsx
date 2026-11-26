/**
 * 批次身份卡：这批到底是谁跑的、用什么题库、什么判分方案、跑了多久。
 *
 * 原来这些字段散在「对比下拉的文案」和几个徽标里，用户想复现一次评测
 * 得自己拼。这里一次性摆齐，时间统一换算成本地时区。
 */

import type { ReactNode } from 'react';
import type { OjBatch } from '../../../types';
import { formatCost, formatDuration, formatTime } from '../format';
import { verdictClass, verdictText } from '../labels';

function duration(batch: OjBatch): number | null {
  const start = new Date(batch.started_at).getTime();
  const end = new Date(batch.finished_at).getTime();
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
  return end - start;
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="oj-fact">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function sha(value: string | undefined): string {
  return value ? value.slice(0, 12) : '—';
}

export function BatchHeader({ batch }: { batch: OjBatch }) {
  const kpi = batch.summary.kpi;
  const evidence = batch.summary.evidence;
  const cases = kpi.case_count ?? kpi.cases;
  const trials = kpi.trials ?? 1;
  return (
    <section className="panel oj-batch-header" aria-label={`批次 ${batch.id} 身份卡`}>
      <div className="tag-row">
        <h2>批次 #{batch.id}</h2>
        <span className={`status-pill ${verdictClass(batch.verdict)}`}>
          {verdictText(batch.verdict)}
        </span>
      </div>
      <dl className="oj-facts">
        <Fact label="被测模型">{batch.model || '—'}</Fact>
        <Fact label="裁判模型">
          {evidence?.judge_model || '未开启（规则门禁仍然有效）'}
        </Fact>
        <Fact label="开始">
          <time dateTime={batch.started_at || undefined}>{formatTime(batch.started_at)}</time>
        </Fact>
        <Fact label="完成">
          <time dateTime={batch.finished_at || undefined}>{formatTime(batch.finished_at)}</time>
        </Fact>
        <Fact label="耗时">{formatDuration(duration(batch))}</Fact>
        <Fact label="用例">
          {cases} 题{trials > 1 ? ` · 每题 ${trials} 次` : ''}
        </Fact>
        <Fact label="题库指纹">
          <code>{sha(batch.suite_sha)}</code>
        </Fact>
        <Fact label="判分方案">
          <code>{sha(batch.plan_sha)}</code>
        </Fact>
        <Fact label="成本">{formatCost(kpi.cost_usd, kpi.cost_note)}</Fact>
      </dl>
    </section>
  );
}
