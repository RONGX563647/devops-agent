/** 轨迹步骤列表：轮次为分隔节点，步骤一行一条，异常与 diff 就地展开。
 *
 * 四种排布共用同一份 TraceStep 与同一份计时：
 * - 轮次：按轮分组，折叠/展开只到「一级节点」（轮次与步骤行），步骤详情默认收起；
 * - 时长：全局降序，估算值沉底，行尾给占比条与帕累托合计；
 * - 调用：按 model/tool 聚合，点一行就地展开每一次调用，右侧同步给这组统计；
 * - 记忆：检索 / 裁剪 / 压缩 / 写入单独成一条轨迹。
 *
 * 视图组件按职责拆到 ./traceViews/ 下，本文件保留容器与原有公开导出。
 */
import { useState } from 'react';
import type { AgentContext } from '../../api/client';
import type { TraceStep, TraceTurn } from '../../types';
import type { CallGroup } from '../traceData/calls';
import { durationRows, topShare } from '../traceData/stats';
import { CallRows } from './CallRows';
import { ContextRow } from './ContextRow';
import { matchesStep } from './Highlight';
import { MemoryRows } from './MemoryRows';
import type { TraceMode } from './modes';
import { StepRow } from './StepRow';
import { TurnHeader } from './TurnHeader';

export { Composition, fmtTok } from './Composition';
export { matchesStep } from './Highlight';
export type { TraceMode } from './modes';

interface Props {
  turns: TraceTurn[];
  mode: TraceMode;
  query: string;
  activeStepId: string | null;
  activeTurnId: string | null;
  /** 「调用」视图里右侧正在展示统计的那一组。 */
  activeGroupKey?: string | null;
  context: AgentContext | null;
  /** 轮次折叠态（上层已算好默认值与持久化）：true = 收起。 */
  collapsed: Record<string, boolean>;
  onToggleTurn: (turnId: string) => void;
  onSelectStep: (step: TraceStep) => void;
  onSelectTurn: (turn: TraceTurn) => void;
  onSelectGroup?: (group: CallGroup) => void;
}

const CAPTIONS: Record<TraceMode, string> = {
  turn: '按对话轮次分组：点轮次标题折叠整轮，点任一步骤行展开原文',
  time: '按耗时降序（模型、思考、工具、解析全都在内）：估算值沉底，最慢的排最前',
  call: '按调用聚合：模型与工具分开，点一级节点看统计，展开再看每一次调用',
  memory: '检索 / 裁剪 / 压缩 / 写入各一行：记忆动作决定了模型这一轮看见什么',
};

export function TraceList({
  turns,
  mode,
  query,
  activeStepId,
  activeTurnId,
  activeGroupKey = null,
  context,
  collapsed,
  onToggleTurn,
  onSelectStep,
  onSelectTurn,
  onSelectGroup,
}: Props) {
  // 步骤详情默认收起：轨迹先给「做了哪些步」，原文要点开才看，长会话才不会一屏塞满参数
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const isOpen = (id: string) => expanded[id] ?? false;
  const toggle = (id: string) =>
    setExpanded((prev) => ({ ...prev, [id]: !(prev[id] ?? false) }));

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
          activeGroupKey={activeGroupKey}
          onSelect={onSelectStep}
          onSelectGroup={onSelectGroup ?? (() => undefined)}
        />
      </>
    );
  }

  if (mode === 'memory') {
    return (
      <>
        <p className="trace-caption">{CAPTIONS.memory}</p>
        <MemoryRows
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

  return (
    <div className="trace-list">
      <p className="trace-caption">{CAPTIONS.turn}</p>
      {turns.map((turn) => {
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
