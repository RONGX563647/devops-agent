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
import { useState } from 'react';
import { fetchRuns, fetchSummary, fetchTimeseries } from '../api/client';
import { useRunStream } from '../api/useRunStream';
import { MetricCard } from '../components/MetricCard';
import { StatusBadge } from '../components/StatusBadge';

function formatMs(ms: number | null): string {
  if (ms === null) return '—';
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`;
}

export function Dashboard() {
  const summary = useQuery({ queryKey: ['summary'], queryFn: fetchSummary, refetchInterval: 5000 });
  const timeseries = useQuery({ queryKey: ['timeseries'], queryFn: () => fetchTimeseries(7), refetchInterval: 10000 });
  const recent = useQuery({ queryKey: ['runs', 'recent'], queryFn: () => fetchRuns({ limit: 8 }), refetchInterval: 5000 });
  const stream = useRunStream();
  const [sessionId, setSessionId] = useState('live-1');
  const [input, setInput] = useState('用一句话说明什么是 CI');

  const s = summary.data;

  return (
    <div className="page">
      <h1>总览 Dashboard</h1>
      <p className="subtitle">Agent 运行好坏的实时画像（数据来自后端观测 API）</p>

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
          <div className="chart-wrap">
            <ResponsiveContainer width="100%" height={260}>
              <AreaChart data={timeseries.data ?? []}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" />
                <XAxis dataKey="date" fontSize={12} />
                <YAxis fontSize={12} allowDecimals={false} />
                <Tooltip />
                <Area type="monotone" dataKey="success" name="成功" stroke="#16a34a" fill="#bbf7d0" />
                <Area type="monotone" dataKey="failed" name="失败" stroke="#dc2626" fill="#fecaca" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </section>

        <section className="panel">
          <h2>实时流式运行</h2>
          <p className="hint">POST /run/stream · SSE 逐帧渲染（用户强调的流式能力）</p>
          <div className="stream-form">
            <input
              aria-label="会话 ID"
              value={sessionId}
              onChange={(e) => setSessionId(e.target.value)}
              disabled={stream.running}
            />
            <input
              aria-label="用户输入"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              disabled={stream.running}
            />
            <button onClick={() => void stream.start(sessionId, input)} disabled={stream.running}>
              {stream.running ? '运行中…' : '开始运行'}
            </button>
            {stream.running && <button onClick={stream.stop}>停止</button>}
          </div>
          <div className="stream-output">
            {stream.text || (stream.running ? '等待首个 token…' : '点击「开始运行」查看流式输出')}
          </div>
          {stream.error && <div className="error">错误：{stream.error}</div>}
          <div className="stream-meta">
            事件 {stream.events.length} 帧 · 输入 token {stream.inputTokens} / 输出 token {stream.outputTokens}
            {stream.runId !== null && ` · run #${stream.runId}`}
          </div>
        </section>
      </div>

      <section className="panel">
        <h2>最近运行</h2>
        <table className="data-table">
          <thead>
            <tr>
              <th>ID</th><th>会话</th><th>域</th><th>状态</th>
              <th>健康分</th><th>耗时</th><th>Token</th><th>开始时间</th><th></th>
            </tr>
          </thead>
          <tbody>
            {(recent.data?.items ?? []).map((run) => (
              <tr key={run.id}>
                <td>#{run.id}</td>
                <td>{run.session_id}</td>
                <td>{run.domain}</td>
                <td><StatusBadge status={run.status} /></td>
                <td>{run.health_score ?? '—'}</td>
                <td>{formatMs(run.duration_ms)}</td>
                <td>{run.total_tokens}</td>
                <td className="muted">{run.started_at.slice(0, 19)}</td>
                <td><Link to={`/runs/${run.id}`}>详情</Link></td>
              </tr>
            ))}
            {recent.data?.items.length === 0 && (
              <tr><td colSpan={9} className="muted">暂无运行记录，先用上面的流式面板跑一次。</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
