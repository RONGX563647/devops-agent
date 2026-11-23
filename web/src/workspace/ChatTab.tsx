/** 对话 tab：订阅会话 store 渲染消息与过程时间线，SSE 生命周期由 store 托管。 */
import { useEffect, useMemo, useRef, useState } from 'react';
import type { ChatMessage } from '../types';
import { Markdown } from './Markdown';
import { send, stop, useSession } from './store';
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

/** 助手消息：过程行在上，Markdown 正文在下，操作条在末尾。 */
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
  return (
    <div className={`chat-msg assistant${message.error ? ' has-error' : ''}`}>
      {message.steps.length > 0 && <StepList steps={message.steps} />}
      {message.text && (
        <div className="chat-bubble assistant-body">
          <Markdown content={message.text} />
        </div>
      )}
      {message.error && (
        <div className="chat-error-line">
          <span className="chat-error-tag">中断</span>
          <span>{message.error}</span>
        </div>
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
  const [input, setInput] = useState('');
  const [activeId, setActiveId] = useState('');
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  const composingRef = useRef(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const messages = view.messages;
  const turns = useMemo(() => turnsFromMessages(messages), [messages]);
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

  // 输入框随内容自动增高，到 200px 后交给内部滚动。
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input]);

  /** 发起一轮：首次发送时把草稿会话 id 落到 URL，再交给 store 后台执行。 */
  function run(text: string) {
    const value = text.trim();
    if (!value || view.running) return;
    onFirstSend?.();
    send(session, value);
  }

  function submit() {
    if (!input.trim() || view.running) return;
    const text = input;
    setInput('');
    run(text);
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
  const canSend = Boolean(input.trim()) && !view.running;

  return (
    <div className="chat-wrap">
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
                  <div className="chat-bubble">{m.text}</div>
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
      <TraceRail
        turns={turns}
        activeId={activeId}
        streaming={view.running}
        onLocate={(turn) => locate(turn.id)}
        onOpenTrace={onOpenTrace}
      />
      <div className="chat-composer">
        <div className="chat-compose">
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
            <span className="composer-hint">
              {view.running ? '生成中，仍可继续输入，停止后可发送' : 'Enter 发送 · Shift+Enter 换行'}
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
