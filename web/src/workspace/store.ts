/** 会话工作台的状态中枢：模块级 Map 持有每个会话的消息与流，组件通过 useSyncExternalStore 订阅。
 *
 * 放在 React 组件之外，切换 tab / 会话 / 重挂载都不会丢消息，流也能在后台继续跑。
 */
import { useCallback, useSyncExternalStore } from 'react';
import { fetchRuns } from '../api/client';
import {
  applyEvent,
  INITIAL_STATE,
  streamRun,
  type StreamState,
} from '../api/useRunStream';
import type { Attachment, ChatMessage, Run } from '../types';
import {
  loadMessages,
  saveMessages,
  listStoredSessions,
  type StoredSession,
} from './history';
import { newId } from './id';
import { toSessionTitle } from './sidebar-utils';

/** 超过该时长没有任何事件即判定连接假死。 */
const WATCHDOG_MS = 60_000;
/** 流式过程中写盘节流，避免逐 token 打满 localStorage。 */
const PERSIST_MS = 500;

export interface SessionSnapshot {
  sessionId: string;
  messages: ChatMessage[];
  running: boolean;
  loading: boolean;
  eventCount: number;
  inputTokens: number;
  outputTokens: number;
  runId: number | null;
  error: string | null;
  modelName: string;
}

interface Entry {
  sessionId: string;
  messages: ChatMessage[];
  stream: StreamState;
  running: boolean;
  loading: boolean;
  hydrated: boolean;
  abort: AbortController | null;
  /** 代次：每次发起/停止 +1，旧流的回调据此丢弃，避免污染新会话。 */
  generation: number;
  persistTimer: ReturnType<typeof setTimeout> | null;
  snapshot: SessionSnapshot;
}

const entries = new Map<string, Entry>();
const listeners = new Set<() => void>();
let version = 0;

function toSnapshot(entry: Entry): SessionSnapshot {
  return {
    sessionId: entry.sessionId,
    messages: entry.messages,
    running: entry.running,
    loading: entry.loading,
    eventCount: entry.stream.eventCount,
    inputTokens: entry.stream.inputTokens,
    outputTokens: entry.stream.outputTokens,
    runId: entry.stream.runId,
    error: entry.stream.error,
    modelName: entry.stream.modelName,
  };
}

