/** 对话 tab：订阅会话 store 渲染消息与过程时间线，SSE 生命周期由 store 托管。 */
import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  deleteAttachment,
  fetchHealth,
  switchSessionPermission,
  uploadAttachment,
} from '../api/client';
import type {
  Attachment,
  ChatMessage,
  ConfirmScope,
  PendingAttachment,
  PermissionKey,
} from '../types';
import { newId } from './id';
import { extractLinks } from './links';
import { Markdown } from './Markdown';
import { PermissionCard } from './PermissionCard';
import { PermissionPicker } from './PermissionPicker';
import { confirm, refreshPending, send, setPermission, stop, useSession } from './store';
import { StepList } from './StepRow';
import { formatClock } from './time';
import { TraceRail } from './TraceRail';
import { turnsFromMessages } from './trace';

interface Props {
  session: string;
  /** 首次发送时通知外层把「新会话」落到 URL 上。 */
  onFirstSend?: () => void;
  onOpenTrace?: () => void;
}

/** 距底小于该像素值时视为"用户还在底部"，可以继续吸底。 */
export const STICK_THRESHOLD = 80;

/** 判断是否应自动吸底：上翻读历史时不能把视口强行拽回底部。 */
export function shouldStickToBottom(
  scrollHeight: number,
  scrollTop: number,
  clientHeight: number,
  threshold: number = STICK_THRESHOLD,
): boolean {
  return scrollHeight - scrollTop - clientHeight < threshold;
}

export { formatClock };

/** 文件大小的人类可读形式：附件动辄几 MB，纯字节数看不出来体量。 */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 附件芯片右侧的计量文案：上传中 / 解析失败 / 体积与字数。 */
export function attachMeta(item: Attachment): string {
  if (item.status === 'uploading') return '上传中…';
  if (item.status === 'error') return '无法解析';
  const truncated = item.truncated ? '（已截断）' : '';
  return `${formatSize(item.size)} · ${item.chars} 字${truncated}`;
}

function GitHubIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

function GlobeIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.2" />
      <path
        d="M2 8h12M8 2c1.9 2 1.9 10 0 12M8 2c-1.9 2-1.9 10 0 12"
        stroke="currentColor"
        strokeWidth="1.2"
      />
    </svg>
  );
}

function PaperclipIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M9.8 6.2 5.4 10.6a2 2 0 0 0 2.8 2.8l4.9-4.9a3.2 3.2 0 0 0-4.5-4.5L4 8.9"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** 空状态的示例问题：点一下直接发起会话。 */
const SAMPLES = [
  '这个仓库的 CI 流程是怎么跑的？',
  '解释一下 deployment 阶段的失败原因',
  '帮我给流水线加一个 lint 关卡',
  '最近失败最多的阶段是哪个？',
];

function SendIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M8 13V3.6M8 3.6 3.6 8M8 3.6 12.4 8"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function SparkIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path
        d="M10 3.2l1.5 4.1 4.1 1.5-4.1 1.5L10 14.4 8.5 10.3 4.4 8.8l4.1-1.5L10 3.2Z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <path
        d="M15.2 13.2l.7 1.9 1.9.7-1.9.7-.7 1.9-.7-1.9-1.9-.7 1.9-.7.7-1.9Z"
        fill="currentColor"
        opacity=".55"
      />
    </svg>
  );
}

/** 复制正文；非安全上下文没有 clipboard 时静默跳过。 */
async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard?.writeText(text);
  } catch {
    // 忽略
  }
}

/** 助手消息：过程行在上，Markdown 正文在下，操作条在末尾。确认卡片不在这里。 */
function AssistantMessage({
  message,
  live,
  onRetry,
}: {
  message: ChatMessage;
  live: boolean;
  onRetry: (message: ChatMessage) => void;
}) {
  const hasAnswer = Boolean(message.text) || Boolean(message.error);
  // 正在生成的最新一轮：正文先出、过程收在下方；跑完就回到正文上方。
  const steps = message.steps.length > 0 ? <StepList steps={message.steps} live={live} /> : null;
  const body = message.text && (
    <div className="chat-bubble assistant-body">
      <Markdown content={message.text} />
    </div>
  );
  const error = message.error && (
    <div className="chat-error-line">
      <span className="chat-error-tag">中断</span>
      <span>{message.error}</span>
    </div>
  );
  return (
    <div className={`chat-msg assistant${message.error ? ' has-error' : ''}`}>
      {live ? (
        <>
          {body}
          {error}
          {steps}
        </>
      ) : (
        <>
          {steps}
          {body}
          {error}
        </>
      )}
      {message.pending && !message.text && message.steps.length === 0 && (
        <div className="chat-pending mono" aria-live="polite">
          正在处理…
        </div>
      )}
      {hasAnswer && (
        <div className="chat-actions">
          <button
            onClick={() => {
              void copyText(message.text);
            }}
          >
            复制
          </button>
          <button onClick={() => onRetry(message)} disabled={live}>
            重试
          </button>
          <span className="chat-time">{formatClock(message.endedAt ?? message.at)}</span>
        </div>
      )}
    </div>
  );
}

