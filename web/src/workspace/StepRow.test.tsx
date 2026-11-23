// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { ProcessEntry } from '../types';
import { StepList, StepRow, summarize } from './StepRow';

function step(overrides: Partial<ProcessEntry> = {}): ProcessEntry {
  return {
    id: 'tool:1',
    kind: 'tool',
    label: '调用工具 · bash',
    detail: 'ls -la',
    state: 'success',
    ...overrides,
  };
}

afterEach(cleanup);

describe('summarize', () => {
  it('压成单行并按长度截断', () => {
    expect(summarize('a\n  b')).toBe('a b');
    expect(summarize('x'.repeat(80), 10)).toBe('xxxxxxxxxx…');
  });
});

describe('StepRow', () => {
  it('生命周期渲染成一行运行日志', () => {
    const { container } = render(
      <StepRow step={step({ kind: 'lifecycle', label: '模型调用', detail: 'qwen3.8-flash' })} />,
    );
    expect(container.querySelector('.step-log')?.textContent).toBe('模型调用 · qwen3.8-flash');
  });

  it('思考块默认收起，运行中显示思考中…并给出字数', () => {
    const { container } = render(
      <StepRow step={step({ kind: 'thinking', label: '思考中', detail: '先看清问题', state: 'running' })} />,
    );
    expect(screen.getByText('思考中…')).toBeTruthy();
    expect(screen.getByText('5')).toBeTruthy();
    expect(container.querySelector('details')?.hasAttribute('open')).toBe(false);
    expect(screen.getByText('先看清问题')).toBeTruthy();
  });

  it('工具行显示类型、摘要与完成状态，点击展开完整返回', () => {
    const { container } = render(<StepRow step={step({ detail: 'ls -la\n总计 3 个文件' })} />);
    expect(screen.getByText('调用工具 · bash')).toBeTruthy();
    expect(screen.getByText('完成')).toBeTruthy();
    expect(container.querySelector('.step-result')).toBeNull();

    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true');
    expect(container.querySelector('.step-result')?.textContent).toContain('总计 3 个文件');
  });

  it('异常行默认展开，并带 error 语义', () => {
    const { container } = render(
      <StepRow step={step({ kind: 'error', label: '运行失败', detail: '连接超时', state: 'error' })} />,
    );
    expect(container.querySelector('.step-line.error')).not.toBeNull();
    expect(screen.getByRole('button').getAttribute('aria-expanded')).toBe('true');
    // 摘要行与展开的返回块各出现一次
    expect(screen.getAllByText('连接超时')).toHaveLength(2);
  });

  it('无细节的步骤不可展开，也不渲染返回块', () => {
    const { container } = render(
      <StepRow step={step({ kind: 'control', label: '等待用户确认', detail: '', state: 'running' })} />,
    );
    fireEvent.click(screen.getByRole('button'));
    expect(container.querySelector('.step-result')).toBeNull();
    expect(screen.getByText('执行中')).toBeTruthy();
  });

  it('控制类运行中按「等人」语义呈现，结束后退回成功态', () => {
    const { container, rerender } = render(
      <StepRow step={step({ kind: 'control', label: '等待用户确认', detail: '', state: 'running' })} />,
    );
    expect(container.querySelector('.step-line.warn')).not.toBeNull();

    rerender(
      <StepRow step={step({ kind: 'control', label: '用户已确认', detail: '', state: 'success' })} />,
    );
    expect(container.querySelector('.step-line.warn')).toBeNull();
    expect(container.querySelector('.step-line.success')).not.toBeNull();
  });

  it('正文步骤只标记，不重复贴全文', () => {
    const { container } = render(
      <StepRow step={step({ kind: 'text', label: '正文输出', detail: '这是一整段正文' })} />,
    );
    expect(screen.getByText('正文输出')).toBeTruthy();
    // 结束后不再重复标状态，正文本身已经说明“写完了”
    expect(container.querySelector('.step-state')).toBeNull();
    expect(container.textContent).not.toContain('这是一整段正文');
  });

  it('正文仍在输出时标出输出中', () => {
    render(<StepRow step={step({ kind: 'text', label: '正文输出', detail: '半句', state: 'running' })} />);
    expect(screen.getByText('输出中')).toBeTruthy();
  });
});

describe('StepList', () => {
  it('按种类渲染每一行并带 data-kind', () => {
    const { container } = render(
      <StepList
        steps={[
          step({ id: '1', kind: 'lifecycle', label: '开始回复', detail: '' }),
          step({ id: '2', kind: 'thinking', label: '思考', detail: '想', state: 'success' }),
          step({ id: '3', kind: 'tool' }),
          step({ id: '4', kind: 'text', label: '正文输出', detail: '正文' }),
        ]}
      />,
    );
    const kinds = Array.from(container.querySelectorAll('[data-kind]')).map((el) =>
      el.getAttribute('data-kind'),
    );
    expect(kinds).toEqual(['lifecycle', 'thinking', 'tool', 'text']);
  });

  it('空列表不渲染', () => {
    const { container } = render(<StepList steps={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
