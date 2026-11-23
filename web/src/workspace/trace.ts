/** 把会话消息派生为轨迹轮次：轮列表 + 每轮步骤 + 估算耗时。 */
import type { ChatMessage, ProcessEntry, TraceStep, TraceTurn } from '../types';

function toMs(value?: string | null): number {
  if (!value) return 0;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? 0 : ms;
}

function shortQuestion(text: string): string {
  return text.replace(/\s+/g, ' ').trim().slice(0, 42);
}

/** 过程步骤 → 轨迹步骤；lifecycle 与正文标记不作为轨迹条目。 */
function stepToTrace(step: ProcessEntry): TraceStep | null {
  if (step.kind === 'lifecycle' || step.kind === 'text') return null;
  const kind: TraceStep['kind'] =
    step.kind === 'error' ? 'error' : step.kind === 'thinking' ? 'text' : 'tool';
  const title = step.kind === 'thinking'
    ? '模型思考'
    : step.kind === 'control'
      ? step.label
      : step.label;
  return {
    id: step.id,
    kind,
    title,
    text: step.detail,
    at: toMs(step.at),
    durationMs: 0,
    status: step.state,
    toolCallId: step.toolCallId,
    toolName: step.toolName,
    args: step.args,
    result: step.result,
  };
}

/** 把一条助手消息并入当前轮：异常、过程步骤与正文。 */
function appendAssistant(turn: TraceTurn, message: ChatMessage): void {
  const at = toMs(message.at);
  const endedAt = toMs(message.endedAt) || at;
  turn.endedAt = Math.max(turn.endedAt, endedAt, at);
  if (message.error) {
    turn.failed = true;
    turn.items.push({
      id: `${message.id}:error`,
      kind: 'error',
      title: '运行失败',
      text: message.error,
      at: endedAt,
      durationMs: 0,
    });
  }
  for (const step of message.steps) {
    const trace = stepToTrace(step);
    if (!trace) continue;
    if (trace.kind === 'error') turn.failed = true;
    turn.items.push(trace);
  }
  if (message.text) {
    turn.items.push({
      id: `${message.id}:text`,
      kind: 'text',
      title: '模型输出',
      text: message.text,
      at: endedAt,
      durationMs: 0,
    });
  }
  if (message.stopped) turn.failed = true;
}

/**
 * 一轮 = 一条 user 消息 → 下一条 user 消息之前的全部内容。
 *
 * 耗时按「本条时间到下一段时间」估算：后端事件只带时间戳、不带起止时刻，
 * 这个间隔已足够判断慢在模型还是慢在工具。
 */
export function turnsFromMessages(messages: ChatMessage[]): TraceTurn[] {
  const turns: TraceTurn[] = [];
  let current: TraceTurn | null = null;

  const finalizeTimes = (turn: TraceTurn) => {
    turn.items.sort((a, b) => a.at - b.at);
    for (let i = 0; i < turn.items.length; i += 1) {
      const nextAt = turn.items[i + 1]?.at ?? turn.endedAt;
      turn.items[i].durationMs = Math.max(0, nextAt - turn.items[i].at);
    }
    turn.steps = turn.items.length;
    turn.tools = turn.items.filter((item) => item.kind === 'tool').length;
  };

  for (const message of messages) {
    if (message.role === 'user') {
      if (current) finalizeTimes(current);
      const at = toMs(message.at);
      current = {
        id: message.id,
        index: turns.length + 1,
        question: shortQuestion(message.text),
        userText: message.text,
        at,
        startedAt: at,
        endedAt: at,
        steps: 0,
        tools: 0,
        failed: false,
        items: [],
      };
      turns.push(current);
      continue;
    }
    if (current) appendAssistant(current, message);
  }
  if (current) finalizeTimes(current);
  return turns;
}
