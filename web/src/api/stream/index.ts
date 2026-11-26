/** SSE 流式运行：帧解码、事件折叠与 POST 单流读取。
 *
 * 只保留纯逻辑（可单测），会话生命周期与持久化由 workspace/store 负责。
 */

import type { ConfirmDecision, StreamEvent } from '../../types';
import { parseFrame } from './parse';
import type { EventHandler, StreamState } from './state';
import { INPUT_HANDLERS } from './handlers/input';
import { LIFECYCLE_HANDLERS } from './handlers/lifecycle';
import { MEMORY_HANDLERS } from './handlers/memory';
import { MODEL_HANDLERS } from './handlers/model';
import { TEXT_HANDLERS } from './handlers/text';
import { TOOL_HANDLERS } from './handlers/tool';

export { INITIAL_STATE } from './state';
export type { StreamState } from './state';
export { parseContextHint, parseFrame, parseSummary } from './parse';

/** 各事件族的注册表合并成一张按 type 分派的总表。 */
const HANDLERS: Record<string, EventHandler> = {
  ...INPUT_HANDLERS,
  ...LIFECYCLE_HANDLERS,
  ...MEMORY_HANDLERS,
  ...MODEL_HANDLERS,
  ...TEXT_HANDLERS,
  ...TOOL_HANDLERS,
};

/** 把一帧事件折叠进状态：纯函数，按 type 分派到各 handler。 */
export function applyEvent(prev: StreamState, event: StreamEvent): StreamState {
  const next: StreamState = { ...prev, eventCount: prev.eventCount + 1 };
  const handler = HANDLERS[event.type];
  return handler ? handler(next, event) : next;
}

/** 逐帧读取 SSE 流并回调；抽成独立函数便于单测。
 *
 * onActivity 在「读到任意字节」时触发，与帧内容无关：注释心跳帧、工具输出
 * 增量、未闭合的 chunk 都算活动。这样保活判定不依赖 parseFrame 能否解析出
 * 事件（注释帧会被 parseFrame 丢弃，但同样证明连接还活着）。
 */
export async function consumeStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: StreamEvent) => void,
  onActivity?: () => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        // 流结束时把解码器里残留的字节与未闭合的尾帧一起 flush，避免丢掉最后一帧。
        buffer += decoder.decode();
        break;
      }
      onActivity?.();
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
      const frames = buffer.split('\n\n');
      buffer = frames.pop() ?? '';
      for (const frame of frames) {
        const event = parseFrame(frame);
        if (event) onEvent(event);
      }
    }
    const tail = parseFrame(buffer);
    if (tail) onEvent(tail);
  } finally {
    // 读失败或回调抛错时也要解锁：否则 body 一直被锁住，调用方连重读都做不到。
    // 仍有未 settle 的 read 时 releaseLock 会抛，这里忽略——不能拿清理的错盖掉原始异常。
    try {
      reader.releaseLock();
    } catch {
      // 忽略
    }
  }
}

/** 发起一次 POST 流式运行并逐帧回调；调用方负责 abort 与状态收尾。 */
export async function streamRun(
  sessionId: string,
  userInput: string,
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal,
  requestId: string,
  attachmentIds: string[] = [],
  permission?: string,
  webSearch = false,
  onActivity?: () => void,
): Promise<void> {
  const resp = await fetch('/run/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Request-ID': requestId,
    },
    // 只带 id：附件正文留在服务端，由解析管线取用并统一脱敏/裁预算
    body: JSON.stringify({
      session_id: sessionId,
      user_input: userInput,
      attachments: attachmentIds,
      ...(permission ? { permission } : {}),
      // 每次都显式表态：省略会被服务端理解为「按部署默认」，
      // 部署开了 DEVOPS_AGENT_ENABLE_SEARCH 时，界面上的「关」就关不掉了。
      web_search: webSearch,
    }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`流式请求失败 ${resp.status}`);
  }
  await consumeStream(resp.body, onEvent, onActivity);
}

/** 提交确认并接着读同一条 run 的后续事件流。 */
export async function streamConfirm(
  runId: number,
  sessionId: string,
  decisions: ConfirmDecision[],
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal,
  /** 挂起中用户切过档时带上：续跑按当前档位，不再锁死发起轮。 */
  permission?: string,
  onActivity?: () => void,
): Promise<void> {
  const resp = await fetch(`/run/${runId}/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // 只给 id 与决策：命令原文以服务端挂起态为准，客户端改不了要执行的东西
    body: JSON.stringify({
      session_id: sessionId,
      results: decisions,
      ...(permission ? { permission } : {}),
    }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`确认失败 ${resp.status}`);
  }
  await consumeStream(resp.body, onEvent, onActivity);
}
