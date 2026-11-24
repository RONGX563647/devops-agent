/** 质量页共用的数字格式化：缺值统一显示「—」，不显示 0 冒充数据。 */

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(1)}%`;
}

export function formatScore(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return value.toFixed(digits);
}

/** 带符号的 delta：百分点用 pp，评分用绝对值。 */
export function signedDelta(value: number | null | undefined, unit: 'pp' | 'score'): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const scaled = unit === 'pp' ? value * 100 : value;
  const sign = scaled > 0 ? '+' : '';
  return `${sign}${scaled.toFixed(1)}${unit === 'pp' ? 'pp' : ''}`;
}

/** 精度上限：n 条用例时 1/n 就是一个用例的权重。 */
export function precisionLimit(n: number): number {
  return n > 0 ? 1 / n : 1;
}
