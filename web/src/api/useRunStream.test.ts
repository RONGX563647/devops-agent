import { describe, expect, it } from 'vitest';
import type { StreamEvent } from '../types';
import { applyEvent, consumeStream, parseFrame } from './useRunStream';
import { INITIAL_STATE } from './useRunStream';
import type { StreamState } from './useRunStream';

const INITIAL: StreamState = {
  ...INITIAL_STATE,
  running: true,
};

describe('输入解析事件', () => {
  it('折叠为一条 parse 步骤：START 建行、STAGE 追加、END 收尾', () => {
    let state = applyEvent(INITIAL, { type: 'INPUT_PARSE_START', block_id: 'input-parse' });
    expect(state.steps).toHaveLength(1);
    expect(state.steps[0]).toMatchObject({ kind: 'parse', label: '输入解析', state: 'running' });
    expect(state.steps[0].parseStages).toEqual([]);

    state = applyEvent(state, {
      type: 'INPUT_PARSE_STAGE',
      block_id: 'input-parse',
      stage: 'normalize',
      stage_label: '归一化',
      state: 'ok',
      summary: 'CRLF → LF',
      before: 10,
      after: 8,
      changed: true,
    });
    state = applyEvent(state, {
      type: 'INPUT_PARSE_STAGE',
      block_id: 'input-parse',
      stage: 'budget',
      stage_label: '预算裁剪',
      state: 'skip',
      summary: '未超预算',
      changed: false,
    });
    expect(state.steps[0].parseStages?.map((item) => item.stage)).toEqual(['normalize', 'budget']);
    // 父行摘要只算生效的那一个阶段
    expect(state.steps[0].detail).toBe('1/2 阶段生效');

    state = applyEvent(state, {
      type: 'INPUT_PARSE_END',
      block_id: 'input-parse',
      summary: '6 阶段 · 90 → 80 字 · ≈ 20 token',
    });
    expect(state.steps[0].state).toBe('success');
    expect(state.steps[0].detail).toBe('6 阶段 · 90 → 80 字 · ≈ 20 token');
  });

  it('同一阶段重复到达时覆盖而不是追加', () => {
    let state = applyEvent(INITIAL, { type: 'INPUT_PARSE_START', block_id: 'b1' });
    state = applyEvent(state, {
      type: 'INPUT_PARSE_STAGE',
      block_id: 'b1',
      stage: 'redact',
      stage_label: '密钥脱敏',
      state: 'skip',
      summary: '未发现凭据',
    });
    state = applyEvent(state, {
      type: 'INPUT_PARSE_STAGE',
      block_id: 'b1',
      stage: 'redact',
      stage_label: '密钥脱敏',
      state: 'ok',
      summary: '1 处凭据已替换为占位符',
      items: 1,
      changed: true,
    });
    expect(state.steps[0].parseStages).toHaveLength(1);
    expect(state.steps[0].detail).toContain('1 脱敏');
  });

  it('解析阶段缺字段时给出安全缺省值', () => {
    const state = applyEvent(INITIAL, {
      type: 'INPUT_PARSE_STAGE',
      block_id: 'b2',
      stage: 'expand',
    });
    expect(state.steps[0].parseStages?.[0]).toMatchObject({
      stage: 'expand',
      status: 'ok',
      changed: false,
      failed: 0,
    });
  });
});

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
  it('累积文本 delta，并只计数不累积事件数组', () => {
    const next = applyEvent(INITIAL, { type: 'TEXT_BLOCK_DELTA', delta: 'hi' });
    expect(next.text).toBe('hi');
    expect(next.eventCount).toBe(1);
    // 正文不再额外写一条 text 步骤，避免双份
    expect(next.steps).toHaveLength(0);
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

  it('等待确认的事件必须带工具名并标记为错误，不能静默', () => {
    const next = applyEvent(INITIAL, {
      type: 'REQUIRE_USER_CONFIRM',
      reply_id: 'r1',
      tool_call_name: 'Bash',
    });
    expect(next.steps[0]).toMatchObject({
      kind: 'control',
      label: '需要确认 · Bash',
      state: 'error',
    });
    expect(next.steps[0].detail).toContain('无人值守');
  });

  it('确认事件缺工具名时仍给出可读提示', () => {
    const next = applyEvent(INITIAL, { type: 'REQUIRE_USER_CONFIRM', reply_id: 'r1' });
    expect(next.steps[0].label).toBe('需要确认');
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

  it('TOOL_CALL 多工具按 tool_call_id 配对，入参与返回分开累积', () => {
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
    s = applyEvent(s, { type: 'TOOL_RESULT_TEXT_DELTA', tool_call_id: 't1', delta: 'a.txt' });
    s = applyEvent(s, { type: 'TOOL_RESULT_END', tool_call_id: 't1', state: 'success' });
    s = applyEvent(s, { type: 'TOOL_RESULT_END', tool_call_id: 't2', state: 'failed' });
    expect(s.steps).toHaveLength(2);
    expect(s.steps[0]).toMatchObject({
      kind: 'tool',
      label: '调用工具 · bash',
      args: 'ls -la',
      result: 'a.txt',
      state: 'success',
      toolCallId: 't1',
    });
    expect(s.steps[1].state).toBe('error');
  });

  it('工具增量缺少 tool_call_id 时回落到最近一个执行中的工具', () => {
    let s = applyEvent(INITIAL, {
      type: 'TOOL_CALL_START',
      tool_call_id: 't9',
      tool_call_name: 'bash',
    });
    s = applyEvent(s, { type: 'TOOL_CALL_DELTA', delta: 'pwd' });
    expect(s.steps[0].args).toBe('pwd');
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
    expect(next.eventCount).toBe(1);
  });

  it('未知事件类型不改步骤、只计数', () => {
    const next = applyEvent(INITIAL, { type: 'CUSTOM' });
    expect(next.steps).toHaveLength(0);
    expect(next.eventCount).toBe(1);
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

  it('流结束时 flush 未闭合的尾帧', async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"type":"RUN_DONE","text":"tail"}'));
        controller.close();
      },
    });
    const events: StreamEvent[] = [];
    await consumeStream(body, (event) => events.push(event));
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ type: 'RUN_DONE', text: 'tail' });
  });

  it('注释心跳帧被忽略', async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(': ping\n\n'));
        controller.enqueue(encoder.encode('data: {"type":"RUN_DONE"}\n\n'));
        controller.close();
      },
    });
    const events: StreamEvent[] = [];
    await consumeStream(body, (event) => events.push(event));
    expect(events.map((event) => event.type)).toEqual(['RUN_DONE']);
  });
});
