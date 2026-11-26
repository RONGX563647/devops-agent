/**
 * 观测 API 客户端：只做 fetch + JSON 解析，缓存/轮询交给 TanStack Query。
 *
 * 这个文件同时是 `api/` 的公共出口：传输层在 `http.ts`，评测接口在
 * `openjudge.ts`，调用点统一从 `api/client` 引入，避免改一次分层就动几十个 import。
 */

import type {
  Attachment,
  Breakdown,
  Health,
  HealthMetrics,
  InputStage,
  ModelConfig,
  ModelPatch,
  PendingState,
  PermissionCatalog,
  Run,
  RunHealth,
  RunList,
  SecretMeta,
  SecretValue,
  Span,
  SessionModelConfig,
  Summary,
  TimePoint,
} from '../types';
import { getJson, sendJson } from './http';

export { ApiError } from './http';
export * from './openjudge';

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

/** 受控读回一条凭据明文；每次调用服务端都会记一次使用。 */
export function fetchSecretValue(name: string): Promise<SecretValue> {
  return getJson<SecretValue>(`/api/secrets/${encodeURIComponent(name)}/value`);
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

export function fetchMetricsHealth(limit = 200): Promise<HealthMetrics> {
  return getJson<HealthMetrics>(`/api/metrics/health?limit=${limit}`);
}

export function fetchBreakdown(): Promise<Breakdown> {
  return getJson<Breakdown>('/api/metrics/breakdown');
}

export function fetchSummary(): Promise<Summary> {
  return getJson<Summary>('/api/metrics/summary');
}

export function fetchTimeseries(days = 7): Promise<TimePoint[]> {
  return getJson<TimePoint[]>(`/api/metrics/timeseries?days=${days}`);
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

export interface PermissionSwitchResult {
  session_id: string;
  permission: string;
  /** 已热替换当前实例；false 表示没有活跃实例，下一轮才装配。 */
  applied: boolean;
  effective_from: string;
}

/** 会话中切档：服务端原地改权限上下文与工具集，当前流继续跑。 */
export async function switchSessionPermission(
  sessionId: string,
  permission: string,
  webSearch?: boolean,
): Promise<PermissionSwitchResult> {
  const resp = await fetch(`/api/session/${encodeURIComponent(sessionId)}/permission`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, permission, web_search: webSearch }),
  });
  if (!resp.ok) {
    throw new Error(`切档失败 ${resp.status}`);
  }
  return (await resp.json()) as PermissionSwitchResult;
}

/** 运行时模式：Echo 兜底时前端要把它显式说出来，否则回显会被当成「答非所问」 */
export function fetchHealth(): Promise<Health> {
  return getJson<Health>('/health');
}

// --- 模型参数 -----------------------------------------------------------


/** 当前生效参数 + 逐项来源。带 session 时把该会话的覆盖一起算进来。 */
export function fetchModelConfig(session?: string, domain?: string): Promise<ModelConfig> {
  const params = new URLSearchParams();
  if (session) params.set('session', session);
  if (domain) params.set('domain', domain);
  const suffix = params.toString();
  return getJson<ModelConfig>(`/api/model-config${suffix ? `?${suffix}` : ''}`);
}

/** 改部署默认（影响所有会话）。patch 里给 null 表示「重置该项为下层默认」。 */
export function patchModelConfig(
  patch: ModelPatch,
  version?: number,
): Promise<ModelConfig> {
  return sendJson<ModelConfig>('/api/model-config', 'PATCH', { patch, version });
}

/** 清除部署默认里的覆盖项，回落到 yaml / env / 框架默认。 */
export function resetModelConfig(
  body: { keys?: string[]; all?: boolean },
): Promise<ModelConfig> {
  return sendJson<ModelConfig>('/api/model-config/reset', 'POST', body);
}

/** 只改这个会话（合并语义）。运行中也能改：服务端原地热替换。 */
export function patchSessionModelConfig(
  sessionId: string,
  overrides: ModelPatch,
): Promise<SessionModelConfig> {
  return sendJson<SessionModelConfig>(
    `/api/session/${encodeURIComponent(sessionId)}/model-config`,
    'PATCH',
    { overrides, session_id: sessionId },
  );
}

/** 清空该会话的全部覆盖。 */
export function clearSessionModelConfig(sessionId: string): Promise<SessionModelConfig> {
  return sendJson<SessionModelConfig>(
    `/api/session/${encodeURIComponent(sessionId)}/model-config`,
    'DELETE',
  );
}

