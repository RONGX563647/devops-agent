// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  fetchOpenJudgeBatch,
  fetchOpenJudgeBatches,
  fetchOpenJudgeCalibration,
  fetchOpenJudgeCase,
  fetchOpenJudgeCaseDocument,
  fetchOpenJudgeLibrary,
  fetchOpenJudgeSettings,
  fetchOpenJudgeCases,
  fetchOpenJudgeCompare,
  fetchOpenJudgeCoverage,
  fetchOpenJudgeRubrics,
  fetchOpenJudgeRun,
  fetchOpenJudgeScoringConfig,
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
  fetchOpenJudgeCalibration: vi.fn(),
  fetchOpenJudgeCases: vi.fn(),
  fetchOpenJudgeCase: vi.fn(),
  fetchOpenJudgeCompare: vi.fn(),
  fetchOpenJudgeCoverage: vi.fn(),
  fetchOpenJudgeRubrics: vi.fn(),
  fetchOpenJudgeRun: vi.fn(),
  fetchOpenJudgeScoringConfig: vi.fn(),
  startOpenJudgeRun: vi.fn(),
  cancelOpenJudgeRun: vi.fn(),
  fetchOpenJudgeSettings: vi.fn(),
  patchOpenJudgeSettings: vi.fn(),
  fetchOpenJudgeLibrary: vi.fn(),
  fetchOpenJudgeCaseDocument: vi.fn(),
  saveOpenJudgeCase: vi.fn(),
  deleteOpenJudgeCase: vi.fn(),
  restoreOpenJudgeCase: vi.fn(),
}));

const mockSeries = vi.mocked(fetchOpenJudgeSeries);
const mockSuites = vi.mocked(fetchOpenJudgeSuites);
const mockBatches = vi.mocked(fetchOpenJudgeBatches);
const mockBatch = vi.mocked(fetchOpenJudgeBatch);
const mockCalibration = vi.mocked(fetchOpenJudgeCalibration);
const mockCases = vi.mocked(fetchOpenJudgeCases);
const mockCompare = vi.mocked(fetchOpenJudgeCompare);
const mockCoverage = vi.mocked(fetchOpenJudgeCoverage);
const mockRubrics = vi.mocked(fetchOpenJudgeRubrics);
const mockRun = vi.mocked(fetchOpenJudgeRun);
const mockCase = vi.mocked(fetchOpenJudgeCase);
const mockScoringConfig = vi.mocked(fetchOpenJudgeScoringConfig);
const mockSettings = vi.mocked(fetchOpenJudgeSettings);
const mockLibrary = vi.mocked(fetchOpenJudgeLibrary);
const mockCaseDoc = vi.mocked(fetchOpenJudgeCaseDocument);

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

function makeCaseDetail() {
  return {
    id: 11,
    case_id: 'a-fail',
    track: 'A',
    subject: 'repo_qa',
    risk: 'low',
    status: 'success',
    passed: false,
    latency_ms: 1200,
    tokens: { input: 10, output: 5 },
    checks: [{ name: 'must_cite:x', kind: 'content', passed: false, detail: '缺少引用' }],
    judge: {
      correctness: { status: 'scored' as const, score: 3, normalized: 0.5, weight: 0.35, reason: '引用不足' },
      loop: { status: 'skipped' as const, skip_reason: 'rule_failed' },
    },
    scoring: {
      rule_passed: false,
      violations: 0,
      judge_norm: 0.5,
      pass_judge: 0.6,
      passed: false,
      formula: 'judge_norm = sum(normalized * weight) / sum(weight)',
      failure_type: 'rule' as const,
      rule_failed_checks: ['must_cite:x'],
      judge_failed: [],
    },
    events: ['开始', '结束'],
    input_stages: [{ stage: 'prompt' }],
    workspace: { states: {}, diff: '', hashes: {} },
    trace: {
      status: 'success',
      error: '',
      turns: [{ role: 'user', content: '你好' }],
      tool_calls: [{ name: 'Grep', args: { pattern: 'x' }, state: 'done', result_excerpt: '命中一处' }],
      events: ['开始'],
      input_stages: [],
      workspace: { states: {}, diff: '', hashes: {} },
      judge_input: {},
      final_text: '回答',
    },
  };
}

function makeScoringConfig() {
  return {
    plan: {
      name: 'oj-plan-v1',
      sha: 'plan123456789',
      judges: [
        { key: 'correctness', grader: 'CorrectnessGrader', weight: 0.35, scale: [1, 5] as [number, number] },
        { key: 'relevance', grader: 'RelevanceGrader', weight: 0.15, scale: [1, 5] as [number, number] },
      ],
    },
    pass_judge: 0.6,
    track_rules: {
      A: { pass_rate: 0.6, judge: 0.6, min_n: 10 },
      B: { pass_rate: 0.6, judge: 0.6, min_n: 10, max_violations: 0 },
    },
    gates: {
      review: { total: 90, approved: 85, pending: 5, legacy: 70, pending_ids: ['a-new'] },
      rubric: {
        ref: 'repo_qa_quality@v1', name: 'repo_qa_quality', version: 'v1',
        sha: 'rubric123456789', status: 'frozen', reviewer: 'liu', ready: true,
      },
    },
  };
}

