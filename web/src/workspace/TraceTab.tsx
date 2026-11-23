/** 轨迹 tab：泳道 + 轮次列表 + 详情检查面板。
 *
 * 三级粒度：顶部泳道看「时间花在哪」，中间列表看「做了什么」，右侧看「具体参数」。
 * 运行记录（后端快照）仍保留为另一个视图，用于回看落库后的历史运行。
 */
import { useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchContext, fetchRuns } from '../api/client';
import { StatusBadge } from '../components/StatusBadge';
import type { Run, TraceStep, TraceTurn } from '../types';
import { RunTrace } from './RunTrace';
import { useSession } from './store';
import { buildLanes, traceStats, turnsFromMessages } from './trace';
import { TraceInspector } from './TraceInspector';
import { TraceLanes } from './TraceLanes';
import { TraceList, type TraceMode } from './TraceList';
import { formatClock, formatDateTime, formatDuration } from './time';

interface Props {
  session: string | null;
  /** 轨迹页内追问：发送后由外层切回对话。 */
  onAsk?: (text: string) => void;
}

const MODE_HINTS: Record<TraceMode, string> = {
  time: '按耗时降序，最慢的排最前',
  turn: '按轮次分组，点标题折叠整轮',
  call: '按工具聚合，看谁被喊得最多',
};

// 轮次视图是默认入口：能一眼看到每轮装了什么上下文、跑了哪些步
const MODES: { key: TraceMode; label: string }[] = [
  { key: 'turn', label: '轮次' },
  { key: 'time', label: '时长' },
  { key: 'call', label: '调用' },
];

function RunTable({
  items,
  showSession,
  onSelect,
}: {
  items: Run[];
  showSession: boolean;
  onSelect: (id: number) => void;
}) {
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>ID</th>
          {showSession && <th>会话</th>}
          <th>域</th><th>状态</th><th>健康分</th><th>耗时</th><th>Token</th><th>开始时间</th>
        </tr>
      </thead>
      <tbody>
        {items.map((run) => (
          <tr key={run.id} className="trace-row" onClick={() => onSelect(run.id)}>
            <td>#{run.id}</td>
            {showSession && <td>{run.session_id}</td>}
            <td>{run.domain}</td>
            <td><StatusBadge status={run.status} /></td>
            <td>{run.health_score ?? '—'}</td>
            <td>{run.duration_ms === null ? '—' : formatDuration(run.duration_ms)}</td>
            <td>{run.total_tokens}</td>
            <td className="muted">{formatDateTime(run.started_at)}</td>
          </tr>
        ))}
        {items.length === 0 && (
          <tr>
            <td colSpan={showSession ? 8 : 7} className="muted">该会话暂无运行记录。</td>
          </tr>
        )}
      </tbody>
    </table>
  );
}

/** 默认选中「全会话最费时的一步」：打开就能看到最值得看的那一条。 */
function slowestStep(turns: TraceTurn[]): { step: TraceStep; turn: TraceTurn } | null {
  let best: { step: TraceStep; turn: TraceTurn } | null = null;
  for (const turn of turns) {
    for (const step of turn.items) {
      if (!best || step.durationMs > best.step.durationMs) best = { step, turn };
    }
  }
  return best;
}

function StatusBar({
  stats,
  running,
}: {
  stats: ReturnType<typeof traceStats>;
  running: boolean;
}) {
  const cache = stats.cacheRate === null ? null : `${Math.round(stats.cacheRate * 100)}%`;
  return (
    <div className="trace-status mono">
      <span>
        {stats.turns} 轮 · {stats.steps} 步 · {stats.tools} 工具
      </span>
      {stats.tokPerSec !== null && <span>{stats.tokPerSec.toFixed(1)} tok/s</span>}
      <span>
        {stats.tokensIn + stats.tokensOut} tok
        {cache && ` · 缓存 ${cache}`}
      </span>
      <span className={stats.errors > 0 ? 'has-error' : ''}>异常 {stats.errors}</span>
      <span className="trace-status-right">
        {running ? '生成中…' : `会话 ${formatDuration(stats.totalMs)}`}
      </span>
    </div>
  );
}

