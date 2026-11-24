// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { PendingCall } from '../types';
import { clampCommand, PermissionCard, stripAnsi } from './PermissionCard';

function call(over: Partial<PendingCall> = {}): PendingCall {
  return { tool_call_id: 't1', name: 'Bash', input: 'ls -la', risk: 'exec', ...over };
}

afterEach(cleanup);

describe('stripAnsi', () => {
  it('去掉颜色与光标控制序列，保留正文', () => {
    expect(stripAnsi('\u001b[31mFAILED\u001b[0m tests')).toBe('FAILED tests');
    expect(stripAnsi('ok')).toBe('ok');
  });
});

describe('clampCommand', () => {
  it('短命令原样返回，不标截断', () => {
    expect(clampCommand('pytest -q')).toEqual({ text: 'pytest -q', truncated: false });
  });

  it('超长命令截断并标出被折叠', () => {
    const long = 'a'.repeat(500);
    const result = clampCommand(long, 300);
    expect(result.truncated).toBe(true);
    expect(result.text).toHaveLength(301); // 300 字 + 省略号
    expect(result.text.endsWith('…')).toBe(true);
  });

  it('先剥 ANSI 再算长度，避免控制码占额度', () => {
    const colored = `\u001b[32m${'x'.repeat(10)}\u001b[0m`;
    const result = clampCommand(colored, 20);
    expect(result.text).toBe('x'.repeat(10));
    expect(result.truncated).toBe(false);
  });
});

describe('PermissionCard', () => {
  it('列出工具名、风险标签与命令原文', () => {
    render(<PermissionCard calls={[call()]} expiresAt={null} onDecide={() => {}} />);
    expect(screen.getByText('需要你确认')).toBeTruthy();
    expect(screen.getByText('Bash')).toBeTruthy();
    expect(screen.getByText('执行')).toBeTruthy();
    expect(document.querySelector('.confirm-command')?.textContent).toBe('ls -la');
  });

  it('有效至：只显示时分，缺省不显示', () => {
    const { rerender } = render(
      <PermissionCard calls={[call()]} expiresAt={new Date(2026, 10, 23, 9, 5).toISOString()} onDecide={() => {}} />,
    );
    expect(screen.getByText('有效至 09:05')).toBeTruthy();
    rerender(<PermissionCard calls={[call()]} expiresAt={null} onDecide={() => {}} />);
    expect(screen.queryByText(/有效至/)).toBeNull();
  });

  it('四个按钮各自回传对应的选择，默认不含落盘', () => {
    const onDecide = vi.fn();
    render(<PermissionCard calls={[call()]} expiresAt={null} onDecide={onDecide} />);

    fireEvent.click(screen.getByRole('button', { name: '这一次' }));
    fireEvent.click(screen.getByRole('button', { name: '本会话' }));
    fireEvent.click(screen.getByRole('button', { name: '以后都这样' }));
    fireEvent.click(screen.getByRole('button', { name: '拒绝' }));

    expect(onDecide.mock.calls).toEqual([['once'], ['session'], ['forever'], ['reject']]);
  });

  it('高危命令只允许这一次，「本会话」「以后都这样」都禁用', () => {
    render(
      <PermissionCard calls={[call({ input: 'rm -rf /', risk: 'danger' })]} expiresAt={null} onDecide={() => {}} />,
    );
    // 头部提示与条目风险标签各一处
    expect(screen.getAllByText('高危')).toHaveLength(2);
    for (const name of ['本会话', '以后都这样']) {
      const longLived = screen.getByRole('button', { name }) as HTMLButtonElement;
      expect(longLived.disabled).toBe(true);
      expect(longLived.title).toBe('高危命令只允许放行一次');
    }
    // 另两个仍然可用：拒绝/放行一次不能一起被锁死
    expect((screen.getByRole('button', { name: '这一次' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('执行中所有按钮都禁用，避免重复提交', () => {
    render(<PermissionCard calls={[call()]} expiresAt={null} busy onDecide={() => {}} />);
    for (const name of ['拒绝', '这一次', '本会话', '以后都这样']) {
      expect((screen.getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(true);
    }
  });

  it('命令按纯文本渲染，HTML 标签不会变成节点', () => {
    const payload = '<img src=x onerror="alert(1)">';
    render(<PermissionCard calls={[call({ input: payload })]} expiresAt={null} onDecide={() => {}} />);
    expect(document.querySelector('.confirm-command img')).toBeNull();
    expect(document.querySelector('.confirm-command')?.textContent).toBe(payload);
  });

  it('命令里的 ANSI 序列不显示成乱码', () => {
    render(
      <PermissionCard
        calls={[call({ input: '\u001b[31mFAIL\u001b[0m' })]}
        expiresAt={null}
        onDecide={() => {}}
      />,
    );
    expect(document.querySelector('.confirm-command')?.textContent).toBe('FAIL');
  });

  it('超长命令先折叠，点开看全文', () => {
    const long = `curl ${'x'.repeat(1200)}`;
    render(<PermissionCard calls={[call({ input: long })]} expiresAt={null} onDecide={() => {}} />);

    const shown = document.querySelector('.confirm-command')?.textContent ?? '';
    expect(shown.endsWith('…')).toBe(true);
    expect(shown.length).toBeLessThan(long.length);

    fireEvent.click(screen.getByRole('button', { name: '展开完整命令' }));
    expect(document.querySelector('.confirm-command')?.textContent).toBe(long);
    expect(screen.queryByRole('button', { name: '展开完整命令' })).toBeNull();
  });

  it('多条调用一起摊开，每条都有自己的风险标注', () => {
    render(
      <PermissionCard
        calls={[
          call({ tool_call_id: 't1', name: 'Read', input: 'a.py', risk: 'read' }),
          call({ tool_call_id: 't2', name: 'Write', input: 'b.py', risk: 'write' }),
        ]}
        expiresAt={null}
        onDecide={() => {}}
      />,
    );
    expect(document.querySelectorAll('.confirm-item')).toHaveLength(2);
    expect(screen.getByText('只读')).toBeTruthy();
    expect(screen.getByText('写入')).toBeTruthy();
  });
});
