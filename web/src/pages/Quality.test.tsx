// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  fetchEvalBatches,
  fetchEvalTrends,
  fetchRuns,
  postFeedback,
} from '../api/client';
import { Quality } from './Quality';

vi.mock('../api/client', () => ({
  fetchEvalTrends: vi.fn(),
  fetchRuns: vi.fn(),
  postFeedback: vi.fn(),
  fetchEvalBatches: vi.fn(),
  fetchEvalBatch: vi.fn(),
  fetchEvalCases: vi.fn(),
  fetchEvalCompare: vi.fn(),
}));

const mockTrends = vi.mocked(fetchEvalTrends);
const mockRuns = vi.mocked(fetchRuns);
const mockBatches = vi.mocked(fetchEvalBatches);
const mockFeedback = vi.mocked(postFeedback);

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
  mockTrends.mockReset();
  mockRuns.mockReset();
  mockBatches.mockReset();
  mockFeedback.mockReset();
  mockTrends.mockResolvedValue({ count: 0, averages: {}, by_source: {} });
  mockRuns.mockResolvedValue({ items: [], total: 0, limit: 10, offset: 0 });
  mockBatches.mockResolvedValue({ items: [], total: 0 });
});

afterEach(cleanup);

describe('Quality 二级切换', () => {
  it('默认线上反馈，切到评测批次再切回原内容仍在', async () => {
    renderQuality();

    expect(await screen.findByText('各维度平均分')).toBeTruthy();
    expect(screen.getByRole('tab', { name: '线上反馈' }).getAttribute('aria-selected')).toBe('true');

    fireEvent.click(screen.getByRole('tab', { name: '评测批次' }));
    expect(await screen.findByText('还没有评测批次')).toBeTruthy();
    expect(screen.queryByText('各维度平均分')).toBeNull();

    fireEvent.click(screen.getByRole('tab', { name: '线上反馈' }));
    expect(await screen.findByText('各维度平均分')).toBeTruthy();
    expect(screen.getByText('人工反馈')).toBeTruthy();
  });
});
