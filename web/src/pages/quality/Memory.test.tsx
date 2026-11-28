// @vitest-environment jsdom
/** 记忆评测视图：结论、六赛道门禁、配对增益与用例明细。 */
import { cleanup, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchMemoryBatches, fetchMemoryCases } from '../../api/client';
import type { MemoryBatch, MemoryCaseList } from '../../types';
import { Memory } from './Memory';

vi.mock('../../api/client', () => ({
  fetchMemoryBatches: vi.fn(),
  fetchMemoryCases: vi.fn(),
}));

const mockBatches = vi.mocked(fetchMemoryBatches);
const mockCases = vi.mocked(fetchMemoryCases);

function makeBatch(): MemoryBatch {
  return {
    id: 7,
    plan_name: 'memory-v1',
    verdict: 'pass',
    started_at: '2026-11-27T01:00:00Z',
    finished_at: '2026-11-27T01:02:00Z',
    summary: {
      plan: 'memory-v1',
      baseline: 'off',
      primary: 'full',
      verdict: 'pass',
      suite_sha: 'suite123',
      plan_sha: 'plan123',
      kpi: { runs: 8, cases: 4, passed: 8, pass_rate: 1 },
      tracks: {
        MT: {
          total: 6,
          passed: 6,
          tsr: 1,
          ci: [0.6, 1],
          probe_acc: 1,
          contradiction_rate: 0,
          recall_at_5: 1,
          precision_at_5: 1,
          mrr: 1,
          fact_retention: null,
          constraint_retention: null,
          artifact_retention: null,
          junk_rate: null,
          junk_injected: 0,
          leak_count: 0,
          redaction_violation: 0,
        },
        PR: {
          total: 4,
          passed: 4,
          tsr: 1,
          ci: [0.51, 1],
          probe_acc: 1,
          contradiction_rate: null,
          recall_at_5: null,
          precision_at_5: null,
          mrr: null,
          fact_retention: null,
          constraint_retention: 1,
          artifact_retention: null,
          junk_rate: null,
          junk_injected: 0,
          leak_count: 0,
          redaction_violation: 0,
        },
      },
      gains: {
        MT: { n: 6, full_tsr: 1, off_tsr: 0, gain_pp: 100, ci: [1, 1], p: 0.031 },
      },
      harm: { n: 4, harm_rate: 0 },
      red_lines: { leak_count: 0, redaction_violation: 0, constraint_retention: 1 },
      cost: {
        token_overhead_p50: 0.12,
        token_overhead_p95: 0.18,
        latency_p95_ms: 90,
        memory_bytes_total: 2048,
        compression_ratio_avg: 0.2,
      },
      causes: [{ code: 'none', message: '全部 must_pass 通过' }],
    },
  };
}

function renderMemory() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Memory />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockBatches.mockReset();
  mockCases.mockReset();
  mockBatches.mockResolvedValue({ items: [makeBatch()], total: 1, limit: 20, offset: 0 });
  const cases: MemoryCaseList = {
    items: [
      {
        id: 11,
        case_id: 'mt-001',
        track: 'MT',
        tier: 'must_pass',
        variant: 'full',
        trial: 0,
        passed: true,
        probe_total: 1,
        probe_correct: 1,
        latency_ms: 9,
        tokens: 120,
        metrics: {},
      },
    ],
    total: 1,
    limit: 50,
    offset: 0,
  };
  mockCases.mockResolvedValue(cases);
});

afterEach(cleanup);

describe('记忆评测视图', () => {
  it('展示结论、配对增益与红线', async () => {
    renderMemory();
    expect(await screen.findByText('记忆批次 #7')).toBeTruthy();
    expect(screen.getByText('通过', { selector: 'span.badge' })).toBeTruthy();
    expect(screen.getByText(/MT 100pp/)).toBeTruthy();
    expect(screen.getByText(/跨会话泄漏 0/)).toBeTruthy();
  });

  it('六赛道用语义表格并带 caption', async () => {
    renderMemory();
    expect(await screen.findByText('记忆赛道门禁结果（主变体）')).toBeTruthy();
    expect(screen.getByRole('region', { name: '记忆赛道门禁结果' })).toBeTruthy();
    expect(await screen.findByRole('rowheader', { name: /MT · 多轮记忆/ })).toBeTruthy();
  });

  it('用例明细展示探针通过数与延迟', async () => {
    renderMemory();
    expect(await screen.findByText('记忆用例明细（最近 50 条）')).toBeTruthy();
    expect(await screen.findByRole('rowheader', { name: 'mt-001' })).toBeTruthy();
    expect(await screen.findByText('9ms')).toBeTruthy();
  });

  it('没有批次时给可复现命令提示', async () => {
    mockBatches.mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
    renderMemory();
    expect(await screen.findByText('还没有记忆评测批次。')).toBeTruthy();
    expect(screen.getByText(/openjudge memory run/)).toBeTruthy();
  });
});
