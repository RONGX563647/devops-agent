/** 会话工作台的状态中枢：模块级 Map 持有每个会话的消息与流，组件通过 useSyncExternalStore 订阅。
 *
 * 放在 React 组件之外，切换 tab / 会话 / 重挂载都不会丢消息，流也能在后台继续跑。
 */
import { useCallback, useSyncExternalStore } from 'react';
import { fetchPending, fetchRuns } from '../api/client';
import {
  applyEvent,
  INITIAL_STATE,
  streamConfirm,
  streamRun,
  type StreamState,
} from '../api/useRunStream';
import type {
  Attachment,
  AwaitingState,
  ChatMessage,
  CompressionRecord,
  ConfirmDecision,
  PermissionKey,
  Run,
} from '../types';
import {
  loadMessages,
  loadPermission,
  saveMessages,
  savePermission,
  listStoredSessions,
  type StoredSession,
} from './history';
import { newId } from './id';
import { toSessionTitle } from './sidebar-utils';

/** 首包宽限：模型排队首 token 常需几十秒，这段给足容错。 */
const FIRST_EVENT_MS = 90_000;
/** 有事件后的静默窗：收到任意字节即重置，超过才判假死。 */
const IDLE_MS = 45_000;
/** 看门狗扫描周期；误判只会晚一个周期，换来更低的定时器开销。 */
const WATCHDOG_TICK_MS = 5_000;
/** 流式过程中写盘节流，避免逐 token 打满 localStorage。 */
const PERSIST_MS = 500;
/** 新会话的默认档位；服务端会用 /api/permissions 的真实默认覆盖它。 */
export const DEFAULT_PERMISSION: PermissionKey = 'workspace';

const PERMISSION_KEYS: readonly PermissionKey[] = [
  'readonly',
  'workspace',
  'execute',
  'unrestricted',
  'auto',
];

function resolvePermission(raw: string | null): PermissionKey {
  return PERMISSION_KEYS.find((key) => key === raw) ?? DEFAULT_PERMISSION;
}

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
  /** 本会话生效的权限档位。 */
  permission: PermissionKey;
  /** 等待确认的挂起态；非空时发送按钮应禁用。 */
  awaiting: AwaitingState | null;
  /** 当前上下文占用（token）；没拿到任何用量时为 null，圆环不画。 */
  contextTokens: number | null;
  /** 触发压缩的阈值，来自 CONTEXT_BUDGET 或上下文提示。 */
  contextThreshold: number | null;
  /** 模型窗口大小，用于把阈值换算成占用比例。 */
  contextSize: number | null;
  /** 本会话发生过的压缩检查（含未真正压缩的 skipped 记录）。 */
  compressions: CompressionRecord[];
}

interface Entry {
  sessionId: string;
  messages: ChatMessage[];
  stream: StreamState;
  running: boolean;
  loading: boolean;
  hydrated: boolean;
  /** 上次重建历史失败：置位后仍允许重试，避免一次抖动永久空白。 */
  hydrateFailed: boolean;
  abort: AbortController | null;
  permission: PermissionKey;
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
    permission: entry.permission,
    awaiting: entry.stream.awaiting ?? entry.messages[entry.messages.length - 1]?.awaiting ?? null,
    contextTokens: entry.stream.contextTokens,
    contextThreshold: entry.stream.contextThreshold,
    contextSize: entry.stream.contextSize,
    compressions: entry.stream.compressions,
  };
}

function ensureEntry(sessionId: string): Entry {
  const existing = entries.get(sessionId);
  if (existing) return existing;
  const messages = loadMessages(sessionId) ?? [];
  const permission = resolvePermission(loadPermission(sessionId));
  const entry: Entry = {
    sessionId,
    messages,
    stream: { ...INITIAL_STATE },
    running: false,
    loading: false,
    hydrated: messages.length > 0,
    hydrateFailed: false,
    abort: null,
    permission,
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
      permission,
      awaiting: null,
      contextTokens: null,
      contextThreshold: null,
      contextSize: null,
      compressions: [],
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
    awaiting: entry.stream.awaiting ?? undefined,
    modelName: entry.stream.modelName || current.modelName,
    inputTokens: entry.stream.inputTokens,
    outputTokens: entry.stream.outputTokens,
    cacheInputTokens: entry.stream.cacheInputTokens,
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
      awaiting: undefined,
      endedAt: entry.stream.endedAt ?? new Date().toISOString(),
    });
  }
  persistNow(entry);
  emit(entry);
}

/** 挂起收尾：这一轮没结束，只是停住等确认，所以不写 endedAt、不写占位文本。 */
function finalizeParked(entry: Entry): void {
  entry.running = false;
  entry.stream = { ...entry.stream, running: false };
  entry.abort = null;
  const current = entry.messages[entry.messages.length - 1];
  if (current && current.role === 'assistant') {
    replaceLastAssistant(entry, {
      ...current,
      text: entry.stream.text,
      steps: entry.stream.steps,
      error: entry.stream.error ?? current.error,
      pending: false,
      awaiting: entry.stream.awaiting ?? current.awaiting,
      endedAt: null,
    });
  }
  persistNow(entry);
  emit(entry);
}

/** 流结束后的统一收尾：有挂起态就停在确认，否则按正常/失败结束。 */
function settle(entry: Entry, mode: FinalizeMode): void {
  if (entry.stream.awaiting) finalizeParked(entry);
  else finalize(entry, mode);
}

/** 看门狗句柄：touch() 在任意字节到达时调用；dispose() 在流收尾时清理。 */
interface Watchdog {
  touch: () => void;
  dispose: () => void;
}

/** 建立流看门狗：字节层保活 + 首包/静默双阈值 + 回前台重校准。
 *
 * - 首包前用 FIRST_EVENT_MS（模型排队慢），收到任意字节后改用 IDLE_MS。
 * - touch 由 consumeStream 的 onActivity 驱动，注释帧/未闭合 chunk 也算活动。
 * - 隐藏标签页的 setInterval 会被节流，回前台时若本没超时就重置计时，
 *   避免节流期的假死补判把一条仍在收字节的流砍掉。
 */
function startWatchdog(
  entry: Entry,
  generation: number,
  controller: AbortController,
): Watchdog {
  let lastEventAt = Date.now();
  let firstByteSeen = false;

  const expire = () => {
    if (generation !== entry.generation || !entry.running) return;
    const limit = firstByteSeen ? IDLE_MS : FIRST_EVENT_MS;
    if (Date.now() - lastEventAt < limit) return;
    entry.stream = {
      ...entry.stream,
      error: `服务端 ${Math.round(limit / 1000)} 秒无响应，已断开`,
      running: false,
    };
    controller.abort();
    finalize(entry, 'error');
  };

  const timer = setInterval(expire, WATCHDOG_TICK_MS);
  const onVisible = () => {
    if (document.visibilityState !== 'visible') return;
    if (generation !== entry.generation || !entry.running) return;
    // 只重置「本来还没超时」的流；真断流的计时不因切回前台而清零。
    if (Date.now() - lastEventAt < IDLE_MS) lastEventAt = Date.now();
  };
  document.addEventListener('visibilitychange', onVisible);

  return {
    touch: () => {
      lastEventAt = Date.now();
      firstByteSeen = true;
    },
    dispose: () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    },
  };
}

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
  const entry = entries.get(sessionId);
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

function runToMessages(run: Run): ChatMessage[] {  return [
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
