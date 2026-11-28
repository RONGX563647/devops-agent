/**
 * 质量概览 —— 首屏就回答「这批能不能用」。
 *
 * 结构固定为四段：结论、范围与原因、赛道门禁矩阵、趋势。
 * 结论、范围、原因、证据复用批次的 `CausesPanel`：概览与批次页对同一份
 * summary 说同样的话，是「同一个结论只有一种说法」的实现方式，
 * 而不是在概览里再写一套简化文案。
 *
 * 首屏加载中要显式占位：先渲染「还没有批次」再被数据替换，是两次不同的结论。
 */
import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import {
  fetchOpenJudgeBatches,
  fetchOpenJudgeSeries,
  fetchOpenJudgeSuites,
} from '../../api/client';
import type { OjSeriesPoint, OjSuites } from '../../types';
import { TrendChart, type TrendPoint } from './charts';
import { formatPercent, formatTime, precisionLimit, signedDelta } from './format';
import { GateMatrix } from './GateMatrix';
import { CausesPanel } from './run/CausesPanel';
import { viewHref } from './view';

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
  return `最新 #${last.batch_id} 通过率 ${formatPercent(last.pass_rate)}，`
    + `较上批 ${signedDelta(delta, 'pp')}；精度上限 ${(limit * 100).toFixed(1)}pp，`
    + `${significant ? '变化可辨识' : '变化在抖动范围内'}。`;
}

function rulesOf(suites: OjSuites | undefined): Record<string, Record<string, number>> {
  const rules: Record<string, Record<string, number>> = {};
  for (const track of suites?.tracks ?? []) rules[track.track] = track.rules;
  return rules;
}

export function Overview() {
  // 链接要带上外层 tab（工作台里质量页挂在 `?tab=quality` 下）。
  const [params] = useSearchParams();
  // 批次列表已带完整 summary：一条请求就够首屏，不用再单独拉批次详情。
  const latest = useQuery({
    queryKey: ['oj-latest-batch'],
    queryFn: () => fetchOpenJudgeBatches(1),
  });
  const series = useQuery({
    queryKey: ['oj-series', 30],
    queryFn: () => fetchOpenJudgeSeries(30),
  });
  const suites = useQuery({ queryKey: ['oj-suites'], queryFn: fetchOpenJudgeSuites });

  if (latest.isLoading || series.isLoading) {
    return <p className="query-loading">正在加载质量趋势…</p>;
  }
  if (latest.isError || series.isError) {
    return <p className="query-error">概览加载失败，稍后自动重试。</p>;
  }

  const batch = latest.data?.items[0];
  const points = series.data?.points ?? [];

  if (!batch) {
    return (
      <section className="panel">
        <h2>最近一次评测</h2>
        <p className="muted">
          还没有批次。到「批次」视图发起一次评测，这里会出现结论、门禁与趋势。
        </p>
        <Link className="primary-btn" to={viewHref(params, 'batches')} replace>
          去发起评测
        </Link>
      </section>
    );
  }

  const summary = batch.summary;
  const kpi = summary.kpi;
  const rules = rulesOf(suites.data);

  return (
    <div className="quality-overview">
      <CausesPanel summary={summary} />
      <p className="hint">
        批次 #{batch.id} · 完成于 {formatTime(batch.finished_at)} · 本地时区
      </p>

      <GateMatrix tracks={summary.tracks} rules={rules} />

      <section className="panel">
        <h2>通过率趋势</h2>
        <TrendChart points={toTrendPoints(points)} caption={trendCaption(points)} />
        <p className="hint">
          最近 {points.length} 个批次；共 {kpi.case_count ?? kpi.cases} 题、
          通过 {kpi.passed} 条结果。
        </p>
      </section>

      <div className="tag-row">
        <Link className="primary-btn" to={viewHref(params, 'batches')} replace>
          看批次明细
        </Link>
        <Link className="oj-link-btn" to={viewHref(params, 'scoreflow')} replace>
          看评分流程
        </Link>
      </div>
    </div>
  );
}
