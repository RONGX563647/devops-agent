/** 模型调用事件：一次调用 = 一个独立步骤，END 累加用量并收尾。 */

import type { EventHandler } from '../state';
import { eventAt, patchStep, upsertStep } from '../state';
import type { ProcessEntry, StreamEvent } from '../../../types';

/** 一轮模型调用 = 一个步骤；同一 reply 里的第 N 次调用必须有自己的 id。 */
function modelStepId(replyId: string, callIndex: number): string {
  return `model:${replyId || 'x'}:${callIndex}`;
}

/** 同一 reply 内已有多少模型步骤，下一个就是第几位。 */
function nextCallIndex(steps: ProcessEntry[], replyId: string): number {
  return (
    steps.filter((step) => step.kind === 'model' && (step.replyId ?? '') === replyId).length + 1
  );
}

/** END 按 FIFO 关掉该 reply 最早一个仍在跑的模型步骤。 */
function runningModelStep(steps: ProcessEntry[], replyId: string): ProcessEntry | null {
  return (
    steps.find(
      (step) =>
        step.kind === 'model' && step.state === 'running' && (step.replyId ?? '') === replyId,
    ) ?? null
  );
}

/** 兼容旧数据：没带 callIndex 时按同组出现顺序反推。 */
function callIndexOf(steps: ProcessEntry[], step: ProcessEntry, replyId: string): number {
  if (step.callIndex) return step.callIndex;
  const group = steps.filter((item) => item.kind === 'model' && (item.replyId ?? '') === replyId);
  return Math.max(1, group.findIndex((item) => item.id === step.id) + 1);
}

/** 一次模型调用落在步骤上的展示字段（统计口径在 handler 里单独累加）。 */
function modelCallPatch(event: StreamEvent) {
  return {
    state: 'success' as const,
    endedAt: event.created_at ?? null,
    tokensIn: event.input_tokens,
    tokensOut: event.output_tokens,
    cacheTokens: event.cache_input_tokens,
  };
}

/** 模型步骤标题：带序号，多轮工具调用时能分清是哪一次。 */
function modelCallLabel(event: StreamEvent, callIndex: number): string {
  return `模型调用 · ${event.model_name ?? '模型'} · #${callIndex}`;
}

/** 事件注册表：新增该族事件类型在此登记，键为 SSE 事件的 type。 */
export const MODEL_HANDLERS: Record<string, EventHandler> = {
  MODEL_CALL_START: (s, e) => {
    const replyId = e.reply_id ?? '';
    const callIndex = nextCallIndex(s.steps, replyId);
    return {
      ...s,
      modelName: e.model_name ?? s.modelName,
      steps: upsertStep(s.steps, {
        id: modelStepId(replyId, callIndex),
        kind: 'model',
        label: `模型调用 · ${e.model_name ?? '未知'} · #${callIndex}`,
        detail: '',
        state: 'running',
        at: eventAt(e),
        replyId,
        callIndex,
      }),
    };
  },
  MODEL_CALL_END: (s, e) => {
    // 一轮里模型会被调用多次：这里必须累加，否则 message 上只剩最后一次的量，
    // 而 RUN_DONE 给的 input 是整轮总和 —— 分子分母不同口径，命中率必被低估。
    const replyId = e.reply_id ?? '';
    const open = runningModelStep(s.steps, replyId);
    const callIndex = open
      ? callIndexOf(s.steps, open, replyId)
      : nextCallIndex(s.steps, replyId);
    return {
      ...s,
      inputTokens: s.inputTokens + (e.input_tokens ?? 0),
      outputTokens: s.outputTokens + (e.output_tokens ?? 0),
      cacheInputTokens: s.cacheInputTokens + (e.cache_input_tokens ?? 0),
      // 本次调用的 prompt 大小 = 此刻上下文占用；首次调用前圆环只能靠 hint 估
      contextTokens: e.input_tokens ?? s.contextTokens,
      steps: patchStep(
        s.steps,
        open?.id ?? modelStepId(replyId, callIndex),
        modelCallPatch(e),
        {
          kind: 'model',
          label: modelCallLabel(e, callIndex),
          detail: '',
          state: 'success',
          at: eventAt(e),
          replyId,
          callIndex,
        },
      ),
    };
  },
};
