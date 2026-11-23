/** 过程时间线：每种事件一行，工具/异常可点开看完整入参与返回。 */
import { useState } from 'react';
import type { ProcessEntry } from '../types';

/** 状态文案：一行右侧只留最短的一个词。 */
const STATE_TEXT: Record<ProcessEntry['state'], string> = {
  running: '执行中',
  success: '完成',
  error: '失败',
};

/** 把多行细节压成一行摘要，超出截断——展开后才是原文。 */
export function summarize(detail: string, max: number = 64): string {
  const line = detail.replace(/\s+/g, ' ').trim();
  return line.length > max ? `${line.slice(0, max)}…` : line;
}

/** 工具 / 控制 / 异常：› 类型 · 摘要 …… 状态，点开在下方贴返回。 */
function ExpandableStep({ step }: { step: ProcessEntry }) {
  const [open, setOpen] = useState(step.kind === 'error');
  const detail = step.detail.trim();
  const hasDetail = detail.length > 0;
  const tone =
    step.kind === 'error'
      ? 'error'
      : step.kind === 'control' && step.state === 'running'
        ? 'warn'
        : step.state;

  return (
    <div className="step-row" data-kind={step.kind}>
      <button
        type="button"
        className={`step-line ${tone}${hasDetail ? '' : ' static'}`}
        aria-expanded={hasDetail ? open : undefined}
        onClick={() => {
          if (hasDetail) setOpen((value) => !value);
        }}
      >
        <span className="step-chev" aria-hidden="true">
          ›
        </span>
        <span className="step-kind">{step.label}</span>
        {hasDetail && (
          <>
            <span className="step-sep" aria-hidden="true">
              ·
            </span>
            <span className="step-detail mono" title={detail}>
              {summarize(detail)}
            </span>
          </>
        )}
        <span className="step-state mono">{STATE_TEXT[step.state]}</span>
      </button>
      {open && hasDetail && <pre className="step-result">{detail}</pre>}
    </div>
  );
}

/** 思考：左侧竖线 + 小圆点 + 字数，默认收起；正文到达后不再占位。 */
function ThinkingStep({ step }: { step: ProcessEntry }) {
  const live = step.state === 'running';
  return (
    <details className="thinking" data-kind="thinking">
      <summary className="thinking-head">
        <span className={`thinking-dot${live ? ' live' : ''}`} aria-hidden="true" />
        <span>{live ? '思考中…' : '思考过程'}</span>
        <span className="thinking-count mono">{step.detail.length}</span>
      </summary>
      <pre className="thinking-body">{step.detail || '…'}</pre>
    </details>
  );
}

/** 正式正文已由 Markdown 渲染，这里只留一个“正文输出”标记，不重复贴全文。 */
function TextStep({ step }: { step: ProcessEntry }) {
  return (
    <div className="step-row" data-kind="text">
      <div className={`step-line static ${step.state}`}>
        <span className="step-chev" aria-hidden="true">
          ›
        </span>
        <span className="step-kind">{step.label}</span>
        {step.state === 'running' && <span className="step-state mono">输出中</span>}
      </div>
    </div>
  );
}

/** 生命周期：读作运行日志，比步骤更弱一档。 */
function LogStep({ step }: { step: ProcessEntry }) {
  return (
    <div className="step-log" data-kind="lifecycle">
      {step.label}
      {step.detail ? ` · ${summarize(step.detail, 80)}` : ''}
    </div>
  );
}

export function StepRow({ step }: { step: ProcessEntry }) {
  if (step.kind === 'lifecycle') return <LogStep step={step} />;
  if (step.kind === 'thinking') return <ThinkingStep step={step} />;
  if (step.kind === 'text') return <TextStep step={step} />;
  return <ExpandableStep step={step} />;
}

/** 一次运行的全部过程行；空列表不占空间。 */
export function StepList({ steps }: { steps: ProcessEntry[] }) {
  if (steps.length === 0) return null;
  return (
    <div className="chat-steps">
      {steps.map((step) => (
        <StepRow key={step.id} step={step} />
      ))}
    </div>
  );
}
