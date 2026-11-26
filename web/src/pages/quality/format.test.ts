import { describe, expect, it } from 'vitest';
import { formatClock, formatCost, formatDuration, formatTime } from './format';

describe('formatTime / formatClock', () => {
  it('把 UTC 时间换算成本地时区：同一瞬间的不同写法必须一致', () => {
    const utc = formatTime('2026-11-25T06:00:00Z');
    const offset = formatTime('2026-11-25T14:00:00+08:00');
    expect(utc).toBe(offset);
    expect(utc).not.toBe('—');
  });

  it('缺值或非法值返回「—」，不显示 1970', () => {
    expect(formatTime('')).toBe('—');
    expect(formatTime(null)).toBe('—');
    expect(formatTime('not-a-date')).toBe('—');
    expect(formatClock(undefined)).toBe('—');
  });

  it('紧凑格式只到分钟', () => {
    expect(formatClock('2026-11-25T06:00:00Z')).not.toContain('2026');
  });
});

describe('formatDuration', () => {
  it('按量级换单位，别让人心算毫秒', () => {
    expect(formatDuration(420)).toBe('420ms');
    expect(formatDuration(1830)).toBe('1.8s');
    expect(formatDuration(95_000)).toBe('1m35s');
  });

  it('非法值不冒充 0', () => {
    expect(formatDuration(null)).toBe('—');
    expect(formatDuration(-1)).toBe('—');
    expect(formatDuration(Number.NaN)).toBe('—');
  });
});

describe('formatCost', () => {
  it('没配单价时显示原因，绝不说「0 美元」', () => {
    expect(formatCost(null, '未配置单价（X）')).toBe('未计价 · 未配置单价（X）');
    expect(formatCost(null)).toBe('未计价');
  });

  it('小额成本保留四位小数，避免被截成 0.00', () => {
    expect(formatCost(0.0123)).toBe('$0.0123');
    expect(formatCost(2)).toBe('$2.00');
  });
});
