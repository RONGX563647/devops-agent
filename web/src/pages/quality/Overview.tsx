/**
 * 质量概览 —— 首屏就回答「这批能不能用」。
 *
 * 数据源是 OpenJudge 的批次序列：结论、关键指标与趋势放在最前面，
 * `insufficient` 必须带原因，不能只留一个没有解释的徽标。
 * 首屏加载中要显式占位：先渲染「还没有批次」再被数据替换，是两次不同的结论。
 */
import { useQuery } from '@tanstack/react-query';
import { fetchOpenJudgeSeries, fetchOpenJudgeSuites } from '../../api/client';
import type { OjSeriesPoint, OjSuites } from '../../types';
import { TrendChart, type TrendPoint } from './charts';
import { formatPercent, precisionLimit, signedDelta } from './format';
import { verdictClass, verdictText } from './labels';

interface Props {
  onShowBatches: () => void;
  onShowScoreFlow: () => void;
}

function lastPoint(points: OjSeriesPoint[]): OjSeriesPoint | null {
  return points.length > 0 ? points[points.length - 1] : null;
}

function toTrendPoints(points: OjSeriesPoint[]): TrendPoint[] {
  return points.map((point) => ({
    label: `#${point.batch_id}`,
    passRate: point.pass_rate,
    n: point.cases,
  }));
}

function trendCaption(points: OjSeriesPoint[]): string {
  if (points.length === 0) return '还没有批次，先跑一次评测。';
  const last = points[points.length - 1];
  const prev = points.length > 1 ? points[points.length - 2] : null;
  if (!prev) {
    return `仅有一个批次：通过率 ${formatPercent(last.pass_rate)}（n=${last.cases}）。`;
  }
  const delta = last.pass_rate - prev.pass_rate;
  const limit = precisionLimit(Math.min(last.cases, prev.cases));
  const significant = Math.abs(delta) >= limit;
  return `最新 #${last.batch_id} 通过率 ${formatPercent(last.pass_rate)}，较上批 ${signedDelta(delta, 'pp')}；`
    + `精度上限 ${(limit * 100).toFixed(1)}pp，${significant ? '变化可辨识' : '变化在抖动范围内'}。`;
}

/** 门禁线：取第一个赛道配置的 pass_rate 阈值，没配置时退回 0.8。 */
function thresholdOf(suites: OjSuites | undefined): number {
  for (const track of suites?.tracks ?? []) {
    const rule = track.rules.pass_rate;
    if (typeof rule === 'number') return rule;
  }
  return 0.8;
}

function conclusionOf(point: OjSeriesPoint): string {
  const base = verdictText(point.verdict, true);
  return `${base} —— 通过率 ${formatPercent(point.pass_rate)}（n=${point.cases}）`;
}

export function Overview({ onShowBatches, onShowScoreFlow }: Props) {
  const series = useQuery({
    queryKey: ['oj-series', 30],
    queryFn: () => fetchOpenJudgeSeries(30),
  });
  const suites = useQuery({ queryKey: ['oj-suites'], queryFn: fetchOpenJudgeSuites });

  if (series.isLoading) {
    return <p className="query-loading">正在加载质量趋势…</p>;
  }
  if (series.isError) {
    return <p className="query-error">概览加载失败，稍后自动重试。</p>;
  }

  const points = series.data?.points ?? [];
  const latest = lastPoint(points);
  const threshold = thresholdOf(suites.data);

  if (!latest) {
    return (
      <section className="panel">
        <h2>最近一次评测</h2>
        <p className="muted">
          还没有批次。到「运行与批次」发起一次评测，这里会出现结论、指标与趋势。
        </p>
        <button type="button" className="primary-btn" onClick={onShowBatches}>
          去发起评测
        </button>
      </section>
    );
  }

  const kpis = [
    { label: '通过率', value: formatPercent(latest.pass_rate), detail: `n=${latest.cases}` },
    { label: '用例数', value: latest.cases, detail: '本批次实际执行' },
    { label: '结论', value: verdictText(latest.verdict), detail: '批次终态' },
    {
      label: '完成时间',
      value: latest.finished_at.replace('T', ' ').slice(0, 19),
      detail: 'UTC',
    },
  ];

  return (
    <div className="quality-overview">
      <section className="panel quality-conclusion">
        <h2>最近一次评测</h2>
        <p className="quality-conclusion-line">
          <span className={`status-pill ${verdictClass(latest.verdict)}`}>
            {verdictText(latest.verdict)}
          </span>
          <strong>{conclusionOf(latest)}</strong>
        </p>
        <p className="hint">批次 #{latest.batch_id} · 完成于 {latest.finished_at}</p>
        <div className="quality-kpis">
          {kpis.map((item) => (
            <div className="quality-kpi" key={item.label}>
              <span className="quality-kpi-label">{item.label}</span>
              <span className="quality-kpi-value">{item.value}</span>
              <span className="quality-kpi-detail">{item.detail}</span>
            </div>
          ))}
        </div>
        <div className="tag-row">
          <button type="button" className="primary-btn" onClick={onShowBatches}>
            看批次明细
          </button>
          <button type="button" className="oj-link-btn" onClick={onShowScoreFlow}>
            看评分流程
          </button>
        </div>
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
