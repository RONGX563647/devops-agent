// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  fetchEvalBatches,
  fetchEvalCases,
  fetchEvalCompare,
  fetchEvalMatrix,
  fetchEvalSeries,
  fetchEvalSuites,
} from '../api/client';
import type { EvalBatch, EvalSuite } from '../types';
import { Quality } from './Quality';

vi.mock('../api/client', () => ({
  fetchEvalSuites: vi.fn(),
  fetchEvalSeries: vi.fn(),
  fetchEvalBatches: vi.fn(),
  fetchEvalBatch: vi.fn(),
  fetchEvalCases: vi.fn(),
  fetchEvalCompare: vi.fn(),
  fetchEvalMatrix: vi.fn(),
}));

const mockSuites = vi.mocked(fetchEvalSuites);
const mockSeries = vi.mocked(fetchEvalSeries);
const mockBatches = vi.mocked(fetchEvalBatches);
const mockCases = vi.mocked(fetchEvalCases);
const mockCompare = vi.mocked(fetchEvalCompare);
const mockMatrix = vi.mocked(fetchEvalMatrix);

function makeBatch(overrides: Partial<EvalBatch> = {}): EvalBatch {
  return {
    id: 7,
    suite: 'devops-core',
    suite_version: '1.0.0',
    suite_sha: 'abc12345',
    prompt_id: 'agent-system',
    prompt_hash: 'abcdef0123456789',
    model: 'qwen',
    profile: 'readonly',
    started_at: '2026-11-24T10:00:00Z',
    finished_at: '2026-11-24T10:05:00Z',
    status: 'completed',
    n: 15,
    passed: 11,
    pass_rate: 0.733,
    hard_failures: {
      safety: 0,
      forbidden_tool: 0,
      status: 0,
      tool: 0,
      text: 0,
      grounding: 0,
      format: 0,
    },
    hard_blocking: 0,
    hard_scoring: 0,
    hard_labels: {},
    judge: { correctness: 3.6 },
    trajectory_score: 0.81,
    p95_ms: 4200,
    tokens_total: 1500,
    cache_input: 0,
    cache_creation: 0,
    cache_hit_rate: null,
    cost_total: 0,
    baseline_id: null,
    verdict: 'insufficient',
    gates: [
      {
        key: 'pass_rate',
        value: 0.733,
        min: 0.8,
        min_samples: 0,
        samples: 15,
        state: 'insufficient',
        reason: 'n_below_gate',
        threshold_state: 'fail',
      },
    ],
    min_cases_for_gate: 40,
    mde: null,
    compatibility: null,
    grader_version: 'v2',
    ...overrides,
  };
}

function makeSuite(): EvalSuite {
  return {
    suite: 'devops-core',
    version: '1.0.0',
    sha256: 'abcdef0123456789',
    counts: { common: 9, variant: 4, edge: 1, adversarial: 1 },
    min_cases_for_gate: 40,
    grader_version: 'v2',
    thresholds: { pass_rate: { min: 0.8, min_samples: 0 } },
    assertions: { must_cite: 2, tool_must: 2, dead: 1 },
    subjects: { prompt: 1, capability: 1 },
    judged_cases: 1,
    cases: [
      {
        id: 'c-001-read-app',
        tags: ['scenario:common'],
        permission: 'readonly',
        subject: 'prompt',
        require_executed: true,
        command_hint: null,
        decision: 'ok',
        problem: null,
        assertions: [
          { kind: 'must_cite', target: 'app.py' },
          { kind: 'tool_must', target: 'Read' },
        ],
      },
      {
        id: 'c-003-run-tests',
        tags: ['scenario:common'],
        permission: 'readonly',
        subject: 'capability',
        require_executed: true,
        command_hint: 'pytest -q',
        decision: 'mismatch',
        problem: '档位拒绝该命令',
        assertions: [{ kind: 'tool_must', target: 'Bash' }],
      },
    ],
  };
}

function renderQuality() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Quality />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  mockSuites.mockReset();
  mockSeries.mockReset();
  mockBatches.mockReset();
  mockCases.mockReset();
  mockCompare.mockReset();
  mockMatrix.mockReset();

  mockSuites.mockResolvedValue({ items: [makeSuite()] });
  mockSeries.mockResolvedValue({
    items: [
      {
        id: 6,
        started_at: '2026-11-23T10:00:00Z',
        n: 15,
        pass_rate: 0.667,
        judge_correctness: 3.4,
        trajectory_score: 0.8,
        p95_ms: 4100,
        hard_total: 0,
        hard_blocking: 0,
        hard_scoring: 0,
        cache_hit_rate: null,
        verdict: 'insufficient',
        prompt_hash: 'aaaa',
      },
      {
        id: 7,
        started_at: '2026-11-24T10:00:00Z',
        n: 15,
        pass_rate: 0.733,
        judge_correctness: 3.6,
        trajectory_score: 0.81,
        p95_ms: 4200,
        hard_total: 0,
        hard_blocking: 0,
        hard_scoring: 0,
        cache_hit_rate: null,
        verdict: 'insufficient',
        prompt_hash: 'bbbb',
      },
    ],
  });
  mockBatches.mockResolvedValue({ items: [makeBatch()], total: 1 });
  mockCases.mockResolvedValue({ items: [], total: 0 });
  mockMatrix.mockResolvedValue({ batches: [], cases: [] });
  mockCompare.mockResolvedValue({
    verdict: 'pass',
    metrics: [],
    cases: [],
    n: 15,
    base_n: 15,
    mde: 0.0667,
    compatibility: { level: 'clean', changed: [], missing: [], reasons: ['一致'] },
  });
});

afterEach(cleanup);

describe('Quality 三视图', () => {
  it('默认落在概览，样本不足要给出原因而不是只留一个徽标', async () => {
    renderQuality();

    expect(await screen.findByText(/样本不足，不参与发布门禁/)).toBeTruthy();
    expect(screen.getByText(/样本 15 < 40/)).toBeTruthy();
    // 线上反馈视图已移除
    expect(screen.queryByText('线上反馈')).toBeNull();
    expect(screen.getByText('概览')).toBeTruthy();
  });

  it('概览的趋势结论带精度上限，不把抖动说成变化', async () => {
    renderQuality();

    expect(
      await screen.findByText(/精度上限 6\.7pp，变化在抖动范围内/),
    ).toBeTruthy();
  });

  it('评测集视图展示配比与恒真断言告警', async () => {
    renderQuality();
    fireEvent.click(screen.getByText('评测集'));

    expect(await screen.findByText('devops-core@1.0.0')).toBeTruthy();
    // 断言总数由 assertions 各项相加得出：must_cite 2 + tool_must 2 + dead 1 = 5
    expect(screen.getByText(/有 1 \/ 5 条断言恒真/)).toBeTruthy();
    expect(screen.getByText(/共 2 条用例/)).toBeTruthy();
  });

  it('展开用例可以看到它断言了什么', async () => {
    renderQuality();
    fireEvent.click(screen.getByText('评测集'));
    fireEvent.click(await screen.findByText('c-001-read-app'));

    expect(screen.getByText('行号定位')).toBeTruthy();
    expect(screen.getByText('app.py')).toBeTruthy();
  });

  it('评测批次视图保留明细入口', async () => {
    renderQuality();
    fireEvent.click(screen.getByText('评测批次'));

    expect(await screen.findByText('#7')).toBeTruthy();
  });
});
