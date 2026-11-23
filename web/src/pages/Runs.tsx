import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useState } from 'react';
import { fetchRuns } from '../api/client';
import { StatusBadge } from '../components/StatusBadge';

function formatMs(ms: number | null): string {
  if (ms === null) return '—';
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`;
}

export function Runs() {
  const [domain, setDomain] = useState('');
  const [status, setStatus] = useState('');
  const [offset, setOffset] = useState(0);
  const limit = 20;

  const runs = useQuery({
    queryKey: ['runs', domain, status, offset],
    queryFn: () => fetchRuns({
      domain: domain || undefined,
      status: status || undefined,
      limit,
      offset,
    }),
    refetchInterval: 5000,
  });

  const total = runs.data?.total ?? 0;

  return (
    <div className="page">
      <h1>运行列表</h1>
      <p className="subtitle">按域 / 状态筛选，点击进入 Trace 详情</p>

      <div className="filters">
        <label>
          域：
          <select value={domain} onChange={(e) => { setDomain(e.target.value); setOffset(0); }}>
            <option value="">全部</option>
            <option value="debug">debug</option>
            <option value="delivery">delivery</option>
            <option value="schedule">schedule</option>
          </select>
        </label>
        <label>
          状态：
          <select value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
            <option value="">全部</option>
            <option value="success">成功</option>
            <option value="failed">失败</option>
            <option value="running">进行中</option>
          </select>
        </label>
        <span className="muted">共 {total} 条</span>
      </div>

      <table className="data-table">
        <thead>
          <tr>
            <th>ID</th><th>会话</th><th>域</th><th>状态</th><th>模型</th>
            <th>健康分</th><th>耗时</th><th>Token</th><th>开始时间</th><th></th>
          </tr>
        </thead>
        <tbody>
          {(runs.data?.items ?? []).map((run) => (
            <tr key={run.id}>
              <td>#{run.id}</td>
              <td>{run.session_id}</td>
              <td>{run.domain}</td>
              <td><StatusBadge status={run.status} /></td>
              <td className="muted">{run.model}</td>
              <td>{run.health_score ?? '—'}</td>
              <td>{formatMs(run.duration_ms)}</td>
              <td>{run.total_tokens}</td>
              <td className="muted">{run.started_at.slice(0, 19)}</td>
              <td><Link to={`/runs/${run.id}`}>Trace →</Link></td>
            </tr>
          ))}
          {runs.data?.items.length === 0 && (
            <tr><td colSpan={10} className="muted">无匹配记录</td></tr>
          )}
        </tbody>
      </table>

      <div className="pagination">
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>上一页</button>
        <span className="muted">{offset + 1}–{Math.min(offset + limit, total)} / {total}</span>
        <button disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>下一页</button>
      </div>
    </div>
  );
}
