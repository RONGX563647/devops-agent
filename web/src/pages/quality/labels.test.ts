import { describe, expect, it } from 'vitest';
import { TIER_LABEL, TRACK_KEYS, VERDICT_OPTIONS, verdictClass, verdictText } from './labels';

describe('verdictText', () => {
  it('短文案用于徽标，长文案用于概览结论', () => {
    expect(verdictText('pass')).toBe('通过');
    expect(verdictText('insufficient')).toBe('样本不足');
    expect(verdictText('insufficient', true)).toBe('样本不足，不参与发布门禁');
    expect(verdictText('fail', true)).toBe('未通过：存在硬失败或门禁未达');
  });

  it('局部与未证实是两句话，不能合并', () => {
    expect(verdictText('partial')).toBe('局部');
    expect(verdictText('unvalidated')).toBe('未证实');
    expect(verdictText('partial', true)).toContain('不作发布门禁');
    expect(verdictText('unvalidated', true)).toContain('校准');
  });

  it('未知结论原样透出，不伪装成通过', () => {
    expect(verdictText('not_comparable')).toBe('not_comparable');
  });
});

describe('verdictClass', () => {
  it('样本不足、回退、取消各有独立样式', () => {
    expect(verdictClass('pass')).toBe('is-pass');
    expect(verdictClass('fail')).toBe('is-fail');
    expect(verdictClass('insufficient')).toBe('is-insufficient');
    expect(verdictClass('warn')).toBe('is-warn');
    expect(verdictClass('cancelled')).toBe('is-cancelled');
    expect(verdictClass('running')).toBe('is-pending');
  });

  it('局部与未证实不复用黄红两色', () => {
    expect(verdictClass('partial')).toBe('is-partial');
    expect(verdictClass('unvalidated')).toBe('is-unvalidated');
    expect(verdictClass('partial')).not.toBe(verdictClass('unvalidated'));
  });
});

describe('共用常量', () => {
  it('赛道固定五行，顺序稳定', () => {
    expect(TRACK_KEYS).toEqual(['A', 'B', 'C', 'D', 'E']);
  });

  it('过滤下拉覆盖全部结论态，且第一项是「全部」', () => {
    const values = VERDICT_OPTIONS.map((item) => item.value);
    expect(values[0]).toBe('');
    for (const key of ['pass', 'partial', 'unvalidated', 'fail', 'insufficient']) {
      expect(values).toContain(key);
    }
  });

  it('分层文案两种都有，未知值不静默为空', () => {
    expect(TIER_LABEL.must_pass).toBe('必过');
    expect(TIER_LABEL.challenge).toBe('挑战');
  });
});
