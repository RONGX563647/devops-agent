/** 左侧会话栏：合并后端 runs 聚合与本地缓存的会话，负责分组、搜索与选择。 */
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchRuns } from '../api/client';
import type { Run } from '../types';
import {
  GROUP_ORDER,
  groupOf,
  toSessionTitle,
  type SessionGroup,
  type SessionItem,
} from './sidebar-utils';
import { listSessions } from './store/history';
import { useStoreVersion } from './store/state';
import type { StoredSession } from './history';

export { GROUP_ORDER, groupOf, toSessionTitle };
export type { SessionGroup, SessionItem };

export function aggregateSessions(runs: Run[]): SessionItem[] {
  const map = new Map<string, SessionItem & { firstAt: string }>();
  for (const run of runs) {
    const cur = map.get(run.session_id);
    if (!cur) {
      map.set(run.session_id, {
        id: run.session_id,
        title: toSessionTitle(run.input),
        lastAt: run.started_at,
        firstAt: run.started_at,
        runCount: 1,
        failedCount: run.status === 'failed' ? 1 : 0,
      });
      continue;
    }
    cur.runCount += 1;
    cur.failedCount += run.status === 'failed' ? 1 : 0;
    if (run.started_at > cur.lastAt) cur.lastAt = run.started_at;
    // 标题取最早那条输入，避免最新一轮的短问题覆盖会话主题
    if (run.started_at < cur.firstAt) {
      cur.firstAt = run.started_at;
      cur.title = toSessionTitle(run.input);
    }
  }
  return [...map.values()]
    .sort((a, b) => b.lastAt.localeCompare(a.lastAt))
    .map((item) => ({
      id: item.id,
      title: item.title,
      lastAt: item.lastAt,
      runCount: item.runCount,
      failedCount: item.failedCount,
    }));
}

/** 合并远端会话与本地缓存：本地新会话（后端还没有运行记录）也要出现在侧栏。 */
export function mergeSessions(remote: SessionItem[], local: StoredSession[]): SessionItem[] {
  const merged = new Map(remote.map((item) => [item.id, { ...item }]));
  for (const item of local) {
    const existing = merged.get(item.id);
    if (existing) {
      existing.messageCount = item.messageCount;
      if (existing.runCount === 0) existing.lastAt = item.lastAt;
      continue;
    }
    merged.set(item.id, {
      id: item.id,
      title: item.title,
      lastAt: item.lastAt,
      runCount: 0,
      failedCount: 0,
      messageCount: item.messageCount,
    });
  }
  return [...merged.values()].sort((a, b) => b.lastAt.localeCompare(a.lastAt));
}

/** 会话列表数据源：侧栏与顶部统计共用同一个 queryKey，缓存自动复用。 */
export function useSessionRuns() {
  return useQuery({
    queryKey: ['runs', 'sessions'],
    queryFn: () => fetchRuns({ limit: 200 }),
    refetchInterval: 10000,
  });
}

/** 合并后的会话列表；订阅 store 版本号，本地新会话创建后立即出现。 */
export function useSessionList(): SessionItem[] {
  const runs = useSessionRuns();
  useStoreVersion();
  return mergeSessions(aggregateSessions(runs.data?.items ?? []), listSessions());
}

interface Props {
  selected: string | null;
  onSelect: (id: string | null) => void;
  /** 真正开一个新会话（清空当前对话），与「选中全部会话」区分。 */
  onNew: () => void;
}

function MarkIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M3.2 5.5 8 2.8l4.8 2.7v5L8 13.2l-4.8-2.7v-5Z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <path d="M8 8v5.2M8 8 3.4 5.6M8 8l4.6-2.4" stroke="currentColor" strokeWidth="1.1" opacity=".7" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M8 3.4v9.2M3.4 8h9.2" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

export function SessionSidebar({ selected, onSelect, onNew }: Props) {
  const runs = useSessionRuns();
  const sessions = useSessionList();
  const [query, setQuery] = useState('');

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter(
      (s) => s.title.toLowerCase().includes(q) || s.id.toLowerCase().includes(q),
    );
  }, [sessions, query]);

  const grouped = useMemo(() => {
    const buckets = new Map<SessionGroup, SessionItem[]>();
    for (const item of filtered) {
      const group = groupOf(item.lastAt);
      const list = buckets.get(group) ?? [];
      list.push(item);
      buckets.set(group, list);
    }
    return GROUP_ORDER.filter((g) => buckets.has(g)).map((g) => [g, buckets.get(g)!] as const);
  }, [filtered]);

  return (
    <aside className="session-sidebar">
      <div className="session-sidebar-brand">
        <span className="rail-mark">
          <MarkIcon />
        </span>
        <span>
          <span className="brand-name">DevOps Agent</span>
          <span className="brand-meta">workspace</span>
        </span>
      </div>
      <button className="session-new" onClick={onNew}>
        <PlusIcon />
        新会话
      </button>
      <input
        className="session-search"
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="搜索会话"
        aria-label="搜索会话"
      />
      <button
        className={`session-item ${selected === null ? 'active' : ''}`}
        onClick={() => onSelect(null)}
      >
        <span className="session-name">全部会话</span>
        <span className="session-count">{sessions.length}</span>
      </button>
      {grouped.map(([group, items]) => (
        <div key={group}>
          <div className="session-group-label">{group}</div>
          {items.map((s) => {
            const count = s.runCount || s.messageCount || 0;
            return (
              <button
                key={s.id}
                className={`session-item ${selected === s.id ? 'active' : ''}`}
                onClick={() => onSelect(s.id)}
                title={`${s.id} · ${s.runCount} 次运行 · ${s.messageCount ?? 0} 轮对话${
                  s.failedCount ? ` · ${s.failedCount} 失败` : ''
                }`}
              >
                <span className="session-name">{s.title}</span>
                <span className={`session-count ${s.failedCount ? 'has-failed' : ''}`}>{count}</span>
              </button>
            );
          })}
        </div>
      ))}
      {filtered.length === 0 && !runs.isLoading && query && (
        <p className="session-empty">没有匹配「{query}」的会话。</p>
      )}
      {sessions.length === 0 && !runs.isLoading && !query && (
        <p className="session-empty">暂无会话，去「对话」tab 跑一次。</p>
      )}
      {runs.isError && <p className="session-empty">会话加载失败。</p>}
    </aside>
  );
}
