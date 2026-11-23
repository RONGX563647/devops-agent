/** SSE 流式运行 hook：消费 POST /run/stream，实时累积文本与 token。 */

import { useCallback, useRef, useState } from 'react';
import type { StreamEvent } from '../types';

export interface StreamState {
  running: boolean;
  text: string;
  events: StreamEvent[];
  inputTokens: number;
  outputTokens: number;
  runId: number | null;
  error: string | null;
}

const INITIAL: StreamState = {
  running: false,
  text: '',
  events: [],
  inputTokens: 0,
  outputTokens: 0,
  runId: null,
  error: null,
};

// 手写 SSE 帧解析而非引入 fetch-event-source：本应用只需 POST + data 帧 + abort，
// 约 10 行即可覆盖，且后端每帧的 type 已写在 data 里、不依赖 event: 行语义。
// 按 SSE 规范兼容 CRLF 与 \n 两种换行，并支持多行 data 拼接。
export function parseFrame(frame: string): StreamEvent | null {
  const normalized = frame.replace(/\r\n/g, '\n');
  const dataLines = normalized
    .split('\n')
    .filter((l) => l.startsWith('data:'))
    .map((l) => l.slice(5).trimStart());
  if (dataLines.length === 0) return null;
  try {
    return JSON.parse(dataLines.join('\n')) as StreamEvent;
  } catch {
    return null;
  }
}

/** 把一帧事件折叠进状态：纯函数，便于单测，也让 start 的复杂度降下来。 */
export function applyEvent(prev: StreamState, event: StreamEvent): StreamState {
  const next: StreamState = { ...prev, events: [...prev.events, event] };
  switch (event.type) {
    case 'TEXT_BLOCK_DELTA':
      if (event.delta) next.text = prev.text + event.delta;
      break;
    case 'RUN_DONE':
      next.text = event.text ?? prev.text;
      next.runId = event.run_id ?? null;
      next.inputTokens = event.input_tokens ?? prev.inputTokens;
      next.outputTokens = event.output_tokens ?? prev.outputTokens;
      next.running = false;
      break;
    case 'RUN_ERROR':
      next.error = event.error ?? '未知错误';
      next.running = false;
      break;
  }
  return next;
}

/** 逐帧读取 SSE 流并回调；抽成独立函数便于单测，也降低 start 的复杂度。 */
export async function consumeStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';
    for (const frame of frames) {
      const event = parseFrame(frame);
      if (event) onEvent(event);
    }
  }
}

export function useRunStream() {
  const [state, setState] = useState<StreamState>(INITIAL);
  const abortRef = useRef<AbortController | null>(null);

  const start = useCallback(async (sessionId: string, userInput: string) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setState({ ...INITIAL, running: true });

    try {
      const resp = await fetch('/run/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Request-ID': crypto.randomUUID(),
        },
        body: JSON.stringify({ session_id: sessionId, user_input: userInput }),
        signal: controller.signal,
      });
      if (!resp.ok || !resp.body) {
        throw new Error(`流式请求失败 ${resp.status}`);
      }

      await consumeStream(resp.body, (event) => {
        setState((prev) => applyEvent(prev, event));
      });
      setState((prev) => ({ ...prev, running: false }));
    } catch (err) {
      if (controller.signal.aborted) return;
      setState((prev) => ({
        ...prev,
        running: false,
        error: err instanceof Error ? err.message : String(err),
      }));
    }
  }, []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    setState((prev) => ({ ...prev, running: false }));
  }, []);

  return { ...state, start, stop };
}
