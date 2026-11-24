/** 会话工作台的状态中枢：模块级 Map 持有每个会话的消息与流，组件通过 useSyncExternalStore 订阅。
 *
 * 放在 React 组件之外，切换 tab / 会话 / 重挂载都不会丢消息，流也能在后台继续跑。
 */

import { useCallback, useSyncExternalStore } from 'react';
import { INITIAL_STATE, type StreamState } from '../../api/useRunStream';
import type { AwaitingState, ChatMessage, CompressionRecord, PermissionKey } from '../../types';
import { loadMessages, loadPermission, saveMessages } from '../history';

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

export interface Entry {
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

export const entries = new Map<string, Entry>();
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

export function ensureEntry(sessionId: string): Entry {
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

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function emit(entry: Entry): void {
  entry.snapshot = toSnapshot(entry);
  version += 1;
  for (const listener of [...listeners]) listener();
}

export function persistNow(entry: Entry): void {
  if (entry.persistTimer) {
    clearTimeout(entry.persistTimer);
    entry.persistTimer = null;
  }
  saveMessages(entry.sessionId, entry.messages);
}

export function schedulePersist(entry: Entry): void {
  if (entry.persistTimer) return;
  entry.persistTimer = setTimeout(() => {
    entry.persistTimer = null;
    saveMessages(entry.sessionId, entry.messages);
  }, PERSIST_MS);
}

export function replaceLastAssistant(entry: Entry, next: ChatMessage): void {
  const idx = entry.messages.length - 1;
  entry.messages = entry.messages.map((message, i) => (i === idx ? next : message));
}

/** 把流状态回写到当前助手消息上。 */
export function syncAssistant(entry: Entry): void {
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

export type FinalizeMode = 'done' | 'error' | 'stopped';

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
export function finalize(entry: Entry, mode: FinalizeMode): void {
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
export function settle(entry: Entry, mode: FinalizeMode): void {
  if (entry.stream.awaiting) finalizeParked(entry);
  else finalize(entry, mode);
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
