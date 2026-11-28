/** 左侧会话栏：顶层是工作区，工作区底下挂最近会话；没有归属的老会话单独一栏。
 *
 * 三层信息的分工：
 * - 工作区分组来自 `GET /api/workspaces/sessions`（一次请求拿全量，避免 N 个并发请求乱序）；
 * - 会话本身仍以 runs 聚合为底（标题取最早一条输入），本地草稿再从 localStorage 合并进来；
 * - 工作区接口不可用（未开启 / 老后端）时整体回落到「按时间分组」的老视图。
 */
import { useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchRuns, fetchSessionBoard, fetchWorkspaceControl } from '../api/client';
import type { Run } from '../types';
import { NewWorkspaceDialog, type SourceKind } from '../pages/workspaces/NewWorkspaceDialog';
import {
  GROUP_ORDER,
  groupOf,
  toSessionTitle,
  type SessionGroup,
  type SessionItem,
} from './sidebar-utils';
import { buildWorkspaceGroups, unboundSessions } from './sidebar-workspaces';
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
  /** 当前草稿会话打算落在哪个工作区：用于高亮，也用于把没归属的会话归位。 */
  workspace: string | null;
  onSelect: (id: string | null) => void;
  /** 真正开一个新会话（清空当前对话），与「选中全部会话」区分。 */
  onNew: () => void;
  /** 在指定工作区开新会话：归属由首轮请求携带的 workspace_id 落地。 */
  onNewInWorkspace: (workspaceId: string) => void;
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

function FolderIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M2.2 4.6c0-.6.5-1.1 1.1-1.1h2.6l1.3 1.5h5.4c.6 0 1.1.5 1.1 1.1v5.6c0 .6-.5 1.1-1.1 1.1H3.3c-.6 0-1.1-.5-1.1-1.1V4.6Z"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** 一行会话：标题 + 计数；失败数用红色计数提示。 */
function SessionRow({
  item,
  active,
  onSelect,
}: {
  item: SessionItem;
  active: boolean;
  onSelect: () => void;
}) {
  const count = item.runCount || item.messageCount || 0;
  return (
    <button
      className={`session-item ${active ? 'active' : ''}`}
      onClick={onSelect}
      title={`${item.id} · ${item.runCount} 次运行 · ${item.messageCount ?? 0} 轮对话${
        item.failedCount ? ` · ${item.failedCount} 失败` : ''
      }`}
    >
      <span className="session-name">{item.title}</span>
      <span className={`session-count ${item.failedCount ? 'has-failed' : ''}`}>{count}</span>
    </button>
  );
}

