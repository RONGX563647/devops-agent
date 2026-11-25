// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, cancelOpenJudgeRun, fetchOpenJudgeRun, startOpenJudgeRun } from '../../../api/client';
import type { OjRun } from '../../../types';
import { RunBar } from './RunBar';

vi.mock('../../../api/client', () => {
  class MockApiError extends Error {
    status: number;
    detail: unknown;

    constructor(message: string, status: number, detail: unknown) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.detail = detail;
    }
  }
  return {
    ApiError: MockApiError,
    startOpenJudgeRun: vi.fn(),
    fetchOpenJudgeRun: vi.fn(),
    cancelOpenJudgeRun: vi.fn(),
  };
});

const mockStart = vi.mocked(startOpenJudgeRun);
const mockRun = vi.mocked(fetchOpenJudgeRun);
const mockCancel = vi.mocked(cancelOpenJudgeRun);

function makeRun(overrides: Partial<OjRun> = {}): OjRun {
  return {
    id: 11,
    status: 'running',
    track: 'A',
    total: 28,
    done: 12,
    judge: false,
    batch_id: null,
    error: '',
    elapsed_ms: 3000,
    current_case: 'd-07',
    ...overrides,
  };
}

function renderBar(onFinished = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  render(
    <QueryClientProvider client={client}>
      <RunBar onFinished={onFinished} />
    </QueryClientProvider>,
  );
  return { onFinished, invalidate };
}

beforeEach(() => {
  mockStart.mockReset();
  mockRun.mockReset();
  mockCancel.mockReset();
  mockStart.mockResolvedValue({ run_id: 11, total: 28 });
  mockRun.mockResolvedValue(makeRun());
});

afterEach(cleanup);

describe('RunBar', () => {
  it('点开始运行按默认参数发起评测并显示进度', async () => {
    renderBar();
    fireEvent.click(screen.getByText('开始运行'));

    await waitFor(() =>
      expect(mockStart).toHaveBeenCalledWith({ track: 'A', limit: 10, judge: false }),
    );
    expect(await screen.findByText(/已完成 12\/28 · 当前 d-07/)).toBeTruthy();
    expect(screen.getByText('取消')).toBeTruthy();
  });

  it('409 时挂到已有任务上继续轮询，而不是报错', async () => {
    mockStart.mockRejectedValue(
      new ApiError('请求失败 409', 409, { message: '已有任务在跑', run_id: 77 }),
    );
    mockRun.mockResolvedValue(makeRun({ id: 77, done: 3, total: 20, current_case: 'a-01' }));
    renderBar();

    fireEvent.click(screen.getByText('开始运行'));

    await waitFor(() => expect(mockRun).toHaveBeenCalledWith(77));
    expect(await screen.findByText(/已接上 #77 的进度/)).toBeTruthy();
    expect(screen.queryByText(/发起失败/)).toBeNull();
  });

  it('运行中每 2 秒轮询一次进度', async () => {
    renderBar();
    fireEvent.click(screen.getByText('开始运行'));

    await waitFor(() => expect(mockRun).toHaveBeenCalled());
    await waitFor(() => expect(mockRun.mock.calls.length).toBeGreaterThanOrEqual(2), {
      timeout: 4000,
    });
  });

  it('完成后刷新批次缓存并切到新批次', async () => {
    mockStart.mockResolvedValue({ run_id: 12, total: 5 });
    mockRun.mockResolvedValue(
      makeRun({ id: 12, status: 'done', total: 5, done: 5, batch_id: 9, current_case: '' }),
    );
    const { onFinished, invalidate } = renderBar();

    fireEvent.click(screen.getByText('开始运行'));

    await waitFor(() => expect(onFinished).toHaveBeenCalledWith(9));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['oj-batches'] });
  });

  it('失败时显示原因并给出重试按钮', async () => {
    mockStart.mockResolvedValue({ run_id: 13, total: 5 });
    mockRun.mockResolvedValue(
      makeRun({ id: 13, status: 'failed', total: 5, done: 2, error: '模型额度不足' }),
    );
    renderBar();

    fireEvent.click(screen.getByText('开始运行'));

    expect(await screen.findByText(/失败：模型额度不足/)).toBeTruthy();
    expect(screen.getByText('重试')).toBeTruthy();
  });

  it('取消按钮调用取消端点', async () => {
    mockCancel.mockResolvedValue({ id: 11, status: 'cancelled' });
    renderBar();
    fireEvent.click(screen.getByText('开始运行'));

    fireEvent.click(await screen.findByText('取消'));
    await waitFor(() => expect(mockCancel).toHaveBeenCalledWith(11));
  });
});
