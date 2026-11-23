/** 对话 tab：SSE 流式运行 + 消息列表（复用 useRunStream）。 */
import { useEffect, useRef, useState } from 'react';
import { useRunStream } from '../api/useRunStream';

interface Message {
  role: 'user' | 'assistant';
  text: string;
  error?: string;
  pending?: boolean;
}

interface Props {
  session: string | null;
}

export function ChatTab({ session }: Props) {
  const stream = useRunStream();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('用一句话说明什么是 CI');
  const activeRef = useRef<number | null>(null);

  useEffect(() => {
    const idx = activeRef.current;
    if (idx === null) return;
    setMessages((prev) =>
      prev.map((m, i) =>
        i === idx
          ? { ...m, text: stream.text, error: stream.error ?? undefined, pending: false }
          : m,
      ),
    );
  }, [stream.text, stream.error]);

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

  const sessionLabel = session ?? 'live-1';

  return (
    <div className="page">
      <p className="subtitle">会话 {sessionLabel} · POST /run/stream 逐帧渲染</p>
      <div className="chat-thread">
        {messages.map((m, i) => (
          <div className={`chat-msg ${m.role}`} key={i}>
            <span className="chat-role">{m.role === 'user' ? '我' : 'Agent'}</span>
            <div className={`chat-bubble ${m.error ? 'has-error' : ''}`}>
              {m.text || (m.pending && stream.running ? '等待首个 token…' : '')}
            </div>
            {m.error && <div className="error">错误：{m.error}</div>}
          </div>
        ))}
        {messages.length === 0 && (
          <p className="muted">输入问题开始对话。当前未配模型凭据时走 EchoRuntime 回显。</p>
        )}
      </div>
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
  );
}
