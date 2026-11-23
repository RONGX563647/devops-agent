// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchRun, fetchRunHealth, fetchRuns, fetchSpans } from '../api/client';
import type { Run, RunHealth, Span } from '../types';
import { RunTrace } from './RunTrace';
import { TraceTab } from './TraceTab';

vi.mock('../api/client', () => ({
  fetchRun: vi.fn(),
  fetchSpans: vi.fn(),
  fetchRunHealth: vi.fn(),
  fetchRuns: vi.fn(),
}));

function makeRun(id: number, sessionId: string): Run {
  return {
    id,
    session_id: sessionId,
    domain: 'delivery',
    status: 'success',
    input: 'q',
    output: 'a',
    started_at: '2026-11-23T10:00:00',
    ended_at: null,
    duration_ms: 100,
    model: 'qwen',
    total_tokens: 10,
    health_score: 80,
    input_tokens: 5,
    output_tokens: 5,
  };
}

const span: Span = {
  id: 1,
  run_id: 7,
  type: 'model',
  name: 'chat',
  start_offset_ms: 0,
  duration_ms: 50,
  input: '',
  output: '',
  tokens: 5,
  status: 'ok',
  error: null,
};

const health: RunHealth = {
  run_id: 7,
  score: 80,
  dimensions: { execution: 90, efficiency: 70, quality: 80, safety: 85 },
};

function wrap(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{node}</QueryClientProvider>);
}

beforeEach(() => {
  vi.mocked(fetchRun).mockResolvedValue(makeRun(7, 's-one'));
  vi.mocked(fetchSpans).mockResolvedValue([span]);
  vi.mocked(fetchRunHealth).mockResolvedValue(health);
  vi.mocked(fetchRuns).mockResolvedValue({
    items: [makeRun(7, 's-one'), makeRun(8, 's-two')],
    total: 2,
    limit: 100,
    offset: 0,
  });
});
afterEach(cleanup);

describe('RunTrace', () => {
  it('渲染运行概览、健康分拆解与瀑布图', async () => {
    wrap(<RunTrace runId={7} />);
    expect(await screen.findByText('Trace · run #7')).toBeTruthy();
    expect(screen.getByText('总分 80')).toBeTruthy();
    expect(screen.getByText(/模型调用 · chat/)).toBeTruthy();
    expect(screen.getByText('50 ms')).toBeTruthy();
  });

  it('run 不存在时显示错误', async () => {
    vi.mocked(fetchRun).mockRejectedValue(new Error('404'));
    wrap(<RunTrace runId={99} />);
    expect(await screen.findByText(/加载失败/)).toBeTruthy();
  });
});

describe('TraceTab', () => {
  it('按会话过滤运行列表', async () => {
    const { container } = wrap(<TraceTab session="s-two" />);
    await waitFor(() => {
      expect(container.querySelector('tbody')?.textContent).toContain('s-two');
    });
    expect(container.querySelector('tbody')?.textContent).not.toContain('s-one');
  });

  it('点击运行进入轨迹详情并可返回', async () => {
    const { container } = wrap(<TraceTab session={null} />);
    await waitFor(() => {
      expect(container.querySelectorAll('tbody tr').length).toBe(2);
    });
    fireEvent.click(container.querySelector('tbody tr')!);
    expect(await screen.findByText('Trace · run #7')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '返回列表' }));
    expect(screen.getByText('全部会话 · 点击运行查看轨迹与健康分')).toBeTruthy();
  });
});
