/** 时间格式化：后端时间戳是 UTC ISO，展示时统一转本地时区，避免出现早 8 小时。 */

/** ISO / 毫秒时间 → Date；空值或非法值返回 null。 */
export function toDate(value?: string | number | null): Date | null {
  if (value === undefined || value === null || value === '') return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

/** ISO → 本地 HH:MM。缺值或非法值返回空串。 */
export function formatClock(value?: string | number | null): string {
  const date = toDate(value);
  if (!date) return '';
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** ISO → 本地 YYYY-MM-DD HH:mm:ss。缺值或非法值返回 '—'。 */
export function formatDateTime(value?: string | number | null): string {
  const date = toDate(value);
  if (!date) return '—';
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** 毫秒耗时 → 人类可读。 */
export function formatDuration(ms: number): string {
  if (ms <= 0) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m${Math.round((ms % 60_000) / 1000)}s`;
}
