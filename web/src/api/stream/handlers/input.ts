/** 输入解析、上下文预算与压缩事件。 */

import type { EventHandler } from '../state';
import { eventAt, patchStep, upsertStep } from '../state';
import { parseStepId, parseSummary, toParseStage } from '../parse';
import { beginCompression, closeCompression } from '../compression';

/** 事件注册表：新增该族事件类型在此登记，键为 SSE 事件的 type。 */
export const INPUT_HANDLERS: Record<string, EventHandler> = {
  // 输入解析：先建父行，再逐阶段补明细，END 把总账写进 detail。
  INPUT_PARSE_START: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: parseStepId(e),
      kind: 'parse',
      label: '输入解析',
      detail: '',
      state: 'running',
      at: eventAt(e),
      parseStages: [],
    }),
  }),
  INPUT_PARSE_STAGE: (s, e) => {
    const id = parseStepId(e);
    const stage = toParseStage(e);
    const found = s.steps.find((item) => item.id === id);
    // 同一阶段重复到达时覆盖而不是追加，保证 stage 在父行里唯一
    const stages = [...(found?.parseStages ?? []).filter((item) => item.stage !== stage.stage), stage];
    return {
      ...s,
      steps: upsertStep(s.steps, {
        id,
        kind: 'parse',
        label: '输入解析',
        detail: parseSummary(stages),
        state: 'running',
        at: eventAt(e),
        parseStages: stages,
      }),
    };
  },
  INPUT_PARSE_END: (s, e) => {
    const id = parseStepId(e);
    const found = s.steps.find((item) => item.id === id);
    return {
      ...s,
      steps: patchStep(
        s.steps,
        id,
        {
          state: 'success',
          detail: e.summary ?? found?.detail ?? '',
          endedAt: e.created_at ?? null,
        },
        { kind: 'parse', label: '输入解析', detail: e.summary ?? '', state: 'success', at: eventAt(e) },
      ),
    };
  },

  // 上下文预算与压缩：圆环与压缩时间线全靠这三类帧。
  // 预算在流最开始到达一次；压缩帧由后端钩子在每次推理前的检查中发出。
  CONTEXT_BUDGET: (s, e) => ({
    ...s,
    contextThreshold: e.threshold ?? s.contextThreshold,
    contextSize: e.context_size ?? s.contextSize,
  }),
  CONTEXT_COMPRESSION_START: (s, e) => ({
    ...s,
    contextThreshold: e.threshold ?? s.contextThreshold,
    // 压缩前这一刻的占用就是圆环该显示的当前值
    contextTokens: e.before_tokens ?? s.contextTokens,
    compressions: [...s.compressions, beginCompression(s, e)],
  }),
  CONTEXT_COMPRESSION_END: (s, e) => closeCompression(s, e),
};
