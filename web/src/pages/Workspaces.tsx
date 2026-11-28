/** 工作区管理页：左栏列表 + 右栏详情，新建走对话框（本地目录 / Git 两种来源）。
 *
 * 上一版是一个全宽原生表单加一张表：所有字段同时展开，来源靠单选框切换，
 * 目录要手敲绝对路径，「浏览」只是把子目录名拼成一行字。结果就是
 * 「不知道该填什么、填错了才报错」。这一版把三件事分开：
 * 1. 列表负责「有哪些」，详情负责「这一个是什么」，创建只处理「新增」；
 * 2. 来源是两张并列卡片，能力不足（白名单为空 / 没装 git）直接禁用并说明原因；
 * 3. 目录由后端 `browse` 逐级浏览，白名单边界由后端判定，前端不拼路径。
 */

import { useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import {
  archiveWorkspace,
  deleteWorkspace,
  fetchWorkspace,
  fetchWorkspaceControl,
  fetchWorkspaceMemory,
  fetchWorkspaceSessions,
  fetchWorkspaces,
  restoreWorkspace,
} from '../api/client';
import type { WorkspaceStatus, WorkspaceSummary } from '../types';
import { NewWorkspaceDialog, type SourceKind } from './workspaces/NewWorkspaceDialog';
import { WorkspaceDetail } from './workspaces/WorkspaceDetail';
import { WorkspaceList } from './workspaces/WorkspaceList';
import { FolderIcon, GitIcon, WorkspaceMark } from './workspaces/icons';
import { errorText } from './workspaces/meta';

/** 搜索同时匹配名称、目录与远端地址：用户认库靠的是路径，不是自己起的名字。 */
function matches(item: WorkspaceSummary, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return (
    item.name.toLowerCase().includes(q) ||
    item.root_path.toLowerCase().includes(q) ||
    item.remote_url.toLowerCase().includes(q)
  );
}

export function Workspaces() {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<WorkspaceStatus>('active');
  const [picked, setPicked] = useState<string | null>(null);
  const [creating, setCreating] = useState<SourceKind | null>(null);
  const [createSeq, setCreateSeq] = useState(0);
  const queryClient = useQueryClient();

  const control = useQuery({
    queryKey: ['workspace-control'],
    queryFn: fetchWorkspaceControl,
    retry: 0,
    staleTime: 30_000,
  });

  const list = useQuery({
    queryKey: ['workspaces'],
    queryFn: () => fetchWorkspaces(),
    retry: 0,
    refetchInterval: 15_000,
  });

  const items = useMemo(() => list.data?.items ?? [], [list.data]);
  const counts = useMemo(
    () => ({
      active: items.filter((item) => item.status === 'active').length,
      archived: items.filter((item) => item.status === 'archived').length,
    }),
    [items],
  );

  const shown = useMemo(
    () => items.filter((item) => item.status === filter && matches(item, query)),
    [items, filter, query],
  );

  // 没有显式选中时落到当前分栏的第一条：进页面就有内容，而不是一块空白。
  const activeId = picked ?? shown[0]?.id ?? null;
  const detail = useQuery({
    queryKey: ['workspace', activeId],
    queryFn: () => fetchWorkspace(activeId as string),
    enabled: Boolean(activeId),
    retry: 0,
  });
  // 只取最近 5 条：详情页要的是「这个工作区记住了什么」，不是记忆浏览器。
  const memories = useQuery({
    queryKey: ['workspace-memory', activeId],
    queryFn: () => fetchWorkspaceMemory(activeId as string, 5),
    enabled: Boolean(activeId),
    retry: 0,
    staleTime: 15_000,
  });
  // 会话同理想法：详情页展示「最近 5 条」，完整列表由左栏分组承担。
  const sessions = useQuery({
    queryKey: ['workspace-sessions', activeId],
    queryFn: () => fetchWorkspaceSessions(activeId as string, 5),
    enabled: Boolean(activeId),
    retry: 0,
    staleTime: 15_000,
  });

  async function refresh() {
    await queryClient.invalidateQueries({ queryKey: ['workspaces'] });
    await queryClient.invalidateQueries({ queryKey: ['workspace-sessions'] });
    if (activeId) await queryClient.invalidateQueries({ queryKey: ['workspace', activeId] });
  }

  function openCreate(kind: SourceKind) {
    setCreating(kind);
    setCreateSeq((seq) => seq + 1);
  }

  const localBlocked = Boolean(
    control.data &&
      (!control.data.allow_local_import || control.data.allowed_local_roots.length === 0),
  );
  const gitBlocked = Boolean(
    control.data && (!control.data.allow_git_import || control.data.git_available === false),
  );

  async function onArchive() {
    if (!activeId) return;
    await archiveWorkspace(activeId);
    setPicked(null);
    await refresh();
  }

  async function onRestore() {
    if (!activeId) return;
    await restoreWorkspace(activeId);
    await refresh();
  }

  async function onDelete(force: boolean) {
    if (!activeId) return;
    await deleteWorkspace(activeId, force);
    setPicked(null);
    await refresh();
  }

  const featureOff = control.isError || list.isError;

  return (
    <div className="wsp-page">
      <header className="wsp-top">
        <Link to="/" className="wsp-brand">
          <span className="rail-mark">
            <WorkspaceMark />
          </span>
          <span className="wsp-brand-name">DevOps Agent</span>
          <span className="wsp-brand-sub">工作区</span>
        </Link>
        <div className="wsp-top-right">
          <span className="wsp-top-hint">一个工作区对应一个代码库</span>
          <Link to="/" className="wsp-btn is-quiet">
            返回对话
          </Link>
        </div>
      </header>

      <div className="wsp-body">
        <WorkspaceList
          items={shown}
          activeId={activeId}
          onSelect={setPicked}
          loading={list.isLoading}
          failed={list.isError}
          query={query}
          onQuery={setQuery}
          filter={filter}
          onFilter={(next) => {
            setFilter(next);
            setPicked(null);
          }}
          counts={counts}
          quota={control.data ? { used: counts.active, max: control.data.max_workspaces } : null}
          onCreate={() => openCreate(localBlocked ? 'git' : 'local')}
        />

        <main className="wsp-main">
          {featureOff ? (
            <div className="wsp-offline">
              <h1>工作区功能不可用</h1>
              <p>
                {errorText(control.error ?? list.error)}。确认部署已开启工作区（
                <code>workspaces.enabled</code>），并且后端进程是包含
                <code>/api/workspaces/control</code> 的新版本。
              </p>
            </div>
          ) : activeId ? (
            <WorkspaceDetail
              key={activeId}
              item={detail.data}
              loading={detail.isLoading}
              memories={memories.data?.items ?? []}
              memoriesLoading={memories.isLoading}
              sessions={sessions.data?.items ?? []}
              sessionsLoading={sessions.isLoading}
              onArchive={onArchive}
              onRestore={onRestore}
              onDelete={onDelete}
            />
          ) : (
            <div className="wsp-empty">
              <span className="wsp-empty-mark">
                <WorkspaceMark size={22} />
              </span>
              <h1>让 Agent 有个固定的代码库</h1>
              <p className="wsp-empty-lede">
                一个工作区 = 一个代码库 + 一份长期记忆：同一工作区的会话共享记忆，会话上下文仍各自隔离。
              </p>
              <p className="wsp-empty-sub">选一个本地目录，或者给它一个 Git 地址。</p>
              <div className="wsp-empty-options">
                <button
                  type="button"
                  className="wsp-option"
                  disabled={localBlocked}
                  onClick={() => openCreate('local')}
                >
                  <span className="wsp-option-icon">
                    <FolderIcon size={18} />
                  </span>
                  <span className="wsp-option-title">本地目录</span>
                  <span className="wsp-option-desc">引用白名单内已有目录，不复制、不搬动</span>
                  <span className="wsp-option-foot">
                    {localBlocked ? '需要先在设置里配置白名单' : '逐级浏览后选用'}
                  </span>
                </button>
                <button
                  type="button"
                  className="wsp-option"
                  disabled={gitBlocked}
                  onClick={() => openCreate('git')}
                >
                  <span className="wsp-option-icon is-git">
                    <GitIcon size={18} />
                  </span>
                  <span className="wsp-option-title">Git 地址</span>
                  <span className="wsp-option-desc">克隆到受管目录，支持分支与凭据名</span>
                  <span className="wsp-option-foot">
                    {gitBlocked ? '本机 git 不可用或部署已关闭' : 'https / ssh / git@'}
                  </span>
                </button>
              </div>
              <p className="wsp-empty-foot">
                本地目录白名单在
                <Link to="/?tab=settings&domain=workspace">「设置 · 工作区」</Link>
                维护；白名单为空时本地导入整体不可用，这是刻意的安全默认。
              </p>
            </div>
          )}
        </main>
      </div>

      {creating ? (
        <NewWorkspaceDialog
          key={createSeq}
          initialKind={creating}
          control={control.data}
          onDismiss={() => setCreating(null)}
          onCreated={(item) => {
            setCreating(null);
            setPicked(item.id);
            setFilter(item.status);
            queryClient.setQueryData(['workspace', item.id], item);
            void queryClient.invalidateQueries({ queryKey: ['workspaces'] });
          }}
        />
      ) : null}
    </div>
  );
}