export function SessionSidebar({ selected, workspace, onSelect, onNew, onNewInWorkspace }: Props) {
  const runs = useSessionRuns();
  const sessions = useSessionList();
  const [query, setQuery] = useState('');
  // 搜索时把每个工作区的窗口开大：只拿最近 5 条的话，早期会话搜不到等于丢了。
  const fetchLimit = query.trim() ? 50 : 5;
  const board = useQuery({
    queryKey: ['workspace-sessions', fetchLimit],
    queryFn: () => fetchSessionBoard(fetchLimit),
    retry: 0,
    refetchInterval: 15_000,
  });
  const control = useQuery({
    queryKey: ['workspace-control'],
    queryFn: fetchWorkspaceControl,
    retry: 0,
    staleTime: 30_000,
  });
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState<SourceKind | null>(null);
  const [createSeq, setCreateSeq] = useState(0);

  const groups = useMemo(() => buildWorkspaceGroups(board.data), [board.data]);
  const unbound = useMemo(() => unboundSessions(board.data, sessions), [board.data, sessions]);
  // 工作区接口不可用（未开启 / 老后端）时不显示分组入口，但会话列表本身照旧。
  const workspacesOff = control.isError;
  const canCreate = !workspacesOff && control.data !== undefined;

  function match(item: SessionItem) {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return item.title.toLowerCase().includes(q) || item.id.toLowerCase().includes(q);
  }

  const searching = query.trim().length > 0;
  const matchedUnbound = unbound.filter(match);
  // 未归属列表不设上限会变成一堵墙：默认只铺最近 12 条，搜索时展开到窗口上限。
  const shownUnbound = matchedUnbound.slice(0, searching ? 50 : 12);
  const hiddenUnbound = matchedUnbound.length - shownUnbound.length;
  const visibleGroups = groups
    .map((group) => ({ ...group, shown: group.sessions.filter(match) }))
    // 搜索时把「一条都不匹配」和「本来就没会话」的工作区一起收起，列表才干净。
    .filter((group) => group.shown.length > 0 || (!searching && group.sessions.length === 0));

  const buckets = new Map<SessionGroup, SessionItem[]>();
  for (const item of shownUnbound) {
    const group = groupOf(item.lastAt);
    const list = buckets.get(group) ?? [];
    list.push(item);
    buckets.set(group, list);
  }
  const timeGroups = GROUP_ORDER.filter((group) => buckets.has(group)).map(
    (group) => [group, buckets.get(group)!] as const,
  );

  const shownCount =
    visibleGroups.reduce((sum, group) => sum + group.shown.length, 0) + matchedUnbound.length;

  function openCreate() {
    // 本地目录导入被禁用时直接给 Git 表单，避免用户先填一张注定被拒的表。
    const blocked =
      control.data &&
      (!control.data.allow_local_import || control.data.allowed_local_roots.length === 0);
    setCreating(blocked ? 'git' : 'local');
    setCreateSeq((seq) => seq + 1);
  }

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

      <nav className="session-nav" aria-label="工作区与会话">
        <div className="session-section-head">
          <h2 className="session-group-label">工作区</h2>
          {!workspacesOff && (
            <button
              className="session-add"
              onClick={openCreate}
              disabled={!canCreate}
              aria-label="新建工作区"
              title="新建工作区（本地目录或 Git 地址）"
            >
              <PlusIcon />
            </button>
          )}
        </div>

        {groups.length === 0 && !workspacesOff && !board.isLoading && (
          <p className="session-empty">还没有工作区，点上面的 + 添加一个。</p>
        )}

        <ul className="session-ws-list">
          {visibleGroups.map((group) => (
            <li
              key={group.id}
              className={`session-ws ${group.status !== 'active' ? 'is-archived' : ''}`}
            >
              <button
                className={`session-ws-head ${workspace === group.id ? 'active' : ''}`}
                onClick={() => onNewInWorkspace(group.id)}
                aria-label={`在「${group.name}」中新开会话`}
                title={`${group.rootPath}${group.status !== 'active' ? '（已归档）' : ''} · 点此新开会话`}
              >
                <span className="session-ws-icon">
                  <FolderIcon />
                </span>
                <span className="session-name">{group.name}</span>
                {group.status !== 'active' && <span className="session-ws-tag">归档</span>}
                <span className="session-count">{group.count}</span>
              </button>
              {group.shown.length > 0 && (
                <ul className="session-ws-sessions">
                  {group.shown.map((item) => (
                    <li key={item.id}>
                      <SessionRow
                        item={item}
                        active={selected === item.id}
                        onSelect={() => onSelect(item.id)}
                      />
                    </li>
                  ))}
                </ul>
              )}
              {group.sessions.length === 0 && <p className="session-empty">还没有会话</p>}
              {!searching && group.count > group.sessions.length && (
                <p className="session-empty">
                  还有 {group.count - group.sessions.length} 条，用上方搜索查找
                </p>
              )}
            </li>
          ))}
        </ul>

        {shownUnbound.length > 0 && (
          <>
            {groups.length > 0 && <h2 className="session-group-label">未归属工作区</h2>}
            <ul className="session-plain-list">
              {timeGroups.map(([label, items]) => (
                <li key={label}>
                  {groups.length > 0 && <div className="session-time-label">{label}</div>}
                  <ul className="session-plain-sublist">
                    {items.map((item) => (
                      <li key={item.id}>
                        <SessionRow
                          item={item}
                          active={selected === item.id}
                          onSelect={() => onSelect(item.id)}
                        />
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
            {hiddenUnbound > 0 && (
              <p className="session-empty">还有 {hiddenUnbound} 条，用上方搜索查找</p>
            )}
          </>
        )}

        {shownCount === 0 && !runs.isLoading && query && (
          <p className="session-empty">没有匹配「{query}」的会话。</p>
        )}
        {sessions.length === 0 && !runs.isLoading && !query && (
          <p className="session-empty">暂无会话，去「对话」tab 跑一次。</p>
        )}
        {runs.isError && <p className="session-empty">会话加载失败。</p>}
      </nav>

      {creating ? (
        <NewWorkspaceDialog
          key={createSeq}
          initialKind={creating}
          control={control.data}
          onDismiss={() => setCreating(null)}
          onCreated={(item) => {
            setCreating(null);
            // 新建后立刻进左栏：分组与工作区列表一起失效重取。
            void queryClient.invalidateQueries({ queryKey: ['workspace-sessions'] });
            void queryClient.invalidateQueries({ queryKey: ['workspaces'] });
            onNewInWorkspace(item.id);
          }}
        />
      ) : null}
    </aside>
  );
}
