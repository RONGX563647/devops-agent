// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatTab, formatClock, shouldStickToBottom } from './ChatTab';

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

describe('shouldStickToBottom', () => {
  it('贴底或接近底部时继续吸底', () => {
    expect(shouldStickToBottom(1000, 600, 400)).toBe(true); // 正好在底部
    expect(shouldStickToBottom(1000, 550, 400)).toBe(true); // 距底 50px
  });

  it('上翻超过阈值后不再吸底，避免拽回阅读位置', () => {
    expect(shouldStickToBottom(1000, 400, 400)).toBe(false); // 距底 200px
  });

  it('内容不足一屏时始终吸底', () => {
    expect(shouldStickToBottom(300, 0, 400)).toBe(true);
  });
});

describe('formatClock', () => {
  it('取本地时分，非法或缺值返回空串', () => {
    const local = new Date(2026, 10, 23, 9, 5);
    expect(formatClock(local.toISOString())).toBe('09:05');
    expect(formatClock(null)).toBe('');
    expect(formatClock('bad')).toBe('');
  });
});

describe('ChatTab', () => {
  it('渲染空态引导卡与输入框', () => {
    render(<ChatTab session={null} />);
    expect(screen.getByText('开始一次会话')).toBeTruthy();
    expect(screen.getByRole('button', { name: /发送/ })).toBeTruthy();
  });

  it('点击示例问题直接发起会话', () => {
    render(<ChatTab session="s-1" />);
    fireEvent.click(screen.getByRole('button', { name: /CI 流程是怎么跑的/ }));
    expect(start).toHaveBeenCalledWith('s-1', '这个仓库的 CI 流程是怎么跑的？');
    expect(screen.getByText('这个仓库的 CI 流程是怎么跑的？')).toBeTruthy();
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

  it('首 token 未到时不留空气泡', () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    stream.running = true;
    rerender(<ChatTab session="s-1" />);
    expect(screen.queryByText('正在推理…')).toBeNull();
    expect(document.querySelector('.chat-msg.assistant .chat-bubble')).toBeNull();
  });

  it('收到文本即完整渲染，不做逐字揭示', () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    stream.running = true;
    stream.text = '持续交付是一种工程实践';
    rerender(<ChatTab session="s-1" />);
    expect(screen.getByText('持续交付是一种工程实践')).toBeTruthy();
    expect(document.querySelector('.chat-cursor')).toBeNull();
  });

  it('流结束后保留全文', () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    stream.running = true;
    stream.text = '流水线失败常见于测试阶段';
    rerender(<ChatTab session="s-1" />);
    stream.running = false;
    rerender(<ChatTab session="s-1" />);
    expect(screen.getByText('流水线失败常见于测试阶段')).toBeTruthy();
  });

  it('助手消息带复制与重试操作条，重试复用原问题', async () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    stream.text = '这是答案';
    stream.endedAt = new Date(2026, 10, 23, 9, 5).toISOString();
    rerender(<ChatTab session="s-1" />);

    expect(await screen.findByText('这是答案')).toBeTruthy();
    expect(screen.getByRole('button', { name: '复制' })).toBeTruthy();
    expect(screen.getByText('09:05')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(start).toHaveBeenCalledTimes(2);
    expect(start).toHaveBeenLastCalledWith('s-1', 'hi');
  });

  it('回答按 Markdown 渲染，而不是整段纯文本', async () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    stream.text = '## 结论\n\n- **先看日志**\n- 再跑 `ruff`';
    rerender(<ChatTab session="s-1" />);

    expect(await screen.findByRole('heading', { level: 2, name: '结论' })).toBeTruthy();
    expect(document.querySelector('.md strong')?.textContent).toBe('先看日志');
    expect(document.querySelector('.md code')?.textContent).toBe('ruff');
  });

  it('过程步骤直接显示在回答上方，工具行可点开返回', async () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    stream.steps = [
      { id: 'reply:1', kind: 'lifecycle', label: '开始回复', detail: '', state: 'success' },
      {
        id: 'tool:t1',
        kind: 'tool',
        label: '调用工具 · bash',
        detail: 'ls -la',
        state: 'success',
      },
    ];
    stream.text = '看完了';
    rerender(<ChatTab session="s-1" />);

    expect(await screen.findByText('开始回复')).toBeTruthy();
    const line = screen.getByRole('button', { name: /调用工具 · bash/ });
    expect(line.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(line);
    expect(document.querySelector('.step-result')?.textContent).toBe('ls -la');
    expect(screen.getByText('看完了')).toBeTruthy();
  });

  it('运行失败时给出中断提示与重试入口', async () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    stream.error = '流式请求失败 500';
    rerender(<ChatTab session="s-1" />);

    expect(await screen.findByText('中断')).toBeTruthy();
    expect(screen.getByText('流式请求失败 500')).toBeTruthy();
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy();
  });
});

afterEach(cleanup);
