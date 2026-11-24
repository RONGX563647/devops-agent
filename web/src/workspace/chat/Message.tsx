/** 助手消息：已完成的过程在正文上方，仍在进行的步骤在正文下方。 */

import type { ChatMessage } from '../../types';
import { Markdown } from '../Markdown';
import { StepList } from '../StepRow';
import { formatClock } from '../time';

/** 复制正文；非安全上下文没有 clipboard 时静默跳过。 */
async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard?.writeText(text);
  } catch {
    // 忽略
  }
}

/** 助手消息：已完成的过程在正文上方，仍在进行的步骤在正文下方；确认卡片不在这里。 */
export function AssistantMessage({
  message,
  live,
  onRetry,
}: {
  message: ChatMessage;
  live: boolean;
  onRetry: (message: ChatMessage) => void;
}) {
  const hasAnswer = Boolean(message.text) || Boolean(message.error);
  // 按每条步骤自己的状态分栏，而不是整段跟着 live 一起搬：
  // 已完成的思考/调用归档到正文上方，仍在进行中的留在正文下方贴着输入区。
  // 运行结束时 finishRunningSteps 会把 running 收尾，整段自然回到上方。
  const settled = message.steps.filter((step) => step.state !== 'running');
  const active = message.steps.filter((step) => step.state === 'running');
  const settledSteps = settled.length > 0 ? <StepList steps={settled} label="已完成过程" /> : null;
  const activeSteps =
    active.length > 0 ? <StepList steps={active} label="进行中" live /> : null;
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
      {settledSteps}
      {body}
      {error}
      {activeSteps}
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
