// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  fetchEvalBatch,
  fetchEvalBatches,
  fetchEvalCases,
  fetchEvalCompare,
  fetchEvalMatrix,
} from '../api/client';
import type { EvalBatch, EvalCaseItem, EvalGate } from '../types';
import { EvalBatches } from './EvalBatches';

vi.mock('../api/client', () => ({
  fetchEvalBatches: vi.fn(),
  fetchEvalBatch: vi.fn(),
  fetchEvalCases: vi.fn(),
  fetchEvalCompare: vi.fn(),
  fetchEvalMatrix: vi.fn(),
}));

const mockBatches = vi.mocked(fetchEvalBatches);
const mockBatch = vi.mocked(fetchEvalBatch);
const mockCases = vi.mocked(fetchEvalCases);
const mockCompare = vi.mocked(fetchEvalCompare);
const mockMatrix = vi.mocked(fetchEvalMatrix);

/** 可比性默认是 clean，个别用例需要不可比时再覆盖。 */
const CLEAN_COMPAT = {
  level: 'clean' as const,
  changed: [] as string[],
  missing: [] as string[],
  reasons: ['所有因子一致，可作为复测样本估计噪声'],
};

/** 7 类硬失败全 0：后端总是下发 7 键，测试夹具也保持一致。 */
const NO_HARD = {
  safety: 0,
  forbidden_tool: 0,
  status: 0,
  tool: 0,
  text: 0,
  grounding: 0,
  format: 0,
} as const;

function makeGate(over: Partial<EvalGate> & Pick<EvalGate, 'key'>): EvalGate {
  return {
    value: 0.9,
    min: 0.8,
    min_samples: 0,
    samples: 100,
    state: 'pass',
    reason: null,
    threshold_state: 'pass',
    ...over,
  };
}

function makeBatch(overrides: Partial<EvalBatch> = {}): EvalBatch {
  return {
    id: 1,
    suite: 'devops-core',
    suite_version: 'v2',
    suite_sha: 'abc123',
    prompt_id: 'p1',
    prompt_hash: '0123456789abcdef',
    model: 'qwen',
    profile: 'workspace',
    started_at: '2026-11-24T10:00:00Z',
    finished_at: '2026-11-24T10:05:00Z',
    status: 'finished',
    n: 100,
    passed: 90,
    pass_rate: 0.9,
    hard_failures: { ...NO_HARD },
    hard_blocking: 0,
    hard_scoring: 0,
    hard_labels: {},
    judge: { correctness: 4.2 },
    trajectory_score: 0.88,
    p95_ms: 1200,
    tokens_total: 1000,
    cost_total: 0.1,
    baseline_id: null,
    verdict: 'pass',
    gates: [
      makeGate({ key: 'pass_rate' }),
      makeGate({ key: 'judge_correctness', value: 4.2, min: 4, min_samples: 10, samples: 20 }),
    ],
    min_cases_for_gate: 40,
    mde: 0.01,
    compatibility: null,
    grader_version: 'v2',
    ...overrides,
  };
}

function makeCase(overrides: Partial<EvalCaseItem> = {}): EvalCaseItem {
  return {
    case_id: 'case-a',
    status: 'passed',
    passed: true,
    tags: ['safety'],
    trajectory_score: 0.9,
    judge: { correctness: 4.6 },
    latency_ms: 800,
    tokens: 120,
    checks: [{ name: 'no_side_effect', ok: true, detail: '未产生副作用' }],
    run_id: 1,
    tool_names: [],
    executed_tools: [],
    denied_tools: [],
    hard_failures: [],
    error: null,
    ...overrides,
  };
}

function renderBatches() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <EvalBatches />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  mockBatches.mockReset();
  mockBatch.mockReset();
  mockCases.mockReset();
  mockCompare.mockReset();
  mockMatrix.mockReset();
  mockMatrix.mockResolvedValue({ batches: [], cases: [] });
  mockCompare.mockResolvedValue({
    verdict: 'pass',
    metrics: [],
    cases: [],
    n: 100,
    base_n: 100,
    mde: 0.01,
    compatibility: CLEAN_COMPAT,
  });
});

afterEach(cleanup);

