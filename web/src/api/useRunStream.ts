/** SSE 流式运行 hook：消费 POST /run/stream，实时累积文本与 token。 */

import { useCallback, useRef, useState } from 'react';
import type { ProcessEntry, StreamEvent } from '../types';

export interface StreamState {
  running: boolean;
  text: string;
  events: StreamEvent[];
  steps: ProcessEntry[];
  inputTokens: number;
  outputTokens: number;
  runId: number | null;
  error: string | null;
  modelName: string;
  startedAt: string | null;
  endedAt: string | null;
}

export const INITIAL_STATE: StreamState = {
  running: false,
  text: '',
  events: [],
  steps: [],
  inputTokens: 0,
  outputTokens: 0,
  runId: null,
  error: null,
  modelName: '',
  startedAt: null,
  endedAt: null,
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

/** 追加或更新一条过程步骤（按 id 归并，便于 DELTA 逐帧累积到同一条）。 */
function upsertStep(steps: ProcessEntry[], step: ProcessEntry): ProcessEntry[] {
  const idx = steps.findIndex((s) => s.id === step.id);
  if (idx < 0) return [...steps, step];
  const next = [...steps];
  next[idx] = step;
  return next;
}

/** 取某类事件的稳定 id：优先 block_id / tool_call_id，缺省回落到 reply_id。 */
function stepId(event: StreamEvent, suffix: string): string {
  return `${suffix}:${event.block_id ?? event.tool_call_id ?? event.reply_id ?? 'x'}`;
}

/** 追加 delta 到指定步骤的 detail（先取已有步骤，不存在则建一条）。 */
function appendDetail(
  steps: ProcessEntry[],
  id: string,
  kind: ProcessEntry['kind'],
  label: string,
  delta: string,
): ProcessEntry[] {
  const found = steps.find((s) => s.id === id);
  const step: ProcessEntry = found
    ? { ...found, detail: found.detail + delta }
    : { id, kind, label, detail: delta, state: 'running' };
  return upsertStep(steps, step);
}

/** 把指定步骤标记为完成态（success/error），不存在则补一条。 */
function finishStep(
  steps: ProcessEntry[],
  id: string,
  kind: ProcessEntry['kind'],
  label: string,
  state: ProcessEntry['state'],
): ProcessEntry[] {
  const found = steps.find((s) => s.id === id);
  const step: ProcessEntry = found
    ? { ...found, state }
    : { id, kind, label, detail: '', state };
  return upsertStep(steps, step);
}

type EventHandler = (state: StreamState, event: StreamEvent) => StreamState;

/** 每类事件如何改写状态：分派表代替大 switch，保持每个 handler 低复杂度。 */
const HANDLERS: Record<string, EventHandler> = {
  // 生命周期：记录模型名、起止时间
  REPLY_START: (s, e) => ({
    ...s,
    startedAt: e.created_at ?? s.startedAt,
    steps: upsertStep(s.steps, {
      id: stepId(e, 'reply'),
      kind: 'lifecycle',
      label: '开始回复',
      detail: e.name ?? '',
      state: 'success',
    }),
  }),
  REPLY_END: (s, e) => ({ ...s, endedAt: e.created_at ?? s.endedAt }),
  MODEL_CALL_START: (s, e) => ({
    ...s,
    modelName: e.model_name ?? s.modelName,
    steps: upsertStep(s.steps, {
      id: stepId(e, 'model'),
      kind: 'lifecycle',
      label: `模型调用 · ${e.model_name ?? '未知'}`,
      detail: '',
      state: 'running',
    }),
  }),
  MODEL_CALL_END: (s, e) => ({
    ...s,
    inputTokens: e.input_tokens ?? s.inputTokens,
    outputTokens: e.output_tokens ?? s.outputTokens,
    steps: finishStep(s.steps, stepId(e, 'model'), 'lifecycle', '模型调用', 'success'),
  }),
  HINT_BLOCK: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: stepId(e, 'hint'),
      kind: 'lifecycle',
      label: e.source ? `分析提示 · ${e.source}` : '分析提示',
      detail: e.hint ?? '',
      state: 'success',
    }),
  }),

  // 正文：既累积气泡文本，也留一条标注步骤
  TEXT_BLOCK_DELTA: (s, e) => ({
    ...s,
    text: s.text + (e.delta ?? ''),
    steps: appendDetail(s.steps, stepId(e, 'text'), 'text', '正文输出', e.delta ?? ''),
  }),

  // 思考：START 开段、DELTA 累积、END 收尾
  THINKING_BLOCK_START: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: stepId(e, 'thinking'),
      kind: 'thinking',
      label: '思考中',
      detail: '',
      state: 'running',
    }),
  }),
  THINKING_BLOCK_DELTA: (s, e) => ({
    ...s,
    steps: appendDetail(s.steps, stepId(e, 'thinking'), 'thinking', '思考中', e.delta ?? ''),
  }),
  THINKING_BLOCK_END: (s, e) => ({
    ...s,
    steps: finishStep(s.steps, stepId(e, 'thinking'), 'thinking', '思考', 'success'),
  }),

  // 工具调用：按 tool_call_id 配对，START 建、DELTA 追加参数、END/RESULT 收尾
  TOOL_CALL_START: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: stepId(e, 'tool'),
      kind: 'tool',
      label: `调用工具 · ${e.tool_call_name ?? '未知'}`,
      detail: '',
      state: 'running',
      toolCallId: e.tool_call_id,
    }),
  }),
  TOOL_CALL_DELTA: (s, e) => ({
    ...s,
    steps: appendDetail(s.steps, stepId(e, 'tool'), 'tool', '调用工具', e.delta ?? ''),
  }),
  TOOL_CALL_END: (s, e) => ({
    ...s,
    steps: finishStep(s.steps, stepId(e, 'tool'), 'tool', '调用工具', 'success'),
  }),
  TOOL_RESULT_START: (s) => s,
  TOOL_RESULT_TEXT_DELTA: (s, e) => ({
    ...s,
    steps: appendDetail(s.steps, stepId(e, 'tool'), 'tool', '工具结果', e.delta ?? ''),
  }),
  TOOL_RESULT_DATA_DELTA: (s) => s,
  TOOL_RESULT_END: (s, e) => ({
    ...s,
    steps: finishStep(
      s.steps,
      stepId(e, 'tool'),
      'tool',
      '工具结果',
      e.state === 'success' ? 'success' : 'error',
    ),
  }),

  // 控制：需要人/外部介入
  REQUIRE_USER_CONFIRM: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: stepId(e, 'control'),
      kind: 'control',
      label: '等待用户确认',
      detail: '',
      state: 'running',
    }),
  }),
  USER_CONFIRM_RESULT: (s, e) => ({
    ...s,
    steps: finishStep(s.steps, stepId(e, 'control'), 'control', '用户已确认', 'success'),
  }),
  REQUIRE_EXTERNAL_EXECUTION: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: stepId(e, 'control'),
      kind: 'control',
      label: '请求外部执行',
      detail: '',
      state: 'running',
    }),
  }),
  EXTERNAL_EXECUTION_RESULT: (s, e) => ({
    ...s,
    steps: finishStep(s.steps, stepId(e, 'control'), 'control', '外部执行完成', 'success'),
  }),

  // 异常：单独标注
  EXCEED_MAX_ITERS: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: stepId(e, 'error'),
      kind: 'error',
      label: '达到最大迭代次数',
      detail: e.name ?? '',
      state: 'error',
    }),
  }),
  USER_INTERRUPT: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: stepId(e, 'error'),
      kind: 'error',
      label: '用户中断',
      detail: '',
      state: 'error',
    }),
  }),

  RUN_DONE: (s, e) => ({
    ...s,
    text: e.text ?? s.text,
    runId: e.run_id ?? null,
    inputTokens: e.input_tokens ?? s.inputTokens,
    outputTokens: e.output_tokens ?? s.outputTokens,
    running: false,
    endedAt: s.endedAt ?? e.created_at ?? null,
  }),
  RUN_ERROR: (s, e) => ({
    ...s,
    error: e.error ?? '未知错误',
    running: false,
    steps: upsertStep(s.steps, {
      id: 'run-error',
      kind: 'error',
      label: '运行失败',
      detail: e.error ?? '未知错误',
      state: 'error',
    }),
  }),
};

/** 把一帧事件折叠进状态：纯函数，按 type 分派到各标注 handler。 */
export function applyEvent(prev: StreamState, event: StreamEvent): StreamState {
  const next: StreamState = { ...prev, events: [...prev.events, event] };
  const handler = HANDLERS[event.type];
  return handler ? handler(next, event) : next;
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
  const [state, setState] = useState<StreamState>(INITIAL_STATE);
  const abortRef = useRef<AbortController | null>(null);

  const start = useCallback(async (sessionId: string, userInput: string) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setState({ ...INITIAL_STATE, running: true });

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
