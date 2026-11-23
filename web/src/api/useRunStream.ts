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

function parseFrame(frame: string): StreamEvent | null {
  const line = frame
    .split('\n')
    .find((l) => l.startsWith('data: '));
  if (!line) return null;
  try {
    return JSON.parse(line.slice(6)) as StreamEvent;
  } catch {
    return null;
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

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split('\n\n');
        buffer = frames.pop() ?? '';
        for (const frame of frames) {
          const event = parseFrame(frame);
          if (!event) continue;
          setState((prev) => {
            const next: StreamState = {
              ...prev,
              events: [...prev.events, event],
            };
            if (event.type === 'TEXT_BLOCK_DELTA' && event.delta) {
              next.text = prev.text + event.delta;
            } else if (event.type === 'RUN_DONE') {
              next.text = event.text ?? prev.text;
              next.runId = event.run_id ?? null;
              next.inputTokens = event.input_tokens ?? prev.inputTokens;
              next.outputTokens = event.output_tokens ?? prev.outputTokens;
              next.running = false;
            } else if (event.type === 'RUN_ERROR') {
              next.error = event.error ?? '未知错误';
              next.running = false;
            }
            return next;
          });
        }
      }
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
