/** 轨迹步骤列表：轮次为分隔节点，步骤一行一条，异常与 diff 就地展开。
 *
 * 三种排布共用同一份 TraceStep 与同一份计时：
 * - 时长：全局降序，估算值沉底，行尾给占比条与帕累托合计；
 * - 轮次：按轮分组，可折叠（折叠态由上层持有，便于「全部折叠/展开」与持久化）；
 * - 调用：按 model/tool 聚合，点一行就地展开每一次调用。
 *
 * 视图组件按职责拆到 ./traceViews/ 下，本文件保留容器与原有公开导出。
 */
import { useState } from 'react';
import type { AgentContext } from '../../api/client';
import type { TraceStep, TraceTurn } from '../../types';
import { durationRows, topShare } from '../traceData/stats';
import { CallRows } from './CallRows';
import { ContextRow } from './ContextRow';
import { matchesStep } from './Highlight';
import { StepRow } from './StepRow';
import { TurnHeader } from './TurnHeader';

export { Composition, fmtTok } from './Composition';
export { matchesStep } from './Highlight';

export type TraceMode = 'time' | 'turn' | 'call';

interface Props {
  turns: TraceTurn[];
  mode: TraceMode;
  query: string;
  activeStepId: string | null;
  activeTurnId: string | null;
  context: AgentContext | null;
  /** 轮次折叠态（上层已算好默认值与持久化）：true = 收起。 */
  collapsed: Record<string, boolean>;
  /** 只渲染最近 N 轮；null = 全部轮次。 */
  visibleTurns: number | null;
  onToggleTurn: (turnId: string) => void;
  /** 把「只看最近 N 轮」放开成全部。 */
  onLoadMore: () => void;
  onSelectStep: (step: TraceStep) => void;
  onSelectTurn: (turn: TraceTurn) => void;
}

const CAPTIONS: Record<TraceMode, string> = {
  time: '按耗时降序（模型、思考、工具、解析全都在内）：估算值沉底，最慢的排最前',
  turn: '按对话轮次分组：点轮次标题折叠整轮，点任一步骤行收起或展开原文',
  call: '按调用聚合：模型与工具分开，点一行展开每一次调用',
};

export function TraceList({
  turns,
  mode,
  query,
  activeStepId,
  activeTurnId,
  context,
  collapsed,
  visibleTurns,
  onToggleTurn,
  onLoadMore,
  onSelectStep,
  onSelectTurn,
}: Props) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  /** 默认展开：进来就能读到参数与返回，点整行收起。 */
  const isOpen = (id: string) => expanded[id] ?? true;
  const toggle = (id: string) =>
    setExpanded((prev) => ({ ...prev, [id]: !(prev[id] ?? true) }));

  if (turns.length === 0) {
    return <p className="trace-empty">该会话还没有可展示的轨迹，去对话里发一条消息。</p>;
  }

  if (mode === 'call') {
    return (
      <>
        <p className="trace-caption">{CAPTIONS.call}</p>
        <CallRows
          turns={turns}
          query={query}
          activeStepId={activeStepId}
          onSelect={onSelectStep}
        />
      </>
    );
  }

  // 时长视图：全局按耗时降序，回答「最慢的是哪一步」，不再按轮分组
  if (mode === 'time') {
    const rows = durationRows(turns, (step) => matchesStep(step, query));
    const measured = rows.filter((row) => row.step.timing.source !== 'estimated');
    return (
      <div className="trace-list">
        <p className="trace-caption">{CAPTIONS.time}</p>
        {rows.length === 0 ? (
          <p className="trace-empty">没有匹配「{query}」的步骤。</p>
        ) : (
          <>
            <div className="trace-steps">
              {rows.map(({ step, turnIndex }) => (
                <StepRow
                  key={step.id}
                  step={step}
                  turnIndex={turnIndex}
                  query={query}
                  active={step.id === activeStepId}
                  expanded={isOpen(step.id)}
                  showShare
                  onSelect={onSelectStep}
                  onToggle={() => toggle(step.id)}
                />
              ))}
            </div>
            <p className="trace-foot mono">
              共 {measured.length} 步计入实测 · 前 {Math.min(3, measured.length)} 步占{' '}
              {Math.round(topShare(rows, 3) * 100)}%
              {rows.length > measured.length && ` · ${rows.length - measured.length} 条估算未计入`}
            </p>
          </>
        )}
      </div>
    );
  }

  const searching = query.trim().length > 0;
  // 搜索时必须放开截断：否则命中的那一轮可能正好在「更早」里没渲染
  const shown = searching || visibleTurns === null ? turns : turns.slice(-visibleTurns);
  const hidden = turns.length - shown.length;

  return (
    <div className="trace-list">
      <p className="trace-caption">{CAPTIONS.turn}</p>
      {hidden > 0 && (
        <button type="button" className="trace-more" onClick={onLoadMore}>
          加载更早的 {hidden} 轮
        </button>
      )}
      {shown.map((turn) => {
        const isCollapsed = collapsed[turn.id] ?? false;
        const items = turn.items.filter((step) => matchesStep(step, query));
        return (
          <section className="trace-turn" key={turn.id} data-turn={turn.index}>
            <TurnHeader
              turn={turn}
              collapsed={isCollapsed}
              active={turn.id === activeTurnId}
              onToggle={() => onToggleTurn(turn.id)}
              onSelect={() => onSelectTurn(turn)}
            />
            {!isCollapsed && (
              <div className="trace-steps">
                <ContextRow turn={turn} context={context} />
                {items.length === 0 ? (
                  <p className="trace-empty">
                    {turn.items.length === 0 ? '这一轮还没有过程步骤。' : `没有匹配「${query}」的步骤。`}
                  </p>
                ) : (
                  items.map((step) => (
                    <StepRow
                      key={step.id}
                      step={step}
                      query={query}
                      active={step.id === activeStepId}
                      expanded={isOpen(step.id)}
                      showShare={false}
                      onSelect={onSelectStep}
                      onToggle={() => toggle(step.id)}
                    />
                  ))
                )}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}
