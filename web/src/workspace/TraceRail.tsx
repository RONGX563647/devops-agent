/** 对话右侧刻度条：每轮一个刻度，定位并显示该轮概况。 */
import { useState } from 'react';
import type { TraceTurn } from '../types';
import { formatClock, formatDuration } from './time';

interface Props {
  turns: TraceTurn[];
  activeId: string;
  streaming: boolean;
  onLocate: (turn: TraceTurn) => void;
  onOpenTrace?: () => void;
}

/** 刻度条：hover 显示该轮问题与耗时，点击滚动到对应轮次。 */
export function TraceRail({ turns, activeId, streaming, onLocate, onOpenTrace }: Props) {
  const [hover, setHover] = useState<string | null>(null);
  const hovered = turns.find((turn) => turn.id === hover);

  return (
    <div className="ws-rail" aria-label="对话轮次定位">
      <div className="ws-rail-track">
        {turns.length === 0 && <span className="ws-rail-empty" aria-hidden="true" />}
        {turns.map((turn) => {
          const active = turn.id === activeId;
          const running = streaming && turn === turns[turns.length - 1];
          return (
            <button
              key={turn.id}
              type="button"
              className={`ws-tick${active ? ' active' : ''}${turn.failed ? ' failed' : ''}${running ? ' live' : ''}`}
              aria-label={`第 ${turn.index} 轮：${turn.question || '空问题'}`}
              aria-current={active ? 'true' : undefined}
              onMouseEnter={() => setHover(turn.id)}
              onMouseLeave={() => setHover(null)}
              onFocus={() => setHover(turn.id)}
              onBlur={() => setHover(null)}
              onClick={() => onLocate(turn)}
            >
              <i aria-hidden="true" />
            </button>
          );
        })}
      </div>
      {hovered && (
        <div className="ws-rail-card" role="tooltip">
          <strong>
            第 {hovered.index} 轮
            <span className="ws-rail-time mono">{formatClock(hovered.at)}</span>
          </strong>
          <p>{hovered.question || '（空问题）'}</p>
          <span className="ws-rail-meta mono">
            {hovered.tools} 次工具调用 ·{' '}
            {/* 优先实测（span 并集）；老数据没有 span 时退回起止差 */}
            {formatDuration(hovered.measuredMs || hovered.endedAt - hovered.startedAt)}
          </span>
        </div>
      )}
      {onOpenTrace && (
        <button type="button" className="ws-rail-open" onClick={onOpenTrace}>
          轨迹
        </button>
      )}
    </div>
  );
}
