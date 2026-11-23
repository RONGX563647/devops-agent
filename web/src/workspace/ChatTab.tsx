/** 对话 tab：SSE 流式运行 + 消息行（用户软底块 / 助手 Markdown / 过程时间线）。 */
import { useEffect, useRef, useState } from 'react';
import { useRunStream } from '../api/useRunStream';
import type { ProcessEntry } from '../types';
import { Markdown } from './Markdown';
import { StepList } from './StepRow';

interface Message {
  role: 'user' | 'assistant';
  text: string;
  error?: string;
  pending?: boolean;
  steps?: ProcessEntry[];
  modelName?: string;
  inputTokens?: number;
  outputTokens?: number;
  startedAt?: string | null;
  endedAt?: string | null;
}

interface Props {
  session: string | null;
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

/** ISO 时间戳 → HH:MM，用于消息操作条；缺值或非法值返回空串。 */
export function formatClock(value?: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
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

export function ChatTab({ session }: Props) {
  const stream = useRunStream();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const activeRef = useRef<number | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  /** 滚动时记录用户是否仍贴着底部，决定后续 token 是否自动跟随。 */
  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    stickRef.current = shouldStickToBottom(el.scrollHeight, el.scrollTop, el.clientHeight);
  }

  // 新 token 或新消息到达时吸底；用户上翻时 stickRef 为假，不打扰阅读。
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !stickRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [stream.text, messages.length]);

  // 输入框随内容自动增高，到 200px 后交给内部滚动。
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input]);

  useEffect(() => {
    const idx = activeRef.current;
    if (idx === null) return;
    setMessages((prev) =>
      prev.map((m, i) =>
        i === idx
          ? {
              ...m,
              text: stream.text,
              error: stream.error ?? undefined,
              pending: false,
              steps: stream.steps,
              modelName: stream.modelName,
              inputTokens: stream.inputTokens,
              outputTokens: stream.outputTokens,
              startedAt: stream.startedAt,
              endedAt: stream.endedAt,
            }
          : m,
      ),
    );
  }, [
    stream.text,
    stream.error,
    stream.steps,
    stream.modelName,
    stream.inputTokens,
    stream.outputTokens,
    stream.startedAt,
    stream.endedAt,
  ]);

  /** 追加一问一答并把新消息设为流的目标；重试复用同一条路径。 */
  function runFrom(base: Message[], text: string) {
    const next: Message[] = [
      ...base,
      { role: 'user', text },
      { role: 'assistant', text: '', pending: true },
    ];
    activeRef.current = next.length - 1;
    setMessages(next);
    void stream.start(session ?? 'live-1', text);
  }

  function submit() {
    const text = input.trim();
    if (!text || stream.running) return;
    setInput('');
    runFrom(messages, text);
  }

  /** 重试：丢掉这条回答及其后的消息，用同一条用户输入重跑。 */
  function retry(index: number) {
    if (stream.running) return;
    const asked = messages[index - 1];
    if (!asked || asked.role !== 'user') return;
    runFrom(messages.slice(0, index - 1), asked.text);
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard?.writeText(text);
    } catch {
      // http 非 localhost 等非安全上下文拿不到 clipboard，静默跳过
    }
  }

  const empty = messages.length === 0;
  const canSend = Boolean(input.trim()) && !stream.running;

  return (
    <div className="chat-wrap">
      <div
        className={`chat-scroll${empty ? ' is-empty' : ''}`}
        ref={scrollRef}
        onScroll={handleScroll}
      >
        <div className="chat-thread">
          {messages.map((m, i) => {
            if (m.role === 'user') {
              return (
                <div className="chat-msg user" key={i}>
                  <div className="chat-bubble">{m.text}</div>
                </div>
              );
            }
            const hasAnswer = Boolean(m.text) || Boolean(m.error);
            return (
              <div className={`chat-msg assistant${m.error ? ' has-error' : ''}`} key={i}>
                {m.steps && <StepList steps={m.steps} />}
                {m.text && (
                  <div className="chat-bubble assistant-body">
                    <Markdown content={m.text} />
                  </div>
                )}
                {m.error && (
                  <div className="chat-error-line">
                    <span className="chat-error-tag">中断</span>
                    <span>{m.error}</span>
                  </div>
                )}
                {hasAnswer && (
                  <div className="chat-actions">
                    <button onClick={() => void copy(m.text)}>复制</button>
                    <button onClick={() => retry(i)} disabled={stream.running}>
                      重试
                    </button>
                    <span className="chat-time">{formatClock(m.endedAt)}</span>
                  </div>
                )}
              </div>
            );
          })}
          {empty && (
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
                  <button key={q} className="chat-sample" onClick={() => runFrom(messages, q)}>
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
      <div className="chat-composer">
        <div className="chat-compose">
          <textarea
            ref={textareaRef}
            aria-label="用户输入"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            disabled={stream.running}
            placeholder="给 DevOps Agent 发消息…"
            rows={1}
          />
          <div className="composer-bar">
            <span className="composer-hint">Enter 发送 · Shift+Enter 换行</span>
            {stream.running && (
              <button className="send-btn stop" onClick={stream.stop}>
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
          <span>{stream.events.length} 帧事件</span>
          <span>
            {stream.inputTokens} in / {stream.outputTokens} out
          </span>
          {stream.runId !== null && <span>run #{stream.runId}</span>}
        </div>
      </div>
    </div>
  );
}
