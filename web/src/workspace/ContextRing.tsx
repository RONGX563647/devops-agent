/** 输入框侧的上下文占用圆环 + 压缩过程时间线。
 *
 * 数据来源只有三类帧（见 api/useRunStream.ts）：
 *   CONTEXT_BUDGET            → 阈值与模型窗口，流开始时一次
 *   CONTEXT_COMPRESSION_START → 压缩开始，带检查前 token
 *   CONTEXT_COMPRESSION_END   → 压缩结束，带前后 token / 是否真压了 / 摘要长度
 * 另外 MODEL_CALL_END 的 input_tokens 与系统提醒里的精确 token 数也会刷新占用。
 *
 * 两个刻意的取舍：
 * 1. 没有用量数据时不画圆环 —— 画个 0% 会让人误以为上下文是空的；
 * 2. 未触发压缩的检查（compressed=false）不进时间线 —— 每次推理前都会检查一次，
 *    全画出来就是刷屏；只在「最近一次检查被跳过」时用一行小字说明。
 */
import type { CompressionRecord } from '../types';
import { formatClock } from './time';

/** 占用率的语义分档：沿用全站 ok / warn / danger，不另立颜色。 */
export type ContextTone = 'ok' | 'warn' | 'danger';

/** 千分位：token 动辄六位数，裸数字读不出量级。 */
export function formatTokens(value: number): string {
  return Math.round(value)
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/** 占用率（0~1+）：阈值未知时返回 null，调用方据此决定不画。 */
export function contextRatio(
  tokens: number | null,
  threshold: number | null,
): number | null {
  if (tokens === null || threshold === null || threshold <= 0) return null;
  return tokens / threshold;
}

/** 分档：60% 以下从容，60~85% 提醒，85% 以上该压了。 */
export function toneFor(ratio: number): ContextTone {
  if (ratio >= 0.85) return 'danger';
  if (ratio >= 0.6) return 'warn';
  return 'ok';
}

/** 圆环上的百分比文案：超过 100% 也照实显示，不截断成 100%。 */
export function formatRatio(ratio: number): string {
  return `${Math.round(ratio * 100)}%`;
}

const RING_SIZE = 22;
const STROKE = 2.4;
const RADIUS = (RING_SIZE - STROKE) / 2;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

interface RingProps {
  tokens: number | null;
  threshold: number | null;
  /** 模型窗口，只用于 tooltip 说明阈值是怎么来的。 */
  contextSize?: number | null;
  /** 有压缩正在进行：环体呼吸，表示此刻数据还会变。 */
  busy?: boolean;
}

/** 输入栏里的占用圆环。没有数据时返回 null，不留空位。 */
export function ContextRing({ tokens, threshold, contextSize, busy = false }: RingProps) {
  const ratio = contextRatio(tokens, threshold);
  if (ratio === null) return null;
  const tone = toneFor(ratio);
  // 视觉上最多画满一圈，但文案与外圈颜色仍按真实比例走。
  const filled = Math.min(ratio, 1);
  const label = `上下文占用 ${formatRatio(ratio)}，${formatTokens(tokens ?? 0)} / ${formatTokens(
    threshold ?? 0,
  )} tokens`;
  const title = [
    `上下文 ${formatTokens(tokens ?? 0)} / ${formatTokens(threshold ?? 0)} tokens（${formatRatio(ratio)}）`,
    contextSize ? `模型窗口 ${formatTokens(contextSize)}，达到阈值即自动压缩` : '',
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <span
      className={`ctx-ring is-${tone}${busy ? ' is-busy' : ''}`}
      role="img"
      aria-label={label}
      title={title}
    >
      <svg width={RING_SIZE} height={RING_SIZE} viewBox={`0 0 ${RING_SIZE} ${RING_SIZE}`} aria-hidden="true">
        <circle
          className="ctx-ring-track"
          cx={RING_SIZE / 2}
          cy={RING_SIZE / 2}
          r={RADIUS}
          strokeWidth={STROKE}
        />
        <circle
          className="ctx-ring-fill"
          cx={RING_SIZE / 2}
          cy={RING_SIZE / 2}
          r={RADIUS}
          strokeWidth={STROKE}
          strokeDasharray={`${CIRCUMFERENCE * filled} ${CIRCUMFERENCE}`}
          strokeLinecap="round"
          transform={`rotate(-90 ${RING_SIZE / 2} ${RING_SIZE / 2})`}
        />
      </svg>
      <span className="ctx-ring-label mono">{formatRatio(ratio)}</span>
    </span>
  );
}

/** 只保留真正发生过的事：running 与 done 进时间线，skipped 只计数。 */
export function visibleCompressions(records: CompressionRecord[]): CompressionRecord[] {
  return records.filter((item) => item.state !== 'skipped');
}

/** 压缩比例：压前压后都有值才给，用于「−60%」这种结论。 */
function shrinkText(item: CompressionRecord): string {
  if (item.after === null || item.before <= 0) return '';
  const saved = 1 - item.after / item.before;
  if (saved <= 0) return '未缩小';
  return `省 ${Math.round(saved * 100)}%`;
}

interface TimelineProps {
  records: CompressionRecord[];
  /** 折叠阈值：超过这个条数只显示最近几次，避免长会话把输入区顶走。 */
  limit?: number;
}

/** 压缩过程时间线：常驻在输入框上方，只在真的压过（或正在压）时出现。 */
export function CompressionTimeline({ records, limit = 3 }: TimelineProps) {
  const shown = visibleCompressions(records);
  if (shown.length === 0) return null;
  const running = shown.some((item) => item.state === 'running');
  const skipped = records.length - shown.length;
  const visible = shown.slice(-limit);
  const hidden = shown.length - visible.length;

  return (
    <div className={`ctx-timeline${running ? ' is-running' : ''}`} role="status" aria-live="polite">
      <div className="ctx-timeline-head">
        <span className="ctx-timeline-title">
          {running ? '正在压缩上下文' : `上下文已压缩 ${shown.length} 次`}
        </span>
        <span className="ctx-timeline-meta mono">
          {hidden > 0 ? `另有 ${hidden} 次更早` : ''}
          {hidden > 0 && skipped > 0 ? ' · ' : ''}
          {skipped > 0 ? `${skipped} 次检查未触发` : ''}
        </span>
      </div>
      <ol className="ctx-timeline-list" aria-label="压缩记录">
        {visible.map((item) => (
          <li className={`ctx-item is-${item.state}`} key={item.id}>
            <span className="ctx-dot" aria-hidden="true" />
            <span className="ctx-item-flow mono">
              {formatTokens(item.before)}
              {' → '}
              {item.state === 'running' ? '…' : formatTokens(item.after ?? 0)}
            </span>
            <span className="ctx-item-note">
              {item.state === 'running'
                ? '摘要生成中'
                : [shrinkText(item), item.summaryChars > 0 ? `摘要 ${formatTokens(item.summaryChars)} 字` : '']
                    .filter(Boolean)
                    .join(' · ')}
            </span>
            <span className="ctx-item-time mono">{formatClock(item.at)}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}
