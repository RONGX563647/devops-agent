/** 观测 API 客户端：只做 fetch + JSON 解析，缓存/轮询交给 TanStack Query。 */

import type {
  EvalTrends,
  Run,
  RunHealth,
  RunList,
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

export function fetchRun(id: number): Promise<Run> {
  return getJson<Run>(`/api/runs/${id}`);
}

export function fetchSpans(id: number): Promise<Span[]> {
  return getJson<Span[]>(`/api/runs/${id}/spans`);
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
