/** 上下文构成条：首枪输入里缓存命中与本次实算两段。 */

/** 令牌数按人眼习惯缩写；数值本身都是模型回传的精确 token。 */
export function fmtTok(value: number): string {
  if (value >= 10_000) return `${Math.round(value / 1000)}k`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

/** 上下文构成条：首枪输入里多少命中缓存、多少是本次实算，两段都来自模型回传。
 *
 * 列表里的第 00 步与右侧「上下文」页签共用这一份，口径不能分叉。
 */
export function Composition({ input, cache }: { input: number; cache: number }) {
  // 两段都来自模型回传：命中缓存 = cache_input_tokens，本次计算 = input − cache。
  // 不再倒推「历史/系统提示各占多少」——那是估算，误差全堆在历史那一段。
  const hit = Math.max(0, Math.min(cache, input));
  const fresh = Math.max(0, input - hit);
  const parts = [
    { key: 'cache', label: '命中缓存', value: hit },
    { key: 'fresh', label: '本次计算', value: fresh },
  ];
  return (
    <div className="ctx-comp">
      <div
        className="ctx-comp-bar"
        role="img"
        aria-label={`首枪输入 ${input} token，其中命中缓存 ${hit}`}
      >
        {parts.map((part) =>
          part.value > 0 ? (
            <span
              key={part.key}
              className="ctx-seg"
              data-kind={part.key}
              style={{ flexGrow: part.value }}
              title={`${part.label} ${part.value.toLocaleString()} tok`}
            />
          ) : null,
        )}
      </div>
      <div className="ctx-legend mono">
        {parts.map((part) => (
          <span key={part.key}>
            <i data-kind={part.key} aria-hidden="true" />
            {part.label} {fmtTok(part.value)}
          </span>
        ))}
      </div>
    </div>
  );
}
