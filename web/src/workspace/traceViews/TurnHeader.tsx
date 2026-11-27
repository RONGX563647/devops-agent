/** 轮次标题：折叠、选中与轮级摘要。 */

import type { TraceStep, TraceTurn } from '../../types';
import { formatDuration } from '../time';
import { webAccessLabel } from '../web-access';
import { Highlight } from './Highlight';

/** 折叠态也要能判断「值不值得展开」：步数、工具数、耗时、最慢一步、异常、缓存。 */
function turnSlowest(turn: TraceTurn): TraceStep | null {
  let best: TraceStep | null = null;
  for (const step of turn.items) {
    if (step.synthetic || step.timing.source === 'estimated') continue;
    if (!best || step.durationMs > best.durationMs) best = step;
  }
  return best;
}

export function TurnHeader({
  turn,
  collapsed,
  active,
  onToggle,
  onSelect,
}: {
  turn: TraceTurn;
  collapsed: boolean;
  active: boolean;
  onToggle: () => void;
  onSelect: () => void;
}) {
  // 附件数取用户实际挂上的文件数；历史回放没有消息级附件时回落到解析阶段读到的份数
  const files = turn.attachments ?? turn.parse?.attachments ?? 0;
  // 这一轮模型的缓存命中率：缓存令牌 ÷ 入参令牌，没有字段就不显示
  const cachePct =
    turn.tokens.in > 0 && turn.tokens.cache > 0
      ? Math.round((turn.tokens.cache / turn.tokens.in) * 100)
      : null;
  const slowest = turnSlowest(turn);
  return (
    <div className={`trace-turn-head${active ? ' active' : ''}`}>
      {/* 折叠是独立动作：点标题是「选中这一轮」，点箭头才是收起，两件事不抢一次点击 */}
      <button
        type="button"
        className="trace-caret-btn"
        aria-expanded={!collapsed}
        aria-label={collapsed ? '展开本轮' : '折叠本轮'}
        onClick={onToggle}
      >
        <span className="trace-caret" aria-hidden="true">
          {collapsed ? '›' : '⌄'}
        </span>
      </button>
      <button
        type="button"
        className="trace-turn-toggle"
        aria-pressed={active}
        title="选中这一轮：右侧显示本轮概要、完整上下文与计时"
        onClick={() => {
          onSelect();
          // 收起的轮次点标题就顺手展开，否则右侧有内容、左边看不到步骤
          if (collapsed) onToggle();
        }}
      >
        <span className="trace-turn-no mono">第 {turn.index} 轮</span>
        <span className="trace-turn-question">
          用户：<Highlight text={turn.question || '（空问题）'} query="" />
        </span>
      </button>
      <button type="button" className="trace-turn-meta mono" onClick={onSelect}>
        {turn.steps} 步 · {turn.tools} 工具 · {formatDuration(turn.measuredMs)}
        {slowest && (
          <span className="trace-turn-slowest">
            {' '}· 最慢 {slowest.toolName || slowest.title} {formatDuration(slowest.durationMs)}
          </span>
        )}
        {turn.errorCount > 0 && <span className="trace-turn-fail"> · {turn.errorCount} 异常</span>}
        {cachePct !== null && <span className="trace-turn-cache"> · 缓存 {cachePct}%</span>}
        {files ? <span> · {files} 附件</span> : null}
        {turn.parse?.refs ? <span> · {turn.parse.refs} 引用</span> : null}
        {turn.webAccess ? <span> · 网络：{webAccessLabel(turn.webAccess)}</span> : null}
        {turn.repeatOf && <span className="trace-turn-repeat"> · 同第 {turn.repeatOf} 轮</span>}
      </button>
    </div>
  );
}
