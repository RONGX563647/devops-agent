/** 会话历史本地持久化：消息按会话存 localStorage，另维护一份会话索引供侧栏使用。 */
import type { ChatMessage } from '../types';
import { toSessionTitle } from './sidebar-utils';

const MESSAGE_PREFIX = 'da.chat.';
const INDEX_KEY = 'da.sessions';
const MAX_MESSAGES = 200;
/** 单条工具返回最长保留字符数，避免日志把 localStorage 撑爆。 */
const MAX_TEXT = 20_000;

export interface StoredSession {
  id: string;
  title: string;
  lastAt: string;
  /** 用户消息数，用于本地新会话在侧栏的计数。 */
  messageCount: number;
}

function storage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

function clip(text: string): string {
  return text.length > MAX_TEXT ? text.slice(0, MAX_TEXT) : text;
}

/** 落盘前裁剪：只保留最近 200 条，并截断超长的工具返回。 */
export function trimMessages(messages: ChatMessage[]): ChatMessage[] {
  return messages.slice(-MAX_MESSAGES).map((message) => ({
    ...message,
    steps: message.steps.map((step) => ({
      ...step,
      result: step.result ? clip(step.result) : step.result,
      args: step.args ? clip(step.args) : step.args,
      // 解析阶段的 detail 可能是整份引用清单，同样要压住体积
      parseStages: step.parseStages?.map((stage) => ({
        ...stage,
        detail: clip(stage.detail),
      })),
    })),
  }));
}

function parseMessages(raw: string | null): ChatMessage[] | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return null;
    return parsed.filter(
      (item): item is ChatMessage =>
        Boolean(item) && typeof item === 'object' && 'role' in item && 'text' in item,
    );
  } catch {
    return null;
  }
}

export function loadMessages(sessionId: string): ChatMessage[] | null {
  if (!sessionId) return null;
  return parseMessages(storage()?.getItem(MESSAGE_PREFIX + sessionId) ?? null);
}

function sessionMeta(sessionId: string, messages: ChatMessage[]): StoredSession {
  const firstUser = messages.find((message) => message.role === 'user');
  const last = messages[messages.length - 1];
  return {
    id: sessionId,
    title: toSessionTitle(firstUser?.text ?? ''),
    lastAt: last?.at ?? new Date().toISOString(),
    messageCount: messages.filter((message) => message.role === 'user').length,
  };
}

function readIndex(): StoredSession[] {
  try {
    const parsed = JSON.parse(storage()?.getItem(INDEX_KEY) ?? '[]') as unknown;
    return Array.isArray(parsed) ? (parsed as StoredSession[]) : [];
  } catch {
    return [];
  }
}

function writeIndex(items: StoredSession[]): void {
  try {
    storage()?.setItem(INDEX_KEY, JSON.stringify(items.slice(-500)));
  } catch {
    // 隐私模式 / 配额满：忽略
  }
}

function upsertIndex(meta: StoredSession): void {
  const items = readIndex().filter((item) => item.id !== meta.id);
  items.push(meta);
  writeIndex(items.sort((a, b) => a.lastAt.localeCompare(b.lastAt)));
}

export function saveMessages(sessionId: string, messages: ChatMessage[]): void {
  if (!sessionId) return;
  const store = storage();
  if (!store) return;
  const trimmed = trimMessages(messages);
  try {
    store.setItem(MESSAGE_PREFIX + sessionId, JSON.stringify(trimmed));
  } catch {
    // 配额满：本次跳过，索引仍保留，界面上还有内存态
  }
  upsertIndex(sessionMeta(sessionId, trimmed));
}

export function listStoredSessions(): StoredSession[] {
  return readIndex().slice().sort((a, b) => b.lastAt.localeCompare(a.lastAt));
}

export function removeSession(sessionId: string): void {
  if (!sessionId) return;
  try {
    storage()?.removeItem(MESSAGE_PREFIX + sessionId);
  } catch {
    // 忽略
  }
  writeIndex(readIndex().filter((item) => item.id !== sessionId));
}

/** 测试用：清空全部会话缓存。 */
export function clearAllHistory(): void {
  const store = storage();
  if (!store) return;
  const keys: string[] = [];
  for (let i = 0; i < store.length; i += 1) {
    const key = store.key(i);
    if (key && (key.startsWith(MESSAGE_PREFIX) || key === INDEX_KEY)) keys.push(key);
  }
  keys.forEach((key) => store.removeItem(key));
}
