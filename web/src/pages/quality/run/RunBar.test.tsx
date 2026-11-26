// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ApiError,
  cancelOpenJudgeRun,
  fetchOpenJudgeBatches,
  fetchOpenJudgeLibrary,
  fetchOpenJudgeRun,
  startOpenJudgeRun,
} from '../../../api/client';
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
    fetchOpenJudgeLibrary: vi.fn(),
    fetchOpenJudgeBatches: vi.fn(),
    cancelOpenJudgeRun: vi.fn(),
  };
});

const mockStart = vi.mocked(startOpenJudgeRun);
const mockRun = vi.mocked(fetchOpenJudgeRun);
const mockLibrary = vi.mocked(fetchOpenJudgeLibrary);
const mockBatches = vi.mocked(fetchOpenJudgeBatches);
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
  mockLibrary.mockReset();
  mockBatches.mockReset();
  mockCancel.mockReset();
  mockStart.mockResolvedValue({ run_id: 11, total: 28 });
  mockRun.mockResolvedValue(makeRun());
  mockLibrary.mockResolvedValue({
    items: [
      {
        id: 'a-1',
        track: 'A',
        risk: 'low',
        permission: 'readonly',
        subject: 'repo_qa',
        fixture: 'python-basic',
        offline: false,
        source: 'repo',
        edited: false,
      },
      {
        id: 'b-07',
        track: 'B',
        risk: 'high',
        permission: 'workspace',
        subject: 'permission',
        fixture: 'python-basic',
        offline: false,
        source: 'overlay',
        edited: true,
      },
    ],
    total: 2,
    suite: { cases: 2, suite_sha: 'abcdef123456', overlay: 1, deleted: 0, dir: '/tmp/x' },
    thresholds: {},
    limits: {
      max_concurrency: 4,
      min_timeout: 30,
      max_timeout: 900,
      max_limit: 30,
      tracks: ['A', 'B'],
      risks: ['low', 'high'],
      permissions: ['readonly', 'workspace'],
    },
  });
  // 默认让 a-1 / b-07 都是历史失败用例，保持原选择器测试语义。
  mockBatches.mockResolvedValue({
    items: [
      {
        id: 1,
        verdict: 'fail',
        model: 'm',
        suite_sha: 'abcdef123456',
        plan_sha: 'p',
        started_at: '',
        finished_at: '',
        summary: {
          verdict: 'fail',
          kpi: {} as never,
          tracks: {},
          subjects: {},
          failures: [
            {
              case_id: 'a-1',
              track: 'A',
              status: 'failed',
              violations: 0,
              failed_checks: [],
              judge_dims: [],
              failure_type: 'rule',
              repro: '',
            },
            {
              case_id: 'b-07',
              track: 'B',
              status: 'failed',
              violations: 0,
              failed_checks: [],
              judge_dims: [],
              failure_type: 'rule',
              repro: '',
            },
          ],
          pareto: { rule: [], judge: [] },
        },
      },
    ],
    total: 1,
    limit: 20,
    offset: 0,
  });
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

  it('选了用例后只提交 case_ids 与 judge，赛道和条数禁用', async () => {
    renderBar();
    fireEvent.click(screen.getByText('选择用例'));

    fireEvent.click(await screen.findByRole('checkbox', { name: /a-1/ }));
    const track = screen.getByLabelText(/赛道/) as HTMLSelectElement;
    const limit = screen.getByLabelText(/条数上限/) as HTMLInputElement;
    expect(track.disabled).toBe(true);
    expect(limit.disabled).toBe(true);
    expect(screen.getByText(/已选 1\/30/)).toBeTruthy();

    fireEvent.click(screen.getByText('开始运行'));
    await waitFor(() =>
      expect(mockStart).toHaveBeenCalledWith({ case_ids: ['a-1'], judge: false }),
    );
  });

  it('多选面板支持搜索与清空', async () => {
    renderBar();
    fireEvent.click(screen.getByText('选择用例'));

    await screen.findByRole('checkbox', { name: /a-1/ });
    fireEvent.change(screen.getByLabelText('搜索用例'), { target: { value: 'b-07' } });
    expect(screen.queryByRole('checkbox', { name: /a-1/ })).toBeNull();
    fireEvent.click(await screen.findByRole('checkbox', { name: /b-07/ }));
    fireEvent.click(screen.getByText('清空'));
    expect(screen.queryByRole('checkbox', { name: /b-07/ })).toBeTruthy();
    expect((screen.getByRole('checkbox', { name: /b-07/ }) as HTMLInputElement).checked).toBe(false);
  });

  it('点跑全部按 limit=0 且不带赛道发起全量评测', async () => {
    renderBar();
    fireEvent.click(screen.getByText('跑全部'));

    await waitFor(() =>
      expect(mockStart).toHaveBeenCalledWith({ track: '', limit: 0, judge: false }),
    );
  });

  it('选择器只列历史失败用例，未失败的不出现', async () => {
    mockBatches.mockResolvedValue({
      items: [
        {
          id: 2,
          verdict: 'fail',
          model: 'm',
          suite_sha: 'abcdef123456',
          plan_sha: 'p',
          started_at: '',
          finished_at: '',
          summary: {
            verdict: 'fail',
            kpi: {} as never,
            tracks: {},
            subjects: {},
            failures: [
              {
                case_id: 'a-1',
                track: 'A',
                status: 'failed',
                violations: 0,
                failed_checks: [],
                judge_dims: [],
                failure_type: 'rule',
                repro: '',
              },
            ],
            pareto: { rule: [], judge: [] },
          },
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    renderBar();
    fireEvent.click(screen.getByText('选择用例'));

    expect(await screen.findByRole('checkbox', { name: /a-1/ })).toBeTruthy();
    expect(screen.queryByRole('checkbox', { name: /b-07/ })).toBeNull();
    expect(screen.getByText(/仅显示历史批次中失败过的用例（共 1 条）/)).toBeTruthy();
  });
});
