// @vitest-environment jsdom
/**
 * 质量页集成测试：视图路由、概览门禁矩阵、批次下钻与归因分桶。
 *
 * 重点覆盖 SPEC-0008 里前端要修的那几条：F1 单一阈值线、F2 批次分页、
 * F3 门禁下钻不分类型、F6 时间本地化、F7 五赛道固定五行、F9 批次身份。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createCaseFromBatch,
  fetchOpenJudgeBatch,
  fetchOpenJudgeBatches,
  fetchOpenJudgeCalibration,
  fetchOpenJudgeCase,
  fetchOpenJudgeCaseDocument,
  fetchOpenJudgeCases,
  fetchOpenJudgeCompare,
  fetchOpenJudgeCoverage,
  fetchOpenJudgeLibrary,
  fetchOpenJudgeRubrics,
  fetchOpenJudgeRun,
  fetchOpenJudgeScoringConfig,
  fetchOpenJudgeSeries,
  fetchOpenJudgeSettings,
  fetchOpenJudgeSuites,
} from '../api/client';
import type { OjBatch } from '../types';
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
  createCaseFromBatch: vi.fn(),
}));

const mockSeries = vi.mocked(fetchOpenJudgeSeries);
const mockSuites = vi.mocked(fetchOpenJudgeSuites);
const mockBatches = vi.mocked(fetchOpenJudgeBatches);
const mockBatch = vi.mocked(fetchOpenJudgeBatch);
const mockCalibration = vi.mocked(fetchOpenJudgeCalibration);
const mockCases = vi.mocked(fetchOpenJudgeCases);
const mockCase = vi.mocked(fetchOpenJudgeCase);
const mockCompare = vi.mocked(fetchOpenJudgeCompare);
const mockCoverage = vi.mocked(fetchOpenJudgeCoverage);
const mockRubrics = vi.mocked(fetchOpenJudgeRubrics);
const mockRun = vi.mocked(fetchOpenJudgeRun);
const mockScoringConfig = vi.mocked(fetchOpenJudgeScoringConfig);
const mockSettings = vi.mocked(fetchOpenJudgeSettings);
const mockLibrary = vi.mocked(fetchOpenJudgeLibrary);
const mockCaseDoc = vi.mocked(fetchOpenJudgeCaseDocument);
const mockBackfill = vi.mocked(createCaseFromBatch);

const LIMITS = {
  max_concurrency: 4,
  min_timeout: 30,
  max_timeout: 900,
  max_limit: 30,
  tracks: ['A', 'B'],
  risks: ['low'],
  permissions: ['readonly'],
  tiers: ['must_pass', 'challenge'],
};

/** 一个「只跑了 A」的批次：范围不足 → partial，并带一条 advisory 原因。 */
function makeBatch(overrides: Partial<OjBatch> = {}): OjBatch {
  return {
    id: 3,
    verdict: 'partial',
    model: 'deepseek-v4.1-flash',
    suite_sha: 'suite123456789abc',
    plan_sha: 'plan123456789abc',
    started_at: '2026-11-25T06:00:00Z',
    finished_at: '2026-11-25T06:10:00Z',
    summary: {
      verdict: 'partial',
      scope: { mode: 'single_track', measured_tracks: ['A'], unmeasured_tracks: ['B', 'C', 'D', 'E'] },
      causes: [
        {
          code: 'judge_below',
          message: 'A 赛道：裁判均分 0.52 低于门槛 0.60',
          severity: 'advisory',
          track: 'A',
        },
      ],
      evidence: {
        judge_level: 'soft',
        judge_model: 'qwen3.8-flash',
        calibration_checked: true,
        calibration_n: 42,
        calibration_spearman: 0.64,
        measured_tracks: 1,
      },
      truncated: { failures: true, pareto: false, total_failures: 120 },
      challenge_score: 0.5,
      case_total: 90,
      kpi: {
        cases: 30,
        case_count: 28,
        trials: 2,
        passed: 24,
        judge_n: 20,
        violations: 0,
        pass_at_k: 0.9,
        pass_hat_k: 0.75,
        judge_failed_cases: 4,
        rule_failed_cases: 4,
        not_judged_cases: 8,
        cost_usd: null,
        cost_note: '未配置单价（DEVOPS_AGENT_OPENJUDGE_USD_PER_1K）',
      },
      tracks: {
        A: {
          n: 28,
          total: 28,
          passed: 24,
          pass_rate: 0.857,
          rule_pass_rate: 0.857,
          judge_avg: 0.52,
          violations: 0,
          ci: [0.68, 0.95],
          verdict: 'partial',
          challenge: { total: 2, passed: 1 },
          reasons: [
            {
              code: 'judge_below',
              actual: 0.52,
              threshold: 0.6,
              message: '裁判均分 0.52 低于门槛 0.60',
              severity: 'advisory' as const,
            },
          ],
        },
      },
      subjects: {},
      stages: {},
      failures: [
        {
          case_id: 'a-rule',
          track: 'A',
          status: 'success',
          violations: 0,
          failed_checks: ['must_cite:x'],
          judge_dims: [],
          failure_type: 'rule',
          repro: 'run --case a-rule',
        },
        {
          case_id: 'a-judge',
          track: 'A',
          status: 'success',
          violations: 0,
          failed_checks: [],
          judge_dims: ['correctness'],
          failure_type: 'judge_score',
          repro: 'run --case a-judge',
        },
      ],
      pareto: { rule: [{ check: 'must_cite:x', count: 1 }], judge: [{ check: 'correctness', count: 1 }] },
    },
    ...overrides,
  };
}

