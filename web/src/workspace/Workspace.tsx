/** 会话工作台：左侧会话栏 + 单行顶栏（会话标题、运行统计、视图 tab）。 */
import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { fetchWorkspaceSession } from '../api/client';
import { SessionSidebar } from './SessionSidebar';
import { useSessionList } from './sidebar-sessions';
import { Dashboard } from '../pages/Dashboard';
import { Quality } from '../pages/Quality';
import { Cost } from '../pages/Cost';
import { ChatTab } from './chat/ChatTab';
import { SecretsTab } from './SecretsTab';
import { SettingsTab } from './settings/SettingsTab';
import { TraceTab } from './TraceTab';
import { send } from './store/actions';
import { ensureHistory } from './store/history';
import { newId } from './id';

/** 一级导航按性质分三组（SPEC-0022 §4.1）。
 *
 * 七个 tab 平铺时，「会话级」「跨会话观测」「配置」三种性质挤在一行，
 * 用户不知道该往哪看。分组只是加一层视觉层次，`parseTab` 的合法集合不变 ——
 * 旧深链（`?tab=secrets`）零改动。
 */
const TABS = [
  { key: 'chat', label: '对话', group: '会话' },
  { key: 'trace', label: '轨迹', group: '会话' },
  { key: 'metrics', label: '指标', group: '观测' },
  { key: 'quality', label: '质量', group: '观测' },
  { key: 'cost', label: '成本', group: '观测' },
  { key: 'secrets', label: '密钥', group: '观测' },
  { key: 'settings', label: '设置', group: '配置' },
] as const;

const TAB_GROUPS = [...new Set(TABS.map((t) => t.group))];

export type TabKey = (typeof TABS)[number]['key'];

const VALID_KEYS = new Set<string>(TABS.map((t) => t.key));

function parseTab(value: string | null): TabKey {
  return value && VALID_KEYS.has(value) ? (value as TabKey) : 'chat';
}

interface Props {
  /** 供旧路由深链复用：指定初始 tab。 */
  initialTab?: string | null;
  initialSession?: string | null;
}

export function Workspace({ initialTab = null, initialSession = null }: Props) {
  const [params, setParams] = useSearchParams();
  const tab = parseTab(params.get('tab') ?? initialTab);
  const session = params.get('session') ?? initialSession;
  // 新会话先用一个草稿 id 承载消息，首次发送时再落到 URL，避免固定 live-1。
  const [draftId, setDraftId] = useState(() => newId('sess'));
  // 草稿会话准备落进哪个工作区：只对「还没发出第一轮」的会话有意义。
  // 已有会话的归属以服务端绑定为准，再从界面传一次会把 409 引进来。
  const [workspace, setWorkspace] = useState<string | null>(null);
  const sessionId = session ?? draftId;

  const sessions = useSessionList();
  const queryClient = useQueryClient();
  // 顶栏标题以服务端为准：改名只写进绑定行，runs 聚合里仍是原始输入。
  const detail = useQuery({
    queryKey: ['session', session],
    queryFn: () => fetchWorkspaceSession(session as string),
    enabled: Boolean(session),
    retry: 0,
    staleTime: 30_000,
  });
  const current = session ? sessions.find((item) => item.id === session) : undefined;
  const title = session ? (detail.data?.title || current?.title || session) : '新会话';
  const totalRuns = sessions.reduce((sum, item) => sum + item.runCount, 0);
  const failedRuns = sessions.reduce((sum, item) => sum + item.failedCount, 0);

  // 选中历史会话时，本地没有缓存就用后端 runs 重建只读历史。
  useEffect(() => {
    if (session) void ensureHistory(session);
  }, [session]);

  function switchTab(key: TabKey) {
    const next = new URLSearchParams(params);
    next.set('tab', key);
    setParams(next, { replace: true });
  }

  function switchSession(id: string | null) {
    const next = new URLSearchParams(params);
    if (id === null) next.delete('session');
    else next.set('session', id);
    setParams(next, { replace: true });
    // 切到别的会话后，上一条草稿选的工作区不再适用；归属交给服务端绑定。
    if (id !== null) setWorkspace(null);
  }

  /** 在某个工作区里新开会话：草稿记住它，首轮请求带上 workspace_id 由后端落绑定。 */
  function startNewInWorkspace(workspaceId: string) {
    setDraftId(newId('sess'));
    switchSession(null);
    setWorkspace(workspaceId);
  }

  /** 首次发送：把草稿 id 写回 URL，让刷新/深链能回到同一会话。 */
  function handleFirstSend() {
    if (!session) switchSession(draftId);
    // 后端刚落下归属，立刻重取分组：新会话要在工作区底下出现，而不是等 15s 轮询。
    void queryClient.invalidateQueries({ queryKey: ['workspace-sessions'] });
  }

  return (
    <div className="workspace">
      <SessionSidebar
        selected={session}
        draft={!session && workspace ? { id: draftId, workspaceId: workspace, title: '新会话' } : null}
        onSelect={switchSession}
        onNewInWorkspace={startNewInWorkspace}
      />
      <div className="workspace-main">
        <header className="workspace-header">
          <h1 className="workspace-title">{title}</h1>
          <span className="workspace-chip">
            {sessions.length} 个会话 · {totalRuns} 次运行
            {failedRuns ? ` · ${failedRuns} 次失败` : ''}
          </span>
          <Link className="workspace-chip" to="/workspaces">
            工作区
          </Link>
          <nav className="workspace-tabs" role="tablist" aria-label="会话视图">
            {TAB_GROUPS.map((group) => (
              <div className="workspace-tabgroup" key={group} role="group" aria-label={group}>
                <span className="workspace-tabgroup-label" aria-hidden="true">
                  {group}
                </span>
                {TABS.filter((t) => t.group === group).map((t) => (
                  <button
                    key={t.key}
                    role="tab"
                    aria-selected={tab === t.key}
                    className={`workspace-tab ${tab === t.key ? 'active' : ''}`}
                    onClick={() => switchTab(t.key)}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
            ))}
          </nav>
        </header>
        <main className={`workspace-body${tab === 'chat' ? ' is-chat' : ''}`} role="tabpanel">
          {tab === 'chat' && (
            <ChatTab
              key={sessionId}
              session={sessionId}
              workspace={session ? null : workspace}
              needsWorkspace={!session && !workspace}
              onFirstSend={handleFirstSend}
              onOpenTrace={() => switchTab('trace')}
            />
          )}
          {tab === 'trace' && (
            <TraceTab
              key={`trace-${session ?? 'all'}`}
              session={session}
              // 轨迹页里追问：消息交给 store 后立刻切回对话，边看边问不打断浏览
              onAsk={
                session
                  ? (text) => {
                      send(sessionId, text);
                      switchTab('chat');
                    }
                  : undefined
              }
            />
          )}
          {tab === 'metrics' && <Dashboard />}
          {tab === 'quality' && <Quality />}
          {tab === 'cost' && <Cost />}
          {tab === 'secrets' && <SecretsTab />}
          {tab === 'settings' && (
            <SettingsTab
              key={`${params.get('section') ?? ''}:${params.get('domain') ?? ''}:${params.get('field') ?? ''}`}
              initialSection={params.get('section') ?? params.get('domain')}
              initialField={params.get('field')}
            />
          )}
        </main>
      </div>
    </div>
  );
}

export { parseTab };
