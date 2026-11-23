import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  fetchEvalTrends,
  fetchRun,
  fetchRunHealth,
  fetchRuns,
  fetchSpans,
  fetchSummary,
  fetchTimeseries,
  postFeedback,
} from './client';

afterEach(() => {
  vi.restoreAllMocks();
});

function mockJson(body: unknown = []) {
  return vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }));
}

describe('client', () => {
  it('fetchRuns 拼接全部查询参数', async () => {
    const spy = mockJson({ items: [], total: 0 });
    await fetchRuns({ domain: 'delivery', status: 'success', limit: 10, offset: 5 });
    expect(spy).toHaveBeenCalledWith(
      '/api/runs?domain=delivery&status=success&limit=10&offset=5',
    );
  });

  it('fetchRuns 空查询不带问号', async () => {
    const spy = mockJson({ items: [], total: 0 });
    await fetchRuns();
    expect(spy).toHaveBeenCalledWith('/api/runs');
  });

  const paths: Array<[() => Promise<unknown>, string]> = [
    [() => fetchRun(1), '/api/runs/1'],
    [() => fetchSpans(1), '/api/runs/1/spans'],
    [() => fetchRunHealth(1), '/api/runs/1/health'],
    [() => fetchSummary(), '/api/metrics/summary'],
    [() => fetchTimeseries(14), '/api/metrics/timeseries?days=14'],
    [() => fetchEvalTrends(), '/api/eval/trends'],
  ];

  it.each(paths)('请求路径 %2', async (call, expected) => {
    const spy = mockJson();
    await call();
    expect(spy).toHaveBeenCalledWith(expected);
  });

  it('非 2xx 抛错', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 500 }));
    await expect(fetchRuns()).rejects.toThrow('请求失败 500');
  });

  it('postFeedback 发送 JSON body', async () => {
    const spy = mockJson({ id: 1, run_id: 2, score: 5 });
    await postFeedback(2, 5, 'good');
    expect(spy).toHaveBeenCalledWith('/api/feedback/2', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ score: 5, comment: 'good', source: 'human' }),
    });
  });

  it('postFeedback 非 2xx 抛错', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 400 }));
    await expect(postFeedback(2, 5, 'bad')).rejects.toThrow('反馈失败 400');
  });
});
