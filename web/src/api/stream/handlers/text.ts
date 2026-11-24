/** 正文与思考块事件：正文只累积到气泡文本，不再另存一份到步骤 detail。 */

import type { EventHandler } from '../state';
import { appendToStep, blockStepId, eventAt, patchStep, upsertStep } from '../state';

export const TEXT_HANDLERS: Record<string, EventHandler> = {
  // 正文只累积到气泡文本，不再另存一份到步骤 detail（避免双份与 O(n²)）。
  TEXT_BLOCK_DELTA: (s, e) => ({ ...s, text: s.text + (e.delta ?? '') }),

  THINKING_BLOCK_START: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: blockStepId(e, 'thinking'),
      kind: 'thinking',
      label: '思考中',
      detail: '',
      state: 'running',
      at: eventAt(e),
    }),
  }),
  THINKING_BLOCK_DELTA: (s, e) => ({
    ...s,
    steps: appendToStep(
      s.steps,
      blockStepId(e, 'thinking'),
      'detail',
      e.delta ?? '',
      { kind: 'thinking', label: '思考中', detail: '', state: 'running', at: eventAt(e) },
    ),
  }),
  THINKING_BLOCK_END: (s, e) => ({
    ...s,
    steps: patchStep(
      s.steps,
      blockStepId(e, 'thinking'),
      { state: 'success', endedAt: e.created_at ?? null },
      { kind: 'thinking', label: '思考', detail: '', state: 'success', at: eventAt(e) },
    ),
  }),
};
