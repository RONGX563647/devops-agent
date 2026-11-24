/** 上下文压缩记录：START/END 帧折进 StreamState.compressions。 */

import type { CompressionRecord, StreamEvent } from '../../types';
import { eventAt, type StreamState } from './state';

/** 压缩记录 id：优先用事件时间戳，同一毫秒内的重复压缩退回复位置。 */
function compressionId(event: StreamEvent): string {
  return `comp:${event.created_at ?? event.reply_id ?? 'x'}`;
}

/** 找最后一条还没结束的压缩记录（START 之后、END 之前）。 */
function openCompression(records: CompressionRecord[]): CompressionRecord | undefined {
  for (let index = records.length - 1; index >= 0; index -= 1) {
    if (records[index].state === 'running') return records[index];
  }
  return undefined;
}

/** 一条压缩记录的初始形态：START 用它建行，缺 START 的 END 也用它兜底。 */
export function beginCompression(state: StreamState, event: StreamEvent): CompressionRecord {
  return {
    id: compressionId(event),
    before: event.before_tokens ?? 0,
    after: null,
    threshold: event.threshold ?? state.contextThreshold ?? 0,
    state: 'running',
    summaryChars: 0,
    at: eventAt(event),
    endedAt: null,
  };
}

/** END 帧折成记录上的补丁：有没有真压决定 state 与 after。 */
function endPatch(
  event: StreamEvent,
  running: CompressionRecord | undefined,
): Pick<CompressionRecord, 'after' | 'state' | 'summaryChars' | 'endedAt'> {
  const compressed = event.compressed === true;
  return {
    after: compressed ? (event.after_tokens ?? null) : null,
    state: compressed ? 'done' : 'skipped',
    summaryChars: event.summary_chars ?? running?.summaryChars ?? 0,
    endedAt: event.created_at ?? null,
  };
}

/** 把 END 事件折进记录列表：有 START 就收尾，没有就补一条完整的。 */
export function closeCompression(state: StreamState, event: StreamEvent): StreamState {
  const running = openCompression(state.compressions);
  const patch = endPatch(event, running);
  const compressions = running
    ? state.compressions.map((item) => (item.id === running.id ? { ...item, ...patch } : item))
    : [...state.compressions, { ...beginCompression(state, event), ...patch }];
  return {
    ...state,
    compressions,
    // 压完之后当下占用就是压缩后的量；没压则保持原值
    contextTokens: patch.after ?? state.contextTokens,
    contextThreshold: event.threshold ?? state.contextThreshold,
  };
}
