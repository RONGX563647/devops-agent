/** 记忆事件：检索 / 裁剪 / 压缩 / 写入各落一个步骤，失败与跳过同样可见。 */

import type { EventHandler } from '../state';
import { eventAt, upsertStep } from '../state';
import type { ProcessEntry, StreamEvent } from '../../../types';

function layerName(event: StreamEvent): string {
  const layer = event.layer ?? '';
  if (layer === 'short') return '短期';
  if (layer === 'episodic') return '中期';
  if (layer === 'long' || layer === 'long_term') return '长期';
  return layer || '记忆';
}

function memory(event: StreamEvent): Record<string, unknown> {
  return event.memory ?? {};
}

function numberAt(source: Record<string, unknown>, key: string): number {
  const value = source[key];
  return typeof value === 'number' ? value : 0;
}

function compressDetail(event: StreamEvent): string {
  const payload = memory(event);
  const before = numberAt(payload, 'tokens_before');
  const after = numberAt(payload, 'tokens_after');
  return `${Math.round(before / 1000)}k → ${Math.round(after / 1000)}k`;
}

const DETAILS: Record<string, (event: StreamEvent) => string> = {
  MEMORY_RETRIEVE_END: (event) =>
    `命中 ${event.hits ?? 0} 条 · 注入 ${event.injected_chars ?? 0} 字`,
  MEMORY_TRIM: (event) =>
    `回收 ${event.reclaimed_chars ?? 0} 字 · 旧工具结果 ${event.dropped_tool_results ?? 0} 条`,
  MEMORY_COMPRESS_END: compressDetail,
  MEMORY_WRITE_END: (event) => `新增 ${event.stored ? 1 : 0} / 去重 ${event.deduped ? 1 : 0}`,
};

function summaryOf(event: StreamEvent): string {
  return DETAILS[event.type]?.(event) ?? '';
}

function labelOf(event: StreamEvent): string {
  const skipped = event.state === 'skipped' ? '（跳过）' : '';
  switch (event.type) {
    case 'MEMORY_RETRIEVE_END':
      return `记忆检索 · ${layerName(event)}${skipped}`;
    case 'MEMORY_TRIM':
      return '记忆裁剪 · 短期';
    case 'MEMORY_COMPRESS_END':
      return `记忆压缩 · 短期→中期${skipped}`;
    case 'MEMORY_WRITE_END':
      return `记忆写入 · ${layerName(event)}`;
    default:
      return '记忆';
  }
}

/** 每帧一条步骤；id 带上当前步骤数，同一类事件多次出现不会互相覆盖。 */
function memoryHandler(): EventHandler {
  return (state, event) => {
    const at = eventAt(event);
    const detail = summaryOf(event);
    const step: ProcessEntry = {
      id: `memory:${event.type}:${state.steps.length}`,
      kind: 'memory',
      label: labelOf(event),
      detail,
      state: event.state === 'error' ? 'error' : 'success',
      at,
      endedAt: at,
      args: '',
      result: JSON.stringify(memory(event)),
    };
    return { ...state, steps: upsertStep(state.steps, step) };
  };
}

const handler = memoryHandler();

export const MEMORY_HANDLERS: Record<string, EventHandler> = {
  MEMORY_RETRIEVE_END: handler,
  MEMORY_TRIM: handler,
  MEMORY_COMPRESS_END: handler,
  MEMORY_WRITE_END: handler,
};