describe('EvalBatches', () => {
  it('空态显示引导与 CLI 提示', async () => {
    mockBatches.mockResolvedValue({ items: [], total: 0 });
    renderBatches();

    expect(await screen.findByText('还没有评测批次')).toBeTruthy();
    expect(screen.getByText(/devops_agent\.eval run/)).toBeTruthy();
  });

  it('错误态点击重试会重新拉取', async () => {
    mockBatches
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce({ items: [], total: 0 });
    renderBatches();

    expect(await screen.findByText('评测批次加载失败。')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));

    expect(await screen.findByText('还没有评测批次')).toBeTruthy();
    expect(mockBatches).toHaveBeenCalledTimes(2);
  });

  it('批次列表渲染 suite、prompt hash 前 8 位、硬门槛与 verdict', async () => {
    const first = makeBatch({ id: 1, suite: 'devops-core', prompt_hash: '0123456789abcdef' });
    const second = makeBatch({ id: 2, suite: 'safety-suite', verdict: 'warn' });
    mockBatches.mockResolvedValue({ items: [first, second], total: 2 });
    renderBatches();

    const row = (await screen.findByText('#1')).closest('tr');
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText('devops-core@v2')).toBeTruthy();
    expect(within(row as HTMLElement).getByText(/p1 · 01234567/)).toBeTruthy();
    expect(within(row as HTMLElement).getByText('✓')).toBeTruthy();
    expect(within(row as HTMLElement).getByText('通过')).toBeTruthy();
    expect(screen.getByText('safety-suite@v2')).toBeTruthy();
    expect(screen.getByText('警告')).toBeTruthy();
  });

  it('n<40 时指标卡标样本不足且不染色', async () => {
    const batch = makeBatch({ n: 10, passed: 8, pass_rate: 0.8 });
    mockBatches.mockResolvedValue({ items: [batch], total: 1 });
    mockBatch.mockResolvedValue({ batch, tags: {}, subjects: {}, cases: [] });
    mockCases.mockResolvedValue({ items: [], total: 0 });
    renderBatches();

    fireEvent.click(await screen.findByText('#1'));
    expect(await screen.findByText('全部硬失败分类均为 0')).toBeTruthy();

    expect(screen.getAllByText('样本不足')).toHaveLength(5);
    expect(document.querySelector('.eval-metrics .tone-good')).toBeNull();
    expect(document.querySelector('.eval-metrics .tone-bad')).toBeNull();
  });

  it('硬门槛 >0 时展示红色不建议合并横幅', async () => {
    const batch = makeBatch({
      hard_failures: { ...NO_HARD, safety: 1 },
      hard_blocking: 1,
      verdict: 'fail',
    });
    mockBatches.mockResolvedValue({ items: [batch], total: 1 });
    mockBatch.mockResolvedValue({ batch, tags: {}, subjects: {}, cases: [] });
    mockCases.mockResolvedValue({ items: [], total: 0 });
    renderBatches();

    fireEvent.click(await screen.findByText('#1'));

    expect(await screen.findByText(/不建议合并/)).toBeTruthy();
    expect(document.querySelector('.hard-gate-banner.is-bad')).not.toBeNull();
  });

  it('对比表按 better 方向给 delta 上色', async () => {
    const base = makeBatch({ id: 1, suite: 'devops-core' });
    const head = makeBatch({ id: 2, suite: 'devops-core' });
    mockBatches.mockResolvedValue({ items: [base, head], total: 2 });
    mockCompare.mockResolvedValue({
      verdict: 'warn',
      metrics: [
        { key: 'pass_rate', base: 0.8, head: 0.9, delta: 0.1, better: 'higher' },
        { key: 'p95_ms', base: 120, head: 100, delta: -20, better: 'lower' },
        { key: 'tokens', base: 100, head: 140, delta: 40, better: 'lower' },
      ],
      cases: [
        { case_id: 'c1', base_passed: true, head_passed: false },
        { case_id: 'c2', base_passed: false, head_passed: true },
        { case_id: 'c3', base_passed: true, head_passed: true },
      ],
      n: 2,
      base_n: 2,
      mde: 0.5,
      compatibility: CLEAN_COMPAT,
    });
    renderBatches();

    const rateRow = (await screen.findByText('pass_rate')).closest('tr') as HTMLElement;
    expect(within(rateRow).getByText('+0.1').className).toContain('delta-good');

    const p95Row = screen.getByText('p95_ms').closest('tr') as HTMLElement;
    expect(within(p95Row).getByText('-20').className).toContain('delta-good');

    const tokenRow = screen.getByText('tokens').closest('tr') as HTMLElement;
    expect(within(tokenRow).getByText('+40').className).toContain('delta-bad');

    expect(document.querySelector('.compare-banner.verdict-warn')).not.toBeNull();
    expect(screen.getByText('变化用例（2）')).toBeTruthy();
    expect(screen.queryByText('c3')).toBeNull();
    expect(mockCompare).toHaveBeenCalledWith(1, 2);
  });

  it('用例筛选把 status/tag 传给接口，搜索与展开失败明细生效', async () => {
    const batch = makeBatch({ id: 7 });
    const passed = makeCase({ case_id: 'case-a' });
    const failed = makeCase({
      case_id: 'case-b',
      status: 'failed',
      passed: false,
      checks: [{ name: 'forbidden_tool', ok: false, detail: '调用危险工具' }],
    });
    mockBatches.mockResolvedValue({ items: [batch], total: 1 });
    mockBatch.mockResolvedValue({
      batch,
      tags: { safety: { n: 2, passed: 1 }, perf: { n: 3, passed: 3 } },
      subjects: { prompt: { n: 2, passed: 1 } },
      cases: [passed, failed],
    });
    mockCases.mockResolvedValue({ items: [passed, failed], total: 2 });
    renderBatches();

    fireEvent.click(await screen.findByText('#7'));
    expect(await screen.findByText('case-a')).toBeTruthy();

    fireEvent.change(screen.getByLabelText('用例状态'), { target: { value: 'failed' } });
    await waitFor(() =>
      expect(mockCases).toHaveBeenLastCalledWith(
        7,
        expect.objectContaining({ status: 'failed', limit: 50, offset: 0 }),
      ),
    );

    fireEvent.change(screen.getByLabelText('用例标签'), { target: { value: 'safety' } });
    await waitFor(() =>
      expect(mockCases).toHaveBeenLastCalledWith(
        7,
        expect.objectContaining({ tag: 'safety' }),
      ),
    );

    fireEvent.change(screen.getByLabelText('搜索用例'), { target: { value: 'case-b' } });
    expect(screen.queryByText('case-a')).toBeNull();
    expect(screen.getByText('case-b')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '展开 case-b' }));
    expect(screen.getByText('调用危险工具')).toBeTruthy();
  });
});
