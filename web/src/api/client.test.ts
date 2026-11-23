import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  deleteAttachment,
  deleteSecret,
  fetchEvalTrends,
  fetchInputStages,
  fetchRun,
  fetchRunHealth,
  fetchRuns,
  fetchSecrets,
  fetchSpans,
  fetchSummary,
  fetchTimeseries,
  postFeedback,
  uploadAttachment,
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
    [() => fetchInputStages(1), '/api/runs/1/input-stages'],
    [() => fetchRunHealth(1), '/api/runs/1/health'],
    [() => fetchSummary(), '/api/metrics/summary'],
    [() => fetchTimeseries(14), '/api/metrics/timeseries?days=14'],
    [() => fetchEvalTrends(), '/api/eval/trends'],
    [() => fetchSecrets(), '/api/secrets'],
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

  it('uploadAttachment 用 FormData 提交文件', async () => {
    const spy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response(JSON.stringify({ id: 'x', name: 'a.md' }), { status: 200 }));

    await uploadAttachment(new File(['hi'], 'a.md'));

    const [, init] = spy.mock.calls[0];
    expect(spy.mock.calls[0][0]).toBe('/api/attachments');
    expect(init?.method).toBe('POST');
    expect(init?.body).toBeInstanceOf(FormData);
    expect((init?.body as FormData).get('file')).toBeInstanceOf(File);
  });

  it('uploadAttachment 把 413 翻成体积超限文案', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 413 }));
    await expect(uploadAttachment(new File(['x'], 'big.zip'))).rejects.toThrow('文件超过上传上限');
  });

  it('uploadAttachment 其它错误码带状态值', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 500 }));
    await expect(uploadAttachment(new File(['x'], 'a.md'))).rejects.toThrow('上传失败 500');
  });

  it('deleteAttachment 走 DELETE', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
    await deleteAttachment('abc123');
    expect(spy).toHaveBeenCalledWith('/api/attachments/abc123', { method: 'DELETE' });
  });

  it('deleteSecret 对名字做 URL 编码', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));
    await deleteSecret('a b');
    expect(spy).toHaveBeenCalledWith('/api/secrets/a%20b', { method: 'DELETE' });
  });

  it('deleteSecret 非 2xx 抛错', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 404 }));
    await expect(deleteSecret('missing')).rejects.toThrow('删除失败 404');
  });
});
