/** 轨迹列表的一行：徽标、摘要、缓存列、占比、耗时与展开体。 */

import type { TraceStep } from '../../types';
import { formatDuration } from '../time';
import { Diff } from './Diff';
import { BADGE, Highlight } from './Highlight';
import { StepDetail, expandable } from './StepDetail';

/** 这几类标题只是类别名，和行首徽标重复，不再单独占一列。 */
const GENERIC_TITLES = new Set(['思考', '模型输出', '输入解析', '模型调用']);

/** 模型这一步的缓存命中率：缓存令牌 ÷ 入参令牌。没有字段时返回 null，不编数。 */
function cacheRate(step: TraceStep): number | null {
  if (!step.cacheTokens || !step.tokensIn) return null;
  return Math.round((step.cacheTokens / step.tokensIn) * 100);
}

/** 占比条：宽度就是这个步骤吃掉全会话实测耗时的比例，比只看毫秒直观。 */
function ShareCell({ step }: { step: TraceStep }) {
  if (step.timing.source === 'estimated') {
    return (
      <span className="trace-share" title="按相邻步骤估算，不参与占比统计">
        —
      </span>
    );
  }
  const pct = Math.round(step.share * 100);
  return (
    <span className="trace-share" title={`占全会话实测 ${pct}%`}>
      <span className="trace-share-track" aria-hidden="true">
        <span className="trace-share-fill" style={{ width: `${Math.min(100, pct)}%` }} />
      </span>
      <span className="mono">{pct}%</span>
    </span>
  );
}

export function StepRow({
  step,
  query,
  active,
  expanded,
  turnIndex,
  showShare,
  onSelect,
  onToggle,
}: {
  step: TraceStep;
  query: string;
  active: boolean;
  expanded: boolean;
  /** 平铺视图里标出这一步属于第几轮；分组视图里用不着。 */
  turnIndex?: number;
  showShare: boolean;
  onSelect: (step: TraceStep) => void;
  onToggle: () => void;
}) {
  const duration =
    step.durationMs > 0
      ? `${step.timing.source === 'estimated' ? '≈' : ''}${formatDuration(step.durationMs)}`
      : '—';
  const summary = step.argsSummary || step.resultSummary || step.text;
  const canExpand = expandable(step);
  const cache = cacheRate(step);
  return (
    <>
      <div className={`trace-step-wrap${active ? ' active' : ''}`}>
        <button
          type="button"
          className={`trace-step${active ? ' active' : ''}`}
          data-kind={step.kind}
          aria-current={active ? 'true' : undefined}
          aria-expanded={canExpand ? expanded : undefined}
          title={canExpand ? (expanded ? '点击收起原文' : '点击展开原文') : undefined}
          onClick={() => {
            onSelect(step);
            if (canExpand) onToggle();
          }}
        >
          <span className="trace-idx mono">{String(step.index).padStart(2, '0')}</span>
          <span className="trace-badge" data-kind={step.kind}>
            {step.title === '模型输出' ? '输出' : BADGE[step.kind]}
          </span>
          <span className="trace-main">
            {turnIndex !== undefined && (
              <span className="trace-turn-tag mono">第 {turnIndex} 轮</span>
            )}
            {!GENERIC_TITLES.has(step.title) && (
              <span className="trace-title mono">{step.toolName || step.title}</span>
            )}
            {summary && (
              <span className="trace-summary mono">
                <Highlight text={summary} query={query} />
              </span>
            )}
            {step.resultSummary && step.argsSummary && (
              <span className="trace-result">
                <span className="trace-arrow" aria-hidden="true">→</span>
                <Highlight text={step.resultSummary} query={query} />
              </span>
            )}
          </span>
          {cache !== null ? (
            <span className="trace-cache" title="缓存命中：缓存令牌 ÷ 入参令牌">
              缓存 {cache}%
            </span>
          ) : (
            <span className="trace-cache-slot" aria-hidden="true" />
          )}
          {showShare ? <ShareCell step={step} /> : <span className="trace-share" aria-hidden="true" />}
          <span
            className="trace-dur mono"
            title={step.timing.source === 'estimated' ? '按相邻步骤估算' : '真实起止'}
          >
            {duration}
          </span>
          <span className={`trace-dot ${step.status ?? 'success'}`} aria-hidden="true" />
          <span className="trace-caret" aria-hidden="true">
            {canExpand ? (expanded ? '⌄' : '›') : ''}
          </span>
        </button>
      </div>
      {step.diff && <Diff step={step} />}
      {canExpand && expanded && <StepDetail step={step} />}
    </>
  );
}
