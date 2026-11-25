// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  fetchOpenJudgeBatch,
  fetchOpenJudgeBatches,
  fetchOpenJudgeCases,
  fetchOpenJudgeCompare,
  fetchOpenJudgeCoverage,
  fetchOpenJudgeRubrics,
  fetchOpenJudgeRun,
  fetchOpenJudgeSeries,
  fetchOpenJudgeSuites,
} from '../api/client';
import { Quality } from './Quality';

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  fetchOpenJudgeSeries: vi.fn(),
  fetchOpenJudgeSuites: vi.fn(),
  fetchOpenJudgeBatches: vi.fn(),
  fetchOpenJudgeBatch: vi.fn(),
  fetchOpenJudgeCases: vi.fn(),
  fetchOpenJudgeCase: vi.fn(),
  fetchOpenJudgeCompare: vi.fn(),
  fetchOpenJudgeCoverage: vi.fn(),
  fetchOpenJudgeRubrics: vi.fn(),
  fetchOpenJudgeRun: vi.fn(),
  startOpenJudgeRun: vi.fn(),
  cancelOpenJudgeRun: vi.fn(),
}));

const mockSeries = vi.mocked(fetchOpenJudgeSeries);
const mockSuites = vi.mocked(fetchOpenJudgeSuites);
const mockBatches = vi.mocked(fetchOpenJudgeBatches);
const mockBatch = vi.mocked(fetchOpenJudgeBatch);
const mockCases = vi.mocked(fetchOpenJudgeCases);
const mockCompare = vi.mocked(fetchOpenJudgeCompare);
const mockCoverage = vi.mocked(fetchOpenJudgeCoverage);
const mockRubrics = vi.mocked(fetchOpenJudgeRubrics);
const mockRun = vi.mocked(fetchOpenJudgeRun);

function makeOjBatch() {
  return {
    id: 3,
    verdict: 'pass' as const,
    model: 'deepseek-v4.1-flash',
    suite_sha: 'suite123456789',
    plan_sha: 'plan123456789',
    started_at: '2026-11-25T06:00:00Z',
    finished_at: '2026-11-25T06:10:00Z',
    summary: {
      verdict: 'pass' as const,
      kpi: { cases: 90, passed: 80, judge_n: 60, violations: 0 },
      tracks: {
        A: {
          n: 28,
          passed: 24,
          pass_rate: 0.857,
          judge_avg: 0.72,
          violations: 0,
          verdict: 'pass' as const,
        },
      },
      subjects: {},
      failures: [
        {
          case_id: 'a-fail',
          track: 'A',
          status: 'success',
          violations: 0,
          failed_checks: ['must_cite:x'],
          repro: 'python -m devops_agent.openjudge run --case a-fail --no-judge',
        },
      ],
      pareto: [{ check: 'must_cite:x', count: 1 }],
    },
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
  mockSeries.mockReset();
  mockSuites.mockReset();
  mockBatches.mockReset();
  mockBatch.mockReset();
  mockCases.mockReset();
  mockCompare.mockReset();
  mockCoverage.mockReset();
  mockRubrics.mockReset();
  mockRun.mockReset();

  mockSeries.mockResolvedValue({
    points: [
      {
        batch_id: 1,
        finished_at: '2026-11-24T10:00:00Z',
        verdict: 'pass',
        pass_rate: 0.7,
        cases: 10,
        tracks: { A: 0.7 },
      },
      {
        batch_id: 2,
        finished_at: '2026-11-25T10:00:00Z',
        verdict: 'pass',
        pass_rate: 0.7,
        cases: 10,
        tracks: { A: 0.7 },
      },
    ],
  });
  mockSuites.mockResolvedValue({
    total: 90,
    suite_sha: 'suite123456789',
    tracks: [
      {
        track: 'A',
        cases: 28,
        rules: { pass_rate: 0.6, judge: 0.6, min_n: 10 },
        latest: { batch_id: 2, pass_rate: 0.7, verdict: 'pass', finished_at: '2026-11-25T10:00:00Z' },
      },
      {
        track: 'B',
        cases: 24,
        rules: { pass_rate: 0.6, judge: 0.6, min_n: 10 },
        latest: null,
      },
    ],
  });
  mockBatches.mockResolvedValue({ items: [makeOjBatch()], total: 1 });
  mockBatch.mockResolvedValue(makeOjBatch());
  mockCases.mockResolvedValue({
    items: [
      {
        id: 11,
        case_id: 'a-fail',
        track: 'A',
        subject: 'repo_qa',
        risk: 'low',
        status: 'success',
        passed: false,
        latency_ms: 1200,
        tokens: { input: 10, output: 5 },
      },
    ],
    total: 1,
    limit: 50,
    offset: 0,
  });
  mockCompare.mockResolvedValue({
    base: 1,
    head: 2,
    comparable: true,
    suite_sha: ['a', 'b'],
    tracks: { A: { base: 0.5, head: 0.8, delta: 0.3 } },
    flips: [{ case_id: 'a-fail', base: false, head: true }],
  });
  mockCoverage.mockResolvedValue({
    cases: 90,
    suite_sha: 'suite123456789',
    coverage: { tracks: { A: 28, B: 24 } },
    problems: [],
  });
  mockRubrics.mockResolvedValue({ items: [], total: 0 });
  mockRun.mockResolvedValue({
    id: 5,
    status: 'done',
    track: 'A',
    total: 10,
    done: 10,
    judge: false,
    batch_id: 3,
    error: '',
    elapsed_ms: 1000,
    current_case: '',
  });
});

