import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  ApiError,
  cancelOpenJudgeRun,
  deleteAttachment,
  deleteSecret,
  fetchHealth,
  fetchInputStages,
  fetchOpenJudgeBatches,
  fetchOpenJudgeCalibration,
  fetchOpenJudgeCases,
  fetchOpenJudgeRun,
  fetchOpenJudgeRuns,
  fetchOpenJudgeScoringConfig,
  fetchOpenJudgeSeries,
  fetchOpenJudgeSuites,
  fetchPending,
  fetchPermissions,
  fetchRun,
  fetchBreakdown,
  fetchMetricsHealth,
  fetchRunHealth,
  fetchRuns,
  fetchSecrets,
  fetchSpans,
  fetchSummary,
  fetchTimeseries,
  postFeedback,
  startOpenJudgeRun,
  switchSessionPermission,
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

function mockJsonStatus(body: unknown, status: number) {
  return vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(new Response(JSON.stringify(body), { status }));
}

describe('client', () => {
  it('fetchHealth 打 /health，用来判断是不是 Echo 兜底', async () => {
    const spy = mockJson({ status: 'ok', runtime: { kind: 'echo', model: '', notice: '回显' } });
    const data = await fetchHealth();
    expect(spy).toHaveBeenCalledWith('/health');
    expect(data.runtime?.kind).toBe('echo');
  });

  it('switchSessionPermission 打切档端点并回传生效范围', async () => {
    const spy = mockJson({
      session_id: 's-1',
      permission: 'readonly',
      applied: true,
      effective_from: '下一次工具判定与下一次模型调用',
    });
    const data = await switchSessionPermission('s-1', 'readonly', false);
    expect(spy).toHaveBeenCalledWith('/api/session/s-1/permission', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: 's-1', permission: 'readonly', web_search: false }),
    });
    expect(data.applied).toBe(true);
    expect(data.effective_from).toContain('下一次工具判定');
  });

  it('switchSessionPermission 非 2xx 抛错', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 403 }));
    await expect(switchSessionPermission('s-1', 'unrestricted')).rejects.toThrow('切档失败 403');
  });

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
    [() => fetchMetricsHealth(), '/api/metrics/health?limit=200'],
    [() => fetchMetricsHealth(50), '/api/metrics/health?limit=50'],
    [() => fetchBreakdown(), '/api/metrics/breakdown'],
    [() => fetchTimeseries(14), '/api/metrics/timeseries?days=14'],
    [() => fetchSecrets(), '/api/secrets'],
    [() => fetchOpenJudgeSuites(), '/api/openjudge/suites'],
    [() => fetchOpenJudgeSeries(30), '/api/openjudge/series?days=30'],
    [() => fetchOpenJudgeRun(5), '/api/openjudge/runs/5'],
    [() => fetchOpenJudgeRuns(10), '/api/openjudge/runs?limit=10'],
  ];

  it.each(paths)('请求路径 %2', async (call, expected) => {
    const spy = mockJson();
    await call();
    expect(spy).toHaveBeenCalledWith(expected);
  });

  it('fetchOpenJudgeScoringConfig 拉判分方案与门槛', async () => {
    const spy = mockJson({
      plan: { name: 'oj-plan-v1', sha: 'abc', judges: [] },
      pass_judge: 0.6,
      track_rules: {},
    });
    const data = await fetchOpenJudgeScoringConfig();
    expect(spy).toHaveBeenCalledWith('/api/openjudge/scoring-config');
    expect(data.pass_judge).toBe(0.6);
  });

  it('fetchOpenJudgeCalibration 读取当前校准状态', async () => {
    const spy = mockJson({ checked: true, valid: false, missing: ['correctness'] });
    const data = await fetchOpenJudgeCalibration();
    expect(spy).toHaveBeenCalledWith('/api/openjudge/calibration');
    expect(data.valid).toBe(false);
    expect(data.missing).toEqual(['correctness']);
  });

  it('fetchOpenJudgeBatches 把 verdict 拼进查询串', async () => {
    const spy = mockJson({ items: [], total: 0 });
    await fetchOpenJudgeBatches(20, 'fail');
    expect(spy).toHaveBeenCalledWith('/api/openjudge/batches?limit=20&offset=0&verdict=fail');
  });

  it('fetchOpenJudgeCases 把 pageSize 映射为 limit 并带上状态与主题', async () => {
    const spy = mockJson({ items: [], total: 0, limit: 25, offset: 10 });
    await fetchOpenJudgeCases(3, { status: 'success', subject: 'repo', pageSize: 25, offset: 10 });
    expect(spy).toHaveBeenCalledWith(
      '/api/openjudge/batches/3/cases?status=success&subject=repo&limit=25&offset=10',
    );
  });

  it('startOpenJudgeRun POST JSON body 并回传 run_id', async () => {
    const spy = mockJson({ run_id: 42, total: 10 });
    const data = await startOpenJudgeRun({ track: 'A', limit: 10, judge: false });
    expect(spy).toHaveBeenCalledWith('/api/openjudge/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ track: 'A', limit: 10, judge: false }),
    });
    expect(data.run_id).toBe(42);
  });

  it('startOpenJudgeRun 409 时抛 ApiError，detail 里带正在跑的 run_id', async () => {
    mockJsonStatus({ detail: { message: '已有任务在跑', run_id: 77 } }, 409);

    const err = await startOpenJudgeRun({ track: 'A' }).catch((error: unknown) => error);

    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).detail).toEqual({ message: '已有任务在跑', run_id: 77 });
  });

  it('startOpenJudgeRun 400 时 detail 是字符串原因', async () => {
    mockJsonStatus({ detail: '在线评测未开启' }, 400);

    const err = await startOpenJudgeRun({}).catch((error: unknown) => error);

    expect((err as ApiError).detail).toBe('在线评测未开启');
  });

  it('cancelOpenJudgeRun 走 POST 取消端点', async () => {
    const spy = mockJson({ id: 5, status: 'cancelled' });
    const data = await cancelOpenJudgeRun(5);
    expect(spy).toHaveBeenCalledWith('/api/openjudge/runs/5/cancel', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    });
    expect(data.status).toBe('cancelled');
  });

  it('非 2xx 抛错', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('', { status: 500 }));
    await expect(fetchRuns()).rejects.toThrow('请求失败 500');
  });

  it('fetchPermissions 拉本部署开放的档位', async () => {
    const spy = mockJson({ default: 'workspace', available: [] });
    const catalog = await fetchPermissions();
    expect(spy).toHaveBeenCalledWith('/api/permissions');
    expect(catalog.default).toBe('workspace');
  });

  it('fetchPending 把 session_id 编码进查询串，服务端据此校验归属', async () => {
    const spy = mockJson({ run_id: 3, status: 'parked', expires_at: null, tool_calls: [] });
    const pending = await fetchPending(3, 's 1');
    expect(spy).toHaveBeenCalledWith('/run/3/pending?session_id=s%201');
    expect(pending.run_id).toBe(3);
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
