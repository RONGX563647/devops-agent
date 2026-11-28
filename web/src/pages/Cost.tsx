/** 成本 / 性能：token 按模型分账，首 token 与工具耗时单独看，单价可配。 */
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
import { fetchCostMetrics, fetchRuns, fetchSummary, fetchTimeseries } from '../api/client';
import { MetricCard } from '../components/MetricCard';
import { formatDuration } from '../workspace/time';

/** 毫秒 → 可读；没有样本时给「—」，不把缺失显示成 0。 */
function ms(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : formatDuration(value);
}

/** 千分位：token 动辄七位数，不分隔根本数不清。 */
function num(value: number): string {
  return value.toLocaleString('zh-CN');
}

/** 成本：未配置单价时返回 null，由调用方显示「未配置」而不是 ¥0.00。 */
function money(cost: number | null | undefined, currency: string): string | null {
  if (cost === null || cost === undefined) return null;
  const symbol = currency === 'CNY' ? '¥' : `${currency} `;
  return `${symbol}${cost.toFixed(4).replace(/0+$/, '').replace(/\.$/, '')}`;
}

export function Cost() {
  const summary = useQuery({ queryKey: ['summary'], queryFn: fetchSummary, refetchInterval: 10000 });
  const timeseries = useQuery({ queryKey: ['timeseries-cost'], queryFn: () => fetchTimeseries(14), refetchInterval: 10000 });
  const runs = useQuery({ queryKey: ['runs', 'cost'], queryFn: () => fetchRuns({ limit: 50 }) });
  const cost = useQuery({ queryKey: ['metrics-cost'], queryFn: () => fetchCostMetrics(), refetchInterval: 15000 });

  const s = summary.data;
  const c = cost.data;
  const durationBuckets = [
    { label: '<1s', count: 0 },
    { label: '1-3s', count: 0 },
    { label: '3-5s', count: 0 },
    { label: '≥5s', count: 0 },
  ];
  for (const run of runs.data?.items ?? []) {
    const value = run.duration_ms ?? 0;
    if (value < 1000) durationBuckets[0].count += 1;
    else if (value < 3000) durationBuckets[1].count += 1;
    else if (value < 5000) durationBuckets[2].count += 1;
    else durationBuckets[3].count += 1;
  }

  return (
    <div className="page">
      <h1>成本 / 性能</h1>
      <p className="subtitle">token 按模型分账、首 token 与工具耗时、以及按配置单价换算的成本</p>

      {summary.isError && <p className="query-error">成本指标加载失败，稍后自动重试。</p>}
      {cost.isError && <p className="query-error">模型用量与成本加载失败，稍后自动重试。</p>}
      {runs.isError && <p className="query-error">延迟分布所需的运行数据加载失败。</p>}

      <div className="metrics-grid">
        <MetricCard label="总 Token" value={s?.token_proxy ?? null} hint="输入 + 输出" />
        <MetricCard label="输入 Token" value={s?.total_input_tokens ?? null} />
        <MetricCard label="输出 Token" value={s?.total_output_tokens ?? null} />
        <MetricCard
          label="首 token 平均"
          value={ms(c?.ttft.avg)}
          hint={
            c && c.ttft.p95 !== null
              ? `P95 ${ms(c.ttft.p95)} · ${c.ttft.samples} 条记录`
              : '暂无 TTFT 记录（老数据或未开流式）'
          }
        />
        <MetricCard
          label="工具调用平均"
          value={ms(c?.tool_ms.avg)}
          hint={
            c && c.tool_ms.p95 !== null
              ? `P95 ${ms(c.tool_ms.p95)} · ${c.tool_ms.samples} 次采样`
              : `失败 ${s?.tool_failed_count ?? 0} 次`
          }
        />
        <MetricCard
          label="调用成本"
          value={money(c?.total_cost, c?.currency ?? 'CNY')}
          hint={
            c
              ? c.unpriced_models > 0
                ? `${c.unpriced_models} 个模型未配置单价`
                : `按 ${c.priced_models} 个模型的单价计`
              : '需配置 config/model_prices.yaml'
          }
          tone={c?.total_cost !== null && c?.total_cost !== undefined ? 'good' : 'default'}
        />
      </div>

      <section className="panel">
        <h2>按模型统计</h2>
        <p className="hint">
          单价写在 <code>config/model_prices.yaml</code>（元 / {num(c?.unit_tokens ?? 1_000_000)} token）：
          输入与输出分别计价，未配置的模型只统计用量、不计成本。
        </p>
        {cost.isLoading && !c && <p className="query-loading">正在加载模型用量…</p>}
        {c && c.models.length === 0 && <p className="muted">还没有模型调用记录。</p>}
        {c && c.models.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th>模型</th>
                <th>调用</th>
                <th>失败</th>
                <th>输入</th>
                <th>输出</th>
                <th>缓存</th>
                <th>平均耗时</th>
                <th>首 token</th>
                <th>成本</th>
              </tr>
            </thead>
            <tbody>
              {c.models.map((item) => (
                <tr key={item.name}>
                  <td className="mono">{item.name}</td>
                  <td className="mono">{item.calls}</td>
                  <td className={item.errors > 0 ? 'has-error mono' : 'mono'}>{item.errors}</td>
                  <td className="mono">{num(item.input_tokens)}</td>
                  <td className="mono">{num(item.output_tokens)}</td>
                  <td className="mono">{num(item.cache_input_tokens)}</td>
                  <td className="mono">{ms(item.avg_ms)}</td>
                  <td className="mono">
                    {item.avg_ttft_ms === null ? '—' : ms(item.avg_ttft_ms)}
                  </td>
                  <td className="mono">
                    {money(item.cost, c.currency) ?? <span className="muted">未配置单价</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <div className="panel-grid">
        <section className="panel">
          <h2>每日运行量（近 14 天）</h2>
          {timeseries.isError && <p className="query-error">趋势数据加载失败。</p>}
          {timeseries.isLoading && (timeseries.data ?? []).length === 0 && (
            <p className="query-loading">正在加载趋势…</p>
          )}
          {!timeseries.isLoading && !timeseries.isError && (timeseries.data ?? []).length === 0 && (
            <p className="muted">近 14 天还没有运行记录。</p>
          )}
          {(timeseries.data ?? []).length > 0 && (
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
          )}
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
          <p className="hint">
            端到端延迟含工具与模型；模型平均耗时 {ms(c?.model_ms.avg)}、
            P95 {ms(c?.model_ms.p95)}（{c?.model_ms.samples ?? 0} 次采样）。
          </p>
        </section>
      </div>
    </div>
  );
}
