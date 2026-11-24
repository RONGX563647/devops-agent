// @vitest-environment jsdom
/**
 * 上下文圆环与压缩时间线单测。
 *
 * 这两块 UI 的价值全在「说得准」：没有数据就不画（不能显示成 0%）、
 * 百分比与色调必须对得上阈值、压缩记录不能把「检查过但没压」也画成一次压缩。
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import {
  CompressionTimeline,
  ContextRing,
  contextRatio,
  formatRatio,
  formatTokens,
  toneFor,
  visibleCompressions,
} from './ContextRing';
import type { CompressionRecord } from '../types';

afterEach(cleanup);

function record(over: Partial<CompressionRecord> = {}): CompressionRecord {
  return {
    id: 'comp:1',
    before: 103053,
    after: 41208,
    threshold: 102400,
    state: 'done',
    summaryChars: 2140,
    at: '2026-11-24T12:58:27Z',
    endedAt: '2026-11-24T12:58:31Z',
    ...over,
  };
}

describe('上下文占用计算', () => {
  it('阈值未知时不返回比例（上层据此不画圆环）', () => {
    expect(contextRatio(null, 102400)).toBeNull();
    expect(contextRatio(1000, null)).toBeNull();
    expect(contextRatio(1000, 0)).toBeNull();
    expect(contextRatio(51200, 102400)).toBe(0.5);
  });

  it('分档按 60% / 85% 切', () => {
    expect(toneFor(0.2)).toBe('ok');
    expect(toneFor(0.6)).toBe('warn');
    expect(toneFor(0.849)).toBe('warn');
    expect(toneFor(0.85)).toBe('danger');
    expect(toneFor(1.37)).toBe('danger');
  });

  it('token 数带千分位，百分比超出 100% 也不截断', () => {
    expect(formatTokens(103053)).toBe('103,053');
    expect(formatTokens(999)).toBe('999');
    expect(formatRatio(0.834)).toBe('83%');
    expect(formatRatio(1.37)).toBe('137%');
  });
});

describe('ContextRing', () => {
  it('没有用量数据时什么都不渲染', () => {
    const { container } = render(<ContextRing tokens={null} threshold={102400} />);
    expect(container.innerHTML).toBe('');
  });

  it('渲染百分比与可读标签，tooltip 说明阈值来源', () => {
    render(<ContextRing tokens={51200} threshold={102400} contextSize={128000} />);
    const ring = screen.getByRole('img', { name: /上下文占用 50%/ });
    expect(ring.className).toContain('is-ok');
    expect(ring.getAttribute('title')).toContain('模型窗口 128,000');
    expect(ring.textContent).toBe('50%');
  });

  it('接近阈值变 warn、超过变 danger', () => {
    const { rerender } = render(<ContextRing tokens={80000} threshold={102400} />);
    expect(screen.getByRole('img').className).toContain('is-warn');
    rerender(<ContextRing tokens={103053} threshold={102400} />);
    expect(screen.getByRole('img').className).toContain('is-danger');
  });

  it('压缩进行中挂 is-busy 供动画与无障碍识别', () => {
    render(<ContextRing tokens={103053} threshold={102400} busy />);
    expect(screen.getByRole('img').className).toContain('is-busy');
  });
});

describe('CompressionTimeline', () => {
  it('只有未触发的检查时不渲染（否则每次推理都会刷一条）', () => {
    const { container } = render(
      <CompressionTimeline records={[record({ state: 'skipped', after: null })]} />,
    );
    expect(container.innerHTML).toBe('');
    expect(visibleCompressions([record({ state: 'skipped' })])).toHaveLength(0);
  });

  it('展示压缩前后与省下的比例、摘要长度', () => {
    render(<CompressionTimeline records={[record()]} />);
    expect(screen.getByText('上下文已压缩 1 次')).toBeTruthy();
    expect(screen.getByText('103,053 → 41,208')).toBeTruthy();
    expect(screen.getByText('省 60% · 摘要 2,140 字')).toBeTruthy();
  });

  it('进行中给「摘要生成中」，头部改成正在压缩', () => {
    render(
      <CompressionTimeline
        records={[record({ state: 'running', after: null, summaryChars: 0 })]}
      />,
    );
    expect(screen.getByText('正在压缩上下文')).toBeTruthy();
    expect(screen.getByText('103,053 → …')).toBeTruthy();
    expect(screen.getByText('摘要生成中')).toBeTruthy();
  });

  it('超过 limit 只显示最近几次，并说明还有多少更早的', () => {
    const records = [1, 2, 3, 4].map((n) =>
      record({ id: `c${n}`, before: 100000 + n, after: 40000 + n }),
    );
    render(<CompressionTimeline records={records} limit={2} />);
    const list = screen.getByRole('list', { name: '压缩记录' });
    expect(list.querySelectorAll('li')).toHaveLength(2);
    expect(screen.getByText('另有 2 次更早')).toBeTruthy();
  });
});
