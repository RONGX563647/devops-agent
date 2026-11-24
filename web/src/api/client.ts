/** 观测 API 客户端：只做 fetch + JSON 解析，缓存/轮询交给 TanStack Query。 */

import type {
  Attachment,
  EvalBatchDetail,
  EvalBatchList,
  EvalCaseList,
  EvalCompare,
  EvalTrends,
  InputStage,
  PendingState,
  PermissionCatalog,
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

/** 本部署开放的权限档位：前端只能用这里列出来的，越权档服务端会 403。 */
export function fetchPermissions(): Promise<PermissionCatalog> {
  return getJson<PermissionCatalog>('/api/permissions');
}

/** 刷新后恢复挂起态：必须带 session_id，服务端会校验归属。 */
export function fetchPending(runId: number, sessionId: string): Promise<PendingState> {
  return getJson<PendingState>(
    `/run/${runId}/pending?session_id=${encodeURIComponent(sessionId)}`,
  );
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

/** 评测批次列表。running 批次由调用方决定是否轮询。 */
export function fetchEvalBatches(limit = 20, offset = 0): Promise<EvalBatchList> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return getJson<EvalBatchList>(`/api/eval/batches?${params.toString()}`);
}

/** 单个批次详情：带分标签统计与用例快照。 */
export function fetchEvalBatch(id: number): Promise<EvalBatchDetail> {
  return getJson<EvalBatchDetail>(`/api/eval/batches/${id}`);
}

export interface EvalCaseQuery {
  status?: string;
  tag?: string;
  limit?: number;
  offset?: number;
}

/** 分页 / 筛选用例。 */
export function fetchEvalCases(id: number, params: EvalCaseQuery = {}): Promise<EvalCaseList> {
  const query = new URLSearchParams();
  if (params.status) query.set('status', params.status);
  if (params.tag) query.set('tag', params.tag);
  query.set('limit', String(params.limit ?? 50));
  query.set('offset', String(params.offset ?? 0));
  return getJson<EvalCaseList>(`/api/eval/batches/${id}/cases?${query.toString()}`);
}

/** 两次批次对比：verdict + 指标 delta + 变化用例。 */
export function fetchEvalCompare(base: number, head: number): Promise<EvalCompare> {
  const params = new URLSearchParams({ base: String(base), head: String(head) });
  return getJson<EvalCompare>(`/api/eval/compare?${params.toString()}`);
}

/** 工具注册表导出的 schema：Inspector 的「Schema」页签据此展示参数定义。 */
export interface ToolSchema {
  type: string;
  function: {
    name: string;
    description?: string;
    parameters?: unknown;
  };
}

/** 按档位取 schema：放开档实际挂了 Write/Edit，写死只读会让 Schema 页签答错。 */
export function fetchToolSchemas(profile?: string): Promise<ToolSchema[]> {
  const suffix = profile ? `?profile=${encodeURIComponent(profile)}` : '';
  return getJson<ToolSchema[]>(`/api/tools/schema${suffix}`);
}

/** 一个工具在上下文里占多少，以及它完整声明了什么。 */
export interface AgentToolContext {
  name: string;
  description: string;
  /** schema 序列化后的字符数：上下文的大头就在这里。 */
  schema_chars: number;
  tokens: number;
  parameters: Record<string, unknown>;
}

/** 本轮对话动手前手里有的东西：系统提示词、工具 schema、前缀指纹。 */
export interface AgentContext {
  /** 这一轮实际使用的权限档位；工具集随它变化。 */
  profile: string;
  profile_label: string;
  system_prompt: string;
  system_prompt_chars: number;
  system_prompt_tokens: number;
  tools: AgentToolContext[];
  tools_chars: number;
  tools_tokens: number;
  /** 系统提示 + 工具 schema 的 token 估算：能命中缓存的就是这一段。 */
  prefix_tokens: number;
  /** 前缀指纹：变了说明这一枪必然从缓存读不到。 */
  prefix_fingerprint: string;
  /** 档位约束：追加在每次请求的最后一条消息里，不占前缀。 */
  policy_note: string;
  policy_note_tokens: number;
  workdir: string;
}

export function fetchContext(session?: string): Promise<AgentContext> {
  const suffix = session ? `?session=${encodeURIComponent(session)}` : '';
  return getJson<AgentContext>(`/api/context${suffix}`);
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