function makeScoringConfig() {
  return {
    plan: {
      name: 'oj-plan-v1',
      sha: 'plan123456789',
      judges: [
        { key: 'correctness', grader: 'CorrectnessGrader', weight: 0.35, scale: [1, 5] as [number, number] },
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

function makeCaseDetail(rowId: number) {
  const judge = rowId === 12;
  return {
    id: rowId,
    case_id: judge ? 'a-judge' : 'a-rule',
    track: 'A',
    subject: 'repo_qa',
    risk: 'low',
    status: 'success',
    passed: false,
    latency_ms: 1200,
    tokens: { input: 10, output: 5 },
    tier: 'must_pass',
    trial: 0,
    checks: judge
      ? [{ name: 'must_cite:x', kind: 'content', passed: true, detail: '有引用' }]
      : [{ name: 'must_cite:x', kind: 'content', passed: false, detail: '缺少引用' }],
    judge: {
      correctness: { status: 'scored' as const, score: 3, normalized: 0.5, weight: 0.35, reason: '引用不足' },
    },
    scoring: {
      rule_passed: !judge,
      violations: 0,
      judge_norm: 0.5,
      pass_judge: 0.6,
      passed: false,
      formula: 'judge_norm = sum(normalized * weight) / sum(weight)',
      failure_type: judge ? ('judge_score' as const) : ('rule' as const),
      rule_failed_checks: judge ? [] : ['must_cite:x'],
      judge_failed: [],
    },
    events: ['开始', '结束'],
    input_stages: [],
    workspace: {},
    trace: {
      status: 'success',
      error: '',
      turns: [{ role: 'user', content: '你好' }],
      tool_calls: [],
      events: ['开始'],
      input_stages: [],
      workspace: {},
      judge_input: {},
      final_text: '回答',
    },
  };
}

function rowOf(id: number, caseId: string) {
  return {
    id,
    case_id: caseId,
    track: 'A',
    subject: 'repo_qa',
    risk: 'low',
    status: 'success',
    passed: false,
    latency_ms: 1200,
    tokens: { input: 10, output: 5 },
    tier: 'must_pass',
    trial: 0,
  };
}

function renderQuality(initial = '/?view=overview') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initial]}>
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
  for (const fn of [
    mockSeries, mockSuites, mockBatches, mockBatch, mockCalibration, mockCases, mockCase,
    mockCompare, mockCoverage, mockRubrics, mockRun, mockScoringConfig, mockSettings,
    mockLibrary, mockCaseDoc, mockBackfill,
  ]) {
    fn.mockReset();
  }

  mockSeries.mockResolvedValue({
    points: [
      { batch_id: 2, finished_at: '2026-11-24T10:00:00Z', verdict: 'fail', pass_rate: 0.5, cases: 10, tracks: { A: 0.5 } },
      { batch_id: 3, finished_at: '2026-11-25T10:00:00Z', verdict: 'partial', pass_rate: 0.8, cases: 10, tracks: { A: 0.8 } },
    ],
  });
  mockSuites.mockResolvedValue({
    total: 90,
    suite_sha: 'suite123456789',
    tracks: [
      { track: 'A', cases: 28, rules: { pass_rate: 0.6, judge: 0.6, min_n: 10 }, latest: null },
      { track: 'B', cases: 24, rules: { pass_rate: 0.6, judge: 0.6, min_n: 10, max_violations: 0 }, latest: null },
    ],
  });
  mockBatches.mockResolvedValue({ items: [makeBatch()], total: 45, limit: 20, offset: 0 });
  mockBatch.mockResolvedValue(makeBatch());
  mockCalibration.mockResolvedValue({
    checked: true,
    valid: false,
    level: 'soft',
    missing: ['correctness'],
    required_n: 100,
    required_spearman: 0.7,
    progress: {
      n: 42,
      spearman: 0.64,
      next: 'hard',
      soft: { n: 30, spearman: 0.6 },
      hard: { n: 100, spearman: 0.7 },
    },
  });
  mockCases.mockResolvedValue({
    items: [rowOf(11, 'a-rule'), rowOf(12, 'a-judge')],
    total: 2,
    limit: 50,
    offset: 0,
  });
  mockCase.mockImplementation(async (_batchId: number, rowId: number) => makeCaseDetail(rowId));
  mockScoringConfig.mockResolvedValue(makeScoringConfig());
  mockCompare.mockResolvedValue({
    base: 1, head: 2, comparable: true, suite_sha: ['a', 'b'],
    tracks: { A: { base: 0.5, head: 0.8, delta: 0.3 } }, flips: [],
  });
  mockCoverage.mockResolvedValue({
    cases: 90, suite_sha: 'suite123456789', coverage: { tracks: { A: 28 } }, problems: [],
  });
  mockRubrics.mockResolvedValue({ items: [], total: 0 });
  mockSettings.mockResolvedValue({
    values: { target_model: '', judge_model: '', judge: false, concurrency: 2, timeout: 180 },
    effective: { target_model: 'deepseek-v4.1-flash', judge_model: 'qwen3.8-flash' },
    limits: LIMITS,
  });
  mockLibrary.mockResolvedValue({
    items: [
      {
        id: 'a-1', track: 'A', risk: 'low', permission: 'readonly', subject: 'repo_qa',
        fixture: 'python-basic', offline: false, source: 'repo', edited: false,
      },
    ],
    total: 1,
    suite: { cases: 1, suite_sha: 'abcdef123456', overlay: 0, deleted: 0, dir: '/tmp/x' },
    thresholds: { A: { pass_rate: 0.6 } },
    limits: LIMITS,
  });
  mockCaseDoc.mockResolvedValue({
    case: { _source: 'repo', _edited: false, id: 'a-1', track: 'A', turns: ['你好'] },
  });
  mockRun.mockResolvedValue({
    id: 5, status: 'done', track: 'A', total: 10, done: 10, judge: false,
    batch_id: 3, error: '', elapsed_ms: 1000, current_case: '',
  });
  mockBackfill.mockResolvedValue({
    case: { id: 'a-rule-b3' },
    suite: { cases: 91, suite_sha: 'abcdef123456', overlay: 1, deleted: 0, dir: '/tmp/x' },
  } as never);
});

