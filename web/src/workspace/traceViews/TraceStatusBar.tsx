/** 会话轨迹底部状态栏：轮次数、步骤数、token、异常与两个耗时口径。 */

import type { TraceStats } from '../../types';
import { formatDuration } from '../time';

export function TraceStatusBar({
  stats,
  running,
}: {
  stats: TraceStats;
  running: boolean;
}) {
  const cache = stats.cacheRate === null ? null : `${Math.round(stats.cacheRate * 100)}%`;
  // 首枪必然冷读：单列出来，免得把「新轮次的第一枪」当成缓存失效
  const cold =
    stats.firstCallRate !== null &&
    stats.cacheRate !== null &&
    stats.firstCallRate < stats.cacheRate - 0.05
      ? `${Math.round(stats.firstCallRate * 100)}%`
      : null;
  return (
    <div className="trace-status mono">
      <span>
        {stats.turns} 轮 · {stats.steps} 步 · {stats.tools} 工具 · {stats.modelCalls} 模型
      </span>
      {stats.tokPerSec !== null && <span>{stats.tokPerSec.toFixed(1)} tok/s</span>}
      <span>
        {stats.tokensIn + stats.tokensOut} tok
        {cache && ` · 缓存 ${cache}`}
        {cold && <span className="trace-status-cold"> · 首枪 {cold}</span>}
      </span>
      <span className={stats.errors > 0 ? 'has-error' : ''}>异常 {stats.errors}</span>
      <span className="trace-status-right" title="实测 = 所有步骤占用区间的并集；墙钟 = 首轮问题到末轮结束">
        {running ? '生成中…' : `实测 ${formatDuration(stats.measuredMs)} · 墙钟 ${formatDuration(stats.wallMs)}`}
      </span>
    </div>
  );
}
