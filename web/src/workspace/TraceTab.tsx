/** 轨迹 tab：泳道 + 轮次列表 + 详情检查面板。
 *
 * 三级粒度：顶部泳道看「时间花在哪」，中间列表看「做了什么」，右侧看「具体参数」。
 * 运行记录（后端快照）仍保留为另一个视图，用于回看落库后的历史运行。
 *
 * 视图控制（折叠态、仅看异常、选中的调用组）放在本层：它是会话级状态，
 * 需要跨 tab 持久化（sessionStorage），列表只负责渲染。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchContext, fetchRuns } from '../api/client';
import { StatusBadge } from '../components/StatusBadge';
import type { Run, TraceStep, TraceTurn } from '../types';
import { RunTrace } from './RunTrace';
import { useSession } from './store/state';
import { buildLanes, turnsFromMessages } from './traceData/build';
import { groupCalls, isErrorStep } from './traceData/calls';
import { traceStats } from './traceData/stats';
import { TraceInspector } from './TraceInspector';
import { TraceLanes } from './TraceLanes';
import { TraceList, type TraceMode } from './traceViews/TraceList';
import { TraceToolbar } from './traceViews/TraceToolbar';
import { TraceStatusBar } from './traceViews/TraceStatusBar';
import { formatClock, formatDateTime, formatDuration } from './time';

interface Props {
  session: string | null;
  /** 轨迹页内追问：发送后由外层切回对话。 */
  onAsk?: (text: string) => void;
}

/** 折叠态存 sessionStorage：切 tab / 刷新回来还是同一屏，不用重新点。 */
function collapseKey(session: string): string {
  return `trace:collapsed:${session}`;
}

function loadCollapsed(session: string): Record<string, boolean> {
  try {
    const raw = sessionStorage.getItem(collapseKey(session));
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed as Record<string, unknown>).map(([key, value]) => [key, Boolean(value)]),
    );
  } catch {
    // 隐私模式/禁用存储：退化为「不记住」，不影响功能
    return {};
  }
}

function saveCollapsed(session: string, value: Record<string, boolean>): void {
  try {
    sessionStorage.setItem(collapseKey(session), JSON.stringify(value));
  } catch {
    // 同上：存不进去就不存
  }
}

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

/** 默认选中「全会话最费时的一步」：打开就能看到最值得看的那一条。
 *
 * 估算值与模型正文不参与评选：插值出来的「最慢」会把真实瓶颈挤掉。
 */
function slowestStep(turns: TraceTurn[]): { step: TraceStep; turn: TraceTurn } | null {
  let best: { step: TraceStep; turn: TraceTurn } | null = null;
  for (const turn of turns) {
    for (const step of turn.items) {
      if (step.synthetic || step.timing.source === 'estimated') continue;
      if (!best || step.durationMs > best.step.durationMs) best = { step, turn };
    }
  }
  return best;
}

/** 仅看异常：把每轮裁到只剩失败步骤，并丢掉没有失败的轮。
 *
 * 裁剪的是「步骤」而不是整轮：一轮里 40 步只挂 1 步，整轮藏掉就再也找不到那条报错。
 */
