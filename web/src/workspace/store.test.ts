// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchRuns } from '../api/client';
import type { Run, StreamEvent } from '../types';
import { clearAllHistory, loadMessages } from './history';
import {
  ensureHistory,
  listSessions,
  resetStore,
  send,
  stop,
  useSession,
} from './store';

const streamRunMock = vi.hoisted(() => vi.fn());

vi.mock('../api/useRunStream', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/useRunStream')>();
  return { ...actual, streamRun: streamRunMock };
});

vi.mock('../api/client', () => ({ fetchRuns: vi.fn() }));

type Handler = (event: StreamEvent) => void;

let handler: Handler;
let resolveRun: () => void;

function mockStream() {
  streamRunMock.mockImplementation(
    (
      _session: string,
      _text: string,
      onEvent: Handler,
      signal: AbortSignal,
    ): Promise<void> => {
      handler = onEvent;
      return new Promise<void>((resolve, reject) => {
        resolveRun = resolve;
        signal.addEventListener('abort', () => reject(new Error('aborted')));
      });
    },
  );
}

function makeRun(id: number, sessionId: string): Run {
  return {
    id,
    session_id: sessionId,
    domain: 'delivery',
    status: 'success',
    input: '历史问题',
    output: '历史回答',
    started_at: '2026-11-23T10:00:00',
    ended_at: '2026-11-23T10:00:02',
    duration_ms: 2000,
    model: 'qwen',
    total_tokens: 20,
    health_score: 80,
    input_tokens: 10,
    output_tokens: 10,
  };
}

beforeEach(() => {
  resetStore();
  clearAllHistory();
  streamRunMock.mockReset();
  vi.mocked(fetchRuns).mockReset();
  mockStream();
});

afterEach(cleanup);

describe('会话 store', () => {
  it('发送后后台累积事件，结束后落盘并保留全文', async () => {
    const { result } = renderHook(() => useSession('s-1'));
    act(() => send('s-1', '你好'));
    expect(result.current.running).toBe(true);
    expect(result.current.messages.map((m) => m.role)).toEqual(['user', 'assistant']);

    act(() => handler({ type: 'TEXT_BLOCK_DELTA', delta: '你好' }));
    act(() => handler({ type: 'TEXT_BLOCK_DELTA', delta: '，世界' }));
    expect(result.current.messages[1].text).toBe('你好，世界');

    await act(async () => {
      resolveRun();
    });
    expect(result.current.running).toBe(false);
    expect(result.current.messages[1].pending).toBe(false);
    expect(loadMessages('s-1')?.[1].text).toBe('你好，世界');
  });

  it('停止且没有产出时留下可见占位，而不是空气泡', () => {
    const { result } = renderHook(() => useSession('s-2'));
    act(() => send('s-2', 'hi'));
    act(() => stop('s-2'));
    expect(result.current.running).toBe(false);
    expect(result.current.messages[1].text).toBe('（已停止）');
    expect(result.current.messages[1].stopped).toBe(true);
  });

  it('停止后旧流事件因代次不匹配被丢弃', () => {
    const { result } = renderHook(() => useSession('s-3'));
    act(() => send('s-3', 'hi'));
    const stale = handler;
    act(() => stop('s-3'));
    act(() => stale({ type: 'TEXT_BLOCK_DELTA', delta: '迟到的内容' }));
    expect(result.current.messages[1].text).toBe('（已停止）');
  });

  it('listSessions 汇总本地会话标题与消息数', () => {
    const { result } = renderHook(() => useSession('s-4'));
    act(() => send('s-4', '  排查 CI  '));
    const sessions = listSessions();
    expect(sessions[0]).toMatchObject({ id: 's-4', messageCount: 1 });
    expect(sessions[0].title).toBe('排查 CI');
    expect(result.current.messages[0].text).toBe('  排查 CI  ');
  });

  it('ensureHistory 用后端 runs 重建只读历史', async () => {
    vi.mocked(fetchRuns).mockResolvedValue({
      items: [makeRun(11, 's-5')],
      total: 1,
      limit: 200,
      offset: 0,
    });
    const { result } = renderHook(() => useSession('s-5'));
    await act(async () => {
      await ensureHistory('s-5');
    });
    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[0].text).toBe('历史问题');
    expect(result.current.messages[1].text).toBe('历史回答');
    expect(result.current.messages[1].runId).toBe(11);
  });
});
