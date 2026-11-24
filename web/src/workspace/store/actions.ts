/** 会话动作：发起/停止一轮、切档、确认续跑与挂起恢复。 */

import { fetchPending } from '../../api/client';
import { applyEvent, INITIAL_STATE, streamConfirm, streamRun } from '../../api/stream';
import type { Attachment, ChatMessage, ConfirmDecision, PermissionKey } from '../../types';
import { newId } from '../id';
import { savePermission } from '../history';
import {
  emit,
  ensureEntry,
  finalize,
  persistNow,
  replaceLastAssistant,
  schedulePersist,
  settle,
  syncAssistant,
} from './state';
import { startWatchdog } from './watchdog';

/** 发起一轮对话：追加 user + assistant 占位消息，并在后台消费 SSE。 */
export function send(
  sessionId: string,
  text: string,
  attachments: Attachment[] = [],
  permission?: PermissionKey,
  webSearch = false,
): void {
  if (!sessionId) return;
  const entry = ensureEntry(sessionId);
  if (entry.running) return;
  if (permission && permission !== entry.permission) setPermission(sessionId, permission);

  const now = new Date().toISOString();
  const files = attachments.filter((item) => item.status === 'ok');
  const user: ChatMessage = {
    id: newId('msg'),
    role: 'user',
    text,
    at: now,
    steps: [],
    attachments: files.length > 0 ? files : undefined,
  };
  const assistant: ChatMessage = {
    id: newId('msg'),
    role: 'assistant',
    text: '',
    at: now,
    steps: [],
    pending: true,
    inputTokens: 0,
    outputTokens: 0,
    runId: null,
    endedAt: null,
  };
  entry.messages = [...entry.messages, user, assistant];
  entry.stream = { ...INITIAL_STATE, running: true, startedAt: now };
  entry.running = true;
  entry.generation += 1;
  const generation = entry.generation;

  const controller = new AbortController();
  entry.abort = controller;

  persistNow(entry);
  emit(entry);

  const watchdog = startWatchdog(entry, generation, controller);

  void streamRun(
    sessionId,
    text,
    (event) => {
      if (generation !== entry.generation) return;
      entry.stream = applyEvent(entry.stream, event);
      syncAssistant(entry);
      schedulePersist(entry);
      emit(entry);
    },
    controller.signal,
    newId('req'),
    files.map((item) => item.id),
    entry.permission,
    webSearch,
    watchdog.touch,
  )
    .then(() => {
      if (generation !== entry.generation) return;
      settle(entry, 'done');
    })
    .catch((err: unknown) => {
      if (generation !== entry.generation) return;
      if (controller.signal.aborted) return;
      entry.stream = {
        ...entry.stream,
        running: false,
        error: err instanceof Error ? err.message : String(err),
      };
      syncAssistant(entry);
      finalize(entry, 'error');
    })
    .finally(() => watchdog.dispose());
}

/** 主动停止：推进代次，旧流回调全部失效。 */
export function stop(sessionId: string): void {
  const entry = ensureEntry(sessionId);
  if (!entry || !entry.running) return;
  entry.generation += 1;
  entry.abort?.abort();
  entry.abort = null;
  entry.stream = { ...entry.stream, running: false };
  finalize(entry, 'stopped');
}

/** 切换本会话的档位（本地态）：运行中的热替换由调用方另发端点请求。 */
export function setPermission(sessionId: string, key: PermissionKey): void {
  const entry = ensureEntry(sessionId);
  if (entry.permission === key) return;
  entry.permission = key;
  savePermission(sessionId, key);
  emit(entry);
}

/** 提交确认并接着跑同一条 run（不新开一轮，所以不追加消息）。 */
export function confirm(sessionId: string, decisions: ConfirmDecision[]): void {
  const entry = ensureEntry(sessionId);
  if (entry.running) return;
  const awaiting = entry.stream.awaiting ?? entry.messages[entry.messages.length - 1]?.awaiting;
  if (!awaiting?.runId) return;

  entry.running = true;
  // 卡片先收起来：正在执行时还挂着「等待确认」会误导
  entry.stream = { ...entry.stream, running: true, error: null, awaiting: null };
  // 消息上的挂起态也要一起清，否则快照会回落到它、卡片又冒出来
  syncAssistant(entry);
  entry.generation += 1;
  const generation = entry.generation;
  const controller = new AbortController();
  entry.abort = controller;
  const watchdog = startWatchdog(entry, generation, controller);
  emit(entry);

  void streamConfirm(
    awaiting.runId,
    sessionId,
    decisions,
    (event) => {
      if (generation !== entry.generation) return;
      entry.stream = applyEvent(entry.stream, event);
      syncAssistant(entry);
      schedulePersist(entry);
      emit(entry);
    },
    controller.signal,
    entry.permission,
    watchdog.touch,
  )
    .then(() => {
      if (generation !== entry.generation) return;
      settle(entry, 'done');
    })
    .catch((err: unknown) => {
      if (generation !== entry.generation) return;
      if (controller.signal.aborted) return;
      entry.stream = {
        ...entry.stream,
        running: false,
        error: err instanceof Error ? err.message : String(err),
      };
      syncAssistant(entry);
      finalize(entry, 'error');
    })
    .finally(() => watchdog.dispose());
}

/** 刷新后恢复挂起卡片：权威状态在服务端，前端只是把它取回来。 */
export async function refreshPending(sessionId: string): Promise<void> {
  const entry = ensureEntry(sessionId);
  const last = entry.messages[entry.messages.length - 1];
  const awaiting = entry.stream.awaiting ?? last?.awaiting;
  if (entry.running || !awaiting?.runId) return;
  try {
    const pending = await fetchPending(awaiting.runId, sessionId);
    entry.stream = {
      ...entry.stream,
      awaiting: {
        runId: pending.run_id,
        calls: pending.tool_calls,
        expiresAt: pending.expires_at,
      },
    };
    syncAssistant(entry);
  } catch {
    // 404 / 403 / 409：挂起已失效（超时、已确认、换实例），把卡片清掉
    entry.stream = { ...entry.stream, awaiting: null };
    if (last?.role === 'assistant') replaceLastAssistant(entry, { ...last, awaiting: undefined });
  }
  emit(entry);
}
