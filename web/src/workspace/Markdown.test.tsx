// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Markdown } from './Markdown';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Markdown', () => {
  it('渲染标题、列表与行内标记', () => {
    const { container } = render(
      <Markdown content={'## 结论\n\n- 第一步\n- **重点**：用 `ruff` 检查'} />,
    );
    expect(screen.getByRole('heading', { level: 2, name: '结论' })).toBeTruthy();
    expect(container.querySelectorAll('li')).toHaveLength(2);
    expect(container.querySelector('strong')?.textContent).toBe('重点');
    expect(container.querySelector('code')?.textContent).toBe('ruff');
  });

  it('GFM 表格包在可滚动容器里', () => {
    const { container } = render(<Markdown content={'| 项 | 值 |\n| --- | --- |\n| a | 1 |'} />);
    expect(container.querySelector('.table-wrap table')).not.toBeNull();
    expect(container.querySelector('th')?.textContent).toBe('项');
  });

  it('代码块带复制按钮，点击写入剪贴板', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    const { container } = render(<Markdown content={'```bash\nruff check .\n```'} />);

    expect(container.querySelector('pre code')?.textContent).toContain('ruff check .');
    fireEvent.click(screen.getByRole('button', { name: '复制代码' }));

    expect(await screen.findByText('已复制')).toBeTruthy();
    expect(writeText).toHaveBeenCalledWith('ruff check .\n');
  });

  it('站外链接新窗口打开，站内链接保持原行为', () => {
    const { container } = render(
      <Markdown content={'[外链](https://example.com) 与 [站内](/run/1)'} />,
    );
    const [external, internal] = Array.from(container.querySelectorAll('a'));
    expect(external.getAttribute('target')).toBe('_blank');
    expect(external.getAttribute('rel')).toContain('noreferrer');
    expect(internal.getAttribute('target')).toBeNull();
  });

  it('不渲染原始 HTML，避免注入', () => {
    const { container } = render(<Markdown content={'<img src=x onerror=alert(1)>'} />);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('[onerror]')).toBeNull();
  });
});
