/** 观测 API 客户端：只做 fetch + JSON 解析，缓存/轮询交给 TanStack Query。 */

import type {
  Attachment,
  EvalTrends,
  InputStage,
  Run,
  RunHealth,
  RunList,
  SecretMeta,
  Span,
  Summary,
  TimePoint,
} from '../types';

async function getJson<T>(path: string): Promise<T> {
  const resp = await fetch(path);
  if (!resp.ok) {
    throw new Error(`请求失败 ${resp.status}: ${path}`);
  }
  return (await resp.json()) as T;
}

export interface RunQuery {
  domain?: string;
  status?: string;
  limit?: number;
  offset?: number;
}

export function fetchRuns(q: RunQuery = {}): Promise<RunList> {
  const params = new URLSearchParams();
  if (q.domain) params.set('domain', q.domain);
  if (q.status) params.set('status', q.status);
  if (q.limit) params.set('limit', String(q.limit));
  if (q.offset) params.set('offset', String(q.offset));
  const suffix = params.toString();
  return getJson<RunList>(`/api/runs${suffix ? `?${suffix}` : ''}`);
}

/** 上传一份文件：服务端抽好正文后只回元信息，正文不回传浏览器。 */
export async function uploadAttachment(file: File): Promise<Attachment> {
  const form = new FormData();
  form.append('file', file);
  const resp = await fetch('/api/attachments', { method: 'POST', body: form });
  if (!resp.ok) {
    // 413 是体积超限：把服务端的原因透出来，比「请求失败」有用得多
    const detail = resp.status === 413 ? '文件超过上传上限' : `上传失败 ${resp.status}`;
    throw new Error(detail);
  }
  return (await resp.json()) as Attachment;
}

export async function deleteAttachment(id: string): Promise<void> {
  await fetch(`/api/attachments/${id}`, { method: 'DELETE' });
}

export function fetchSecrets(): Promise<SecretMeta[]> {
  return getJson<SecretMeta[]>('/api/secrets');
}

export async function deleteSecret(name: string): Promise<void> {
  const resp = await fetch(`/api/secrets/${encodeURIComponent(name)}`, { method: 'DELETE' });
  if (!resp.ok) throw new Error(`删除失败 ${resp.status}`);
}

export function fetchRun(id: number): Promise<Run> {
  return getJson<Run>(`/api/runs/${id}`);
}

export function fetchSpans(id: number): Promise<Span[]> {
  return getJson<Span[]>(`/api/runs/${id}/spans`);
}

export function fetchInputStages(id: number): Promise<InputStage[]> {
  return getJson<InputStage[]>(`/api/runs/${id}/input-stages`);
}

export function fetchRunHealth(id: number): Promise<RunHealth> {
  return getJson<RunHealth>(`/api/runs/${id}/health`);
}

export function fetchSummary(): Promise<Summary> {
  return getJson<Summary>('/api/metrics/summary');
}

export function fetchTimeseries(days = 7): Promise<TimePoint[]> {
  return getJson<TimePoint[]>(`/api/metrics/timeseries?days=${days}`);
}

export function fetchEvalTrends(): Promise<EvalTrends> {
  return getJson<EvalTrends>('/api/eval/trends');
}

export async function postFeedback(
  runId: number,
  score: number,
  comment: string,
): Promise<{ id: number; run_id: number; score: number }> {
  const resp = await fetch(`/api/feedback/${runId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ score, comment, source: 'human' }),
  });
  if (!resp.ok) {
    throw new Error(`反馈失败 ${resp.status}`);
  }
  return (await resp.json()) as { id: number; run_id: number; score: number };
}
