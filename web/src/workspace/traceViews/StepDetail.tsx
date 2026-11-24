/** 就地展开的原文：入参 / 返回 / 正文。 */

import type { TraceStep } from '../../types';

/** JSON 入参折成多行再看；不是 JSON 就原样返回，别把日志改样。 */
function pretty(text: string): string {
  const raw = text.trim();
  if (!raw.startsWith('{') && !raw.startsWith('[')) return raw;
  try {
    return JSON.stringify(JSON.parse(raw) as unknown, null, 2);
  } catch {
    return raw;
  }
}

/** 就地展开的原文：入参 / 返回 / 正文，先看关键两句再看全文。 */
export function StepDetail({ step }: { step: TraceStep }) {
  const blocks: { label: string; text: string }[] = [];
  if (step.args) blocks.push({ label: '入参', text: pretty(step.args) });
  if (step.result) blocks.push({ label: '返回', text: step.result });
  if (blocks.length === 0 && step.text) {
    blocks.push({ label: step.title === '思考' ? '思考' : '正文', text: step.text });
  }
  if (blocks.length === 0) {
    return (
      <div className="trace-detail">
        <p className="trace-detail-empty">这一步没有可展开的原文。</p>
      </div>
    );
  }
  return (
    <div className="trace-detail">
      {blocks.map((block) => (
        <div className="trace-detail-block" key={block.label}>
          <span className="trace-detail-label">{block.label}</span>
          <pre className="trace-detail-code mono">{block.text}</pre>
        </div>
      ))}
    </div>
  );
}

/** 行内摘要已说完的步骤（解析计数、短思考、短输出）不再给箭头，免得点开是同一句话。 */
export function expandable(step: TraceStep): boolean {
  if (step.args || step.result) return true;
  return step.kind === 'text' && (step.text ?? '').length > 100;
}
