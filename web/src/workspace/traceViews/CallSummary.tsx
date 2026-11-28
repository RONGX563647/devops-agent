/** 调用组统计面板：点「调用」视图里的一级节点，右侧回答「这组到底稳不稳」。 */

import type { TraceStep } from '../../types';
import { failureReasons, type CallGroup } from '../traceData/calls';
import { percentile } from '../traceData/stats';
import { formatDuration } from '../time';

const KIND_TEXT: Record<CallGroup['type'], string> = {
  model: '模型调用组',
  tool: '工具调用组',
};

/** 百分比：分母为 0 时给 —，不把「没有调用」说成 0%。 */
function rate(part: number, whole: number): string {
  return whole > 0 ? `${Math.round((part / whole) * 100)}%` : '—';
}

export function CallSummary({
  group,
  onSelect,
}: {
  group: CallGroup;
  onSelect: (step: TraceStep) => void;
}) {
  const durations = group.calls.map((call) => call.step.durationMs);
  const avg = group.total / group.calls.length;
  const p95 = percentile(durations, 0.95);
  const reasons = failureReasons(group);
  const slowest = [...group.calls].sort((a, b) => b.step.durationMs - a.step.durationMs).slice(0, 3);
  const tokensIn = group.calls.reduce((sum, call) => sum + (call.step.tokensIn ?? 0), 0);
  const tokensOut = group.calls.reduce((sum, call) => sum + (call.step.tokensOut ?? 0), 0);
  const cache = group.calls.reduce((sum, call) => sum + (call.step.cacheTokens ?? 0), 0);
  return (
    <div className="inspect-summary">
      <div className="inspect-grid">
        <span className="inspect-key">层级</span>
        <span>
          {KIND_TEXT[group.type]} · {group.name}
        </span>
        <span className="inspect-key">调用次数</span>
        <span className="mono">{group.calls.length} 次</span>
        <span className="inspect-key">失败</span>
        <span className={group.failed > 0 ? 'inspect-status error' : 'mono'}>
          {group.failed} 次 · 失败率 {rate(group.failed, group.calls.length)}
        </span>
        <span className="inspect-key">合计耗时</span>
        <span className="mono">{formatDuration(group.total)}</span>
        <span className="inspect-key">均值</span>
        <span className="mono">{formatDuration(avg)}</span>
        <span className="inspect-key">P95</span>
        <span className="mono">{p95 === null ? '—' : formatDuration(p95)}</span>
        <span className="inspect-key">最慢</span>
        <span className="mono">{formatDuration(group.slowest.durationMs)}</span>
        {group.type === 'model' && (tokensIn > 0 || tokensOut > 0) && (
          <>
            <span className="inspect-key">Token</span>
            <span className="mono">
              入 {tokensIn} / 出 {tokensOut}
              {cache > 0 ? ` · 缓存 ${cache}` : ''}
            </span>
          </>
        )}
      </div>

      <div className="inspect-section">
        <div className="inspect-section-head">
          <span>失败原因分类</span>
          <span className="mono inspect-muted">{group.failed} 次</span>
        </div>
        {reasons.length === 0 ? (
          <p className="inspect-empty">这组没有失败调用。</p>
        ) : (
          <ul className="failure-list">
            {reasons.map((item) => (
              <li key={item.reason}>
                <span className="failure-count mono">{item.count}</span>
                <span className="failure-reason">{item.reason}</span>
                <button
                  type="button"
                  className="inspect-link"
                  onClick={() => onSelect(item.steps[0])}
                >
                  查看
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="inspect-section">
        <div className="inspect-section-head">
          <span>最慢的调用</span>
        </div>
        <ul className="inspect-links">
          {slowest.map((call) => (
            <li key={call.step.id}>
              <button type="button" className="inspect-link" onClick={() => onSelect(call.step)}>
                第 {call.turnIndex} 轮 · 第 {String(call.step.index).padStart(2, '0')} 步
              </button>
              <span className="mono">{formatDuration(call.step.durationMs)}</span>
            </li>
          ))}
        </ul>
      </div>
      <p className="inspect-note">
        失败率 = 失败次数 ÷ 调用次数；原因按报错首行归并，同一句反复出现才值得先修。
      </p>
    </div>
  );
}
