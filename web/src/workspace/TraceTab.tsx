/** 轨迹 tab：当前会话的运行列表 + 选中运行的瀑布图。 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchRuns } from '../api/client';
import { StatusBadge } from '../components/StatusBadge';
import { RunTrace } from './RunTrace';

interface Props {
  session: string | null;
}

function formatMs(ms: number | null): string {
  if (ms === null) return '—';
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`;
}

export function TraceTab({ session }: Props) {
  const [selected, setSelected] = useState<number | null>(null);
  const runs = useQuery({
    queryKey: ['runs', 'trace', session],
    queryFn: () => fetchRuns({ limit: 100 }),
    refetchInterval: 5000,
  });

  const all = runs.data?.items ?? [];
  const items = session === null ? all : all.filter((r) => r.session_id === session);

  if (selected !== null) {
    return <RunTrace runId={selected} onClose={() => setSelected(null)} />;
  }

  return (
    <div className="page">
      <p className="subtitle">
        {session === null ? '全部会话' : `会话 ${session}`} · 点击运行查看轨迹与健康分
      </p>
      <table className="data-table">
        <thead>
          <tr>
            <th>ID</th><th>会话</th><th>域</th><th>状态</th>
            <th>健康分</th><th>耗时</th><th>Token</th><th>开始时间</th>
          </tr>
        </thead>
        <tbody>
          {items.map((run) => (
            <tr key={run.id} className="trace-row" onClick={() => setSelected(run.id)}>
              <td>#{run.id}</td>
              <td>{run.session_id}</td>
              <td>{run.domain}</td>
              <td><StatusBadge status={run.status} /></td>
              <td>{run.health_score ?? '—'}</td>
              <td>{formatMs(run.duration_ms)}</td>
              <td>{run.total_tokens}</td>
              <td className="muted">{run.started_at.slice(0, 19)}</td>
            </tr>
          ))}
          {items.length === 0 && (
            <tr><td colSpan={8} className="muted">该会话暂无运行记录。</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