afterEach(cleanup);

describe('质量页视图路由', () => {
  it('四个视图由 URL 决定，选中态用 aria-current 而不是 tab 角色', async () => {
    renderQuality('/?view=overview');

    const nav = screen.getByRole('navigation', { name: '质量视图' });
    expect(nav).toBeTruthy();
    const links = screen.getAllByRole('link');
    expect(links.map((link) => link.textContent)).toEqual(
      expect.arrayContaining([expect.stringContaining('概览')]),
    );
    expect(screen.getByRole('link', { name: /概览/ }).getAttribute('aria-current')).toBe('page');
    expect(screen.queryByRole('tablist')).toBeNull();
    expect(screen.queryByText('运行与批次')).toBeNull();
  });

  it('旧的 ?focus=scoreflow 仍能落到评分流程视图', async () => {
    renderQuality('/?focus=scoreflow');
    await screen.findByText('评分流程');
    expect(screen.getByRole('link', { name: /评分流程/ }).getAttribute('aria-current')).toBe('page');
  });

  it('用例与模型视图能改模型、能打开用例', async () => {
    renderQuality('/?view=studio');
    expect(await screen.findByText('评测模型')).toBeTruthy();
    fireEvent.click(await screen.findByText('a-1'));
    expect(await screen.findByLabelText('用例 JSON')).toBeTruthy();
  });
});