function SessionTrace({
  session,
  runs,
  runsLoading,
  runsError,
  onOpenRun,
  onAsk,
}: {
  session: string;
  runs: Run[];
  runsLoading: boolean;
  runsError: boolean;
  onOpenRun: (id: number) => void;
  onAsk?: (text: string) => void;
}) {
  const view = useSession(session);
  const [mode, setMode] = useState<'trace' | 'runs'>('trace');
  const [layout, setLayout] = useState<TraceMode>('turn');
  const [query, setQuery] = useState('');
  const [ask, setAsk] = useState('');
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);

  // 第 0 步要展示的系统提示词与工具清单来自后端装配区，读不到就显示为空而不编造
  const context = useQuery({
    queryKey: ['agent-context'],
    queryFn: fetchContext,
    staleTime: 5 * 60_000,
    retry: false,
  });
  const turns = useMemo(() => turnsFromMessages(view.messages), [view.messages]);
  const lanes = useMemo(() => buildLanes(turns), [turns]);
  const stats = useMemo(() => traceStats(turns, lanes), [turns, lanes]);
  const initial = useMemo(() => slowestStep(turns), [turns]);

  const activeStepId = selectedStepId ?? initial?.step.id ?? null;
  const flat = useMemo(() => turns.flatMap((turn) => turn.items), [turns]);
  const activeStep = flat.find((step) => step.id === activeStepId) ?? null;
  const activeTurn =
    (activeStep && turns.find((turn) => turn.items.some((step) => step.id === activeStep.id))) ||
    turns.find((turn) => turn.id === selectedTurnId) ||
    null;

  /** j/k 或上下键在步骤间移动；/ 聚焦搜索；Esc 清掉手选。 */
  function handleKey(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.target instanceof HTMLInputElement) return;
    const index = flat.findIndex((step) => step.id === activeStepId);
    if (event.key === 'j' || event.key === 'ArrowDown') {
      event.preventDefault();
      setSelectedStepId(flat[Math.min(flat.length - 1, index + 1)]?.id ?? null);
    } else if (event.key === 'k' || event.key === 'ArrowUp') {
      event.preventDefault();
      setSelectedStepId(flat[Math.max(0, index - 1)]?.id ?? null);
    } else if (event.key === '/') {
      event.preventDefault();
      searchRef.current?.focus();
    } else if (event.key === 'Escape') {
      setSelectedStepId(null);
    }
  }

  function submitAsk() {
    const text = ask.trim();
    if (!text || !onAsk) return;
    setAsk('');
    onAsk(text);
  }

  return (
    <div className="ws-trace" onKeyDown={handleKey}>
      <div className="ws-trace-head">
        <div>
          <h2 className="ws-trace-title">会话轨迹</h2>
          <p className="ws-trace-sub mono">
            {turns.length} 轮 · {stats.tools} 次工具 · {formatDuration(stats.totalMs)}
            {view.running ? ' · 生成中' : ''}
          </p>
        </div>
        <div className="ws-trace-switch">
          <button className={mode === 'trace' ? 'active' : ''} onClick={() => setMode('trace')}>
            会话轨迹
          </button>
          <button className={mode === 'runs' ? 'active' : ''} onClick={() => setMode('runs')}>
            运行记录 {runs.length}
          </button>
        </div>
      </div>

      {mode === 'runs' ? (
        <div className="ws-runs">
          {runsError && <p className="query-error">运行记录加载失败。</p>}
          {runsLoading && runs.length === 0 ? (
            <p className="query-loading">正在加载运行记录…</p>
          ) : (
            <RunTable items={runs} showSession={false} onSelect={onOpenRun} />
          )}
        </div>
      ) : (
        <>
          <div className="trace-toolbar">
            <div className="trace-modes" role="tablist" aria-label="轨迹维度">
              {MODES.map((item) => (
                <button
                  key={item.key}
                  role="tab"
                  aria-selected={layout === item.key}
                  className={layout === item.key ? 'active' : ''}
                  title={MODE_HINTS[item.key]}
                  onClick={() => setLayout(item.key)}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <input
              ref={searchRef}
              className="session-search trace-search"
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索步骤、工具、参数…（/）"
              aria-label="搜索轨迹"
            />
          </div>

          <TraceLanes
            lanes={lanes}
            activeStepId={activeStepId}
            onSelect={(id) => setSelectedStepId(id)}
          />

          <div className="trace-body" tabIndex={0} role="group" aria-label="轨迹步骤">
            <div className="trace-scroll">
              <TraceList
                turns={turns}
                mode={layout}
                query={query}
                activeStepId={activeStepId}
                activeTurnId={activeTurn?.id ?? null}
                context={context.data ?? null}
                onSelectStep={(step) => setSelectedStepId(step.id)}
                onSelectTurn={(turn) => setSelectedTurnId(turn.id)}
              />
            </div>
            {/* key 随步骤变：换一条时页签回到「概述」，避免误读上一条的参数 */}
            <TraceInspector
              key={activeStep?.id ?? 'none'}
              step={activeStep}
              turn={activeTurn}
              totalMs={stats.totalMs}
            />
          </div>

          <StatusBar stats={stats} running={view.running} />

          {onAsk && (
            <div className="trace-ask">
              <input
                value={ask}
                onChange={(e) => setAsk(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    submitAsk();
                  }
                }}
                placeholder="继续追问，回车发送并回到对话…"
                aria-label="继续追问"
              />
              <button type="button" onClick={submitAsk} disabled={!ask.trim()}>
                发送
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function TraceTab({ session, onAsk }: Props) {
  const [selected, setSelected] = useState<number | null>(null);
  const runs = useQuery({
    // 一次取 200 条，前后端都不再做二次截断
    queryKey: ['runs', 'trace', session],
    queryFn: () => fetchRuns({ limit: 200 }),
    refetchInterval: 5000,
  });
  const all = runs.data?.items ?? [];
  const items = session === null ? all : all.filter((run) => run.session_id === session);

  if (selected !== null) {
    return <RunTrace runId={selected} onClose={() => setSelected(null)} />;
  }

  if (session === null) {
    return (
      <div className="page">
        <p className="subtitle">全部会话 · 点击运行查看轨迹与健康分</p>
        {runs.isError && <p className="query-error">运行记录加载失败。</p>}
        {runs.isLoading && all.length === 0 ? (
          <p className="query-loading">正在加载运行记录…</p>
        ) : (
          <RunTable items={items} showSession onSelect={setSelected} />
        )}
      </div>
    );
  }

  return (
    <SessionTrace
      session={session}
      runs={items}
      runsLoading={runs.isLoading}
      runsError={runs.isError}
      onOpenRun={setSelected}
      onAsk={onAsk}
    />
  );
}

export { formatClock };
