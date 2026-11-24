/** 工具调用事件：START 建、DELTA 追加入参、RESULT_* 追加返回，各自独立字段。 */

import type { EventHandler } from '../state';
import { appendToStep, eventAt, patchStep, upsertStep } from '../state';
import type { ProcessEntry, StreamEvent } from '../../../types';

/** 工具步骤以 tool_call_id 为稳定键；缺省才回落到 block_id / reply_id。 */
function toolStepId(event: StreamEvent): string {
  return `tool:${event.tool_call_id ?? event.block_id ?? event.reply_id ?? 'x'}`;
}

/** 工具增量没有携带 id 时，回落到最近一个仍在执行的工具步骤。 */
function lastRunningToolId(steps: ProcessEntry[]): string | null {
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    if (steps[i].kind === 'tool' && steps[i].state === 'running') return steps[i].id;
  }
  return null;
}

export const TOOL_HANDLERS: Record<string, EventHandler> = {
  TOOL_CALL_START: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: toolStepId(e),
      kind: 'tool',
      label: `调用工具 · ${e.tool_call_name ?? '未知'}`,
      detail: '',
      state: 'running',
      toolCallId: e.tool_call_id,
      toolName: e.tool_call_name,
      args: '',
      result: '',
      at: eventAt(e),
    }),
  }),
  TOOL_CALL_DELTA: (s, e) => {
    const id = e.tool_call_id
      ? toolStepId(e)
      : (lastRunningToolId(s.steps) ?? toolStepId(e));
    return {
      ...s,
      steps: appendToStep(s.steps, id, 'args', e.delta ?? '', {
        kind: 'tool',
        label: `调用工具 · ${e.tool_call_name ?? '未知'}`,
        detail: '',
        state: 'running',
        toolCallId: e.tool_call_id,
        toolName: e.tool_call_name,
        at: eventAt(e),
      }),
    };
  },
  TOOL_CALL_END: (s, e) => {
    const id = e.tool_call_id ? toolStepId(e) : (lastRunningToolId(s.steps) ?? toolStepId(e));
    return { ...s, steps: patchStep(s.steps, id, { toolName: e.tool_call_name }, {
      kind: 'tool',
      label: `调用工具 · ${e.tool_call_name ?? '未知'}`,
      detail: '',
      state: 'running',
      at: eventAt(e),
    }) };
  },
  TOOL_RESULT_START: (s, e) => {
    const id = e.tool_call_id ? toolStepId(e) : (lastRunningToolId(s.steps) ?? toolStepId(e));
    return {
      ...s,
      steps: patchStep(s.steps, id, { state: 'running' }, {
        kind: 'tool',
        label: `调用工具 · ${e.tool_call_name ?? '未知'}`,
        detail: '',
        state: 'running',
        toolCallId: e.tool_call_id,
        toolName: e.tool_call_name,
        at: eventAt(e),
      }),
    };
  },
  TOOL_RESULT_TEXT_DELTA: (s, e) => {
    const id = e.tool_call_id ? toolStepId(e) : (lastRunningToolId(s.steps) ?? toolStepId(e));
    return {
      ...s,
      steps: appendToStep(s.steps, id, 'result', e.delta ?? '', {
        kind: 'tool',
        label: '工具结果',
        detail: '',
        state: 'running',
        toolCallId: e.tool_call_id,
        at: eventAt(e),
      }),
    };
  },
  TOOL_RESULT_DATA_DELTA: (s) => s,
  TOOL_RESULT_END: (s, e) => {
    const id = e.tool_call_id ? toolStepId(e) : (lastRunningToolId(s.steps) ?? toolStepId(e));
    const state = e.state === 'success' ? 'success' : 'error';
    return {
      ...s,
      steps: patchStep(s.steps, id, { state, endedAt: e.created_at ?? null }, {
        kind: 'tool',
        label: '工具结果',
        detail: '',
        state,
        toolCallId: e.tool_call_id,
        at: eventAt(e),
      }),
    };
  },
};
