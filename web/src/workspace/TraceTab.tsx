/** 轨迹 tab：由会话消息派生的轮次轨迹（时间轴 + 轮列表 + 详情），并保留后端运行入口。 */
import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchRuns } from '../api/client';
import { StatusBadge } from '../components/StatusBadge';
import type { Run, TraceStep, TraceTurn } from '../types';
import { RunTrace } from './RunTrace';
import { useSession } from './store';
import { turnsFromMessages } from './trace';
import { formatClock, formatDateTime, formatDuration } from './time';

interface Props {
  session: string | null;
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

const STEP_TONE: Record<TraceStep['kind'], string> = {
  text: 'text',
  tool: 'tool',
  error: 'error',
};

/** 单步详情：工具分入参与返回，文本与异常直接贴原文。 */
function StepDetail({ step }: { step: TraceStep }) {
  return (
    <div className="ws-step-detail">
      <div className="ws-step-detail-head">
        <span className={`ws-kind ${STEP_TONE[step.kind]}`}>{step.kind === 'tool' ? '工具' : step.kind === 'error' ? '异常' : '输出'}</span>
        <strong>{step.title}</strong>
        <span className="muted mono">{formatDuration(step.durationMs)}</span>
        {step.status && <span className={`mono ws-status ${step.status}`}>{step.status}</span>}
      </div>
      {step.kind === 'tool' ? (
        <>
          <div className="ws-block">
            <span className="ws-block-label">入参</span>
            <pre>{step.args || '（无）'}</pre>
          </div>
          <div className="ws-block">
            <span className="ws-block-label">返回</span>
            <pre>{step.result || '（暂无返回）'}</pre>
          </div>
        </>
      ) : (
        <pre className="ws-step-text">{step.text || '（空）'}</pre>
      )}
    </div>
  );
}

function SessionTrace({
  session,
  runs,
  runsLoading,
  runsError,
  onOpenRun,
}: {
  session: string;
  runs: Run[];
  runsLoading: boolean;
  runsError: boolean;
  onOpenRun: (id: number) => void;
}) {
  const view = useSession(session);
  const [mode, setMode] = useState<'trace' | 'runs'>('trace');
  const [query, setQuery] = useState('');
  const [selectedTurnId, setSelectedTurnId] = useState('');
  const [selectedStepId, setSelectedStepId] = useState('');

  const turns = useMemo(() => turnsFromMessages(view.messages), [view.messages]);
  const totalMs = turns.reduce((sum, turn) => sum + Math.max(0, turn.endedAt - turn.startedAt), 0);
  const toolCount = turns.reduce((sum, turn) => sum + turn.tools, 0);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return turns;
    return turns.filter((turn) => {
      const haystack = [
        turn.userText,
        ...turn.items.flatMap((item) => [item.title, item.text, item.args ?? '', item.result ?? '']),
      ]
        .join(' ')
        .toLowerCase();
      return haystack.includes(q);
    });
  }, [turns, query]);

  const activeTurn: TraceTurn | null =
    filtered.find((turn) => turn.id === selectedTurnId) ?? filtered[filtered.length - 1] ?? null;
  const activeStep = activeTurn?.items.find((item) => item.id === selectedStepId) ?? null;

  function selectTurn(turn: TraceTurn) {
    setSelectedTurnId(turn.id);
    setSelectedStepId('');
  }

  return (
    <div className="ws-trace">
      <div className="ws-trace-head">
        <div>
          <h2 className="ws-trace-title">会话轨迹</h2>
          <p className="ws-trace-sub mono">
            {turns.length} 轮 · {toolCount} 次工具 · {formatDuration(totalMs)}
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
          <div className="ws-trace-strip" aria-label="轮次耗时分布">
            {filtered.map((turn) => {
              const ms = Math.max(1, turn.endedAt - turn.startedAt);
              return (
                <button
                  key={turn.id}
                  type="button"
                  className={`ws-seg${turn === activeTurn ? ' active' : ''}${turn.failed ? ' failed' : ''}`}
                  style={{ flexGrow: ms }}
                  title={`第 ${turn.index} 轮 · ${formatDuration(ms)}`}
                  onClick={() => selectTurn(turn)}
                >
                  <span>第 {turn.index} 轮</span>
                </button>
              );
            })}
            {filtered.length === 0 && <span className="ws-strip-empty">暂无轮次</span>}
          </div>

          <div className="ws-trace-body">
            <aside className="ws-trace-list">
              <input
                className="session-search"
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索问题或工具"
                aria-label="搜索轨迹"
              />
              {filtered.map((turn) => (
                <button
                  key={turn.id}
                  type="button"
                  className={`ws-turn${turn === activeTurn ? ' active' : ''}${turn.failed ? ' failed' : ''}`}
                  onClick={() => selectTurn(turn)}
                >
                  <span className="ws-turn-index mono">#{turn.index}</span>
                  <span className="ws-turn-main">
                    <span className="ws-turn-question">{turn.question || '（空问题）'}</span>
                    <span className="ws-turn-meta mono">
                      {formatClock(turn.at)} · {turn.tools} 工具 · {formatDuration(turn.endedAt - turn.startedAt)}
                    </span>
                  </span>
                </button>
              ))}
              {filtered.length === 0 && (
                <p className="session-empty">
                  {turns.length === 0
                    ? '该会话还没有可展示的轨迹，去对话里发一条消息。'
                    : `没有匹配「${query}」的轮次。`}
                </p>
              )}
            </aside>

            <section className="ws-trace-detail">
              {!activeTurn && (
                <div className="ws-detail-empty">
                  <strong>轨迹由对话消息派生</strong>
                  <p>每一轮包含模型输出与工具调用；点左侧轮次或上方时间轴定位。</p>
                </div>
              )}
              {activeTurn && (
                <>
                  <div className="ws-detail-head">
                    <div>
                      <span className="muted mono">第 {activeTurn.index} 轮 · {formatClock(activeTurn.at)}</span>
                      <p className="ws-detail-question">{activeTurn.userText || '（空问题）'}</p>
                    </div>
                    <span className={`mono ws-status ${activeTurn.failed ? 'error' : 'success'}`}>
                      {activeTurn.failed ? '有异常' : '正常'}
                    </span>
                  </div>
                  <div className="ws-detail-steps">
                    {activeTurn.items.map((step) => (
                      <button
                        key={step.id}
                        type="button"
                        className={`ws-step-item${step === activeStep ? ' active' : ''}`}
                        onClick={() => setSelectedStepId(step.id)}
                      >
                        <span className={`ws-kind ${STEP_TONE[step.kind]}`}>
                          {step.kind === 'tool' ? '工具' : step.kind === 'error' ? '异常' : '输出'}
                        </span>
                        <span className="ws-step-title">{step.title}</span>
                        <span className="ws-step-meta mono">{formatDuration(step.durationMs)}</span>
                      </button>
                    ))}
                    {activeTurn.items.length === 0 && (
                      <p className="session-empty">这一轮还没有过程步骤。</p>
                    )}
                  </div>
                  {activeStep ? (
                    <StepDetail step={activeStep} />
                  ) : (
                    <div className="ws-detail-empty">
                      <p>点一个步骤查看入参、返回与耗时。</p>
                    </div>
                  )}
                </>
              )}
            </section>
          </div>
          {turns.length > 0 && runs.length > 0 && (
            <p className="ws-trace-note muted">
              仅展示最近加载的消息与运行；更多历史请切换「运行记录」进入后端快照。
            </p>
          )}
        </>
      )}
    </div>
  );
}

export function TraceTab({ session }: Props) {
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
    />
  );
}
