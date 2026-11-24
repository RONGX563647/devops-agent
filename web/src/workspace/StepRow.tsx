/** 过程时间线：每种事件一行，工具/异常可点开看完整入参与返回。 */
import { useState } from 'react';
import type { ParseStage, ProcessEntry } from '../types';
import { formatClock } from './time';

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
  const args = (step.args ?? '').trim();
  const result = (step.result ?? '').trim();
  const legacy = step.detail.trim();
  // 入参与返回分字段存放；旧数据只有 detail，保持原样兼容。
  const summarySource = args || legacy || result;
  const hasDetail = Boolean(args || result || legacy);
  const clock = formatClock(step.at);
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
        {summarySource && (
          <>
            <span className="step-sep" aria-hidden="true">
              ·
            </span>
            <span className="step-detail mono" title={summarySource}>
              {summarize(summarySource)}
            </span>
          </>
        )}
        {clock && <span className="step-time mono">{clock}</span>}
        <span className="step-state mono">{STATE_TEXT[step.state]}</span>
      </button>
      {open && hasDetail && (
        <div className="step-blocks">
          {args && (
            <div className="step-block">
              <span className="step-block-label">入参</span>
              <pre className="step-result">{step.args}</pre>
            </div>
          )}
          {!args && legacy && <pre className="step-result">{legacy}</pre>}
          {result && (
            <div className="step-block">
              <span className="step-block-label">返回</span>
              <pre className="step-result">{step.result}</pre>
            </div>
          )}
        </div>
      )}
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

/** 解析阶段的计量列：不同阶段算的不是同一件事，按语义各给一种。 */
export function stageMetric(stage: ParseStage): string {
  if (stage.stage === 'detect') return stage.items ? `${stage.items} 条` : '—';
  if (stage.stage === 'expand') return stage.after ? `+${stage.after} 字` : '—';
  if (stage.stage === 'redact') return stage.items ? `${stage.items} 处` : '—';
  if (stage.before || stage.after) return `${stage.before} → ${stage.after}`;
  return `${stage.ms}ms`;
}

/** 输入解析：一行总账 + 折叠的阶段明细，只列真正生效的处理。 */
function ParseStep({ step }: { step: ProcessEntry }) {
  const stages = step.parseStages ?? [];
  // 无改动且没出错的阶段不展示——「没做什么」不该占一行
  const visible = stages.filter(
    (stage) => stage.changed || stage.status === 'error' || stage.failed > 0,
  );
  const hidden = stages.length - visible.length;
  return (
    <details className="parse-step" data-kind="parse" open={step.state === 'running'}>
      <summary className="step-line parse">
        <span className="step-chev" aria-hidden="true">
          ›
        </span>
        <span className="step-kind">{step.label}</span>
        {step.detail && (
          <>
            <span className="step-sep" aria-hidden="true">
              ·
            </span>
            <span className="step-detail mono" title={step.detail}>
              {summarize(step.detail, 72)}
            </span>
          </>
        )}
        <span className="step-state mono">{STATE_TEXT[step.state]}</span>
      </summary>
      <div className="parse-stages">
        {visible.map((stage) => (
          <div className="parse-stage" key={stage.stage} data-status={stage.status}>
            <span className="parse-stage-label">{stage.label}</span>
            <span className="parse-stage-summary mono" title={stage.detail || stage.summary}>
              {stage.summary}
            </span>
            <span className="parse-stage-metric mono">{stageMetric(stage)}</span>
          </div>
        ))}
        {visible.length === 0 && <p className="parse-stage muted">输入无需处理，原样透传。</p>}
        {hidden > 0 && (
          <p className="parse-stage muted">另有 {hidden} 项无改动，完整解析链见运行记录。</p>
        )}
      </div>
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

/** 生命周期与模型调用：读作运行日志，比步骤更弱一档。 */
function LogStep({ step }: { step: ProcessEntry }) {
  return (
    <div className="step-log" data-kind={step.kind}>
      {step.label}
      {step.detail ? ` · ${summarize(step.detail, 80)}` : ''}
    </div>
  );
}

export function StepRow({ step }: { step: ProcessEntry }) {
  if (step.kind === 'lifecycle' || step.kind === 'model') return <LogStep step={step} />;
  if (step.kind === 'thinking') return <ThinkingStep step={step} />;
  if (step.kind === 'parse') return <ParseStep step={step} />;
  if (step.kind === 'text') return <TextStep step={step} />;
  return <ExpandableStep step={step} />;
}

/** 一组过程行：默认折叠，摘要行仍显示最新一步在干什么。

调用方按步骤状态把一组拆成「已完成」「进行中」两栏——定稿的归档到正文
上方，还在跑的留在正文下方。两组各自折叠，想看明细点摘要即可。
*/
export function StepList({
  steps,
  live = false,
  label = '过程',
}: {
  steps: ProcessEntry[];
  live?: boolean;
  label?: string;
}) {
  if (steps.length === 0) return null;
  const last = steps[steps.length - 1];
  const latest = last.detail ? `${last.label} · ${last.detail}` : last.label;
  return (
    <details className="chat-steps-fold" data-live={live || undefined} data-label={label}>
      <summary className="chat-steps-summary">
        <span className="step-chev" aria-hidden="true">
          ›
        </span>
        <span className="step-kind">{label}</span>
        <span className="step-detail mono" title={latest}>
          {summarize(latest, 72)}
        </span>
        <span className="step-count mono">{steps.length} 步</span>
        {live && <span className="step-state mono">执行中</span>}
      </summary>
      <div className="chat-steps">
        {steps.map((step) => (
          <StepRow key={step.id} step={step} />
        ))}
      </div>
    </details>
  );
}
