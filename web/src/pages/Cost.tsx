import { useQuery } from '@tanstack/react-query';
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { fetchRuns, fetchSummary, fetchTimeseries } from '../api/client';
import { MetricCard } from '../components/MetricCard';

export function Cost() {
  const summary = useQuery({ queryKey: ['summary'], queryFn: fetchSummary, refetchInterval: 10000 });
  const timeseries = useQuery({ queryKey: ['timeseries-cost'], queryFn: () => fetchTimeseries(14), refetchInterval: 10000 });
  const runs = useQuery({ queryKey: ['runs', 'cost'], queryFn: () => fetchRuns({ limit: 50 }) });

  const s = summary.data;
  const durationBuckets = [
    { label: '<1s', count: 0 },
    { label: '1-3s', count: 0 },
    { label: '3-5s', count: 0 },
    { label: '≥5s', count: 0 },
  ];
  for (const run of runs.data?.items ?? []) {
    const ms = run.duration_ms ?? 0;
    if (ms < 1000) durationBuckets[0].count += 1;
    else if (ms < 3000) durationBuckets[1].count += 1;
    else if (ms < 5000) durationBuckets[2].count += 1;
    else durationBuckets[3].count += 1;
  }

  return (
    <div className="page">
      <h1>成本 / 性能</h1>
      <p className="subtitle">token 用量、成本代理值与延迟分布</p>

      <div className="metrics-grid">
        <MetricCard label="总 Token" value={s?.token_proxy ?? null} hint="输入 + 输出" />
        <MetricCard label="输入 Token" value={s?.total_input_tokens ?? null} />
        <MetricCard label="输出 Token" value={s?.total_output_tokens ?? null} />
        <MetricCard label="P95 延迟" value={s?.p95_duration_ms === null || s?.p95_duration_ms === undefined ? null : `${s.p95_duration_ms} ms`} />
        <MetricCard label="工具调用" value={s?.tool_call_count ?? null} hint={`失败 ${s?.tool_failed_count ?? 0} 次`} />
      </div>

      <div className="panel-grid">
        <section className="panel">
          <h2>每日运行量（近 14 天）</h2>
          <div className="chart-wrap">
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={timeseries.data ?? []}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" />
                <XAxis dataKey="date" fontSize={12} />
                <YAxis fontSize={12} allowDecimals={false} />
                <Tooltip />
                <Bar dataKey="total" name="运行数" fill="#6366f1" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>

        <section className="panel">
          <h2>延迟分布（最近 50 次）</h2>
          <div className="chart-wrap">
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={durationBuckets}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" />
                <XAxis dataKey="label" fontSize={12} />
                <YAxis fontSize={12} allowDecimals={false} />
                <Tooltip />
                <Bar dataKey="count" name="次数" fill="#0ea5e9" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <p className="hint">成本换算需按模型单价：输入/输出单价不同，已在后端拆分 token 字段。</p>
        </section>
      </div>
    </div>
  );
}