afterEach(cleanup);

describe('Quality 两个视图', () => {
  it('只有「概览」与「运行与批次」两个 tab，旧 tab 已删除', () => {
    renderQuality();

    expect(screen.getByText('概览')).toBeTruthy();
    expect(screen.getByText('运行与批次')).toBeTruthy();
    expect(screen.queryByText('评测集')).toBeNull();
    expect(screen.queryByText('评测批次')).toBeNull();
    expect(screen.queryByText('OpenJudge')).toBeNull();
  });

  it('概览展示最近批次与通过率趋势', async () => {
    renderQuality();

    expect(await screen.findByText(/批次 #2/)).toBeTruthy();
    expect(screen.getByText(/通过率 70\.0%（n=10）/)).toBeTruthy();
    expect(await screen.findByText(/精度上限 10\.0pp，变化在抖动范围内/)).toBeTruthy();
  });

  it('概览在加载中显示占位而不是「还没有批次」', async () => {
    mockSeries.mockReturnValue(new Promise(() => {}));
    renderQuality();

    expect(screen.getByText('正在加载质量趋势…')).toBeTruthy();
    expect(screen.queryByText(/还没有批次/)).toBeNull();
  });

  it('无批次时概览给出发起评测入口', async () => {
    mockSeries.mockResolvedValue({ points: [] });
    renderQuality();

    expect(await screen.findByText(/还没有批次/)).toBeTruthy();
    expect(screen.getByText('去发起评测')).toBeTruthy();
  });

  it('运行与批次视图展示发起入口、题库与批次明细', async () => {
    renderQuality();
    fireEvent.click(screen.getByText('运行与批次'));

    expect(await screen.findByText('发起评测')).toBeTruthy();
    expect((await screen.findAllByText(/#3/)).length).toBeGreaterThan(0);
    expect((await screen.findAllByText('A 仓库问答')).length).toBeGreaterThan(0);
    expect((await screen.findAllByText('a-fail')).length).toBeGreaterThan(0);
    expect((await screen.findAllByText('must_cite:x')).length).toBeGreaterThan(0);
    // 题库卡片：有最近批次的显示通过率，没有的显式提示
    expect(await screen.findByText('尚无批次')).toBeTruthy();
  });
});
