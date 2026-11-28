import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { fetchBreakdown, fetchMetricsHealth, fetchRuns, fetchSummary, fetchTimeseries } from '../api/client';
import { MetricCard } from '../components/MetricCard';
import { AverageRadar, DeficitChart, ScoreHistogram } from '../components/HealthCharts';
import { StatusBadge } from '../components/StatusBadge';
import { formatDateTime, formatDuration } from '../workspace/time';

function formatMs(ms: number | null): string {
  if (ms === null) return '—';
  return formatDuration(ms);
}

export function Dashboard() {
  const summary = useQuery({ queryKey: ['summary'], queryFn: fetchSummary, refetchInterval: 5000 });
  const timeseries = useQuery({ queryKey: ['timeseries'], queryFn: () => fetchTimeseries(7), refetchInterval: 10000 });
  const recent = useQuery({ queryKey: ['runs', 'recent'], queryFn: () => fetchRuns({ limit: 8 }), refetchInterval: 5000 });
  const health = useQuery({
    queryKey: ['metrics-health'],
    queryFn: () => fetchMetricsHealth(200),
    refetchInterval: 15000,
  });
  const breakdown = useQuery({
    queryKey: ['metrics-breakdown'],
    queryFn: fetchBreakdown,
    refetchInterval: 15000,
  });

  const s = summary.data;
  const points = timeseries.data ?? [];
  const recentItems = recent.data?.items ?? [];
  const h = health.data;
  const b = breakdown.data;

  return (
    <div className="page">
      <h1>总览 Dashboard</h1>
      <p className="subtitle">Agent 运行好坏的实时画像（数据来自后端观测 API）</p>

      {summary.isError && <p className="query-error">总览指标加载失败，稍后自动重试。</p>}

      <div className="metrics-grid">
        <MetricCard
          label="健康分"
          value={s?.avg_health_score ?? null}
          hint="0-100 加权聚合"
          tone={s?.avg_health_score !== null && s?.avg_health_score !== undefined && s.avg_health_score >= 70 ? 'good' : 'default'}
        />
        <MetricCard
          label="成功率"
          value={s?.success_rate === null || s?.success_rate === undefined ? null : `${(s.success_rate * 100).toFixed(1)}%`}
          hint={`${s?.success_count ?? 0}/${s?.total_runs ?? 0} 次运行`}
          tone={s?.success_rate !== null && s?.success_rate !== undefined && s.success_rate >= 0.8 ? 'good' : 'bad'}
        />
        <MetricCard label="P95 延迟" value={formatMs(s?.p95_duration_ms ?? null)} hint="端到端耗时" />
        <MetricCard
          label="Token 成本代理"
          value={s?.token_proxy ?? null}
          hint={`输入 ${s?.total_input_tokens ?? 0} / 输出 ${s?.total_output_tokens ?? 0}`}
        />
      </div>

      <div className="panel-grid">
        <section className="panel">
          <h2>健康度趋势（近 7 天运行量）</h2>
          {timeseries.isError && <p className="query-error">趋势数据加载失败。</p>}
          {timeseries.isLoading && points.length === 0 && <p className="query-loading">正在加载趋势…</p>}
          {!timeseries.isLoading && !timeseries.isError && points.length === 0 && (
            <p className="muted">近 7 天还没有运行记录。</p>
          )}
          {points.length > 0 && (
            <div className="chart-wrap">
              <ResponsiveContainer width="100%" height={260}>
                <AreaChart data={points}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" />
                  <XAxis dataKey="date" fontSize={12} />
                  <YAxis fontSize={12} allowDecimals={false} />
                  <Tooltip />
                  <Area type="monotone" dataKey="success" name="成功" stroke="#16a34a" fill="#bbf7d0" />
                  <Area type="monotone" dataKey="failed" name="失败" stroke="#dc2626" fill="#fecaca" />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}
        </section>

        <section className="panel">
          <h2>运行构成</h2>
          <p className="hint">概览来自 /api/summary；实时对话请到「对话」tab。</p>
          <ul className="summary-list">
            <li>
              <span>总运行</span>
              <strong className="mono">{s?.total_runs ?? 0}</strong>
            </li>
            <li>
              <span>成功</span>
              <strong className="mono">{s?.success_count ?? 0}</strong>
            </li>
            <li>
              <span>失败</span>
              <strong className="mono">{s?.failed_count ?? 0}</strong>
            </li>
            <li>
              <span>工具调用</span>
              <strong className="mono">{s?.tool_call_count ?? 0}</strong>
            </li>
            <li>
              <span>工具失败</span>
              <strong className="mono">{s?.tool_failed_count ?? 0}</strong>
            </li>
            <li>
              <span>工具失败率</span>
              <strong className="mono">
                {b?.tool.failure_rate === null || b?.tool.failure_rate === undefined
                  ? '—'
                  : `${(b.tool.failure_rate * 100).toFixed(1)}%`}
              </strong>
            </li>
            <li>
              <span>权限拒绝率</span>
              <strong className="mono">
                {b?.permission.deny_rate === null || b?.permission.deny_rate === undefined
                  ? '—'
                  : `${(b.permission.deny_rate * 100).toFixed(1)}%`}
              </strong>
            </li>
            <li>
              <span>缓存命中率</span>
              <strong className="mono">
                {b?.tokens.cache_hit_rate === null || b?.tokens.cache_hit_rate === undefined
                  ? '—'
                  : b.tokens.cache_recorded
                    ? `${(b.tokens.cache_hit_rate * 100).toFixed(1)}%`
                    : '无缓存数据'}
              </strong>
            </li>
            <li>
              <span>单次 token</span>
              <strong className="mono">{b?.model.avg_tokens_per_call ?? '—'}</strong>
            </li>
          </ul>
          <div className="panel-links">
            <Link to="/?tab=quality">看质量评估</Link>
            <Link to="/?tab=cost">看成本分布</Link>
          </div>
        </section>
      </div>

      {health.isError && <p className="query-error">健康分聚合加载失败，稍后自动重试。</p>}
      {h && (
        <div className="panel-grid">
          <section className="panel">
            <h2>健康分分布</h2>
            <p className="hint">
              最近 {h.runs} 次运行按当前口径重算 · 均值 {h.avg_score ?? '—'} · P50 {h.p50 ?? '—'} · P90{' '}
              {h.p90 ?? '—'}
              {h.coverage_avg !== null ? ` · 平均覆盖度 ${Math.round(h.coverage_avg * 100)}%` : ''}
              {h.confidence_avg !== null ? ` · 平均置信度 ${Math.round(h.confidence_avg * 100)}%` : ''}
            </p>
            <ScoreHistogram
              buckets={h.distribution}
              caption="均值相同也可能一个「都很稳」一个「一半满分一半崩」，所以看分布"
            />
          </section>
          <section className="panel">
            <h2>四维均值</h2>
            <p className="hint">权重 执行 35% · 效率 25% · 质量 25% · 安全 15%，缺失维度不参与加权。</p>
            <AverageRadar averages={h.dimensions} caption="缺口越大越该先修，但先看样本数（n）" />
          </section>
        </div>
      )}

      <div className="panel-grid">
        <section className="panel">
          <h2>拖分排行</h2>
          <p className="hint">权重 ×(100 − 均值)：数字越大，修这个维度越划算。</p>
          {h ? (
            <DeficitChart
              rows={h.deficits}
              caption={`等级分布 ${Object.entries(h.grades)
                .map(([grade, count]) => `${grade}×${count}`)
                .join(' · ') || '暂无'}`}
            />
          ) : (
            <p className="muted">加载中…</p>
          )}
        </section>
        <section className="panel">
          <h2>失败原因 Top</h2>
          <p className="hint">
            整轮失败与逐次调用报错一起统计；同一个报错反复出现才值得先修，没有原因文本的也会单列。
          </p>
          {breakdown.isError && <p className="query-error">失败聚类加载失败。</p>}
          {b && b.failures.length === 0 && <p className="muted">没有失败记录。</p>}
          {b && b.failures.length > 0 && (
            <ul className="failure-list">
              {b.failures.map((item) => (
                <li key={`${item.kind}:${item.name}:${item.reason}`}>
                  <span className="failure-count mono">{item.count}</span>
                  {item.kind !== 'run' && (
                    <span className="failure-kind mono" title={`${item.kind} · ${item.name}`}>
                      {item.name || item.kind}
                    </span>
                  )}
                  <span className="failure-reason">{item.reason}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <section className="panel">
        <h2>最近运行</h2>
        {recent.isError && <p className="query-error">最近运行加载失败。</p>}
        {recent.isLoading && recentItems.length === 0 && (
          <p className="query-loading">正在加载最近运行…</p>
        )}
        {!recent.isLoading && !recent.isError && recentItems.length === 0 && (
          <p className="muted">暂无运行记录，先去「对话」tab 跑一次。</p>
        )}
        {recentItems.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th><th>会话</th><th>域</th><th>状态</th>
                <th>健康分</th><th>耗时</th><th>Token</th><th>开始时间</th><th></th>
              </tr>
            </thead>
            <tbody>
              {recentItems.map((run) => (
                <tr key={run.id}>
                  <td>#{run.id}</td>
                  <td>{run.session_id}</td>
                  <td>{run.domain}</td>
                  <td><StatusBadge status={run.status} /></td>
                  <td>{run.health_score ?? '—'}</td>
                  <td>{formatMs(run.duration_ms)}</td>
                  <td>{run.total_tokens}</td>
                  <td className="muted">{formatDateTime(run.started_at)}</td>
                  <td><Link to={`/runs/${run.id}`}>详情</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
