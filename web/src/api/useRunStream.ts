/** SSE 流式运行：帧解码、事件折叠与 POST 单流读取。
 *
 * 只保留纯逻辑（可单测），会话生命周期与持久化由 workspace/store 负责。
 */

import type { ProcessEntry, StreamEvent } from '../types';

export interface StreamState {
  running: boolean;
  text: string;
  steps: ProcessEntry[];
  inputTokens: number;
  outputTokens: number;
  runId: number | null;
  error: string | null;
  modelName: string;
  startedAt: string | null;
  endedAt: string | null;
  /** 已接收事件数（只计数，不再累积整个事件数组，避免逐 token O(n²) 复制）。 */
  eventCount: number;
}

export const INITIAL_STATE: StreamState = {
  running: false,
  text: '',
  steps: [],
  inputTokens: 0,
  outputTokens: 0,
  runId: null,
  error: null,
  modelName: '',
  startedAt: null,
  endedAt: null,
  eventCount: 0,
};

// 手写 SSE 帧解析而非引入 fetch-event-source：本应用只需 POST + data 帧 + abort，
// 按 SSE 规范兼容 CRLF 与 \n 两种换行，并支持多行 data 拼接；注释行（心跳）自动忽略。
export function parseFrame(frame: string): StreamEvent | null {
  const normalized = frame.replace(/\r\n/g, '\n');
  const dataLines = normalized
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart());
  if (dataLines.length === 0) return null;
  try {
    return JSON.parse(dataLines.join('\n')) as StreamEvent;
  } catch {
    return null;
  }
}

function eventAt(event: StreamEvent): string {
  return event.created_at ?? new Date().toISOString();
}

/** 工具步骤以 tool_call_id 为稳定键；缺省才回落到 block_id / reply_id。 */
function toolStepId(event: StreamEvent): string {
  return `tool:${event.tool_call_id ?? event.block_id ?? event.reply_id ?? 'x'}`;
}

/** 文本/思考等块级步骤按 block_id 归并。 */
function blockStepId(event: StreamEvent, kind: string): string {
  return `${kind}:${event.block_id ?? event.reply_id ?? 'x'}`;
}

/** 工具增量没有携带 id 时，回落到最近一个仍在执行的工具步骤。 */
function lastRunningToolId(steps: ProcessEntry[]): string | null {
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    if (steps[i].kind === 'tool' && steps[i].state === 'running') return steps[i].id;
  }
  return null;
}

function upsertStep(steps: ProcessEntry[], step: ProcessEntry): ProcessEntry[] {
  const idx = steps.findIndex((item) => item.id === step.id);
  if (idx < 0) return [...steps, step];
  const next = [...steps];
  const prev = next[idx];
  next[idx] = {
    ...prev,
    ...step,
    at: prev.at ?? step.at,
    args: step.args ?? prev.args,
    result: step.result ?? prev.result,
  };
  return next;
}

/** 向指定步骤的某个字段追加增量；步骤不存在时按 base 补一条。 */
function appendToStep(
  steps: ProcessEntry[],
  id: string,
  field: 'args' | 'result' | 'detail',
  delta: string,
  base: Omit<ProcessEntry, 'id'>,
): ProcessEntry[] {
  const found = steps.find((item) => item.id === id);
  let step: ProcessEntry;
  if (found) {
    step = field === 'args'
      ? { ...found, args: (found.args ?? '') + delta }
      : field === 'result'
        ? { ...found, result: (found.result ?? '') + delta }
        : { ...found, detail: found.detail + delta };
  } else {
    step = field === 'args'
      ? { ...base, id, args: delta }
      : field === 'result'
        ? { ...base, id, result: delta }
        : { ...base, id, detail: delta };
  }
  return upsertStep(steps, step);
}

function patchStep(
  steps: ProcessEntry[],
  id: string,
  patch: Partial<ProcessEntry>,
  base: Omit<ProcessEntry, 'id'>,
): ProcessEntry[] {
  const found = steps.find((item) => item.id === id);
  return upsertStep(steps, found ? { ...found, ...patch } : { ...base, id, ...patch });
}

/** 运行正常结束：把仍在执行的过程步骤收尾为完成。 */
function finishRunningSteps(steps: ProcessEntry[]): ProcessEntry[] {
  return steps.map((step) => (step.state === 'running' ? { ...step, state: 'success' } : step));
}

type EventHandler = (state: StreamState, event: StreamEvent) => StreamState;

