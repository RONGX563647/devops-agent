/** 会话工作台：左侧会话栏 + 单行顶栏（会话标题、运行统计、视图 tab）。 */
import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { SessionSidebar, useSessionList } from './SessionSidebar';
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

const TABS = [
  { key: 'chat', label: '对话' },
  { key: 'trace', label: '轨迹' },
  { key: 'metrics', label: '指标' },
  { key: 'quality', label: '质量' },
  { key: 'cost', label: '成本' },
  { key: 'secrets', label: '密钥' },
  { key: 'settings', label: '设置' },
] as const;

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
  const sessionId = session ?? draftId;

  const sessions = useSessionList();
  const current = session ? sessions.find((item) => item.id === session) : undefined;
  const title = session ? (current?.title ?? session) : '新会话';
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
  }

  /** 新会话 = 换一个草稿 id 并清空选中；即使当前已经在「新会话」也生效。 */
  function startNewSession() {
    setDraftId(newId('sess'));
    switchSession(null);
  }

  /** 首次发送：把草稿 id 写回 URL，让刷新/深链能回到同一会话。 */
  function handleFirstSend() {
    if (!session) switchSession(draftId);
  }

  return (
    <div className="workspace">
      <SessionSidebar selected={session} onSelect={switchSession} onNew={startNewSession} />
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
            {TABS.map((t) => (
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
          </nav>
        </header>
        <main className={`workspace-body${tab === 'chat' ? ' is-chat' : ''}`} role="tabpanel">
          {tab === 'chat' && (
            <ChatTab
              key={sessionId}
              session={sessionId}
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
              key={params.get('domain') ?? 'default'}
              initialDomain={params.get('domain')}
            />
          )}
        </main>
      </div>
    </div>
  );
}

export { parseTab };
