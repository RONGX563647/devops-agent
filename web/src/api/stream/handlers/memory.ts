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

/** 明细优先取 memory 子对象，缺失时回退顶层（兼容旧帧）。 */
function readNumber(event: StreamEvent, key: string): number {
  const value = memory(event)[key];
  if (typeof value === 'number') return value;
  return numberAt(event as unknown as Record<string, unknown>, key);
}

function readBool(event: StreamEvent, key: string): boolean {
  const value = memory(event)[key];
  if (typeof value === 'boolean') return value;
  const top = (event as unknown as Record<string, unknown>)[key];
  return typeof top === 'boolean' ? top : false;
}

function historyMessages(event: StreamEvent): number {
  const zones = memory(event).zones;
  if (!zones || typeof zones !== 'object') return 0;
  const history = (zones as Record<string, unknown>).history;
  if (!history || typeof history !== 'object') return 0;
  return numberAt(history as Record<string, unknown>, 'messages');
}

function compressDetail(event: StreamEvent): string {
  const before = readNumber(event, 'tokens_before');
  const after = readNumber(event, 'tokens_after');
  return `${Math.round(before / 1000)}k → ${Math.round(after / 1000)}k`;
}

const DETAILS: Record<string, (event: StreamEvent) => string> = {
  MEMORY_RETRIEVE_END: (event) =>
    `命中 ${readNumber(event, 'hits')} 条 · 注入 ${readNumber(event, 'injected_chars')} 字`,
  MEMORY_TRIM: (event) =>
    `回收 ${readNumber(event, 'reclaimed_chars')} 字 · 旧工具结果 ${readNumber(event, 'dropped_tool_results')} 条`,
  MEMORY_COMPRESS_END: compressDetail,
  MEMORY_WRITE_END: (event) =>
    `新增 ${readBool(event, 'stored') ? 1 : 0} / 去重 ${readBool(event, 'deduped') ? 1 : 0}`,
  CONTEXT_ASSEMBLED: (event) =>
    `历史 ${historyMessages(event)} 条 · 注入 ${readNumber(event, 'injected_chars')} 字`,
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
    case 'CONTEXT_ASSEMBLED':
      return '上下文装配';
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
  CONTEXT_ASSEMBLED: handler,
};
