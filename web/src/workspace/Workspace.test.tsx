// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Run } from '../types';
import { aggregateSessions } from './SessionSidebar';
import { Workspace, parseTab } from './Workspace';

function makeRun(
  id: number,
  sessionId: string,
  status: Run['status'] = 'success',
  startedAt = `2026-11-23T10:00:${String(id).padStart(2, '0')}`,
): Run {
  return {
    id,
    session_id: sessionId,
    domain: 'delivery',
    status,
    input: 'q',
    output: 'a',
    started_at: startedAt,
    ended_at: null,
    duration_ms: 100,
    model: 'qwen',
    total_tokens: 10,
    health_score: 80,
    input_tokens: 5,
    output_tokens: 5,
  };
}

function mockRuns(runs: Run[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      body: null,
      json: async () => ({ items: runs, total: runs.length, limit: 200, offset: 0 }),
    })),
  );
}

function renderWorkspace(entry = '/?tab=chat') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <Workspace />
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
  mockRuns([
    makeRun(1, 's-one'),
    makeRun(2, 's-one', 'failed'),
    makeRun(3, 's-two', 'success', '2026-11-22T09:00:00'),
  ]);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('parseTab', () => {
  it('合法 key 原样返回，非法与空值回退 chat', () => {
    expect(parseTab('metrics')).toBe('metrics');
    expect(parseTab('trace')).toBe('trace');
    expect(parseTab('bogus')).toBe('chat');
    expect(parseTab(null)).toBe('chat');
  });
});

describe('aggregateSessions', () => {
  it('按 session_id 聚合、统计失败数并按最新时间排序', () => {
    const sessions = aggregateSessions([
      makeRun(1, 's-one'),
      makeRun(2, 's-one', 'failed'),
      makeRun(3, 's-two', 'success', '2026-11-22T09:00:00'),
    ]);
    expect(sessions).toHaveLength(2);
    expect(sessions[0].id).toBe('s-one');
    expect(sessions[0].runCount).toBe(2);
    expect(sessions[0].failedCount).toBe(1);
    expect(sessions[1].id).toBe('s-two');
  });
});

describe('Workspace', () => {
  it('渲染五个 tab 且默认选中 chat', () => {
    renderWorkspace();
    expect(screen.getAllByRole('tab')).toHaveLength(5);
    expect(screen.getByRole('tab', { name: '对话' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByText(/输入问题开始对话/)).toBeTruthy();
  });

  it('从 URL 读取初始 tab，点击切换到指标', async () => {
    renderWorkspace('/?tab=metrics');
    expect(screen.getByRole('tab', { name: '指标' }).getAttribute('aria-selected')).toBe('true');
    fireEvent.click(screen.getByRole('tab', { name: '成本' }));
    expect((await screen.findByRole('tab', { name: '成本' })).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('tab', { name: '指标' }).getAttribute('aria-selected')).toBe('false');
  });

  it('点击会话切换标题并选中态', async () => {
    renderWorkspace();
    fireEvent.click(await screen.findByRole('button', { name: /s-one/ }));
    expect(screen.getByRole('heading', { name: 's-one' })).toBeTruthy();
  });
});
