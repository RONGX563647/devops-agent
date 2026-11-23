import { describe, expect, it } from 'vitest';
import { formatClock, formatDateTime, formatDuration, toDate } from './time';

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

  it('耗时分级展示', () => {
    expect(formatDuration(0)).toBe('—');
    expect(formatDuration(320)).toBe('320ms');
    expect(formatDuration(1500)).toBe('1.5s');
    expect(formatDuration(65_000)).toBe('1m5s');
  });
});