function ensureEntry(sessionId: string): Entry {
  const existing = entries.get(sessionId);
  if (existing) return existing;
  const messages = loadMessages(sessionId) ?? [];
  const entry: Entry = {
    sessionId,
    messages,
    stream: { ...INITIAL_STATE },
    running: false,
    loading: false,
    hydrated: messages.length > 0,
    abort: null,
    generation: 0,
    persistTimer: null,
    snapshot: {
      sessionId,
      messages,
      running: false,
      loading: false,
      eventCount: 0,
      inputTokens: 0,
      outputTokens: 0,
      runId: null,
      error: null,
      modelName: '',
    },
  };
  entries.set(sessionId, entry);
  return entry;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function emit(entry: Entry): void {
  entry.snapshot = toSnapshot(entry);
  version += 1;
  for (const listener of [...listeners]) listener();
}

function persistNow(entry: Entry): void {
  if (entry.persistTimer) {
    clearTimeout(entry.persistTimer);
    entry.persistTimer = null;
  }
  saveMessages(entry.sessionId, entry.messages);
}

function schedulePersist(entry: Entry): void {
  if (entry.persistTimer) return;
  entry.persistTimer = setTimeout(() => {
    entry.persistTimer = null;
    saveMessages(entry.sessionId, entry.messages);
  }, PERSIST_MS);
}

function replaceLastAssistant(entry: Entry, next: ChatMessage): void {
  const idx = entry.messages.length - 1;
  entry.messages = entry.messages.map((message, i) => (i === idx ? next : message));
}

/** 把流状态回写到当前助手消息上。 */
function syncAssistant(entry: Entry): void {
  const current = entry.messages[entry.messages.length - 1];
  if (!current || current.role !== 'assistant') return;
  replaceLastAssistant(entry, {
    ...current,
    text: entry.stream.text,
    steps: entry.stream.steps,
    error: entry.stream.error ?? undefined,
    pending: entry.stream.running,
    modelName: entry.stream.modelName || current.modelName,
    inputTokens: entry.stream.inputTokens,
    outputTokens: entry.stream.outputTokens,
    runId: entry.stream.runId ?? current.runId,
    endedAt: entry.stream.endedAt ?? current.endedAt,
  });
}

type FinalizeMode = 'done' | 'error' | 'stopped';

/** 结束态文案：无正文且无步骤时给一句可见占位，避免留下空气泡。 */
function placeholderText(
  mode: FinalizeMode,
  text: string,
  error: string | null,
  hasSteps: boolean,
): string {
  if (text) return text;
  if (mode === 'stopped' && !hasSteps) return '（已停止）';
  if (mode === 'done' && !error && !hasSteps) return '（无输出）';
  return text;
}

/** 收尾当前流：落盘、结束 pending，并在无产出时给出可见占位。 */
function finalize(entry: Entry, mode: FinalizeMode): void {
  entry.running = false;
  entry.stream = { ...entry.stream, running: false };
  entry.abort = null;
  const current = entry.messages[entry.messages.length - 1];
  if (current && current.role === 'assistant') {
    replaceLastAssistant(entry, {
      ...current,
      text: placeholderText(
        mode,
        entry.stream.text,
        entry.stream.error,
        entry.stream.steps.length > 0,
      ),
      steps: entry.stream.steps,
      error: entry.stream.error ?? current.error,
      pending: false,
      stopped: mode === 'stopped' || undefined,
      endedAt: entry.stream.endedAt ?? new Date().toISOString(),
    });
  }
  persistNow(entry);
  emit(entry);
}

/** 发起一轮对话：追加 user + assistant 占位消息，并在后台消费 SSE。 */
export function send(sessionId: string, text: string, attachments: Attachment[] = []): void {
  if (!sessionId) return;
  const entry = ensureEntry(sessionId);
  if (entry.running) return;

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

  let lastEventAt = Date.now();
  const watchdog = setInterval(() => {
    if (generation !== entry.generation || !entry.running) return;
    if (Date.now() - lastEventAt < WATCHDOG_MS) return;
    entry.stream = {
      ...entry.stream,
      error: `服务端 ${WATCHDOG_MS / 1000} 秒无响应，已断开`,
      running: false,
    };
    controller.abort();
    finalize(entry, 'error');
  }, 5000);

  void streamRun(
    sessionId,
    text,
    (event) => {
      if (generation !== entry.generation) return;
      lastEventAt = Date.now();
      entry.stream = applyEvent(entry.stream, event);
      syncAssistant(entry);
      schedulePersist(entry);
      emit(entry);
    },
    controller.signal,
    newId('req'),
    files.map((item) => item.id),
  )
    .then(() => {
      if (generation !== entry.generation) return;
      finalize(entry, 'done');
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
    .finally(() => clearInterval(watchdog));
}

/** 主动停止：推进代次，旧流回调全部失效。 */
export function stop(sessionId: string): void {
  const entry = entries.get(sessionId);
  if (!entry || !entry.running) return;
  entry.generation += 1;
  entry.abort?.abort();
  entry.abort = null;
  entry.stream = { ...entry.stream, running: false };
  finalize(entry, 'stopped');
}

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
  } catch {
    // 后端不可用：保留空历史，用户仍可发起新问题
    entry.hydrated = true;
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

/** 订阅单个会话；消息/流状态变化时返回新的快照。 */
export function useSession(sessionId: string): SessionSnapshot {
  const getSnapshot = useCallback(() => ensureEntry(sessionId).snapshot, [sessionId]);
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** 订阅全局版本号，用于侧栏等需要感知任意会话变化的地方。 */
export function useStoreVersion(): number {
  const getSnapshot = useCallback(() => version, []);
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** 测试用：清空内存态与监听器。 */
export function resetStore(): void {
  for (const entry of entries.values()) {
    if (entry.persistTimer) clearTimeout(entry.persistTimer);
    entry.abort?.abort();
  }
  entries.clear();
  listeners.clear();
  version = 0;
}
