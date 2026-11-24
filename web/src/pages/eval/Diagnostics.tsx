/** 质量诊断：把「为什么没过」摊开成风险分层、漏斗与归因三张表。 */

import type { EvalBatch } from '../../types';
import { MetricCard } from '../../components/MetricCard';
import { formatPercent } from './format';

const STAGE_LABEL: Record<string, string> = {
  total: '总量',
  executed: '执行',
  safe: '无红线',
  routed: '工具路线',
  answered: '结果',
  formatted: '引用格式',
  passed: '严格通过',
};

const ATTR_LABEL: Record<string, string> = {
  ok: '通过',
  safety: '安全红线',
  model_answer: '答案错误',
  model_route: '工具路线错误',
  budget: '预算超限',
  format: '引用/格式',
  harness: '环境/执行',
  assertion: '题库断言',
  timeout: '超时',
  judge: '裁判',
};

/** 能力/有效样本两张卡：与严格通过率并排，解释「为什么严格通过率低」。 */
export function RateMetrics({ batch, badge }: { batch: EvalBatch; badge?: string }) {
  return (
    <>
      <MetricCard
        label="能力通过率"
        value={formatPercent(batch.capability_pass_rate ?? null)}
        hint={`${batch.capability_passed ?? 0}/${batch.n}（不含引用/预算）`}
        badge={badge}
      />
      <MetricCard
        label="有效样本通过率"
        value={formatPercent(batch.attributed_pass_rate ?? null)}
        hint="仅模型/安全归因样本"
        badge={badge}
      />
    </>
  );
}

export function QualityDiagnostics({ batch }: { batch: EvalBatch }) {
  const risk = Object.entries(batch.risk ?? {});
  const funnel = batch.funnel ?? [];
  const attribution = Object.entries(batch.attribution ?? {});
  if (risk.length === 0 && funnel.length === 0 && attribution.length === 0) {
    return <p className="hint">本批未记录归因（旧版 CLI 产出）。用新版 eval run 重跑即可。</p>;
  }
  return (
    <div className="eval-diagnostics">
      {funnel.length > 0 && (
        <>
          <h4>判定漏斗</h4>
          <p className="hint">
            {funnel
              .map((stage) => `${STAGE_LABEL[stage.stage] ?? stage.stage} ${stage.n}`)
              .join(' → ')}
          </p>
        </>
      )}
      {risk.length > 0 && (
        <>
          <h4>风险分层</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>分层</th>
                <th>n</th>
                <th>有效样本</th>
                <th>有效通过率（门禁）</th>
                <th>能力通过率</th>
                <th>红线失败</th>
              </tr>
            </thead>
            <tbody>
              {risk.map(([layer, bucket]) => (
                <tr key={layer}>
                  <td>
                    <code>{layer}</code>
                  </td>
                  <td>{bucket.n}</td>
                  <td>{bucket.attributed ?? bucket.n}</td>
                  <td>{formatPercent(bucket.attributed_rate ?? bucket.capability_rate)}</td>
                  <td>{formatPercent(bucket.capability_rate)}</td>
                  <td className={bucket.safety_failures > 0 ? 'gate-fail' : undefined}>
                    {bucket.safety_failures}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {attribution.length > 0 && (
        <>
          <h4>失败归因</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>归因</th>
                <th>用例数</th>
                <th>其中能力通过</th>
              </tr>
            </thead>
            <tbody>
              {attribution.map(([key, bucket]) => (
                <tr key={key}>
                  <td>{ATTR_LABEL[key] ?? key}</td>
                  <td>{bucket.n}</td>
                  <td>{bucket.passed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
