// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchRuns } from '../api/client';
import type { ConfirmDecision, PendingCall, Run, StreamEvent } from '../types';
import { clearAllHistory, loadMessages, loadPermission } from './history';
import {
  confirm,
  ensureHistory,
  listSessions,
  refreshPending,
  resetStore,
  send,
  setPermission,
  stop,
  useSession,
} from './store';

const streamRunMock = vi.hoisted(() => vi.fn());
const streamConfirmMock = vi.hoisted(() => vi.fn());
const fetchPendingMock = vi.hoisted(() => vi.fn());

vi.mock('../api/useRunStream', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/useRunStream')>();
  return { ...actual, streamRun: streamRunMock, streamConfirm: streamConfirmMock };
});

vi.mock('../api/client', () => ({ fetchRuns: vi.fn(), fetchPending: fetchPendingMock }));

type Handler = (event: StreamEvent) => void;

let handler: Handler;
let resolveRun: () => void;
let confirmHandler: Handler;
let resolveConfirm: () => void;

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
  streamConfirmMock.mockImplementation(
    (
      _runId: number,
      _session: string,
      _decisions: ConfirmDecision[],
      onEvent: Handler,
      signal: AbortSignal,
    ): Promise<void> => {
      confirmHandler = onEvent;
      return new Promise<void>((resolve, reject) => {
        resolveConfirm = resolve;
        signal.addEventListener('abort', () => reject(new Error('aborted')));
      });
    },
  );
}

/** 一条待确认调用：input 只是展示文本。 */
const CALLS: PendingCall[] = [
  { tool_call_id: 't1', name: 'Bash', input: 'rm -rf build', risk: 'danger' },
];

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
  streamConfirmMock.mockReset();
  fetchPendingMock.mockReset();
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

  it('ensureHistory 首次失败不把会话锁死，下次还能重试', async () => {
    vi.mocked(fetchRuns).mockRejectedValueOnce(new Error('500'));
    const { result } = renderHook(() => useSession('s-6'));

    await act(async () => {
      await ensureHistory('s-6');
    });
    // 失败后 hydrated 不能置位（否则下面第二次调用会被早退挡掉），只留空历史
    expect(result.current.messages).toHaveLength(0);
    expect(result.current.loading).toBe(false);
    expect(vi.mocked(fetchRuns)).toHaveBeenCalledTimes(1);

    vi.mocked(fetchRuns).mockResolvedValue({
      items: [makeRun(21, 's-6')],
      total: 1,
      limit: 200,
      offset: 0,
    });
    await act(async () => {
      await ensureHistory('s-6');
    });
    expect(vi.mocked(fetchRuns)).toHaveBeenCalledTimes(2);
    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[0].text).toBe('历史问题');
    expect(result.current.messages[1].runId).toBe(21);
  });
});

describe('权限档位', () => {
  it('切档只影响后续轮次，并落到 localStorage', () => {
    const { result } = renderHook(() => useSession('s-p'));
    act(() => setPermission('s-p', 'readonly'));
    expect(result.current.permission).toBe('readonly');
    expect(loadPermission('s-p')).toBe('readonly');

    act(() => send('s-p', 'hi'));
    // 档位只作为请求参数与会话状态存在，不再写进用户气泡
    expect('permission' in result.current.messages[0]).toBe(false);
    // 第 7 个实参才是 permission（前面依次是会话、正文、回调、signal、请求 id、附件）
    expect(streamRunMock.mock.calls[0][6]).toBe('readonly');
  });

  it('send 带档位时先落档再发，避免请求与界面不一致', () => {
    const { result } = renderHook(() => useSession('s-q'));
    act(() => send('s-q', 'hi', [], 'execute'));
    expect(result.current.permission).toBe('execute');
    expect(loadPermission('s-q')).toBe('execute');
    expect(streamRunMock.mock.calls[0][6]).toBe('execute');
  });

  it('老会话里的权限标记读历史时丢掉，消息本身保留', () => {
    localStorage.setItem(
      'da.chat.s-legacy',
      JSON.stringify([
        {
          id: 'u1',
          role: 'user',
          text: '旧问题',
          at: '2026-11-23T10:00:00',
          steps: [],
          permission: 'readonly',
        },
      ]),
    );
    const { result } = renderHook(() => useSession('s-legacy'));
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].text).toBe('旧问题');
    expect('permission' in result.current.messages[0]).toBe(false);
  });

  it('重复切同一档不产生多余提交', () => {
    const { result } = renderHook(() => useSession('s-r'));
    act(() => setPermission('s-r', 'readonly'));
    const before = result.current;
    act(() => setPermission('s-r', 'readonly'));
    // 快照对象不变：没有真实变化就不该通知订阅者
    expect(result.current).toBe(before);
  });
});