function renderQuality() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Quality />
      </MemoryRouter>
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
  mockCalibration.mockReset();
  mockCases.mockReset();
  mockCompare.mockReset();
  mockCoverage.mockReset();
  mockRubrics.mockReset();
  mockRun.mockReset();
  mockCase.mockReset();
  mockScoringConfig.mockReset();

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
  mockCalibration.mockResolvedValue({
    checked: true,
    valid: false,
    missing: ['correctness'],
    required_n: 100,
    required_spearman: 0.7,
  });
  mockCase.mockResolvedValue(makeCaseDetail());
  mockScoringConfig.mockResolvedValue(makeScoringConfig());
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
  mockSettings.mockResolvedValue({
    values: { target_model: '', judge_model: '', judge: false, concurrency: 2, timeout: 180 },
    effective: { target_model: 'deepseek-v4.1-flash', judge_model: 'qwen3.8-flash' },
    limits: {
      max_concurrency: 4,
      min_timeout: 30,
      max_timeout: 900,
      max_limit: 30,
      tracks: ['A', 'B'],
      risks: ['low'],
      permissions: ['readonly'],
    },
  });
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
    ],
    total: 1,
    suite: { cases: 1, suite_sha: 'abcdef123456', overlay: 0, deleted: 0, dir: '/tmp/x' },
    thresholds: { A: { pass_rate: 0.6 } },
    limits: {
      max_concurrency: 4,
      min_timeout: 30,
      max_timeout: 900,
      max_limit: 30,
      tracks: ['A', 'B'],
      risks: ['low'],
      permissions: ['readonly'],
    },
  });
  mockCaseDoc.mockResolvedValue({
    case: { _source: 'repo', _edited: false, id: 'a-1', track: 'A', turns: ['你好'] },
  });
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

describe('Quality 三个视图', () => {
  it('只有「概览 / 运行与批次 / 用例与模型」三个 tab，旧 tab 已删除', () => {
    renderQuality();

    expect(screen.getByText('概览')).toBeTruthy();
    expect(screen.getByText('运行与批次')).toBeTruthy();
    expect(screen.getByText('用例与模型')).toBeTruthy();
    expect(screen.queryByText('评测集')).toBeNull();
    expect(screen.queryByText('评测批次')).toBeNull();
    expect(screen.queryByText('OpenJudge')).toBeNull();
  });

  it('用例与模型视图能改模型、能打开用例', async () => {
    renderQuality();
    fireEvent.click(screen.getByText('用例与模型'));

    expect(await screen.findByText('评测模型')).toBeTruthy();
    expect(await screen.findByText('题库')).toBeTruthy();
    fireEvent.click(await screen.findByText('a-1'));
    expect(await screen.findByLabelText('用例 JSON')).toBeTruthy();
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

  it('评分流程可展开，阈值来自 scoring-config', async () => {
    renderQuality();
    fireEvent.click(screen.getByText('运行与批次'));

    expect(await screen.findByText('评分流程')).toBeTruthy();
    const toggle = screen.getByRole('button', { name: '展开依据' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(toggle);
    expect(await screen.findByText('归一化合成')).toBeTruthy();
    expect(screen.getByText(/判分方案 oj-plan-v1/)).toBeTruthy();
    expect(screen.getByRole('button', { name: '收起依据' }).getAttribute('aria-expanded')).toBe('true');
    expect(mockScoringConfig).toHaveBeenCalled();
  });

  it('评分流程展示三道门禁，未校准只报告不参与通过判定', async () => {
    renderQuality();
    fireEvent.click(screen.getByText('运行与批次'));

    expect(await screen.findByText('可信度门禁')).toBeTruthy();
    expect(screen.getByText('用例审核')).toBeTruthy();
    expect(screen.getByText('已审 85/90')).toBeTruthy();
    expect(screen.getByText('rubric 冻结')).toBeTruthy();
    expect(screen.getByText('已冻结 repo_qa_quality@v1')).toBeTruthy();
    expect(screen.getByText('裁判校准')).toBeTruthy();
    expect(screen.getByText('未校准')).toBeTruthy();
    expect(screen.getByText(/缺 correctness/)).toBeTruthy();
    expect(mockCalibration).toHaveBeenCalled();
  });

  it('失败列表可定位到固定抽屉并在四个 Tab 间切换', async () => {
    renderQuality();
    fireEvent.click(screen.getByText('运行与批次'));

    fireEvent.click(await screen.findByText('定位'));
    expect(await screen.findByRole('dialog', { name: '用例诊断' })).toBeTruthy();
    expect(screen.getByText('判定依据')).toBeTruthy();
    expect(screen.getByRole('tab', { name: '规则断言' }).getAttribute('aria-selected')).toBe('true');
    fireEvent.click(screen.getByRole('tab', { name: '语义裁判' }));
    expect(await screen.findByText('CorrectnessGrader')).toBeTruthy();
    fireEvent.click(screen.getByText('轨迹证据'));
    expect(await screen.findByText('对话轮次（1）')).toBeTruthy();
  });

  it('批次结论过滤把 verdict 传给后端', async () => {
    renderQuality();
    fireEvent.click(screen.getByText('运行与批次'));
    await screen.findByText('批次');

    fireEvent.change(screen.getByLabelText('结论'), { target: { value: 'fail' } });
    await waitFor(() => expect(mockBatches).toHaveBeenCalledWith(20, 'fail'));
  });
});
