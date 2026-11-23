// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatTab } from './ChatTab';

const start = vi.fn();
const stop = vi.fn();

vi.mock('../api/useRunStream', () => ({
  useRunStream: () => ({
    running: false,
    text: '',
    events: [],
    inputTokens: 0,
    outputTokens: 0,
    runId: null,
    error: null,
    start,
    stop,
  }),
}));

beforeEach(() => {
  start.mockClear();
  stop.mockClear();
});

describe('ChatTab', () => {
  it('渲染空态与输入框', () => {
    render(<ChatTab session={null} />);
    expect(screen.getByText(/输入问题开始对话/)).toBeTruthy();
    expect(screen.getByRole('button', { name: /发送/ })).toBeTruthy();
  });

  it('提交后追加用户消息并调用 start', () => {
    render(<ChatTab session="s-1" />);
    const box = screen.getByRole('textbox', { name: '用户输入' });
    fireEvent.change(box, { target: { value: '你好' } });
    fireEvent.click(screen.getByRole('button', { name: /发送/ }));
    expect(screen.getByText('你好')).toBeTruthy();
    expect(start).toHaveBeenCalledWith('s-1', '你好');
  });

  it('无会话时用 live-1 作为会话 ID', () => {
    render(<ChatTab session={null} />);
    const box = screen.getByRole('textbox', { name: '用户输入' });
    fireEvent.change(box, { target: { value: 'hi' } });
    fireEvent.click(screen.getByRole('button', { name: /发送/ }));
    expect(start).toHaveBeenCalledWith('live-1', 'hi');
  });
});

afterEach(cleanup);
