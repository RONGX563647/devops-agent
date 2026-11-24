/** 会话动作：发起/停止一轮、切档、确认续跑与挂起恢复。 */

import { fetchPending } from '../../api/client';
import { applyEvent, INITIAL_STATE, streamConfirm, streamRun } from '../../api/stream';
import type {
  Attachment,
  ChatMessage,
  ConfirmDecision,
  PermissionKey,
  StreamEvent,
} from '../../types';
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
  type Entry,
} from './state';
import { startWatchdog } from './watchdog';

/** 一轮流的启动器：事件回调、中断信号与保活钩子都由统一命令提供。 */
type StreamStarter = (
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal,
  touch: () => void,
) => Promise<void>;

/** 统一一轮流式生命周期：代次守卫、AbortController、看门狗与收尾只在这里写一次。 */
function executeStreamCommand(entry: Entry, start: StreamStarter): void {
  // 1) 开新一轮：代次递增 + 新 controller，旧流的回调自此全部失效
  entry.running = true;
  entry.generation += 1;
  const generation = entry.generation;
  const controller = new AbortController();
  entry.abort = controller;

  // 2) 看门狗兜底假死；事件过代次守卫后折叠、同步消息、节流落盘并广播
  const watchdog = startWatchdog(entry, generation, controller);
  const onEvent = (event: StreamEvent) => {
    if (generation !== entry.generation) return;
    entry.stream = applyEvent(entry.stream, event);
    syncAssistant(entry);
    schedulePersist(entry);
    emit(entry);
  };
  emit(entry);

  // 3) 启动请求：正常结束统一交给 settle（有挂起就停在确认，否则按 done 收尾）
  void start(onEvent, controller.signal, watchdog.touch)
    .then(() => {
      if (generation !== entry.generation) return;
      settle(entry, 'done');
    })
    // 4) 失败收尾：主动 abort 属预期静默返回；真错误写入 error 并落错误态
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

/** 发起一轮对话：追加 user + assistant 占位消息，并在后台消费 SSE。 */
export function send(
  sessionId: string,
  text: string,
  attachments: Attachment[] = [],
  permission?: PermissionKey,
  webSearch = false,
): void {
  // 1) 前置校验与档位：空会话/运行中直接忽略，档位有变先落档再发
  if (!sessionId) return;
  const entry = ensureEntry(sessionId);
  if (entry.running) return;
  if (permission && permission !== entry.permission) setPermission(sessionId, permission);

  // 2) 追加 user 原文与 assistant 空占位，附件只带解析成功的
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
  // 3) 流状态回到空态起跑，并先把占位消息落盘；广播由统一命令负责
  entry.stream = { ...INITIAL_STATE, running: true, startedAt: now };
  persistNow(entry);

  // 4) 只拼请求参数，其余生命周期交给 executeStreamCommand
  executeStreamCommand(entry, (onEvent, signal, touch) =>
    streamRun(
      sessionId,
      text,
      onEvent,
      signal,
      newId('req'),
      files.map((item) => item.id),
      entry.permission,
      webSearch,
      touch,
    ),
  );
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
  // 1) 必须处于挂起且未运行，否则没有可续跑的 run
  const entry = ensureEntry(sessionId);
  if (entry.running) return;
  const awaiting = entry.stream.awaiting ?? entry.messages[entry.messages.length - 1]?.awaiting;
  if (!awaiting?.runId) return;
  const runId = awaiting.runId;

  // 2) 先收起卡片：流态与消息上的挂起态一起清，否则快照会回落到它
  entry.stream = { ...entry.stream, running: true, error: null, awaiting: null };
  syncAssistant(entry);

  // 3) 续跑沿用同一条 run，只把 runId 与决策封成 start
  executeStreamCommand(entry, (onEvent, signal, touch) =>
    streamConfirm(runId, sessionId, decisions, onEvent, signal, entry.permission, touch),
  );
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
