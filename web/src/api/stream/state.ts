/** 流式状态与步骤折叠的公共原子操作。 */

import type {
  AwaitingState,
  CompressionRecord,
  ProcessEntry,
  StreamEvent,
} from '../../types';

export interface StreamState {
  running: boolean;
  text: string;
  steps: ProcessEntry[];
  inputTokens: number;
  outputTokens: number;
  /** 命中前缀缓存的输入 token，用于底部遥测栏的缓存命中率。 */
  cacheInputTokens: number;
  /**
   * 当前上下文占用（token）：取最近一次模型调用回传的 prompt 大小。
   * 这是「窗口里现在有多少」的实测值，不是本轮累计消耗 —— 两者差一个数量级。
   */
  contextTokens: number | null;
  /** 触发压缩的阈值（默认 0.8 × 窗口 = 102400），由 CONTEXT_BUDGET 下发。 */
  contextThreshold: number | null;
  /** 模型窗口大小，用于显示「103k / 128k 窗口」。 */
  contextSize: number | null;
  /** 已发生的上下文压缩记录，按时间正序；只保留真正压过的。 */
  compressions: CompressionRecord[];
  runId: number | null;
  error: string | null;
  modelName: string;
  startedAt: string | null;
  endedAt: string | null;
  /** 已接收事件数（只计数，不再累积整个事件数组，避免逐 token O(n²) 复制）。 */
  eventCount: number;
  /** 等待确认的挂起态；非空表示这一轮还没结束，只是停住了。 */
  awaiting: AwaitingState | null;
}

export const INITIAL_STATE: StreamState = {
  running: false,
  text: '',
  steps: [],
  inputTokens: 0,
  outputTokens: 0,
  cacheInputTokens: 0,
  contextTokens: null,
  contextThreshold: null,
  contextSize: null,
  compressions: [],
  runId: null,
  error: null,
  modelName: '',
  startedAt: null,
  endedAt: null,
  eventCount: 0,
  awaiting: null,
};

export type EventHandler = (state: StreamState, event: StreamEvent) => StreamState;

export function eventAt(event: StreamEvent): string {
  return event.created_at ?? new Date().toISOString();
}

/** 文本/思考等块级步骤按 block_id 归并。 */
export function blockStepId(event: StreamEvent, kind: string): string {
  return `${kind}:${event.block_id ?? event.reply_id ?? 'x'}`;
}

export function upsertStep(steps: ProcessEntry[], step: ProcessEntry): ProcessEntry[] {
  const idx = steps.findIndex((item) => item.id === step.id);
  if (idx < 0) return [...steps, step];
  const next = [...steps];
  const prev = next[idx];
  next[idx] = {
    ...prev,
    ...step,
    at: prev.at ?? step.at,
    args: step.args ?? prev.args,
    result: step.result ?? prev.result,
  };
  return next;
}

/** 向指定步骤的某个字段追加增量；步骤不存在时按 base 补一条。 */
export function appendToStep(
  steps: ProcessEntry[],
  id: string,
  field: 'args' | 'result' | 'detail',
  delta: string,
  base: Omit<ProcessEntry, 'id'>,
): ProcessEntry[] {
  const found = steps.find((item) => item.id === id);
  let step: ProcessEntry;
  if (found) {
    step = field === 'args'
      ? { ...found, args: (found.args ?? '') + delta }
      : field === 'result'
        ? { ...found, result: (found.result ?? '') + delta }
        : { ...found, detail: found.detail + delta };
  } else {
    step = field === 'args'
      ? { ...base, id, args: delta }
      : field === 'result'
        ? { ...base, id, result: delta }
        : { ...base, id, detail: delta };
  }
  return upsertStep(steps, step);
}

export function patchStep(
  steps: ProcessEntry[],
  id: string,
  patch: Partial<ProcessEntry>,
  base: Omit<ProcessEntry, 'id'>,
): ProcessEntry[] {
  const found = steps.find((item) => item.id === id);
  return upsertStep(steps, found ? { ...found, ...patch } : { ...base, id, ...patch });
}

/** 运行正常结束：把仍在执行的过程步骤收尾为完成。 */
export function finishRunningSteps(steps: ProcessEntry[]): ProcessEntry[] {
  return steps.map((step) => (step.state === 'running' ? { ...step, state: 'success' } : step));
}