describe('挂起与确认', () => {
  /** 走完一轮挂起：确认请求 → 挂起帧 → 流结束。 */
  async function park() {
    const hook = renderHook(() => useSession('s-c'));
    act(() => send('s-c', 'hi'));
    act(() =>
      handler({
        type: 'REQUIRE_USER_CONFIRM',
        reply_id: 'r1',
        tool_call_name: 'Bash',
        tool_calls: CALLS,
      }),
    );
    act(() =>
      handler({
        type: 'RUN_PARKED',
        run_id: 42,
        expires_at: '2026-11-23T10:10:00',
        tool_calls: CALLS,
      }),
    );
    await act(async () => {
      resolveRun();
    });
    return hook;
  }

  it('挂起后不写 endedAt、不追加消息，卡片带出待确认调用', async () => {
    const { result } = await park();
    expect(result.current.running).toBe(false);
    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[1].endedAt).toBeNull();
    expect(result.current.awaiting).toMatchObject({
      runId: 42,
      expiresAt: '2026-11-23T10:10:00',
      calls: CALLS,
    });
  });

  it('确认后接着跑同一条 run，不新开一轮', async () => {
    const { result } = await park();
    const decisions: ConfirmDecision[] = [{ tool_call_id: 't1', confirmed: true, scope: 'once' }];
    act(() => confirm('s-c', decisions));

    expect(streamConfirmMock.mock.calls[0][0]).toBe(42);
    expect(streamConfirmMock.mock.calls[0][1]).toBe('s-c');
    expect(streamConfirmMock.mock.calls[0][2]).toEqual(decisions);
    // 续跑带上当前档位：挂起中切档后点允许，按新档执行
    expect(streamConfirmMock.mock.calls[0][5]).toBe('workspace');
    expect(result.current.running).toBe(true);
    expect(result.current.awaiting).toBeNull();
    expect(result.current.messages).toHaveLength(2);

    act(() => confirmHandler({ type: 'RUN_DONE', text: '做完了', run_id: 42 }));
    await act(async () => {
      resolveConfirm();
    });
    expect(result.current.messages[1].text).toBe('做完了');
    expect(result.current.messages[1].endedAt).toBeTruthy();
    expect(result.current.awaiting).toBeNull();
    expect(loadMessages('s-c')?.[1].text).toBe('做完了');
  });

  it('没有挂起态时 confirm 直接忽略，不发请求', () => {
    renderHook(() => useSession('s-n'));
    act(() => confirm('s-n', []));
    expect(streamConfirmMock).not.toHaveBeenCalled();
  });

  it('刷新后用服务端挂起态覆盖本地卡片', async () => {
    const { result } = await park();
    fetchPendingMock.mockResolvedValue({
      run_id: 42,
      status: 'parked',
      expires_at: '2026-11-23T10:20:00',
      tool_calls: CALLS,
    });
    await act(async () => {
      await refreshPending('s-c');
    });
    expect(fetchPendingMock).toHaveBeenCalledWith(42, 's-c');
    expect(result.current.awaiting?.expiresAt).toBe('2026-11-23T10:20:00');
  });

  it('挂起已失效（超时/已确认）时把卡片清掉，别留个点不动的按钮', async () => {
    const { result } = await park();
    fetchPendingMock.mockRejectedValue(new Error('请求失败 404'));
    await act(async () => {
      await refreshPending('s-c');
    });
    expect(result.current.awaiting).toBeNull();
    expect(result.current.messages[1].awaiting).toBeUndefined();
  });
});
