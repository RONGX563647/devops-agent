/** 调用视图的数据层：按 model/tool 聚合，列表与右侧统计面板共用同一份分组。
 *
 * 拆出来是因为「点一级节点看统计」需要和列表拿到完全相同的组——
 * 两边各算一次，失败率与均值迟早会对不上。
 */

import type { TraceStep, TraceTurn } from '../../types';

/** 一次调用：步骤本身 + 它属于第几轮（平铺列表里必须标出来）。 */
export interface CallRow {
  step: TraceStep;
  turnIndex: number;
}

/** 一个调用组：同一模型的若干次调用，或同一工具的若干次调用。 */
export interface CallGroup {
  key: string;
  type: 'model' | 'tool';
  name: string;
  calls: CallRow[];
  total: number;
  failed: number;
  slowest: TraceStep;
}

export type CallSort = 'total' | 'count' | 'fail';

/** 失败原因归类的一行：同一句报错归并计数，附上原始步骤便于跳转。 */
export interface FailureReason {
  reason: string;
  count: number;
  steps: TraceStep[];
}

/** 一条步骤是否算「异常」：工具/模型明确报错，或本身就是异常行。 */
export function isErrorStep(step: TraceStep): boolean {
  return step.kind === 'error' || step.status === 'error';
}

/** 报错原文压成一行：取首个非空行，超长截断，空则给可读占位。 */
export function reasonOf(step: TraceStep): string {
  const raw = [step.result, step.text, step.argsSummary]
    .map((value) => (value ?? '').trim())
    .find((value) => value.length > 0);
  if (!raw) return '（未记录失败原因）';
  const line = raw.split('\n').map((part) => part.trim()).find((part) => part.length > 0) ?? raw;
  return line.length > 120 ? `${line.slice(0, 120)}…` : line;
}

/** 进调用视图的步骤：合成的补位行与其它 kind 都不是真实调用。 */
function isCallStep(step: TraceStep): boolean {
  if (step.synthetic) return false;
  return step.kind === 'tool' || step.kind === 'model';
}

/** 组名：优先工具名，其次标题，最后退回 kind 的中文兜底。 */
function stepName(step: TraceStep): string {
  return step.toolName || step.title || (step.kind === 'model' ? '模型' : '工具');
}

/** 把一次调用并进已有的组：次数、合计、失败数、最慢一个。 */
function accumulate(group: CallGroup, row: CallRow): void {
  group.calls.push(row);
  group.total += row.step.durationMs;
  if (row.step.status === 'error') group.failed += 1;
  if (row.step.durationMs > group.slowest.durationMs) group.slowest = row.step;
}

/** 分组：模型与工具分开聚合，避免「模型调用」盖住真正要看的工具。 */
export function groupCalls(turns: TraceTurn[]): CallGroup[] {
  const groups = new Map<string, CallGroup>();
  for (const turn of turns) {
    for (const step of turn.items) {
      if (!isCallStep(step)) continue;
      const row: CallRow = { step, turnIndex: turn.index };
      const key = `${step.kind}:${stepName(step)}`;
      const group = groups.get(key);
      if (group) {
        accumulate(group, row);
        continue;
      }
      groups.set(key, {
        key,
        type: step.kind as 'model' | 'tool',
        name: stepName(step),
        calls: [row],
        total: step.durationMs,
        failed: step.status === 'error' ? 1 : 0,
        slowest: step,
      });
    }
  }
  return [...groups.values()];
}

/** 排序：合计耗时 / 次数 / 失败率，三者都带次级键，保证顺序稳定。 */
export function sortGroups(groups: CallGroup[], sort: CallSort): CallGroup[] {
  return [...groups].sort((a, b) => {
    if (sort === 'count') return b.calls.length - a.calls.length || b.total - a.total;
    if (sort === 'fail') {
      const rate = (group: CallGroup) => (group.calls.length ? group.failed / group.calls.length : 0);
      return rate(b) - rate(a) || b.total - a.total;
    }
    return b.total - a.total;
  });
}

/** 组内失败原因分类：按原文首行归并，count 降序（同数按原始顺序）。 */
export function failureReasons(group: CallGroup): FailureReason[] {
  const buckets = new Map<string, FailureReason>();
  for (const call of group.calls) {
    if (call.step.status !== 'error' && call.step.kind !== 'error') continue;
    const reason = reasonOf(call.step);
    const bucket = buckets.get(reason);
    if (bucket) {
      bucket.count += 1;
      bucket.steps.push(call.step);
      continue;
    }
    buckets.set(reason, { reason, count: 1, steps: [call.step] });
  }
  return [...buckets.values()].sort((a, b) => b.count - a.count);
}
