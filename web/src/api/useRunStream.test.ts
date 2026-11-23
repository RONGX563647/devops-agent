import { describe, expect, it } from 'vitest';
import type { StreamEvent } from '../types';
import { applyEvent, consumeStream, parseFrame } from './useRunStream';
import { INITIAL_STATE } from './useRunStream';
import type { StreamState } from './useRunStream';

const INITIAL: StreamState = {
  ...INITIAL_STATE,
  running: true,
};

describe('parseFrame', () => {
  it('兼容 CRLF 并解析 JSON', () => {
    expect(parseFrame('data: {"type":"RUN_DONE","text":"ok"}\r\n\r\n')).toEqual({
      type: 'RUN_DONE',
      text: 'ok',
    });
  });

  it('忽略非 data 行', () => {
    expect(parseFrame('event: message\ndata: {"type":"RUN_DONE"}\n\n')).toEqual({
      type: 'RUN_DONE',
    });
  });

  it('非法 JSON 返回 null', () => {
    expect(parseFrame('data: not-json')).toBeNull();
  });

  it('无 data 行返回 null', () => {
    expect(parseFrame('event: ping')).toBeNull();
  });
});

describe('applyEvent', () => {
  it('累积文本 delta', () => {
    const next = applyEvent(INITIAL, { type: 'TEXT_BLOCK_DELTA', delta: 'hi' });
    expect(next.text).toBe('hi');
    expect(next.events).toHaveLength(1);
  });

  it('RUN_DONE 收尾并填充统计', () => {
    const next = applyEvent(INITIAL, {
      type: 'RUN_DONE',
      text: 'done',
      run_id: 7,
      input_tokens: 3,
      output_tokens: 4,
    });
    expect(next).toMatchObject({
      running: false,
      text: 'done',
      runId: 7,
      inputTokens: 3,
      outputTokens: 4,
    });
  });

  it('RUN_ERROR 记录错误', () => {
    expect(applyEvent(INITIAL, { type: 'RUN_ERROR' }).error).toBe('未知错误');
  });

  it('HINT_BLOCK 标注为 lifecycle 且带 hint 文案', () => {
    const next = applyEvent(INITIAL, {
      type: 'HINT_BLOCK',
      block_id: 'b1',
      hint: '已完成分析',
      source: 'reasoning',
    });
    expect(next.steps).toHaveLength(1);
    expect(next.steps[0]).toMatchObject({
      kind: 'lifecycle',
      label: '分析提示 · reasoning',
      detail: '已完成分析',
      state: 'success',
    });
  });

  it('THINKING_BLOCK 三段式：开段 → delta 累积 → END 收尾为同一条', () => {
    let s = applyEvent(INITIAL, { type: 'THINKING_BLOCK_START', block_id: 'b2' });
    s = applyEvent(s, { type: 'THINKING_BLOCK_DELTA', block_id: 'b2', delta: '先' });
    s = applyEvent(s, { type: 'THINKING_BLOCK_DELTA', block_id: 'b2', delta: '看画面' });
    expect(s.steps).toHaveLength(1);
    expect(s.steps[0].detail).toBe('先看画面');
    expect(s.steps[0].state).toBe('running');
    s = applyEvent(s, { type: 'THINKING_BLOCK_END', block_id: 'b2' });
    expect(s.steps).toHaveLength(1);
    expect(s.steps[0].state).toBe('success');
  });

  it('TOOL_CALL 多工具按 tool_call_id 配对，RESULT_END 标记成败', () => {
    let s = applyEvent(INITIAL, {
      type: 'TOOL_CALL_START',
      tool_call_id: 't1',
      tool_call_name: 'bash',
    });
    s = applyEvent(s, {
      type: 'TOOL_CALL_START',
      tool_call_id: 't2',
      tool_call_name: 'read_file',
    });
    s = applyEvent(s, { type: 'TOOL_CALL_DELTA', tool_call_id: 't1', delta: 'ls -la' });
    s = applyEvent(s, { type: 'TOOL_RESULT_END', tool_call_id: 't1', state: 'success' });
    s = applyEvent(s, { type: 'TOOL_RESULT_END', tool_call_id: 't2', state: 'failed' });
    expect(s.steps).toHaveLength(2);
    expect(s.steps[0]).toMatchObject({
      kind: 'tool',
      label: '调用工具 · bash',
      detail: 'ls -la',
      state: 'success',
      toolCallId: 't1',
    });
    expect(s.steps[1].state).toBe('error');
  });

  it('MODEL_CALL 收集模型名与 token，EXCEED_MAX_ITERS 标注为 error', () => {
    let s = applyEvent(INITIAL, {
      type: 'MODEL_CALL_START',
      reply_id: 'r1',
      model_name: 'qwen3.8-flash',
    });
    expect(s.modelName).toBe('qwen3.8-flash');
    expect(s.steps[0]).toMatchObject({ kind: 'lifecycle', state: 'running' });
    s = applyEvent(s, {
      type: 'MODEL_CALL_END',
      reply_id: 'r1',
      input_tokens: 12,
      output_tokens: 34,
    });
    expect(s.inputTokens).toBe(12);
    expect(s.outputTokens).toBe(34);
    expect(s.steps[0].state).toBe('success');
    s = applyEvent(s, { type: 'EXCEED_MAX_ITERS', reply_id: 'r1', name: 'devops' });
    expect(s.steps[1]).toMatchObject({ kind: 'error', label: '达到最大迭代次数', state: 'error' });
  });

  it('HINT_BLOCK 过滤系统提醒类噪音', () => {
    const next = applyEvent(INITIAL, {
      type: 'HINT_BLOCK',
      block_id: 'b9',
      hint: '<system-reminder>current time ...</system-reminder>',
    });
    expect(next.steps).toHaveLength(0);
    expect(next.events).toHaveLength(1);
  });

  it('未知事件类型不改步骤、只入 events', () => {
    const next = applyEvent(INITIAL, { type: 'CUSTOM' });
    expect(next.steps).toHaveLength(0);
    expect(next.events).toHaveLength(1);
  });
});

describe('consumeStream', () => {
  it('跨 chunk 的帧能正确切分', async () => {
    const encoder = new TextEncoder();
    const chunks = [
      'data: {"type":"TEXT_BLOCK_DELTA","delta":"a"}\n\n',
      'data: {"type":"TEXT_BLOCK_DELTA","delta":"b"}\n',
      '\ndata: {"type":"RUN_DONE","text":"ab"}\n\n',
    ];
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    });
    const events: StreamEvent[] = [];
    await consumeStream(body, (event) => events.push(event));
    expect(events.map((event) => event.type)).toEqual([
      'TEXT_BLOCK_DELTA',
      'TEXT_BLOCK_DELTA',
      'RUN_DONE',
    ]);
  });
});