const HANDLERS: Record<string, EventHandler> = {
  REPLY_START: (s, e) => ({
    ...s,
    startedAt: e.created_at ?? s.startedAt,
    steps: upsertStep(s.steps, {
      id: blockStepId(e, 'reply'),
      kind: 'lifecycle',
      label: '开始回复',
      detail: e.name ?? '',
      state: 'success',
      at: eventAt(e),
    }),
  }),
  REPLY_END: (s, e) => ({ ...s, endedAt: e.created_at ?? s.endedAt }),
  MODEL_CALL_START: (s, e) => ({
    ...s,
    modelName: e.model_name ?? s.modelName,
    steps: upsertStep(s.steps, {
      id: blockStepId(e, 'model'),
      kind: 'lifecycle',
      label: `模型调用 · ${e.model_name ?? '未知'}`,
      detail: '',
      state: 'running',
      at: eventAt(e),
    }),
  }),
  MODEL_CALL_END: (s, e) => ({
    ...s,
    inputTokens: e.input_tokens ?? s.inputTokens,
    outputTokens: e.output_tokens ?? s.outputTokens,
    steps: patchStep(
      s.steps,
      blockStepId(e, 'model'),
      { state: 'success', endedAt: e.created_at ?? null },
      { kind: 'lifecycle', label: '模型调用', detail: '', state: 'success', at: eventAt(e) },
    ),
  }),
  HINT_BLOCK: (s, e) => {
    // AgentScope 会把系统提醒（如当前时间）也作为 HINT_BLOCK 发出，与执行过程无关。
    if (!e.hint || e.hint.includes('<system-reminder>')) return s;
    return {
      ...s,
      steps: upsertStep(s.steps, {
        id: blockStepId(e, 'hint'),
        kind: 'lifecycle',
        label: e.source ? `分析提示 · ${e.source}` : '分析提示',
        detail: e.hint,
        state: 'success',
        at: eventAt(e),
      }),
    };
  },

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
  THINKING_BLOCK_DELTA: (s, e) =>
    ({
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

  // 工具调用：START 建、DELTA 追加入参、RESULT_* 追加返回，各自独立字段。
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

  // 控制：需要人/外部介入
  // 服务端是无人值守的：收到确认请求意味着这一轮到此为止，必须让人看得出来，
  // 否则界面只剩一条「等待用户确认」，run 结束却不报错，像是模型凭空停了。
  REQUIRE_USER_CONFIRM: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: blockStepId(e, 'control'),
      kind: 'control',
      label: e.tool_call_name ? `需要确认 · ${e.tool_call_name}` : '需要确认',
      detail: '服务端无人值守，无法代为确认，本轮运行在此结束。',
      state: 'error',
      at: eventAt(e),
    }),
  }),
  USER_CONFIRM_RESULT: (s, e) => ({
    ...s,
    steps: patchStep(s.steps, blockStepId(e, 'control'), { state: 'success' }, {
      kind: 'control',
      label: '用户已确认',
      detail: '',
      state: 'success',
      at: eventAt(e),
    }),
  }),
  REQUIRE_EXTERNAL_EXECUTION: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: blockStepId(e, 'control'),
      kind: 'control',
      label: '请求外部执行',
      detail: '',
      state: 'running',
      at: eventAt(e),
    }),
  }),
  EXTERNAL_EXECUTION_RESULT: (s, e) => ({
    ...s,
    steps: patchStep(s.steps, blockStepId(e, 'control'), { state: 'success' }, {
      kind: 'control',
      label: '外部执行完成',
      detail: '',
      state: 'success',
      at: eventAt(e),
    }),
  }),

  EXCEED_MAX_ITERS: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: blockStepId(e, 'error'),
      kind: 'error',
      label: '达到最大迭代次数',
      detail: e.name ?? '',
      state: 'error',
      at: eventAt(e),
    }),
  }),
  USER_INTERRUPT: (s, e) => ({
    ...s,
    steps: upsertStep(s.steps, {
      id: blockStepId(e, 'error'),
      kind: 'error',
      label: '用户中断',
      detail: '',
      state: 'error',
      at: eventAt(e),
    }),
  }),

  RUN_DONE: (s, e) => ({
    ...s,
    text: e.text ? e.text : s.text,
    runId: e.run_id ?? null,
    inputTokens: e.input_tokens ?? s.inputTokens,
    outputTokens: e.output_tokens ?? s.outputTokens,
    running: false,
    steps: finishRunningSteps(s.steps),
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
      at: eventAt(e),
    }),
  }),
};

/** 把一帧事件折叠进状态：纯函数，按 type 分派到各 handler。 */
export function applyEvent(prev: StreamState, event: StreamEvent): StreamState {
  const next: StreamState = { ...prev, eventCount: prev.eventCount + 1 };
  const handler = HANDLERS[event.type];
  return handler ? handler(next, event) : next;
}

/** 逐帧读取 SSE 流并回调；抽成独立函数便于单测。 */
export async function consumeStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      // 流结束时把解码器里残留的字节与未闭合的尾帧一起 flush，避免丢掉最后一帧。
      buffer += decoder.decode();
      break;
    }
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
}

/** 发起一次 POST 流式运行并逐帧回调；调用方负责 abort 与状态收尾。 */
export async function streamRun(
  sessionId: string,
  userInput: string,
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal,
  requestId: string,
): Promise<void> {
  const resp = await fetch('/run/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Request-ID': requestId,
    },
    body: JSON.stringify({ session_id: sessionId, user_input: userInput }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`流式请求失败 ${resp.status}`);
  }
  await consumeStream(resp.body, onEvent);
}
