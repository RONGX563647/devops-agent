import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { useState } from 'react';
import { fetchRun, fetchRunHealth, fetchSpans } from '../api/client';
import { StatusBadge } from '../components/StatusBadge';
import type { Span } from '../types';

function spanLabel(span: Span): string {
  if (span.type === 'model') return `模型调用 · ${span.name}`;
  if (span.type === 'tool') return `工具调用 · ${span.name}`;
  return `权限判定 · ${span.name}`;
}

export function RunDetail() {
  const { id } = useParams();
  const runId = Number(id);
  const run = useQuery({ queryKey: ['run', runId], queryFn: () => fetchRun(runId) });
  const spans = useQuery({ queryKey: ['spans', runId], queryFn: () => fetchSpans(runId) });
  const health = useQuery({ queryKey: ['run-health', runId], queryFn: () => fetchRunHealth(runId) });
  const [expanded, setExpanded] = useState<number | null>(null);

  if (run.isLoading) return <div className="page"><p className="subtitle">加载中…</p></div>;
  if (run.isError || !run.data) {
    return (
      <div className="page">
        <h1>运行详情</h1>
        <p className="error">加载失败：run #{runId} 不存在</p>
        <Link to="/runs">返回列表</Link>
      </div>
    );
  }

  const r = run.data;
  const maxOffset = Math.max(
    1,
    ...(spans.data ?? []).map((s) => s.start_offset_ms + (s.duration_ms ?? 0)),
  );

  return (
    <div className="page">
      <h1>Trace 详情 · run #{r.id}</h1>
      <p className="subtitle"><Link to="/runs">← 运行列表</Link></p>

      <section className="panel">
        <h2>运行概览</h2>
        <div className="detail-grid">
          <div><span className="detail-key">会话</span><span>{r.session_id}</span></div>
          <div><span className="detail-key">域</span><span>{r.domain}</span></div>
          <div><span className="detail-key">状态</span><StatusBadge status={r.status} /></div>
          <div><span className="detail-key">模型</span><span>{r.model}</span></div>
          <div><span className="detail-key">耗时</span><span>{r.duration_ms ?? '—'} ms</span></div>
          <div><span className="detail-key">Token</span><span>输入 {r.input_tokens} / 输出 {r.output_tokens}</span></div>
        </div>
        <div className="io-block"><b>输入</b><pre>{r.input}</pre></div>
        <div className="io-block"><b>输出</b><pre>{r.output || '（空）'}</pre></div>
      </section>

      <section className="panel">
        <h2>健康分拆解</h2>
        {health.data ? (
          <div className="health-dims">
            <div className="health-total">总分 {health.data.score}</div>
            {Object.entries(health.data.dimensions).map(([key, value]) => (
              <div className="health-dim" key={key}>
                <span className="dim-name">{key}</span>
                <div className="dim-bar">
                  <div className="dim-fill" style={{ width: `${value ?? 0}%` }} />
                </div>
                <span className="dim-value">{value ?? '—'}</span>
              </div>
            ))}
          </div>
        ) : <p className="muted">计算中…</p>}
      </section>

      <section className="panel">
        <h2>时间线（瀑布图）</h2>
        <p className="hint">横条位置 = 相对 run 起点的偏移，宽度 = 该步骤耗时</p>
        <div className="waterfall">
          {(spans.data ?? []).map((span) => {
            const left = (span.start_offset_ms / maxOffset) * 100;
            const width = Math.max(1.5, ((span.duration_ms ?? 0) / maxOffset) * 100);
            return (
              <div className="waterfall-row" key={span.id}>
                <button
                  className="waterfall-label"
                  onClick={() => setExpanded(expanded === span.id ? null : span.id)}
                >
                  {span.status === 'error' ? '✗' : '•'} {spanLabel(span)}
                </button>
                <div className="waterfall-track">
                  <div
                    className={`waterfall-bar bar-${span.type} ${span.status === 'error' ? 'bar-error' : ''}`}
                    style={{ left: `${left}%`, width: `${width}%` }}
                    title={`${span.duration_ms ?? 0} ms`}
                  />
                </div>
                <span className="waterfall-ms">{span.duration_ms ?? 0} ms</span>
              </div>
            );
          })}
          {(spans.data ?? []).length === 0 && <p className="muted">该运行没有 span（可能用了非流式 /run）。</p>}
        </div>
        {expanded !== null && (() => {
          const span = spans.data?.find((s) => s.id === expanded);
          if (!span) return null;
          return (
            <div className="io-block">
              <b>{spanLabel(span)} · 详情</b>
              <pre>状态: {span.status}{span.error ? ` · 错误: ${span.error}` : ''}{'\n'}输入: {span.input || '（空）'}{'\n'}输出: {span.output || '（空）'}</pre>
            </div>
          );
        })()}
      </section>
    </div>
  );
}
