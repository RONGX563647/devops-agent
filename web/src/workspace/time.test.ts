import { describe, expect, it } from 'vitest';
import { formatAgo, formatClock, formatDateTime, formatDuration, toDate } from './time';

describe('time 工具', () => {
  it('非法时间返回 null / 占位符', () => {
    expect(toDate(null)).toBeNull();
    expect(toDate('not-a-date')).toBeNull();
    expect(formatDateTime(null)).toBe('—');
    expect(formatClock(undefined)).toBe('');
  });

  it('按本地时区格式化', () => {
    const local = new Date(2026, 10, 23, 9, 5, 7);
    const iso = local.toISOString();
    expect(formatClock(iso)).toBe('09:05');
    expect(formatDateTime(iso)).toBe('2026-11-23 09:05:07');
  });

  it('相对时间分级：分钟 / 小时 / 天 / 月，未来时间不出现负数', () => {
    const now = new Date('2026-11-28T12:00:00Z');
    const ago = (ms: number) => new Date(now.getTime() - ms).toISOString();
    expect(formatAgo(ago(10_000), now)).toBe('刚刚');
    expect(formatAgo(ago(5 * 60_000), now)).toBe('5 分钟前');
    expect(formatAgo(ago(3 * 3_600_000), now)).toBe('3 小时前');
    expect(formatAgo(ago(2 * 86_400_000), now)).toBe('2 天前');
    expect(formatAgo(ago(65 * 86_400_000), now)).toBe('2 个月前');
    expect(formatAgo(ago(-60_000), now)).toBe('刚刚');
    expect(formatAgo(null)).toBe('—');
  });

  it('耗时分级展示', () => {
    expect(formatDuration(0)).toBe('—');
    expect(formatDuration(320)).toBe('320ms');
    expect(formatDuration(1500)).toBe('1.5s');
    expect(formatDuration(65_000)).toBe('1m5s');
  });
});
