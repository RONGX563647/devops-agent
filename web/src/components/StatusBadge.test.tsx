// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { StatusBadge } from './StatusBadge';

afterEach(cleanup);

describe('StatusBadge', () => {
  it.each([
    ['success', '成功'],
    ['failed', '失败'],
    ['running', '进行中'],
  ] as const)('%s 显示 %s', (status, label) => {
    const { container } = render(<StatusBadge status={status} />);
    expect(screen.getByText(label)).toBeTruthy();
    expect(container.querySelector(`.badge-${status}`)).not.toBeNull();
  });
});
