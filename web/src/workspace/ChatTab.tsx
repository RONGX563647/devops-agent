/** 对话 tab：SSE 流式运行 + 消息列表（复用 useRunStream）。 */
import { useEffect, useRef, useState } from 'react';
import { useRunStream } from '../api/useRunStream';
import type { ProcessEntry } from '../types';
import { ProcessBlock } from './ProcessBlock';

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

export function ChatTab({ session }: Props) {
  const stream = useRunStream();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('用一句话说明什么是 CI');
  const activeRef = useRef<number | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);

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

  function submit() {
    const text = input.trim();
    if (!text || stream.running) return;
    const idx = messages.length + 1;
    activeRef.current = idx;
    setMessages((prev) => [
      ...prev,
      { role: 'user', text },
      { role: 'assistant', text: '', pending: true },
    ]);
    setInput('');
    void stream.start(session ?? 'live-1', text);
  }

  return (
    <div className="chat-wrap">
      <div className="chat-scroll" ref={scrollRef} onScroll={handleScroll}>
        <div className="chat-thread">
          {messages.map((m, i) => (
            <div className={`chat-msg ${m.role}`} key={i}>
              <span className="chat-role">{m.role === 'user' ? '我' : 'Agent'}</span>
              {m.role === 'assistant' && m.steps && m.steps.length > 0 && (
                <ProcessBlock
                  steps={m.steps}
                  running={stream.running && i === messages.length - 1}
                  modelName={m.modelName ?? ''}
                  inputTokens={m.inputTokens ?? 0}
                  outputTokens={m.outputTokens ?? 0}
                  startedAt={m.startedAt ?? null}
                  endedAt={m.endedAt ?? null}
                />
              )}
              {(m.text || m.error) && (
                <div className={`chat-bubble ${m.error ? 'has-error' : ''}`}>{m.text}</div>
              )}
              {m.error && <div className="error">错误：{m.error}</div>}
            </div>
          ))}
          {messages.length === 0 && (
            <p className="muted">输入问题开始对话。当前未配模型凭据时走 EchoRuntime 回显。</p>
          )}
        </div>
      </div>
      <div className="chat-composer">
        <div className="chat-compose">
          <textarea
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
            placeholder="发送消息…"
          />
          <button onClick={submit} disabled={stream.running || !input.trim()}>
            {stream.running ? '运行中…' : '发送 ↗'}
          </button>
          {stream.running && <button onClick={stream.stop}>停止</button>}
        </div>
        <div className="chat-meta">
          事件 {stream.events.length} 帧 · 输入 token {stream.inputTokens} / 输出 token {stream.outputTokens}
          {stream.runId !== null && ` · run #${stream.runId}`}
        </div>
      </div>
    </div>
  );
}