describe('概览', () => {
  it('结论、范围、原因、证据四段齐全', async () => {
    renderQuality('/?view=overview');

    expect(await screen.findByText(/局部结论：只测了部分赛道/)).toBeTruthy();
    expect(screen.getByText(/范围 单赛道/)).toBeTruthy();
    expect(screen.getByText(/A 赛道：裁判均分 0\.52 低于门槛 0\.60/)).toBeTruthy();
    expect(screen.getByText(/裁判档位/)).toBeTruthy();
    expect(screen.getAllByText(/软校准（可降为未证实）/).length).toBeGreaterThan(0);
  });

  it('门禁矩阵逐赛道给阈值，不再画一条全局阈值线', async () => {
    renderQuality('/?view=overview');

    expect(await screen.findByText('赛道门禁矩阵')).toBeTruthy();
    expect(screen.getAllByText(/通过率 ≥60\.0% · 质量分 ≥0\.60 · 至少 10 条/).length).toBe(2);
    expect(screen.getByText(/违例 ≤0/)).toBeTruthy();
    expect(screen.getByText(/五个赛道的门槛并不相同/)).toBeTruthy();
    // 没有跑过的赛道也要占一行
    expect(screen.getAllByText('未测').length).toBeGreaterThanOrEqual(4);
  });

  it('k=1 之外才显示多次试验的稳定性口径', async () => {
    renderQuality('/?view=overview');
    expect(await screen.findByText(/pass@k（至少过一次）/)).toBeTruthy();
  });

  it('加载中显式占位，不先渲染「还没有批次」', () => {
    mockSeries.mockReturnValue(new Promise(() => {}));
    renderQuality('/?view=overview');
    expect(screen.getByText('正在加载质量趋势…')).toBeTruthy();
    expect(screen.queryByText(/还没有批次/)).toBeNull();
  });

  it('无批次时给出去批次的入口', async () => {
    mockSeries.mockResolvedValue({ points: [] });
    mockBatches.mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
    renderQuality('/?view=overview');
    expect(await screen.findByText(/还没有批次/)).toBeTruthy();
    expect(screen.getByRole('link', { name: '去发起评测' })).toBeTruthy();
  });
});

