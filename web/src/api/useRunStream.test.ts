import { describe, expect, it } from 'vitest';
import type { StreamEvent } from '../types';
import { applyEvent, consumeStream, parseFrame } from './useRunStream';
import type { StreamState } from './useRunStream';

const INITIAL: StreamState = {
  running: true,
  text: '',
  events: [],
  inputTokens: 0,
  outputTokens: 0,
  runId: null,
  error: null,
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
