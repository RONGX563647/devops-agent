/** 左侧会话栏：从 runs 聚合出会话标题、分组与计数，不改后端。 */
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchRuns } from '../api/client';
import type { Run } from '../types';

export interface SessionItem {
  id: string;
  /** 会话标题：取该会话最早一条用户输入，超长截断。 */
  title: string;
  lastAt: string;
  runCount: number;
  failedCount: number;
}

export type SessionGroup = '今天' | '昨天' | '更早';

const GROUP_ORDER: SessionGroup[] = ['今天', '昨天', '更早'];
const TITLE_MAX = 20;

/** 用户输入压成单行并截断，空输入给兜底标题。 */
export function toSessionTitle(input: string, max: number = TITLE_MAX): string {
  const clean = input.replace(/\s+/g, ' ').trim();
  if (!clean) return '未命名会话';
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

/** 按本地日界分组：今天 / 昨天 / 更早。 */
export function groupOf(iso: string, now: Date = new Date()): SessionGroup {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '更早';
  const dayStart = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diffDays = Math.round((dayStart(now) - dayStart(date)) / 86_400_000);
  if (diffDays <= 0) return '今天';
  if (diffDays === 1) return '昨天';
  return '更早';
}

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

/** 会话列表数据源：侧栏与顶部统计共用同一个 queryKey，缓存自动复用。 */
export function useSessionRuns() {
  return useQuery({
    queryKey: ['runs', 'sessions'],
    queryFn: () => fetchRuns({ limit: 200 }),
    refetchInterval: 10000,
  });
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
  const [query, setQuery] = useState('');
  const sessions = useMemo(() => aggregateSessions(runs.data?.items ?? []), [runs.data]);

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
        <span className="session-count">{runs.data?.total ?? 0}</span>
      </button>
      {grouped.map(([group, items]) => (
        <div key={group}>
          <div className="session-group-label">{group}</div>
          {items.map((s) => (
            <button
              key={s.id}
              className={`session-item ${selected === s.id ? 'active' : ''}`}
              onClick={() => onSelect(s.id)}
              title={`${s.id} · ${s.runCount} 次运行${s.failedCount ? ` · ${s.failedCount} 失败` : ''}`}
            >
              <span className="session-name">{s.title}</span>
              <span className={`session-count ${s.failedCount ? 'has-failed' : ''}`}>
                {s.runCount}
              </span>
            </button>
          ))}
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
