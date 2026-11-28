/** 左侧工作区面板：工作区 → 会话两级，会话只能挂在工作区下。
 *
 * 三条产品约束（都落在这一层，别处不再重复判断）：
 * - 没有任何「新会话」入口：新会话必须先在某个工作区里点 +，避免出现无归属会话；
 * - 工作区与会话都可重命名 / 归档，归档只是从默认视图收起，数据不动；
 * - 工作区接口不可用（未开启 / 老后端）时整体回落到「按时间分组」的只读列表。
 */
import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  archiveWorkspace,
  fetchSessionBoard,
  fetchWorkspaceControl,
  restoreWorkspace,
  updateSession,
  updateWorkspace,
} from '../api/client';
import { NewWorkspaceDialog, type SourceKind } from '../pages/workspaces/NewWorkspaceDialog';
import {
  GROUP_ORDER,
  groupOf,
  toSessionTitle,
  type SessionGroup,
  type SessionItem,
} from './sidebar-utils';
import { useSessionList, useSessionRuns } from './sidebar-sessions';
import {
  buildWorkspaceGroups,
  filterGroups,
  injectDraft,
  unboundSessions,
  type DraftSession,
} from './sidebar-workspaces';
import { ChevronIcon, FolderPlusIcon, SearchIcon, SlidersIcon } from './SidebarIcons';
import { SessionRow } from './SidebarSessionRow';
import { WorkspaceBlock } from './SidebarWorkspaceBlock';

export { GROUP_ORDER, groupOf, toSessionTitle };
export type { SessionGroup, SessionItem };

interface Props {
  selected: string | null;
  /** 当前未发送的草稿：只会挂在它选中的工作区底下。 */
  draft: DraftSession | null;
  onSelect: (id: string | null) => void;
  /** 在指定工作区开新会话：归属由首轮请求携带的 workspace_id 落地。 */
  onNewInWorkspace: (workspaceId: string) => void;
}

