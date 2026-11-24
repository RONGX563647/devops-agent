/** 回复/运行生命周期、人机确认与异常收尾事件。 */

import type { EventHandler } from '../state';
import { blockStepId, eventAt, finishRunningSteps, patchStep, upsertStep } from '../state';
import { parseContextHint } from '../parse';

export const LIFECYCLE_HANDLERS: Record<string, EventHandler> = {
  REPLY_START: (s, e) => ({
    ...s,
    startedAt: e.created_at ?? s.startedAt,
    steps: upsertStep(s.steps, {
      id: blockStepId(e, 'reply'),
      kind: 'lifecycle',
      label: '开始回复',
      detail: e.name ?? '',
      state: 'success',
      at: eventAt(e),
    }),
  }),
  REPLY_END: (s, e) => ({ ...s, endedAt: e.created_at ?? s.endedAt }),
  HINT_BLOCK: (s, e) => {
    // 上下文长度提示同样裹在 <system-reminder> 里，但它带精确 token 数：
    // 先抓数（圆环据此提前点亮），再决定要不要作为步骤展示。
    const context = parseContextHint(e.hint ?? '');
    const base = context
      ? { ...s, contextTokens: context.tokens, contextThreshold: context.threshold }
      : s;
    // AgentScope 会把系统提醒（如当前时间）也作为 HINT_BLOCK 发出，与执行过程无关。
    if (!e.hint || e.hint.includes('<system-reminder>')) return base;
    return {
      ...base,
      steps: upsertStep(base.steps, {
        id: blockStepId(e, 'hint'),
        kind: 'lifecycle',
        label: e.source ? `分析提示 · ${e.source}` : '分析提示',
        detail: e.hint,
        state: 'success',
        at: eventAt(e),
      }),
    };
  },

  // 控制：需要人介入
  // 确认请求不再等于「这轮结束」——服务端会把 run 挂起（parked），
  // 用户答完继续跑同一条 run。所以这里标 running 而不是 error。
  REQUIRE_USER_CONFIRM: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: blockStepId(e, 'control'),
      kind: 'control',
      label: e.tool_call_name ? `等待确认 · ${e.tool_call_name}` : '等待确认',
      detail: '',
      state: 'running',
      at: eventAt(e),
    }),
    awaiting: e.tool_calls?.length
      ? { runId: s.runId, calls: e.tool_calls, expiresAt: null }
      : s.awaiting,
  }),
  USER_CONFIRM_RESULT: (s, e) => ({
    ...s,
    steps: patchStep(s.steps, blockStepId(e, 'control'), { state: 'success' }, {
      kind: 'control',
      label: '用户已确认',
      detail: '',
      state: 'success',
      at: eventAt(e),
    }),
  }),
  // 挂起帧：流到此正常结束（不是 done），run 停在等确认。
  // 收尾要打在「刚才那条等待确认」上，不能再造一行——否则同一件事出现两行。
  RUN_PARKED: (s, e) => {
    const open = [...s.steps]
      .reverse()
      .find((step) => step.kind === 'control' && step.state === 'running');
    const id = open?.id ?? blockStepId(e, 'control');
    return {
      ...s,
      running: false,
      runId: e.run_id ?? s.runId,
      endedAt: null,
      awaiting: {
        runId: e.run_id ?? s.runId,
        calls: e.tool_calls ?? s.awaiting?.calls ?? [],
        expiresAt: e.expires_at ?? null,
      },
      steps: patchStep(s.steps, id, { state: 'success' }, {
        kind: 'control',
        label: '等待确认',
        detail: '',
        state: 'success',
        at: eventAt(e),
      }),
    };
  },
  REQUIRE_EXTERNAL_EXECUTION: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: blockStepId(e, 'control'),
      kind: 'control',
      label: '请求外部执行',
      detail: '',
      state: 'running',
      at: eventAt(e),
    }),
  }),
  EXTERNAL_EXECUTION_RESULT: (s, e) => ({
    ...s,
    steps: patchStep(s.steps, blockStepId(e, 'control'), { state: 'success' }, {
      kind: 'control',
      label: '外部执行完成',
      detail: '',
      state: 'success',
      at: eventAt(e),
    }),
  }),

  EXCEED_MAX_ITERS: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: blockStepId(e, 'error'),
      kind: 'error',
      label: '达到最大迭代次数',
      detail: e.name ?? '',
      state: 'error',
      at: eventAt(e),
    }),
  }),
  USER_INTERRUPT: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: blockStepId(e, 'error'),
      kind: 'error',
      label: '用户中断',
      detail: '',
      state: 'error',
      at: eventAt(e),
    }),
  }),

  RUN_DONE: (s, e) => ({
    ...s,
    text: e.text ? e.text : s.text,
    runId: e.run_id ?? s.runId,
    inputTokens: e.input_tokens ?? s.inputTokens,
    outputTokens: e.output_tokens ?? s.outputTokens,
    cacheInputTokens: e.cache_input_tokens ?? s.cacheInputTokens,
    running: false,
    // 正常收尾必须清掉挂起态：否则确认完仍有残留卡片
    awaiting: null,
    steps: finishRunningSteps(s.steps),
    endedAt: s.endedAt ?? e.created_at ?? null,
  }),
  RUN_ERROR: (s, e) => ({
    ...s,
    error: e.error ?? '未知错误',
    running: false,
    // 与 RUN_DONE 一样收尾：失败后不该留着转圈的步骤、挂起卡片或没有终点的耗时
    awaiting: null,
    steps: finishRunningSteps(
      upsertStep(s.steps, {
        id: 'run-error',
        kind: 'error',
        label: '运行失败',
        detail: e.error ?? '未知错误',
        state: 'error',
        at: eventAt(e),
      }),
    ),
    endedAt: s.endedAt ?? e.created_at ?? null,
  }),
};
