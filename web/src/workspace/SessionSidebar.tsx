/** 左侧会话栏：从 runs 按 session_id 聚合，不改后端。 */
import { useQuery } from '@tanstack/react-query';
import { fetchRuns } from '../api/client';
import type { Run } from '../types';

interface SessionItem {
  id: string;
  lastAt: string;
  runCount: number;
  failedCount: number;
}

function aggregateSessions(runs: Run[]): SessionItem[] {
  const map = new Map<string, SessionItem>();
  for (const run of runs) {
    const cur = map.get(run.session_id);
    if (!cur) {
      map.set(run.session_id, {
        id: run.session_id,
        lastAt: run.started_at,
        runCount: 1,
        failedCount: run.status === 'failed' ? 1 : 0,
      });
    } else {
      cur.runCount += 1;
      cur.failedCount += run.status === 'failed' ? 1 : 0;
      if (run.started_at > cur.lastAt) cur.lastAt = run.started_at;
    }
  }
  return [...map.values()].sort((a, b) => b.lastAt.localeCompare(a.lastAt));
}

interface Props {
  selected: string | null;
  onSelect: (id: string | null) => void;
}

export function SessionSidebar({ selected, onSelect }: Props) {
  const runs = useQuery({
    queryKey: ['runs', 'sessions'],
    queryFn: () => fetchRuns({ limit: 200 }),
    refetchInterval: 10000,
  });
  const sessions = aggregateSessions(runs.data?.items ?? []);

  return (
    <aside className="session-sidebar">
      <div className="session-sidebar-brand">DevOps Agent</div>
      <button className="session-new" onClick={() => onSelect(null)}>
        ＋ 新会话
      </button>
      <div className="session-group-label">会话列表</div>
      <button
        className={`session-item ${selected === null ? 'active' : ''}`}
        onClick={() => onSelect(null)}
      >
        <span className="session-name">全部会话</span>
        <span className="session-count">{runs.data?.total ?? 0}</span>
      </button>
      {sessions.map((s) => (
        <button
          key={s.id}
          className={`session-item ${selected === s.id ? 'active' : ''}`}
          onClick={() => onSelect(s.id)}
          title={`${s.runCount} 次运行${s.failedCount ? ` · ${s.failedCount} 失败` : ''}`}
        >
          <span className="session-name">{s.id}</span>
          <span className={`session-count ${s.failedCount ? 'has-failed' : ''}`}>{s.runCount}</span>
        </button>
      ))}
      {sessions.length === 0 && !runs.isLoading && (
        <p className="session-empty">暂无会话，去「对话」tab 跑一次。</p>
      )}
      {runs.isError && <p className="session-empty">会话加载失败。</p>}
    </aside>
  );
}

export { aggregateSessions };