export function SessionSidebar({ selected, draft, onSelect, onNewInWorkspace }: Props) {
  const runs = useSessionRuns();
  const sessions = useSessionList();
  const queryClient = useQueryClient();

  const [query, setQuery] = useState('');
  const [searchOpen, setSearchOpen] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const [menu, setMenu] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState<SourceKind | null>(null);
  const [createSeq, setCreateSeq] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  // 未归属是历史数据：默认收起，不让它把「工作区 → 会话」的层级冲掉
  const [showUnbound, setShowUnbound] = useState(false);

  // 搜索时把每个工作区的窗口开大：只拿最近 8 条的话，早期会话搜不到等于丢了。
  const fetchLimit = query.trim() ? 50 : 8;
  const board = useQuery({
    queryKey: ['workspace-sessions', fetchLimit, showArchived],
    queryFn: () => fetchSessionBoard(fetchLimit, showArchived),
    retry: 0,
    refetchInterval: 15_000,
    // 搜索会换窗口大小 → 换 queryKey；沿用上一份数据，列表不在输入时闪空
    placeholderData: (previous) => previous,
  });
  const control = useQuery({
    queryKey: ['workspace-control'],
    queryFn: fetchWorkspaceControl,
    retry: 0,
    staleTime: 30_000,
  });

  // 工作区接口不可用（未开启 / 老后端）时不显示分组，只回落成只读的按时间列表。
  const workspacesOff = control.isError;
  const canCreate = !workspacesOff && control.data !== undefined;

  const groups = filterGroups(injectDraft(buildWorkspaceGroups(board.data), draft), {
    query,
    showArchived,
  });
  const unbound = workspacesOff
    ? unboundSessions(undefined, sessions)
    : unboundSessions(board.data, []).filter(() => !query.trim());

  const activeWorkspace =
    draft?.workspaceId ??
    groups.find((group) => group.sessions.some((item) => item.id === selected))?.id ??
    null;

  // 菜单与改名都是「一次只服务一行」的瞬态状态：切走就关掉，避免两行同时展开。
  useEffect(() => {
    if (!menu) return;
    function close(event: MouseEvent) {
      const target = event.target as HTMLElement | null;
      if (!target?.closest('.session-menu, .session-more')) setMenu(null);
    }
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [menu]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setMenu(null);
        setEditing(null);
        setSearchOpen(false);
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  async function mutate(action: () => Promise<unknown>, sessionId?: string) {
    setNotice(null);
    try {
      await action();
      await queryClient.invalidateQueries({ queryKey: ['workspace-sessions'] });
      await queryClient.invalidateQueries({ queryKey: ['workspaces'] });
      if (sessionId) await queryClient.invalidateQueries({ queryKey: ['session', sessionId] });
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '操作失败');
    }
  }

  function toggleCollapsed(id: string) {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function openCreate() {
    // 本地目录导入被禁用时直接给 Git 表单，避免用户先填一张注定被拒的表。
    const blocked =
      control.data &&
      (!control.data.allow_local_import || control.data.allowed_local_roots.length === 0);
    setCreating(blocked ? 'git' : 'local');
    setCreateSeq((seq) => seq + 1);
  }

  function menuKey(kind: 'ws' | 'session', id: string) {
    return `${kind}:${id}`;
  }

  return (
    <aside className="session-sidebar">
      <div className="session-head">
        <h2 className="session-head-title">工作区</h2>
        <div className="session-head-actions">
          <button
            className={`session-icon-btn${searchOpen ? ' is-on' : ''}`}
            onClick={() => setSearchOpen((open) => !open)}
            aria-label="搜索会话"
            aria-pressed={searchOpen}
            title="搜索会话"
          >
            <SearchIcon />
          </button>
          <button
            className={`session-icon-btn${showArchived ? ' is-on' : ''}`}
            onClick={() => setShowArchived((on) => !on)}
            aria-label="显示已归档"
            aria-pressed={showArchived}
            title={showArchived ? '隐藏已归档' : '显示已归档'}
          >
            <SlidersIcon />
          </button>
          {!workspacesOff && (
            <button
              className="session-icon-btn"
              onClick={openCreate}
              disabled={!canCreate}
              aria-label="新建工作区"
              title="新建工作区（本地目录或 Git 地址）"
            >
              <FolderPlusIcon />
            </button>
          )}
        </div>
      </div>

      {searchOpen && (
        <input
          className="session-search"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              setQuery('');
              setSearchOpen(false);
            }
          }}
          placeholder="搜索工作区与会话"
          aria-label="搜索会话"
          autoFocus
        />
      )}

      <nav className="session-nav" aria-label="工作区与会话">
        {groups.length === 0 && !board.isLoading && !workspacesOff && (
          <div className="session-blank">
            <p className="session-blank-title">
              {query ? `没有匹配「${query}」的结果` : '还没有工作区'}
            </p>
            {!query && (
              <button className="session-blank-action" onClick={openCreate} disabled={!canCreate}>
                新建第一个工作区
              </button>
            )}
          </div>
        )}

        <ul className="session-ws-list">
          {groups.map((group) => (
            <WorkspaceBlock
              key={group.id}
              group={group}
              active={activeWorkspace === group.id}
              collapsed={collapsed.has(group.id)}
              selected={selected}
              menu={menu}
              editing={editing}
              menuKey={menuKey}
              onToggle={() => toggleCollapsed(group.id)}
              onNew={() => onNewInWorkspace(group.id)}
              onMenu={setMenu}
              onEdit={setEditing}
              onSelect={onSelect}
              onRenameWorkspace={(next) => {
                setEditing(null);
                void mutate(() => updateWorkspace(group.id, { name: next }));
              }}
              onToggleWorkspaceArchive={() => {
                setMenu(null);
                void mutate(() =>
                  group.archived ? restoreWorkspace(group.id) : archiveWorkspace(group.id),
                );
              }}
              onRenameSession={(id, title) => {
                setEditing(null);
                void mutate(() => updateSession(id, { title }), id);
              }}
              onToggleSessionArchive={(item) => {
                setMenu(null);
                void mutate(() => updateSession(item.id, { archived: !item.archived }), item.id);
              }}
            />
          ))}
        </ul>

        {unbound.length > 0 && (
          <>
            {/* 工作区接口不可用时这就是唯一的列表：不需要再套一层开关 */}
            {!workspacesOff && (
              <button
                className="session-unbound-toggle"
                onClick={() => setShowUnbound((open) => !open)}
                aria-expanded={showUnbound}
              >
                <ChevronIcon open={showUnbound} />
                未归属会话
                <span className="session-unbound-count">· {unbound.length}</span>
              </button>
            )}
            {(workspacesOff || showUnbound) && (
              <ul className="session-ws-sessions">
                {unbound.map((item) => (
                  <SessionRow
                    key={item.id}
                    item={item}
                    active={selected === item.id}
                    renaming={false}
                    menu={false}
                    onSelect={() => onSelect(item.id)}
                    onMenu={() => undefined}
                    onRename={() => undefined}
                    onRenameDone={() => undefined}
                    onToggleArchive={() => undefined}
                  />
                ))}
              </ul>
            )}
          </>
        )}

        {!draft && selected === null && groups.length > 0 && !query && (
          <p className="session-hint">点工作区右侧的 + 开始新会话。</p>
        )}
        {runs.isError && <p className="session-hint">会话加载失败。</p>}
        {workspacesOff && <p className="session-hint">工作区接口不可用，当前为只读会话列表。</p>}
        {notice && (
          <p className="session-hint is-error" role="status">
            {notice}
          </p>
        )}
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
