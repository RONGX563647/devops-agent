// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatTab } from './ChatTab';

const start = vi.fn();
const stop = vi.fn();

/** 可变的流状态：测试里直接改写字段后 rerender，即可模拟流式推进。 */
const stream = {
  running: false,
  text: '',
  events: [] as unknown[],
  steps: [] as unknown[],
  inputTokens: 0,
  outputTokens: 0,
  runId: null as number | null,
  error: null as string | null,
  modelName: '',
  startedAt: null as string | null,
  endedAt: null as string | null,
};

vi.mock('../api/useRunStream', () => ({
  useRunStream: () => ({ ...stream, start, stop }),
}));

function resetStream() {
  stream.running = false;
  stream.text = '';
  stream.events = [];
  stream.steps = [];
  stream.inputTokens = 0;
  stream.outputTokens = 0;
  stream.runId = null;
  stream.error = null;
  stream.modelName = '';
  stream.startedAt = null;
  stream.endedAt = null;
}

function submit(text: string) {
  const box = screen.getByRole('textbox', { name: '用户输入' });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: /发送/ }));
}

beforeEach(() => {
  start.mockClear();
  stop.mockClear();
  resetStream();
});

describe('ChatTab', () => {
  it('渲染空态与输入框', () => {
    render(<ChatTab session={null} />);
    expect(screen.getByText(/输入问题开始对话/)).toBeTruthy();
    expect(screen.getByRole('button', { name: /发送/ })).toBeTruthy();
  });

  it('提交后追加用户消息并调用 start', () => {
    render(<ChatTab session="s-1" />);
    submit('你好');
    expect(screen.getByText('你好')).toBeTruthy();
    expect(start).toHaveBeenCalledWith('s-1', '你好');
  });

  it('无会话时用 live-1 作为会话 ID', () => {
    render(<ChatTab session={null} />);
    submit('hi');
    expect(start).toHaveBeenCalledWith('live-1', 'hi');
  });

  it('首 token 未到前显示推理占位，而不是空白气泡', () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    stream.running = true;
    rerender(<ChatTab session="s-1" />);
    expect(screen.getByText('正在推理…')).toBeTruthy();
  });

  it('收到文本后逐字出字并显示光标', async () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');

    stream.running = true;
    stream.text = '持续交付是一种工程实践';
    rerender(<ChatTab session="s-1" />);

    await waitFor(() => {
      expect(document.querySelector('.chat-cursor')).not.toBeNull();
    });
    // 打字机是渐进的：首帧只揭示前几个字，不等同于一次性全出
    expect(screen.queryByText('持续交付是一种工程实践')).toBeNull();
  });

  it('流结束后光标移除并显示全文', async () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');

    stream.running = true;
    stream.text = '流水线失败常见于测试阶段';
    rerender(<ChatTab session="s-1" />);
    await waitFor(() => expect(document.querySelector('.chat-cursor')).not.toBeNull());

    stream.running = false;
    rerender(<ChatTab session="s-1" />);
    expect(await screen.findByText('流水线失败常见于测试阶段')).toBeTruthy();
    expect(document.querySelector('.chat-cursor')).toBeNull();
  });
});

afterEach(cleanup);
