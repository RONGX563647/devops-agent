import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ParseStage, PendingCall, StreamEvent } from '../types';
import {
  applyEvent,
  consumeStream,
  parseFrame,
  parseSummary,
  streamConfirm,
  streamRun,
} from './useRunStream';
import { INITIAL_STATE } from './useRunStream';
import type { StreamState } from './useRunStream';

const INITIAL: StreamState = {
  ...INITIAL_STATE,
  running: true,
};

/** 服务端给的一条待确认调用：input 是展示文本，不是可回传的命令。 */
const CALLS: PendingCall[] = [
  { tool_call_id: 't1', name: 'Bash', input: 'rm -rf build', risk: 'danger' },
];

afterEach(() => {
  vi.restoreAllMocks();
});

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

  it('确认请求不再当错误：仍算运行中，并把待确认调用摊出来', () => {
    const next = applyEvent(INITIAL, {
      type: 'REQUIRE_USER_CONFIRM',
      reply_id: 'r1',
      tool_call_name: 'Bash',
      tool_calls: CALLS,
    });
    expect(next.running).toBe(true);
    expect(next.steps[0]).toMatchObject({
      kind: 'control',
      label: '等待确认 · Bash',
      state: 'running',
    });
    expect(next.awaiting?.calls).toEqual(CALLS);
  });

  it('确认事件缺工具名或调用列表时仍可读，且不伪造挂起态', () => {
    const next = applyEvent(INITIAL, { type: 'REQUIRE_USER_CONFIRM', reply_id: 'r1' });
    expect(next.steps[0].label).toBe('等待确认');
    expect(next.awaiting).toBeNull();
  });

  it('RUN_PARKED 结束这条流但保留挂起态，不写 endedAt', () => {
    let s = applyEvent(INITIAL, {
      type: 'REQUIRE_USER_CONFIRM',
      reply_id: 'r1',
      tool_call_name: 'Bash',
      tool_calls: CALLS,
    });
    s = applyEvent(s, {
      type: 'RUN_PARKED',
      run_id: 12,
      expires_at: '2026-11-23T10:10:00',
      tool_calls: CALLS,
    });
    expect(s.running).toBe(false);
    expect(s.endedAt).toBeNull();
    expect(s.awaiting).toMatchObject({ runId: 12, expiresAt: '2026-11-23T10:10:00' });
    expect(s.steps[0].state).toBe('success');
  });

  it('RUN_DONE 清掉挂起态，确认完不留残卡', () => {
    const parked = applyEvent(INITIAL, {
      type: 'RUN_PARKED',
      run_id: 12,
      expires_at: null,
      tool_calls: CALLS,
    });
    const next = applyEvent(parked, { type: 'RUN_DONE', text: '做完了', run_id: 12 });
    expect(next.awaiting).toBeNull();
    expect(next.running).toBe(false);
    expect(next.text).toBe('做完了');
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
    expect(s.steps[0]).toMatchObject({ kind: 'model', state: 'running' });
    s = applyEvent(s, {
      type: 'MODEL_CALL_END',
      reply_id: 'r1',
      input_tokens: 12,
      output_tokens: 34,
      cache_input_tokens: 8,
    });
    expect(s.inputTokens).toBe(12);
    expect(s.outputTokens).toBe(34);
    expect(s.cacheInputTokens).toBe(8);
    expect(s.steps[0].state).toBe('success');
    // 泳道要按模型调用画区间，token 挂在步骤上供 Inspector 展示
    expect(s.steps[0]).toMatchObject({ tokensIn: 12, tokensOut: 34, cacheTokens: 8 });
    s = applyEvent(s, { type: 'EXCEED_MAX_ITERS', reply_id: 'r1', name: 'devops' });
    expect(s.steps[1]).toMatchObject({ kind: 'error', label: '达到最大迭代次数', state: 'error' });
  });

  it('同一 reply 多次 MODEL_CALL 各占一步，首枪 token/cache 不被末枪覆盖', () => {
    let s = INITIAL_STATE;
    const calls = [
      { input: 3760, cache: 0 },
      { input: 9436, cache: 0 },
      { input: 17147, cache: 9216 },
      { input: 17524, cache: 16384 },
    ];
    for (const [i, call] of calls.entries()) {
      s = applyEvent(s, {
        type: 'MODEL_CALL_START',
        reply_id: 'r1',
        model_name: 'qwen3.8-flash',
      });
      s = applyEvent(s, {
        type: 'MODEL_CALL_END',
        reply_id: 'r1',
        input_tokens: call.input,
        output_tokens: i + 1,
        cache_input_tokens: call.cache,
      });
    }

    const modelSteps = s.steps.filter((step) => step.kind === 'model');
    expect(modelSteps).toHaveLength(4);
    expect(modelSteps.map((step) => step.tokensIn)).toEqual([3760, 9436, 17147, 17524]);
    expect(modelSteps.map((step) => step.cacheTokens)).toEqual([0, 0, 9216, 16384]);
    expect(modelSteps.every((step) => step.state === 'success')).toBe(true);
    expect(s.inputTokens).toBe(calls.reduce((sum, call) => sum + call.input, 0));
    expect(s.cacheInputTokens).toBe(16384 + 9216);
  });

  it('MODEL_CALL 的 token 逐调用累加，RUN_DONE 用后端总和覆盖', () => {
    let s = applyEvent(INITIAL, {
      type: 'MODEL_CALL_START',
      reply_id: 'r1',
      model_name: 'qwen3.8-flash',
    });
    s = applyEvent(s, {
      type: 'MODEL_CALL_END',
      reply_id: 'r1',
      input_tokens: 3060,
      output_tokens: 20,
      cache_input_tokens: 0,
    });
    s = applyEvent(s, { type: 'MODEL_CALL_START', reply_id: 'r2', model_name: 'qwen3.8-flash' });
    s = applyEvent(s, {
      type: 'MODEL_CALL_END',
      reply_id: 'r2',
      input_tokens: 9645,
      output_tokens: 30,
      cache_input_tokens: 9216,
    });
    // 只留最后一次会让命中率被低估「调用次数」倍：这里必须累加
    expect(s.inputTokens).toBe(3060 + 9645);
    expect(s.cacheInputTokens).toBe(9216);

    s = applyEvent(s, {
      type: 'RUN_DONE',
      run_id: 7,
      input_tokens: 12705,
      output_tokens: 50,
      cache_input_tokens: 9216,
      text: '完成',
    });
    expect(s.inputTokens).toBe(12705);
    expect(s.outputTokens).toBe(50);
    expect(s.cacheInputTokens).toBe(9216);
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

describe('parseSummary', () => {
  const stage = (over: Partial<ParseStage>): ParseStage => ({
    stage: 'normalize',
    label: '',
    status: 'ok',
    summary: '',
    before: 0,
    after: 0,
    items: 0,
    ms: 0,
    detail: '',
    changed: false,
    failed: 0,
    ...over,
  });

  it('只列有内容的计数，附件与引用并列', () => {
    const text = parseSummary([
      stage({ stage: 'attach', items: 2, changed: true }),
      stage({ stage: 'detect', items: 1, changed: true }),
      stage({ stage: 'redact', items: 3, changed: true }),
      stage({ stage: 'budget', failed: 1 }),
    ]);
    expect(text).toBe('4/4 阶段生效 · 2 附件 · 1 引用 · 3 脱敏 · 1 处降级');
  });

  it('全阶段无需处理时只留阶段计数', () => {
    expect(parseSummary([stage({ stage: 'normalize' }), stage({ stage: 'budget' })])).toBe(
      '0/2 阶段生效',
    );
  });
});

describe('streamRun', () => {
  function okStream(): ReadableStream<Uint8Array> {
    const encoder = new TextEncoder();
    return new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"type":"RUN_DONE","text":"ok"}\n\n'));
        controller.close();
      },
    });
  }

  it('请求体带上附件 id，正文留在服务端', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(okStream(), { status: 200 }));
    const events: StreamEvent[] = [];

    await streamRun(
      's-1',
      '看下这个文件',
      (event) => events.push(event),
      new AbortController().signal,
      'req-1',
      ['a1b2c3d4e5f60718'],
    );

    const [url, init] = spy.mock.calls[0];
    expect(url).toBe('/run/stream');
    expect(JSON.parse(String(init?.body))).toEqual({
      session_id: 's-1',
      user_input: '看下这个文件',
      attachments: ['a1b2c3d4e5f60718'],
      web_search: false,
    });
    expect(events.map((event) => event.type)).toEqual(['RUN_DONE']);
  });

  it('不传附件时字段为空数组，后端按无附件处理', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(okStream(), { status: 200 }));

    await streamRun('s-1', 'hi', () => {}, new AbortController().signal, 'req-2');

    const body = JSON.parse(String(spy.mock.calls[0][1]?.body));
    expect(body.attachments).toEqual([]);
    // 没选档位就不发字段：让服务端用默认档，前端不猜
    expect('permission' in body).toBe(false);
    // 联网必须显式表态：省略会被服务端当成「按部署默认」，
    // 部署开了检索时界面上的「关」就关不掉这一轮。
    expect(body.web_search).toBe(false);
  });

  it('选过档位才把 permission 写进请求体', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(okStream(), { status: 200 }));

    await streamRun('s-1', 'hi', () => {}, new AbortController().signal, 'req-4', [], 'readonly');

    expect(JSON.parse(String(spy.mock.calls[0][1]?.body)).permission).toBe('readonly');
  });

  it('开启联网时把 web_search=true 写进请求体', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(okStream(), { status: 200 }));

    await streamRun(
      's-1',
      'hi',
      () => {},
      new AbortController().signal,
      'req-5',
      [],
      'workspace',
      true,
    );

    expect(JSON.parse(String(spy.mock.calls[0][1]?.body)).web_search).toBe(true);
  });

  it('非 2xx 抛错，交给 store 记成中断', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 500 }));
    await expect(
      streamRun('s-1', 'hi', () => {}, new AbortController().signal, 'req-3'),
    ).rejects.toThrow('流式请求失败 500');
  });
});