export function ChatTab({ session, onFirstSend, onOpenTrace }: Props) {
  const view = useSession(session);
  // 运行时模式：Echo 兜底意味着没配模型凭据，回复只是把输入回显一遍。
  // 不显式说出来，用户只会看到「模型答非所问」然后反复重试。
  const health = useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    staleTime: 60_000,
    retry: 1,
  });
  const degraded = health.data?.runtime?.kind === 'echo';
  const [input, setInput] = useState('');
  // 联网搜索：跟 DeepSeek 一样做成输入框下方的显式开关，默认关，逐轮生效
  const [webSearch, setWebSearch] = useState(false);
  // 切档结果必须可见：服务端可能没有活跃实例（只改下一轮），也可能拒绝越权档。
  const [permNotice, setPermNotice] = useState('');
  const [files, setFiles] = useState<PendingAttachment[]>([]);
  const [activeId, setActiveId] = useState('');
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  const composingRef = useRef(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const messages = view.messages;
  const turns = useMemo(() => turnsFromMessages(messages), [messages]);
  // 输入即解析：不等到发送，让用户先确认链接被认对了没有。
  const links = useMemo(() => extractLinks(input), [input]);
  const last = messages[messages.length - 1];
  const lastText = last?.text ?? '';

  /** 滚动时记录用户是否仍贴着底部，决定后续 token 是否自动跟随。 */
  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = shouldStickToBottom(el.scrollHeight, el.scrollTop, el.clientHeight);
    syncActiveTurn();
  }

  /** 用可视区顶部反查当前轮次，供右侧刻度条高亮。 */
  function syncActiveTurn() {
    const el = scrollRef.current;
    if (!el || turns.length === 0) return;
    const anchor = el.getBoundingClientRect().top + 72;
    let current = turns[0].id;
    for (const turn of turns) {
      const node = document.getElementById(`msg-${turn.id}`);
      if (!node) continue;
      if (node.getBoundingClientRect().top <= anchor) current = turn.id;
      else break;
    }
    setActiveId(current);
  }

  // 新 token 或新消息到达时吸底；用户上翻时 stickRef 为假，不打扰阅读。
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !stickRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [lastText, messages.length]);

  useEffect(() => {
    syncActiveTurn();
    // turns 变化时同步一次高亮，避免切换会话后刻度条无选中项
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, turns.length]);

  // 重新挂载（刷新/切回会话）时把服务端还挂着的确认找回来，
  // 否则前端丢了卡片、后端还在等，这一轮就永远停住了。
  useEffect(() => {
    void refreshPending(session);
  }, [session]);

  // 输入框随内容自动增高，到 200px 后交给内部滚动。
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input]);

  /** 发起一轮：首次发送时把草稿会话 id 落到 URL，再交给 store 后台执行。 */
  function run(text: string, attachments: Attachment[] = []) {
    const value = text.trim();
    // 只挂附件、不写字也是合法的一轮：这时候问题就是「看看这个文件」
    if ((!value && attachments.length === 0) || view.running || view.awaiting) return;
    onFirstSend?.();
    send(session, value, attachments, view.permission, webSearch);
  }

  /** 确认卡片的三选一：全部待确认调用一起答复。 */
  function decide(scope: ConfirmScope | 'reject') {
    const awaiting = view.awaiting;
    if (!awaiting || awaiting.calls.length === 0) return;
    confirm(
      session,
      awaiting.calls.map((call) => ({
        tool_call_id: call.tool_call_id,
        confirmed: scope !== 'reject',
        scope: scope === 'reject' ? 'once' : scope,
      })),
    );
  }

  /** 运行中也能切：服务端原地热替换当前实例的权限上下文与工具集。 */
  async function changePermission(key: PermissionKey) {
    const previous = view.permission;
    setPermission(session, key);
    setPermNotice('切档中…');
    try {
      const result = await switchSessionPermission(session, key, webSearch);
      setPermNotice(
        result.applied
          ? '已生效：本轮后续工具调用与模型请求按新档位'
          : '下一轮发送按新档位（当前没有活跃会话）',
      );
    } catch (error) {
      // 未开放档 / 会话不匹配：撤回本地选择，别让界面说一套、后端做另一套
      setPermission(session, previous);
      setPermNotice(error instanceof Error ? error.message : '切档失败');
    }
  }

  function submit() {
    const ready = files.filter((item) => item.status === 'ok');
    if ((!input.trim() && ready.length === 0) || view.running || view.awaiting) return;
    const text = input;
    setInput('');
    setFiles([]);
    run(text, ready);
  }

  /** 单个文件上传：成功替换乐观条目，失败把原因留在芯片上。 */
  async function uploadOne(file: File, key: string) {
    try {
      const saved = await uploadAttachment(file);
      setFiles((prev) => prev.map((item) => (item.key === key ? { ...saved, key } : item)));
    } catch (error) {
      const problem = error instanceof Error ? error.message : '上传失败';
      setFiles((prev) =>
        prev.map((item) => (item.key === key ? { ...item, status: 'error', problem } : item)),
      );
    }
  }

  /** 选中文件后先占位再上传：上传期间用户能看到「正在处理哪几个文件」。 */
  function addFiles(picked: FileList | null) {
    if (!picked || picked.length === 0) return;
    const chosen = Array.from(picked);
    const placeholders: PendingAttachment[] = chosen.map((file) => ({
      key: newId('att'),
      id: '',
      name: file.name,
      size: file.size,
      suffix: '',
      chars: 0,
      truncated: false,
      status: 'uploading',
    }));
    setFiles((prev) => [...prev, ...placeholders]);
    void Promise.all(chosen.map((file, index) => uploadOne(file, placeholders[index].key)));
  }

  function removeFile(key: string) {
    const target = files.find((item) => item.key === key);
    setFiles((prev) => prev.filter((item) => item.key !== key));
    // 只删服务端已落盘的那份；上传中的条目没有 id，自然跳过
    if (target?.id) void deleteAttachment(target.id);
  }

  /** 重试：用同一条用户输入再发一轮（保留历史，不重写已发生的内容）。 */
  function retry(message: ChatMessage) {
    if (view.running) return;
    const index = messages.findIndex((item) => item.id === message.id);
    const asked = messages[index - 1];
    if (!asked || asked.role !== 'user') return;
    run(asked.text);
  }

  function locate(turnId: string) {
    const node = document.getElementById(`msg-${turnId}`);
    node?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    setActiveId(turnId);
  }

  const empty = messages.length === 0;
  const readyCount = files.filter((item) => item.status === 'ok').length;
  // 挂起等确认时不让发下一轮：同一会话同时只能有一条未结束的 run
  const canSend =
    (Boolean(input.trim()) || readyCount > 0) && !view.running && !view.awaiting;

  return (
    <div className="chat-wrap">
      {degraded && (
        <div className="runtime-banner" role="status">
          <span className="runtime-banner-tag mono">Echo</span>
          <span className="runtime-banner-text">
            {health.data?.runtime?.notice || '未配置模型凭据，当前回复只是回显输入'}
          </span>
        </div>
      )}
      <div
        className={`chat-scroll${empty ? ' is-empty' : ''}`}
        ref={scrollRef}
        onScroll={handleScroll}
      >
        <div className="chat-thread">
          {view.loading && <div className="chat-loading mono">正在载入会话历史…</div>}
          {messages.map((m) => {
            if (m.role === 'user') {
              return (
                <div className="chat-msg user" id={`msg-${m.id}`} key={m.id}>
                  <div className="chat-bubble">
                    {m.text && <span className="chat-bubble-text">{m.text}</span>}
                    {m.attachments && m.attachments.length > 0 && (
                      <span className="chat-attachments">
                        {m.attachments.map((item) => (
                          <span className="attach-tag" key={item.id} title={item.name}>
                            {item.name}
                          </span>
                        ))}
                      </span>
                    )}
                  </div>
                </div>
              );
            }
            return (
              <AssistantMessage
                key={m.id}
                message={m}
                live={view.running && m.id === last?.id}
                onRetry={retry}
              />
            );
          })}
          {empty && !view.loading && (
            <div className="chat-empty">
              <span className="chat-empty-icon">
                <SparkIcon />
              </span>
              <h2 className="chat-empty-title">开始一次会话</h2>
              <p className="chat-empty-hint">
                描述现象，Agent 会读代码、跑流水线、翻日志。每一步都以一行记录留在回答上方，
                点开就能看它到底调用了什么、拿回了什么。
              </p>
              <div className="chat-samples">
                {SAMPLES.map((q) => (
                  <button key={q} className="chat-sample" onClick={() => run(q)}>
                    <span>{q}</span>
                    <span className="chat-sample-arrow" aria-hidden="true">
                      →
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
      {/* 确认卡片脱离消息流，常驻在输入框上方：消息再长也不会被滚到中间去。 */}
      {view.awaiting && view.awaiting.calls.length > 0 && (
        <div className="confirm-dock">
          <PermissionCard
            calls={view.awaiting.calls}
            expiresAt={view.awaiting.expiresAt}
            busy={view.running}
            onDecide={decide}
          />
        </div>
      )}
      <TraceRail
        turns={turns}
        activeId={activeId}
        streaming={view.running}
        onLocate={(turn) => locate(turn.id)}
        onOpenTrace={onOpenTrace}
      />
      <div className="chat-composer">
        <div className="chat-compose">
          {files.length > 0 && (
            <div className="attach-row">
              {files.map((item) => (
                <span
                  className={`attach-chip${item.status === 'error' ? ' is-error' : ''}`}
                  key={item.key}
                  title={item.problem || item.preview || item.name}
                >
                  <span className="attach-name">{item.name}</span>
                  <span className="attach-meta mono">{attachMeta(item)}</span>
                  <button
                    className="attach-remove"
                    aria-label={`移除 ${item.name}`}
                    onClick={() => removeFile(item.key)}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
          {links.length > 0 && (
            <div className="link-row" role="group" aria-label="消息中的链接">
              {links.map((link) => (
                <a
                  key={link.url}
                  className={`link-chip${link.kind === 'github' ? ' is-github' : ''}`}
                  href={link.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  title={link.url}
                >
                  {link.kind === 'github' ? <GitHubIcon /> : <GlobeIcon />}
                  <span className="link-label">{link.label}</span>
                </a>
              ))}
            </div>
          )}
          <textarea
            ref={textareaRef}
            aria-label="用户输入"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onCompositionStart={() => {
              composingRef.current = true;
            }}
            onCompositionEnd={() => {
              composingRef.current = false;
            }}
            onKeyDown={(e) => {
              // 中文输入法选词时的 Enter 不能当发送；同时兼容 isComposing 标记。
              if (e.key === 'Enter' && !e.shiftKey && !composingRef.current && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder="给 DevOps Agent 发消息…"
            rows={1}
          />
          <div className="composer-bar">
            <PermissionPicker current={view.permission} onChange={changePermission} />
            <button
              type="button"
              className={`search-toggle${webSearch ? ' is-on' : ''}`}
              aria-pressed={webSearch}
              aria-label="联网搜索"
              title="联网搜索：由模型判断是否需要检索；开启后可能产生额外费用"
              onClick={() => setWebSearch((value) => !value)}
              disabled={view.running}
            >
              <GlobeIcon />
              <span>联网</span>
            </button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="attach-input"
              aria-label="选择要上传的文件"
              onChange={(e) => {
                addFiles(e.target.files);
                // 清空 value：同一个文件连选两次也要能再次触发 change
                e.target.value = '';
              }}
            />
            <button
              className="attach-btn"
              aria-label="添加文件"
              title="添加文件（本地解析后随消息一起发送）"
              onClick={() => fileInputRef.current?.click()}
            >
              <PaperclipIcon />
            </button>
            <span className="composer-hint" aria-live="polite">
              {permNotice ||
                (view.awaiting
                  ? '等待你确认下方卡片'
                  : view.running
                    ? '生成中，仍可切档；停止后可发送新消息'
                    : 'Enter 发送 · Shift+Enter 换行')}
            </span>
            {view.running && (
              <button className="send-btn stop" onClick={() => stop(session)}>
                停止
              </button>
            )}
            <button
              className={`send-btn${canSend ? ' ready' : ''}`}
              onClick={submit}
              disabled={!canSend}
              aria-label="发送"
            >
              <SendIcon />
            </button>
          </div>
        </div>
        <div className="chat-meta">
          <span>{view.eventCount} 帧事件</span>
          <span>
            {view.inputTokens} in / {view.outputTokens} out
          </span>
          {view.modelName && <span className="mono">{view.modelName}</span>}
          {view.runId !== null && <span>run #{view.runId}</span>}
        </div>
      </div>
    </div>
  );
}
