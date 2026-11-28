/** 历史兜底与侧栏数据源：本地缓存 + 内存最新状态合并。 */

import { fetchRuns, fetchSpans } from '../../api/client';
import type { ChatMessage, ProcessEntry, ProcessKind, Run, Span } from '../../types';
import { parseWebAccess } from '../web-access';
import { listStoredSessions, type StoredSession } from '../history';
import { toSessionTitle } from '../sidebar-utils';
import { emit, ensureEntry, entries, type Entry } from './state';

/** span 类型 → 过程步骤类别：权限确认在轨迹里是一条控制步骤。 */
const SPAN_KIND: Record<Span['type'], ProcessKind> = {
  model: 'model',
  tool: 'tool',
  memory: 'memory',
  permission: 'control',
};

/** 记忆 span 的名字是 `memory.<动作>`，直接显示英文代码没人看得懂。 */
const MEMORY_LABEL: Record<string, string> = {
  'memory.retrieve': '检索',
  'memory.trim': '裁剪',
  'memory.compress': '压缩',
  'memory.write': '写入',
};

/** 失败时优先用 error 原文：失败原因分类是按这段文字归并的，退回 output 会归错簇。 */
function spanDetail(span: Span): string {
  if (span.status === 'error') return span.error || span.output || '';
  return span.output || '';
}

/** 行首标题：工具与模型用原始标识，记忆动作翻成中文，其余留空给兜底。 */
function spanLabel(span: Span): string {
  if (span.type === 'memory') return MEMORY_LABEL[span.name] ?? span.name;
  if (span.type === 'model') return span.name || '模型调用';
  if (span.type === 'permission') return span.name || '权限确认';
  return span.name || '工具';
}

/** 起止时间：offset 相对 run 起点，duration 为 0 时不编一个结束时间出来。 */
function spanTimes(run: Run, span: Span): { at: string; endedAt: string | null } | null {
  const base = Date.parse(run.started_at);
  if (Number.isNaN(base)) return null;
  const offset = Math.max(0, span.start_offset_ms);
  const duration = Math.max(0, span.duration_ms ?? 0);
  return {
    at: new Date(base + offset).toISOString(),
    endedAt: duration > 0 ? new Date(base + offset + duration).toISOString() : null,
  };
}

/** 逐调用 token 分拆；老记录是 undefined，统一按 0 进图表。 */
function spanTokens(span: Span): { tokensIn: number; tokensOut: number; cacheTokens: number } {
  return {
    tokensIn: span.input_tokens ?? 0,
    tokensOut: span.output_tokens ?? 0,
    cacheTokens: span.cache_input_tokens ?? 0,
  };
}

/** span → 过程步骤。 */
function spanToStep(run: Run, span: Span): ProcessEntry | null {
  const times = spanTimes(run, span);
  if (!times) return null;
  const detail = spanDetail(span);
  return {
    id: `span-${run.id}-${span.id}`,
    kind: SPAN_KIND[span.type] ?? 'control',
    label: spanLabel(span),
    detail,
    state: span.status === 'error' ? 'error' : 'success',
    ...times,
    toolName: span.type === 'tool' ? span.name : undefined,
    args: span.input || undefined,
    result: detail || undefined,
    ...spanTokens(span),
  };
}

/** 一次运行的步骤：runs 只给输入输出，步骤得按 span 偏移还原。
 *
 * 不补这一步，历史会话的轨迹页永远是「1 步 · 0 工具」——泳道空白、实测耗时
 * 显示 `--`、调用视图分不出组，看着像功能没做，其实是没数据。
 * 单个 run 拉不到 spans 只丢它自己的步骤，不影响整条会话。
 */
async function stepsOf(run: Run): Promise<ProcessEntry[]> {
  const spans = await fetchSpans(run.id).catch(() => [] as Span[]);
  const ordered = [...spans].sort(
    (a, b) => (a.seq ?? a.start_offset_ms) - (b.seq ?? b.start_offset_ms),
  );
  return ordered
    .map((span) => spanToStep(run, span))
    .filter((step): step is ProcessEntry => step !== null);
}

async function runToMessages(run: Run): Promise<ChatMessage[]> {
  const steps = await stepsOf(run);
  return [
    {
      id: `run-${run.id}-user`,
      role: 'user',
      text: run.input,
      at: run.started_at,
      steps: [],
    },
    {
      id: `run-${run.id}-assistant`,
      role: 'assistant',
      text: run.output || '（无输出）',
      at: run.ended_at ?? run.started_at,
      steps,
      endedAt: run.ended_at,
      modelName: run.model,
      inputTokens: run.input_tokens,
      outputTokens: run.output_tokens,
      runId: run.id,
      webAccess: parseWebAccess(run.web_access),
      // 运行级失败原因：老数据的 spans 里没有 error，只有 runs.error 记得下来
      error: run.error || undefined,
    },
  ];
}

/** 首次选中会话且本地无历史时，用 /api/runs 重建一份只读历史兜底。 */
export async function ensureHistory(sessionId: string): Promise<void> {
  if (!sessionId) return;
  const entry = ensureEntry(sessionId);
  if (entry.hydrated || entry.loading || entry.running || entry.messages.length > 0) return;
  entry.loading = true;
  emit(entry);
  try {
    const list = await fetchRuns({ limit: 200 });
    const mine = list.items
      .filter((run) => run.session_id === sessionId)
      .sort((a, b) => a.started_at.localeCompare(b.started_at));
    if (entry.messages.length === 0 && mine.length > 0) {
      // 并发拉 spans：串行会让 30 轮的历史等上好几秒，前端看起来像卡死
      const rebuilt = await Promise.all(mine.map((run) => runToMessages(run)));
      if (entry.messages.length === 0) entry.messages = rebuilt.flat();
    }
    entry.hydrated = true;
    entry.hydrateFailed = false;
  } catch {
    // 后端不可用：保留空历史，用户仍可发起新问题。
    // 不置 hydrated——否则一次 500 就让这个会话在本次 tab 生命周期里再也拉不回来；
    // loading 在 finally 里复位，下次选中会话时会自然重试。
    entry.hydrateFailed = true;
  } finally {
    entry.loading = false;
    emit(entry);
  }
}

function metaOf(entry: Entry): StoredSession {
  const firstUser = entry.messages.find((message) => message.role === 'user');
  const last = entry.messages[entry.messages.length - 1];
  return {
    id: entry.sessionId,
    title: toSessionTitle(firstUser?.text ?? ''),
    lastAt: last?.at ?? new Date().toISOString(),
    messageCount: entry.messages.filter((message) => message.role === 'user').length,
  };
}

/** 侧栏数据源：本地缓存 + 内存中的最新状态合并。 */
export function listSessions(): StoredSession[] {
  const merged = new Map(listStoredSessions().map((item) => [item.id, item]));
  for (const entry of entries.values()) {
    if (!entry.messages.some((message) => message.role === 'user')) continue;
    merged.set(entry.sessionId, metaOf(entry));
  }
  return [...merged.values()].sort((a, b) => b.lastAt.localeCompare(a.lastAt));
}
