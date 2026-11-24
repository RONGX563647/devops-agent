/** 轨迹聚合指标：实测/墙钟、吞吐与缓存命中率、时长榜。 */

import type { TraceLanes, TraceStats, TraceStep, TraceTurn } from '../../types';
import { unionLength } from './format';

/** 实测耗时的区间来源：模型/工具用 span 并集，输入解析用阶段自报区间。 */
function workIntervals(turns: TraceTurn[], lanes: TraceLanes): Array<[number, number]> {
  const intervals = [...lanes.model, ...lanes.tool].map(
    (span) => [span.start, span.end] as [number, number],
  );
  for (const turn of turns) {
    for (const item of turn.items) {
      if (item.kind === 'parse' && !item.synthetic && item.durationMs > 0) {
        intervals.push([item.at, item.at + item.durationMs]);
      }
    }
  }
  return intervals;
}

/** 两个数相除给比例；分子为 0 时返回 null（0% 与「没数据」必须可区分）。 */
function ratio(value: number, total: number): number | null {
  if (total <= 0 || value <= 0) return null;
  return value / total;
}

/** 允许分子为 0 的比例：首枪命中 0 是必然成本，不能当成「没数据」。 */
function shareOfTotal(value: number, total: number): number | null {
  return total > 0 ? value / total : null;
}

/** 吞吐：输出 token ÷ 模型占用毫秒，没有模型耗时就给 null。 */
function throughput(modelMs: number, tokensOut: number): number | null {
  if (modelMs <= 0 || tokensOut <= 0) return null;
  return (tokensOut / modelMs) * 1000;
}

/** 顶部状态栏用的聚合指标：实测与墙钟分开给，差值才是用户空闲。 */
export function traceStats(turns: TraceTurn[], lanes: TraceLanes): TraceStats {
  const steps = turns.reduce((sum, turn) => sum + turn.items.length, 0);
  const tools = turns.reduce((sum, turn) => sum + turn.tools, 0);
  const modelCalls = turns.reduce(
    (sum, turn) => sum + turn.items.filter((item) => item.kind === 'model').length,
    0,
  );
  const errors = turns.reduce((sum, turn) => sum + turn.errorCount, 0);
  const tokensIn = turns.reduce((sum, turn) => sum + turn.tokens.in, 0);
  const tokensOut = turns.reduce((sum, turn) => sum + turn.tokens.out, 0);
  const cacheTokens = turns.reduce((sum, turn) => sum + turn.tokens.cache, 0);
  const first = turns[0]?.startedAt ?? 0;
  const last = turns.reduce((max, turn) => Math.max(max, turn.endedAt), first);
  const modelMs = lanes.model.reduce((sum, span) => sum + (span.end - span.start), 0);
  const cold = firstCalls(turns);
  // 实测 = 所有真实占用区间的并集；墙钟 = 起止差，差值即用户空闲
  const measuredMs = unionLength(workIntervals(turns, lanes));
  return {
    totalMs: measuredMs,
    measuredMs,
    wallMs: Math.max(0, last - first),
    turns: turns.length,
    steps,
    tools,
    modelCalls,
    errors,
    tokensIn,
    tokensOut,
    cacheTokens,
    tokPerSec: throughput(modelMs, tokensOut),
    cacheRate: ratio(cacheTokens, tokensIn),
    firstCallRate: cold.seen === 0 ? null : shareOfTotal(cold.cache, cold.in),
  };
}

/** 每轮第一次模型调用的 (命中, 输入, 有几轮真的带缓存字段)。
 *
 * 首枪命中为 0 是**必然**的（缓存还没建立），必须原样给 0 而不是 null；
 * 只有模型压根不回传缓存字段时才返回 null——两种情况含义完全不同。
 */
function firstCalls(turns: TraceTurn[]): { in: number; cache: number; seen: number } {
  let inTok = 0;
  let cacheTok = 0;
  let seen = 0;
  for (const turn of turns) {
    if (!turn.firstCall) continue;
    inTok += turn.firstCall.in;
    cacheTok += turn.firstCall.cache ?? 0;
    if (turn.firstCall.cache !== undefined) seen += 1;
  }
  return { in: inTok, cache: cacheTok, seen };
}

/** 时长榜的一行：步骤 + 它属于第几轮（平铺表格里必须标出来）。 */
export interface TraceRow {
  step: TraceStep;
  turnIndex: number;
}

/** 时长榜排序：估算值沉底，其余按耗时降序。
 *
 * 估算值是「下一步时间 − 本步时间」插出来的，把它排进 Top 3 会把真正慢的步骤挤掉。
 */
export function byDurationDesc(a: { step: TraceStep }, b: { step: TraceStep }): number {
  const estimated = (row: { step: TraceStep }) => (row.step.timing.source === 'estimated' ? 1 : 0);
  if (estimated(a) !== estimated(b)) return estimated(a) - estimated(b);
  return b.step.durationMs - a.step.durationMs;
}

/** 时长视图的行集合：纳入模型、思考、工具、解析、异常；正文行不参与排名。 */
export function durationRows(
  turns: TraceTurn[],
  match: (step: TraceStep) => boolean,
): TraceRow[] {
  return turns
    .flatMap((turn) =>
      turn.items
        .filter((step) => !step.synthetic && match(step))
        .map((step) => ({ step, turnIndex: turn.index })),
    )
    .sort(byDurationDesc);
}

/** p95（nearest-rank）。样本不足 2 个时没有「分位」可言，返回 null 让界面显示 —。 */
export function percentile(values: number[], p: number): number | null {
  if (values.length < 2) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(p * sorted.length) - 1));
  return sorted[index];
}

/** 前 n 步的合计占比：一句话说清「慢得集中还是分散」。 */
export function topShare(rows: TraceRow[], n: number): number {
  return rows
    .filter((row) => row.step.timing.source !== 'estimated')
    .slice(0, n)
    .reduce((sum, row) => sum + row.step.share, 0);
}
