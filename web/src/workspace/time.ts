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

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** ISO → 相对时间（刚刚 / 12 分钟前 / 3 小时前 / 5 天前 / 2 个月前）。
 *
 * 超过一年的旧记录直接给日期：`400 天前` 这种读数对判断「这工作区还活着吗」没有帮助。
 * 未来时间（时钟偏差）按「刚刚」处理，不出现负数。
 */
export function formatAgo(value?: string | number | null, now: Date = new Date()): string {
  const date = toDate(value);
  if (!date) return '—';
  const diff = now.getTime() - date.getTime();
  if (diff < MINUTE) return '刚刚';
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)} 分钟前`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)} 小时前`;
  const days = Math.floor(diff / DAY);
  if (days < 30) return `${days} 天前`;
  if (days < 365) return `${Math.floor(days / 30)} 个月前`;
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** ISO → 侧栏用的极简相对时间：刚刚 / 3分 / 5时 / 2天 / 4月 / 2025-01-02。
 *
 * 侧栏每行右侧只留 5~6 个字符，`5 天前` 这种带空格的说法会把标题挤窄；
 * 精度只到「帮助判断新近」，不是精确时长。
 */
export function formatAgoShort(value?: string | number | null, now: Date = new Date()): string {
  const date = toDate(value);
  if (!date) return '';
  const diff = now.getTime() - date.getTime();
  if (diff < MINUTE) return '刚刚';
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)}分`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)}时`;
  const days = Math.floor(diff / DAY);
  if (days < 30) return `${days}天`;
  if (days < 365) return `${Math.floor(days / 30)}月`;
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}
