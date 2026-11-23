/** 单次运行的轨迹展示（瀑布图 + 健康分 + 输入输出），供路由详情与轨迹 tab 复用。 */
import { useQuery } from '@tanstack/react-query';
import { fetchInputStages, fetchRun, fetchRunHealth, fetchSpans } from '../api/client';
import { StatusBadge } from '../components/StatusBadge';
import type { Span } from '../types';

function spanLabel(span: Span): string {
  if (span.type === 'model') return `模型调用 · ${span.name}`;
  if (span.type === 'tool') return `工具调用 · ${span.name}`;
  return `权限判定 · ${span.name}`;
}

interface Props {
  runId: number;
  onClose?: () => void;
}

export function RunTrace({ runId, onClose }: Props) {
  const run = useQuery({ queryKey: ['run', runId], queryFn: () => fetchRun(runId) });
  const spans = useQuery({ queryKey: ['spans', runId], queryFn: () => fetchSpans(runId) });
  const health = useQuery({ queryKey: ['run-health', runId], queryFn: () => fetchRunHealth(runId) });
  const inputStages = useQuery({
    queryKey: ['input-stages', runId],
    queryFn: () => fetchInputStages(runId),
  });

  if (run.isLoading) return <p className="subtitle">加载中…</p>;
  if (run.isError || !run.data) return <p className="error">加载失败：run #{runId} 不存在</p>;

  const r = run.data;
  const maxOffset = Math.max(
    1,
    ...(spans.data ?? []).map((s) => s.start_offset_ms + (s.duration_ms ?? 0)),
  );

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ fontSize: 16, margin: '0 0 12px' }}>Trace · run #{r.id}</h2>
        {onClose && (
          <button className="trace-close" onClick={onClose}>返回列表</button>
        )}
      </div>

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
        <h2>输入解析链</h2>
        <p className="hint">
          数据库完整保留每次解析的全部阶段（含无改动的），这里按执行顺序全量列出，
          会话轨迹里只展示真正生效的那几条。
        </p>
        <div className="parse-stages">
          {(inputStages.data ?? []).map((stage) => (
            <div className="parse-stage" data-status={stage.status} key={stage.seq}>
              <span className="parse-stage-label">{stage.label}</span>
              <span className="parse-stage-summary mono" title={stage.detail || stage.summary}>
                {stage.summary}
              </span>
              <span className="parse-stage-metric mono">
                {stage.changed ? `${stage.before} → ${stage.after}` : '无改动'}
              </span>
            </div>
          ))}
          {(inputStages.data ?? []).length === 0 && (
            <p className="muted">该运行没有解析记录（可能来自非流式 /run）。</p>
          )}
        </div>
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
                <span className="waterfall-label">{span.status === 'error' ? '✗' : '•'} {spanLabel(span)}</span>
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
      </section>
    </div>
  );
}