describe('批次视图', () => {
  it('身份卡摆出模型、指纹、耗时与成本口径', async () => {
    renderQuality('/?view=batches');

    expect(await screen.findByText('批次 #3')).toBeTruthy();
    expect(screen.getAllByText('deepseek-v4.1-flash').length).toBeGreaterThan(0);
    expect(screen.getByText('qwen3.8-flash')).toBeTruthy();
    expect(screen.getByText('suite1234567')).toBeTruthy();
    expect(screen.getByText('10m0s')).toBeTruthy();
    expect(screen.getAllByText(/未计价 · 未配置单价/).length).toBeGreaterThan(0);
  });

  it('五赛道固定五行，未测赛道显式写出', async () => {
    renderQuality('/?view=batches');
    await screen.findByText('赛道结论');
    expect(screen.getAllByText('本批次未测此赛道')).toHaveLength(4);
    expect(screen.getAllByText('A 仓库问答').length).toBeGreaterThan(0);
  });

  it('批次分页用真实 total，并把它传回后端', async () => {
    renderQuality('/?view=batches');
    expect(await screen.findByText(/共 45 批 · 第 1\/3 页/)).toBeTruthy();
    await waitFor(() => expect(mockBatches).toHaveBeenCalledWith(20, undefined, 0));
    fireEvent.click(screen.getByRole('button', { name: '更早' }));
    await waitFor(() => expect(mockBatches).toHaveBeenCalledWith(20, undefined, 20));
  });

  it('失败按规则与裁判分桶，归因也分开列', async () => {
    renderQuality('/?view=batches');

    expect(await screen.findByText('规则失败 1')).toBeTruthy();
    expect(screen.getByText('裁判低分 1')).toBeTruthy();
    expect(screen.getByText(/断言 must_cite:x/)).toBeTruthy();
    fireEvent.click(screen.getByText('裁判低分 1'));
    expect(screen.getByText(/裁判 correctness/)).toBeTruthy();
    expect(screen.getByText('规则断言')).toBeTruthy();
    expect(screen.getByText('裁判维度')).toBeTruthy();
  });

  it('失败列表被截断时说明总数', async () => {
    renderQuality('/?view=batches');
    expect(await screen.findByText(/本批次共 120 条失败/)).toBeTruthy();
  });

  it('回流按钮把批次用例变成题库草稿', async () => {
    renderQuality('/?view=batches');
    fireEvent.click(await screen.findByText('回流成草稿'));
    await waitFor(() => expect(mockBackfill).toHaveBeenCalledWith(3, 11));
    expect(await screen.findByText('已回流为草稿 a-rule-b3')).toBeTruthy();
  });
});

describe('评分流程与下钻', () => {
  it('评分流程视图默认展开，门禁用「提示」而不是「未校准」', async () => {
    renderQuality('/?view=scoreflow');

    expect(await screen.findByText('归一化合成')).toBeTruthy();
    expect(screen.getByRole('button', { name: '收起依据' }).getAttribute('aria-expanded')).toBe('true');
    expect(screen.getAllByText(/软校准（可降为未证实）/).length).toBeGreaterThan(0);
    expect(screen.getByText(/缺 correctness/)).toBeTruthy();
    expect(screen.getByText(/样本 42\/100 · ρ 0\.64/)).toBeTruthy();
  });

  it('点「语义裁判」的证据只跳到裁判低分那条，而不是第一条失败', async () => {
    renderQuality('/?view=scoreflow');
    await screen.findByText('归一化合成');

    const inspectButtons = screen.getAllByRole('button', { name: '查看证据' });
    fireEvent.click(inspectButtons[1]);

    expect(await screen.findByRole('dialog', { name: '用例诊断' })).toBeTruthy();
    await waitFor(() => expect(mockCase).toHaveBeenCalledWith(3, 12));
  });

  it('门禁没有对应失败时给具体文案，而不是跳错用例', async () => {
    const batch = makeBatch();
    batch.summary.failures = [
      {
        case_id: 'a-rule', track: 'A', status: 'success', violations: 0,
        failed_checks: ['must_cite:x'], judge_dims: [], failure_type: 'rule', repro: 'run --case a-rule',
      },
    ];
    mockBatch.mockResolvedValue(batch);
    mockBatches.mockResolvedValue({ items: [batch], total: 1, limit: 20, offset: 0 });
    renderQuality('/?view=scoreflow');
    await screen.findByText('归一化合成');

    fireEvent.click(screen.getAllByRole('button', { name: '查看证据' })[1]);
    expect(await screen.findByText('本批次没有裁判低分的用例')).toBeTruthy();
  });
});
