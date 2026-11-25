import { describe, expect, it } from 'vitest';
import { verdictClass, verdictText } from './labels';

describe('verdictText', () => {
  it('短文案用于徽标，长文案用于概览结论', () => {
    expect(verdictText('pass')).toBe('通过');
    expect(verdictText('insufficient')).toBe('样本不足');
    expect(verdictText('insufficient', true)).toBe('样本不足，不参与发布门禁');
    expect(verdictText('fail', true)).toBe('未通过：存在硬失败或门禁未达');
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
});
