// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { revealStep, TICK_MS, useTypewriter } from './useTypewriter';

describe('revealStep', () => {
  it('无积压返回 0', () => {
    expect(revealStep(0)).toBe(0);
    expect(revealStep(-5)).toBe(0);
  });

  it('剩余不足一步时直接补完，不留尾巴', () => {
    expect(revealStep(1)).toBe(1);
    expect(revealStep(2)).toBe(2);
    expect(revealStep(3)).toBe(3);
  });

  it('正常积压按每帧 3 字推进', () => {
    expect(revealStep(4)).toBe(3);
    expect(revealStep(80)).toBe(3);
  });

  it('严重积压时加速追赶', () => {
    expect(revealStep(81)).toBe(6);
    expect(revealStep(240)).toBe(6);
    expect(revealStep(241)).toBe(12);
  });
});

describe('useTypewriter', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('按帧推进而非一次性显示', () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useTypewriter('abcdefghij', true));

    expect(result.current.text).toBe('');
    expect(result.current.done).toBe(false);

    act(() => void vi.advanceTimersByTime(TICK_MS));
    expect(result.current.text).toBe('abc');

    act(() => void vi.advanceTimersByTime(TICK_MS));
    expect(result.current.text).toBe('abcdef');
  });

  it('流结束后直接给全文，不再有拖尾动画', () => {
    const { result } = renderHook(() => useTypewriter('完整文本', false));
    expect(result.current.text).toBe('完整文本');
    expect(result.current.done).toBe(true);
  });

  it('从打字中切到结束态会立即补齐剩余内容', () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(
      ({ typing }: { typing: boolean }) => useTypewriter('abcdef', typing),
      { initialProps: { typing: true } },
    );
    act(() => void vi.advanceTimersByTime(TICK_MS));
    expect(result.current.text).toBe('abc');

    rerender({ typing: false });
    expect(result.current.text).toBe('abcdef');
    expect(result.current.done).toBe(true);
  });

  it('target 变长后继续推进', () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(
      ({ target }: { target: string }) => useTypewriter(target, true),
      { initialProps: { target: 'abc' } },
    );
    act(() => void vi.advanceTimersByTime(TICK_MS));
    expect(result.current.text).toBe('abc');
    expect(result.current.done).toBe(true);

    rerender({ target: 'abcdefghi' });
    expect(result.current.done).toBe(false);
    act(() => void vi.advanceTimersByTime(TICK_MS));
    expect(result.current.text).toBe('abcdef');
  });
});
