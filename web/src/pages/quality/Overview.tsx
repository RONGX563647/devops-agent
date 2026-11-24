/**
 * 质量概览 —— 首屏就回答「这批能不能用」。
 *
 * 之前默认落在「线上反馈」，没人打分时首屏是空的；真正的评测结果藏在第二个子页。
 * 这里把结论、四个关键指标与趋势放在最前面，`insufficient` 必须带原因，
 * 不能只留一个没有解释的徽标。
 */
import { useQuery } from '@tanstack/react-query';
import { fetchEvalBatches, fetchEvalSeries, fetchEvalSuites } from '../../api/client';
import type { EvalBatch, EvalSeriesPoint } from '../../types';
import { TrendChart, type TrendPoint } from './charts';
import { formatPercent, formatScore, precisionLimit, signedDelta } from './format';

const VERDICT_TEXT: Record<string, string> = {
  pass: '通过：全部门禁达标',
  fail: '未通过：存在硬失败或门禁未达',
  warn: '回退：指标下降超过精度上限',
  insufficient: '样本不足，不参与发布门禁',
  not_comparable: '不可比：题库/判卷/裁判变了，delta 不能归因',
  running: '运行中',
};

interface Props {
  onShowBatches: () => void;
}

function conclusionOf(batch: EvalBatch): string {
  const base = VERDICT_TEXT[batch.verdict] ?? batch.verdict;
  if (batch.verdict === 'insufficient') {
    return `${base} —— 样本 ${batch.n} < ${batch.min_cases_for_gate}，通过率 ${formatPercent(batch.pass_rate)} 仅供参考`;
  }
  const failed = batch.gates.filter((gate) => gate.state === 'fail');
  if (failed.length > 0) {
    return `${base} —— ${failed.map((gate) => `${gate.key}=${formatScore(gate.value)} < ${gate.min}`).join('；')}`;
  }
  return `${base} —— 通过率 ${formatPercent(batch.pass_rate)}（n=${batch.n}）`;
}

function toTrendPoints(points: EvalSeriesPoint[]): TrendPoint[] {
  return points.map((point) => ({
    label: `#${point.id}`,
    passRate: point.pass_rate,
    n: point.n,
  }));
}

function trendCaption(points: EvalSeriesPoint[]): string {
  if (points.length === 0) return '还没有批次，先跑一次评测。';
  const last = points[points.length - 1];
  const prev = points.length > 1 ? points[points.length - 2] : null;
  if (!prev) {
    return `仅有一个批次：通过率 ${formatPercent(last.pass_rate)}（n=${last.n}）。`;
  }
  const delta = last.pass_rate - prev.pass_rate;
  const limit = precisionLimit(Math.min(last.n, prev.n));
  const significant = Math.abs(delta) >= limit;
  return `最新 #${last.id} 通过率 ${formatPercent(last.pass_rate)}，较上批 ${signedDelta(delta, 'pp')}；`
    + `精度上限 ${(limit * 100).toFixed(1)}pp，${significant ? '变化可辨识' : '变化在抖动范围内'}。`;
}

export function Overview({ onShowBatches }: Props) {
  const series = useQuery({ queryKey: ['eval-series'], queryFn: () => fetchEvalSeries(30) });
  const latest = useQuery({ queryKey: ['eval-batches', 'latest'], queryFn: () => fetchEvalBatches(1) });
  const suites = useQuery({ queryKey: ['eval-suites'], queryFn: fetchEvalSuites });

  const points = series.data?.items ?? [];
  const batch = latest.data?.items[0] ?? null;
  const suite = suites.data?.items[0] ?? null;
  const threshold = suite?.thresholds.pass_rate?.min ?? 0.8;

  if (series.isError || latest.isError) {
    return <p className="query-error">概览加载失败，稍后自动重试。</p>;
  }
  if (!batch) {
    return (
      <section className="panel">
        <h2>最近一次评测</h2>
        <p className="muted">还没有批次。先跑一次评测，这里会出现结论、指标与趋势。</p>
        <code className="cli-hint">
          python -m devops_agent.eval run --suite eval/suites/devops-core.v1.jsonl --out
          workspace/eval/latest
        </code>
      </section>
    );
  }

  const judgeCorrectness = batch.judge?.correctness ?? null;
  const kpis = [
    { label: '通过率', value: formatPercent(batch.pass_rate), detail: `${batch.passed}/${batch.n}` },
    { label: 'judge 正确性', value: formatScore(judgeCorrectness), detail: '1–5 分' },
    { label: '轨迹分', value: formatScore(batch.trajectory_score), detail: '工具选择质量' },
    { label: 'p95 耗时', value: `${batch.p95_ms ?? 0} ms`, detail: '单条用例' },
  ];

  return (
    <div className="quality-overview">
      <section className="panel quality-conclusion">
        <h2>最近一次评测</h2>
        <p className="quality-conclusion-line">
          <span className={`badge badge-${batch.verdict}`}>
            {batch.verdict}
          </span>
          <strong>{conclusionOf(batch)}</strong>
        </p>
        <p className="hint">
          #{batch.id} · {batch.suite}@{batch.suite_version} · {batch.prompt_id} ·{' '}
          {batch.profile} · grader {batch.grader_version || '未记录'}
        </p>
        <div className="quality-kpis">
          {kpis.map((item) => (
            <div className="quality-kpi" key={item.label}>
              <span className="quality-kpi-label">{item.label}</span>
              <span className="quality-kpi-value">{item.value}</span>
              <span className="quality-kpi-detail">{item.detail}</span>
            </div>
          ))}
        </div>
        <button type="button" className="primary-btn" onClick={onShowBatches}>
          看批次明细
        </button>
      </section>

      <section className="panel">
        <h2>通过率趋势</h2>
        <TrendChart
          points={toTrendPoints(points)}
          threshold={threshold}
          caption={trendCaption(points)}
        />
      </section>
    </div>
  );
}