function onlyFailedSteps(turns: TraceTurn[]): TraceTurn[] {
  return turns
    .map((turn) => ({ ...turn, items: turn.items.filter(isErrorStep) }))
    .filter((turn) => turn.items.length > 0);
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
  const [selectedGroupKey, setSelectedGroupKey] = useState<string | null>(null);
  // 只记「用户显式改过的轮」，没记过的用默认策略（只展开最后一轮）
  const [overrides, setOverrides] = useState<Record<string, boolean>>(() => loadCollapsed(session));
  const [onlyErrors, setOnlyErrors] = useState(false);
  const searchRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    saveCollapsed(session, overrides);
  }, [session, overrides]);

  // 第 0 步要展示的系统提示词与工具清单来自后端装配区，读不到就显示为空而不编造
  const context = useQuery({
    queryKey: ['agent-context', session],
    queryFn: () => fetchContext(session),
    staleTime: 5 * 60_000,
    retry: false,
  });
  const turns = useMemo(() => turnsFromMessages(view.messages), [view.messages]);
  const lanes = useMemo(() => buildLanes(turns), [turns]);
  const stats = useMemo(() => traceStats(turns, lanes), [turns, lanes]);
  const initial = useMemo(() => slowestStep(turns), [turns]);

  // 全部轮次默认展示：截断交给轮次折叠，不再用「显示 5 轮」藏掉历史
  const shownTurns = useMemo(
    () => (onlyErrors ? onlyFailedSteps(turns) : turns),
    [turns, onlyErrors],
  );
  const groups = useMemo(() => groupCalls(shownTurns), [shownTurns]);
  const activeGroup = selectedGroupKey
    ? groups.find((group) => group.key === selectedGroupKey) ?? null
    : null;

  /** 默认只展开最后一轮：长会话全展开等于没有折叠。 */
  const lastIndex = shownTurns[shownTurns.length - 1]?.index;
  const collapsed = useMemo(() => {
    const map: Record<string, boolean> = {};
    for (const turn of shownTurns) {
      map[turn.id] = overrides[turn.id] ?? turn.index !== lastIndex;
    }
    return map;
  }, [shownTurns, overrides, lastIndex]);

  const flat = useMemo(() => turns.flatMap((turn) => turn.items), [turns]);
  // 手选了轮次/调用组就不再回落到「最慢一步」：否则点哪轮，右侧都被默认步骤牵着走
  const activeStepId =
    selectedStepId ??
    (selectedTurnId === null && selectedGroupKey === null ? initial?.step.id ?? null : null);
  const activeStep = flat.find((step) => step.id === activeStepId) ?? null;
  const activeTurn =
    (activeStep
      ? turns.find((turn) => turn.items.some((step) => step.id === activeStep.id))
      : null) ??
    turns.find((turn) => turn.id === selectedTurnId) ??
    (activeGroup ? null : initial?.turn) ??
    null;

  function toggleTurn(id: string) {
    setOverrides((prev) => ({ ...prev, [id]: !(prev[id] ?? collapsed[id] ?? false) }));
  }

  function setAllCollapsed(value: boolean) {
    setOverrides(Object.fromEntries(shownTurns.map((turn) => [turn.id, value])));
  }

  /** 胶囊 toggle 语义：已在「轮次」时再点一次 = 全部折叠，再点 = 全部展开。
   *
   * 展开只展开到「轮次 + 步骤行」这一级，步骤详情（入参/返回原文）仍保持收起，
   * 否则一次全展开等于把整段原文铺满屏幕。
   */
  function toggleAllTurns() {
    const anyExpanded = shownTurns.some((turn) => !collapsed[turn.id]);
    setAllCollapsed(anyExpanded);
  }

  function activate(key: TraceMode) {
    if (key === 'turn' && layout === 'turn') {
      toggleAllTurns();
      return;
    }
    setLayout(key);
    if (key !== 'call') setSelectedGroupKey(null);
  }

  function selectStep(step: TraceStep) {
    setSelectedStepId(step.id);
    setSelectedTurnId(null);
    setSelectedGroupKey(null);
  }

  function selectTurn(turn: TraceTurn) {
    setSelectedTurnId(turn.id);
    setSelectedStepId(null);
    setSelectedGroupKey(null);
  }

  function selectGroup(key: string) {
    setSelectedGroupKey(key);
    setSelectedTurnId(null);
    setSelectedStepId(null);
  }

  /** j/k 或上下键在步骤间移动；/ 聚焦搜索；[ ] 切轮；Enter 折叠当前轮；Esc 清手选。 */
  function handleKey(event: React.KeyboardEvent<HTMLDivElement>) {
    const target = event.target;
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) return;
    const index = flat.findIndex((step) => step.id === activeStepId);
    if (event.key === 'j' || event.key === 'ArrowDown') {
      event.preventDefault();
      setSelectedTurnId(null);
      setSelectedGroupKey(null);
      setSelectedStepId(flat[Math.min(flat.length - 1, index + 1)]?.id ?? null);
    } else if (event.key === 'k' || event.key === 'ArrowUp') {
      event.preventDefault();
      setSelectedTurnId(null);
      setSelectedGroupKey(null);
      setSelectedStepId(flat[Math.max(0, index - 1)]?.id ?? null);
    } else if (event.key === '/') {
      event.preventDefault();
      searchRef.current?.focus();
    } else if (event.key === '[' || event.key === ']') {
      // 切换轮次：清掉步骤手选，否则 activeTurn 永远被选中步骤牵着走
      event.preventDefault();
      const current = Math.max(
        0,
        shownTurns.findIndex((turn) => turn.id === activeTurn?.id),
      );
      const next = event.key === ']'
        ? Math.min(shownTurns.length - 1, current + 1)
        : Math.max(0, current - 1);
      const turn = shownTurns[next];
      if (turn) selectTurn(turn);
    } else if (event.key === 'Enter' && !(target instanceof HTMLButtonElement) && activeTurn) {
      // 按钮上的 Enter 交给按钮自己（否则会既触发点击又折叠一次）
      event.preventDefault();
      toggleTurn(activeTurn.id);
    } else if (event.key === 'Escape') {
      setSelectedStepId(null);
      setSelectedTurnId(null);
      setSelectedGroupKey(null);
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
            {turns.length} 轮 · {stats.modelCalls} 次模型 · {stats.tools} 次工具 · 实测{' '}
            {formatDuration(stats.measuredMs)} · 墙钟 {formatDuration(stats.wallMs)}
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
          <TraceToolbar
            layout={layout}
            onSelectMode={activate}
            onlyErrors={onlyErrors}
            onToggleOnlyErrors={() => setOnlyErrors((value) => !value)}
            query={query}
            onQueryChange={setQuery}
            searchRef={searchRef}
          />

          <TraceLanes
            lanes={lanes}
            activeStepId={activeStepId}
            onSelect={(id) => {
              const step = flat.find((item) => item.id === id);
              if (step) selectStep(step);
            }}
          />

          <div className="trace-body" tabIndex={0} role="group" aria-label="轨迹步骤">
            <div className="trace-scroll">
              <TraceList
                turns={shownTurns}
                mode={layout}
                query={query}
                activeStepId={activeStepId}
                activeTurnId={activeTurn?.id ?? null}
                activeGroupKey={selectedGroupKey}
                context={context.data ?? null}
                collapsed={collapsed}
                onToggleTurn={toggleTurn}
                onSelectStep={selectStep}
                onSelectTurn={selectTurn}
                onSelectGroup={(group) => selectGroup(group.key)}
              />
            </div>
            {/* key 随步骤/调用组变：换一条时页签回到「概述」，避免误读上一条的参数；
                只切轮次时不重建，停在「上下文」就能连着看几轮的装配 */}
            <TraceInspector
              key={activeGroup ? `group:${activeGroup.key}` : activeStep?.id ?? 'turn'}
              step={activeStep}
              turn={activeTurn}
              group={activeGroup}
              totalMs={stats.measuredMs}
              profile={context.data?.profile}
              context={context.data ?? null}
              turns={turns}
              onSelectStep={selectStep}
            />
          </div>

          <TraceStatusBar stats={stats} running={view.running} />

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
      // 换会话就重置折叠/搜索等视图状态，避免把上一个会话的折叠态带过来
      key={session}
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
