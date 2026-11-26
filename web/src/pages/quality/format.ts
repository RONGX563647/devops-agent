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

/**
 * 时间本地化：后端一律存 UTC ISO 8601，展示必须换成本地时区。
 *
 * 原实现直接截字符串再标一个「UTC」——带偏移量的时间会被截成错的本地时刻，
 * 而且用户看到的时间与自己手表永远差几个小时。非法值返回「—」而不是 1970。
 */
const LOCAL_TIME = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

const LOCAL_CLOCK = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

function toDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** 完整本地时间，用于结论卡与身份卡。 */
export function formatTime(iso: string | null | undefined): string {
  const date = toDate(iso);
  return date ? LOCAL_TIME.format(date) : '—';
}

/** 紧凑本地时间，用于批次 chip 这种一眼扫过的地方。 */
export function formatClock(iso: string | null | undefined): string {
  const date = toDate(iso);
  return date ? LOCAL_CLOCK.format(date) : '—';
}

/** 耗时：秒级以内给 ms，超过给分秒，避免「1830ms」这种要心算的写法。 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms) || ms < 0) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m${Math.round(seconds - minutes * 60)}s`;
}

/**
 * 成本：未配置单价时显示后端给的原因，绝不写「0 美元」冒充免费。
 * `note` 是后端算不出来的理由（例如缺少单价环境变量）。
 */
export function formatCost(
  usd: number | null | undefined,
  note?: string | null,
): string {
  if (usd === null || usd === undefined || Number.isNaN(usd)) {
    // 用中点连接而不是再包一层括号：后端的原因里本身就带括号。
    return note ? `未计价 · ${note}` : '未计价';
  }
  return `$${usd.toFixed(usd < 1 ? 4 : 2)}`;
}
