/** 会话工作台：左侧会话栏 + 顶部会话标题 + tab 行，指标等作为对话内的 tab。 */
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { SessionSidebar, aggregateSessions, useSessionRuns } from './SessionSidebar';
import { Dashboard } from '../pages/Dashboard';
import { Quality } from '../pages/Quality';
import { Cost } from '../pages/Cost';
import { ChatTab } from './ChatTab';
import { TraceTab } from './TraceTab';

const TABS = [
  { key: 'chat', label: '对话' },
  { key: 'trace', label: '轨迹' },
  { key: 'metrics', label: '指标' },
  { key: 'quality', label: '质量' },
  { key: 'cost', label: '成本' },
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
  const [newNonce, setNewNonce] = useState(0);

  const runs = useSessionRuns();
  const sessions = aggregateSessions(runs.data?.items ?? []);
  const current = session ? sessions.find((s) => s.id === session) : undefined;
  const title = session ? (current?.title ?? session) : '新会话';
  const totalRuns = sessions.reduce((sum, s) => sum + s.runCount, 0);
  const failedRuns = sessions.reduce((sum, s) => sum + s.failedCount, 0);

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

  /** 新会话 = 清空当前对话：换 key 让 ChatTab 重挂载，即使已在「全部会话」也生效。 */
  function startNewSession() {
    setNewNonce((n) => n + 1);
    switchSession(null);
  }

  return (
    <div className="workspace">
      <SessionSidebar selected={session} onSelect={switchSession} onNew={startNewSession} />
      <div className="workspace-main">
        <header className="workspace-header">
          <h1 className="workspace-title">{title}</h1>
          <span className="workspace-chip">
            {totalRuns} 次运行{failedRuns ? ` · ${failedRuns} 次失败` : ''}
          </span>
        </header>
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
        <main className={`workspace-body${tab === 'chat' ? ' is-chat' : ''}`} role="tabpanel">
          {tab === 'chat' && (
            <ChatTab key={`${session ?? 'new'}-${newNonce}`} session={session} />
          )}
          {tab === 'trace' && <TraceTab session={session} />}
          {tab === 'metrics' && <Dashboard />}
          {tab === 'quality' && <Quality />}
          {tab === 'cost' && <Cost />}
        </main>
      </div>
    </div>
  );
}

export { parseTab };
