/** 历史兜底与侧栏数据源：本地缓存 + 内存最新状态合并。 */

import { fetchRuns } from '../../api/client';
import type { ChatMessage, Run } from '../../types';
import { parseWebAccess } from '../web-access';
import { listStoredSessions, type StoredSession } from '../history';
import { toSessionTitle } from '../sidebar-utils';
import { emit, ensureEntry, entries, type Entry } from './state';

function runToMessages(run: Run): ChatMessage[] {
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
      steps: [],
      endedAt: run.ended_at,
      modelName: run.model,
      inputTokens: run.input_tokens,
      outputTokens: run.output_tokens,
      runId: run.id,
      webAccess: parseWebAccess(run.web_access),
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
      entry.messages = mine.flatMap(runToMessages);
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