describe('streamConfirm', () => {
  function okStream(): ReadableStream<Uint8Array> {
    const encoder = new TextEncoder();
    return new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"type":"RUN_DONE","text":"接上了"}\n\n'));
        controller.close();
      },
    });
  }

  it('打到同一条 run 的 confirm，只回 id 与决策', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(okStream(), { status: 200 }));
    const events: StreamEvent[] = [];

    await streamConfirm(
      12,
      's-1',
      [{ tool_call_id: 't1', confirmed: true, scope: 'once' }],
      (event) => events.push(event),
      new AbortController().signal,
    );

    const [url, init] = spy.mock.calls[0];
    expect(url).toBe('/run/12/confirm');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({
      session_id: 's-1',
      results: [{ tool_call_id: 't1', confirmed: true, scope: 'once' }],
    });
    // 客户端不回传命令原文：要执行什么以服务端挂起态为准
    expect(String(init?.body)).not.toContain('rm -rf');
    expect(events.map((event) => event.type)).toEqual(['RUN_DONE']);
  });

  it('挂起中切过档时，确认请求带上当前档位', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(okStream(), { status: 200 }));

    await streamConfirm(
      12,
      's-1',
      [{ tool_call_id: 't1', confirmed: true, scope: 'once' }],
      () => {},
      new AbortController().signal,
      'readonly',
    );

    expect(JSON.parse(String(spy.mock.calls[0][1]?.body))).toMatchObject({
      permission: 'readonly',
    });
  });

  it('非 2xx 抛错，交给 store 记成中断', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 409 }));
    await expect(
      streamConfirm(12, 's-1', [], () => {}, new AbortController().signal),
    ).rejects.toThrow('确认失败 409');
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

  it('onActivity 在每个字节 chunk 触发，注释帧也算活动', async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        // 5 个 chunk：注释帧、半个 data 帧、补齐、未闭合尾帧、关闭
        controller.enqueue(encoder.encode(': ping\n\n'));
        controller.enqueue(encoder.encode('data: {"type":"TEXT_B'));
        controller.enqueue(encoder.encode('LOCK_DELTA","delta":"a"}\n\n'));
        controller.enqueue(encoder.encode('data: {"type":"RUN_DONE"}'));
        controller.close();
      },
    });
    let activity = 0;
    await consumeStream(
      body,
      () => undefined,
      () => {
        activity += 1;
      },
    );
    // 字节层保活：每个非空 chunk 都算一次活动，与帧能否解析无关
    expect(activity).toBe(4);
  });
});
