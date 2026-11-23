// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { MetricCard } from './MetricCard';

afterEach(cleanup);

describe('MetricCard', () => {
  it('null 值显示占位符', () => {
    render(<MetricCard label="通过率" value={null} />);
    expect(screen.getByText('通过率')).toBeTruthy();
    expect(screen.getByText('—')).toBeTruthy();
  });

  it('显示数值、提示与 tone 样式', () => {
    const { container } = render(
      <MetricCard label="耗时" value={120} tone="good" hint="近 7 天" />,
    );
    expect(screen.getByText('120')).toBeTruthy();
    expect(screen.getByText('近 7 天')).toBeTruthy();
    expect(container.querySelector('.tone-good')).not.toBeNull();
  });
});
