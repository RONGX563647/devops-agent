// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Attachment, ChatMessage } from '../types';
import { attachMeta, ChatTab, formatClock, formatSize, shouldStickToBottom } from './ChatTab';

const send = vi.fn();
const stop = vi.fn();
const onFirstSend = vi.fn();
const uploadAttachment = vi.fn();
const deleteAttachment = vi.fn();

vi.mock('../api/client', () => ({
  uploadAttachment: (file: File) => uploadAttachment(file),
  deleteAttachment: (id: string) => deleteAttachment(id),
}));

/** 可变的会话快照：测试直接改写 messages / 运行态后 rerender，模拟流式推进。 */
const snapshot = {
  sessionId: 's-1',
  messages: [] as ChatMessage[],
  running: false,
  loading: false,
  eventCount: 0,
  inputTokens: 0,
  outputTokens: 0,
  runId: null as number | null,
  error: null as string | null,
  modelName: '',
};

vi.mock('./store', () => ({
  useSession: () => snapshot,
  send: (session: string, text: string, attachments: Attachment[] = []) => {
    send(session, text, attachments);
    const now = new Date().toISOString();
    snapshot.messages = [
      ...snapshot.messages,
      { id: `u-${snapshot.messages.length}`, role: 'user', text, at: now, steps: [], attachments },
      {
        id: `a-${snapshot.messages.length}`,
        role: 'assistant',
        text: '',
        at: now,
        steps: [],
        pending: true,
        endedAt: null,
        runId: null,
      },
    ];
    snapshot.running = true;
  },
  stop: (session: string) => stop(session),
}));

function assistant(): ChatMessage {
  return snapshot.messages[snapshot.messages.length - 1];
}

function reset() {
  send.mockClear();
  stop.mockClear();
  onFirstSend.mockClear();
  uploadAttachment.mockReset();
  deleteAttachment.mockReset();
  deleteAttachment.mockResolvedValue(undefined);
  snapshot.messages = [];
  snapshot.running = false;
  snapshot.loading = false;
  snapshot.eventCount = 0;
  snapshot.inputTokens = 0;
  snapshot.outputTokens = 0;
  snapshot.runId = null;
  snapshot.error = null;
  snapshot.modelName = '';
}

function submit(text: string) {
  const box = screen.getByRole('textbox', { name: '用户输入' });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: /发送/ }));
}

/** 服务端返回的附件元信息，测试按需覆盖字段。 */
function attachment(over: Partial<Attachment> = {}): Attachment {
  return {
    id: 'a1b2c3d4e5f60718',
    name: 'notes.md',
    size: 1024,
    suffix: '.md',
    chars: 100,
    truncated: false,
    status: 'ok',
    ...over,
  };
}

/** 选中本地文件：jsdom 的 file input 没有 setter，直接改写 files 属性。 */
function pickFiles(files: File[]) {
  const input = screen.getByLabelText('选择要上传的文件') as HTMLInputElement;
  Object.defineProperty(input, 'files', { value: files, configurable: true });
  fireEvent.change(input);
}

beforeEach(reset);

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
  it('按本地时区取时分，非法或缺值返回空串', () => {
    const local = new Date(2026, 10, 23, 9, 5);
    expect(formatClock(local.toISOString())).toBe('09:05');
    expect(formatClock(null)).toBe('');
    expect(formatClock('bad')).toBe('');
  });
});

describe('ChatTab', () => {
  it('渲染空态引导卡与输入框', () => {
    render(<ChatTab session="s-1" />);
    expect(screen.getByText('开始一次会话')).toBeTruthy();
    expect(screen.getByRole('button', { name: /发送/ })).toBeTruthy();
  });

  it('点击示例问题直接发起会话', () => {
    render(<ChatTab session="s-1" />);
    fireEvent.click(screen.getByRole('button', { name: /CI 流程是怎么跑的/ }));
    expect(send).toHaveBeenCalledWith('s-1', '这个仓库的 CI 流程是怎么跑的？', []);
    expect(screen.getByText('这个仓库的 CI 流程是怎么跑的？')).toBeTruthy();
  });

  it('提交后追加用户消息并调用 send', () => {
    render(<ChatTab session="s-1" />);
    submit('你好');
    expect(screen.getByText('你好')).toBeTruthy();
    expect(send).toHaveBeenCalledWith('s-1', '你好', []);
  });

  it('草稿会话首次发送时通知外层落到 URL，不再固定 live-1', () => {
    render(<ChatTab session="draft-1" onFirstSend={onFirstSend} />);
    submit('hi');
    expect(onFirstSend).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith('draft-1', 'hi', []);
  });

  it('首 token 未到时不留空气泡', () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    rerender(<ChatTab session="s-1" />);
    expect(document.querySelector('.chat-msg.assistant .chat-bubble')).toBeNull();
    expect(screen.getByText('正在处理…')).toBeTruthy();
  });

  it('收到文本即完整渲染，不做逐字揭示', () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    assistant().text = '持续交付是一种工程实践';
    rerender(<ChatTab session="s-1" />);
    expect(screen.getByText('持续交付是一种工程实践')).toBeTruthy();
    expect(document.querySelector('.chat-cursor')).toBeNull();
  });

  it('流结束后保留全文', () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    assistant().text = '流水线失败常见于测试阶段';
    rerender(<ChatTab session="s-1" />);
    snapshot.running = false;
    assistant().pending = false;
    rerender(<ChatTab session="s-1" />);
    expect(screen.getByText('流水线失败常见于测试阶段')).toBeTruthy();
  });

  it('助手消息带复制与重试操作条，重试复用原问题', async () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    assistant().text = '这是答案';
    assistant().pending = false;
    assistant().endedAt = new Date(2026, 10, 23, 9, 5).toISOString();
    snapshot.running = false;
    rerender(<ChatTab session="s-1" />);

    expect(await screen.findByText('这是答案')).toBeTruthy();
    expect(screen.getByRole('button', { name: '复制' })).toBeTruthy();
    expect(screen.getByText('09:05')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(send).toHaveBeenCalledTimes(2);
    expect(send).toHaveBeenLastCalledWith('s-1', 'hi', []);
  });

  it('回答按 Markdown 渲染，而不是整段纯文本', async () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    assistant().text = '## 结论\n\n- **先看日志**\n- 再跑 `ruff`';
    rerender(<ChatTab session="s-1" />);

    expect(await screen.findByRole('heading', { level: 2, name: '结论' })).toBeTruthy();
    expect(document.querySelector('.md strong')?.textContent).toBe('先看日志');
    expect(document.querySelector('.md code')?.textContent).toBe('ruff');
  });

  it('过程步骤显示在回答上方，工具行可点开返回', async () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    assistant().steps = [
      { id: 'reply:1', kind: 'lifecycle', label: '开始回复', detail: '', state: 'success' },
      {
        id: 'tool:t1',
        kind: 'tool',
        label: '调用工具 · bash',
        detail: 'ls -la',
        state: 'success',
      },
    ];
    assistant().text = '看完了';
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
    assistant().error = '流式请求失败 500';
    snapshot.running = false;
    rerender(<ChatTab session="s-1" />);

    expect(await screen.findByText('中断')).toBeTruthy();
    expect(screen.getByText('流式请求失败 500')).toBeTruthy();
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy();
  });

  it('中文输入法组词中的 Enter 不发送，组词结束后才发送', () => {
    render(<ChatTab session="s-1" />);
    const box = screen.getByRole('textbox', { name: '用户输入' });
    fireEvent.change(box, { target: { value: '你好' } });

    fireEvent.keyDown(box, { key: 'Enter', isComposing: true });
    expect(send).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: 'Enter', isComposing: false });
    expect(send).toHaveBeenCalledWith('s-1', '你好', []);
  });

  it('生成中仍可输入，但发送按钮禁用、显示停止按钮', () => {
    const { rerender } = render(<ChatTab session="s-1" />);
    submit('hi');
    rerender(<ChatTab session="s-1" />);

    const box = screen.getByRole('textbox', { name: '用户输入' }) as HTMLTextAreaElement;
    expect(box.disabled).toBe(false);
    fireEvent.change(box, { target: { value: '下一句' } });
    expect(box.value).toBe('下一句');
    expect((screen.getByRole('button', { name: /发送/ }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: '停止' }));
    expect(stop).toHaveBeenCalledWith('s-1');
  });
});

describe('formatSize', () => {
  it('按量级切换 B / KB / MB', () => {
    expect(formatSize(512)).toBe('512 B');
    expect(formatSize(2048)).toBe('2.0 KB');
    expect(formatSize(5 * 1024 * 1024)).toBe('5.0 MB');
  });
});

describe('attachMeta', () => {
  it('上传中、解析失败、正常三态各有文案', () => {
    expect(attachMeta({ ...attachment(), status: 'uploading' })).toBe('上传中…');
    expect(attachMeta({ ...attachment(), status: 'error' })).toBe('无法解析');
    expect(attachMeta({ ...attachment(), size: 2048, chars: 1200 })).toBe('2.0 KB · 1200 字');
    expect(attachMeta({ ...attachment(), size: 900, chars: 40, truncated: true })).toBe(
      '900 B · 40 字（已截断）',
    );
  });
});

describe('ChatTab 附件', () => {
  it('选中文件后先占位，解析完成显示体积与字数', async () => {
    uploadAttachment.mockResolvedValue(attachment({ name: 'notes.md', size: 2048, chars: 1200 }));
    render(<ChatTab session="s-1" />);
    pickFiles([new File(['hello'], 'notes.md')]);

    expect(screen.getByText('notes.md')).toBeTruthy();
    expect(await screen.findByText('2.0 KB · 1200 字')).toBeTruthy();
    expect(uploadAttachment).toHaveBeenCalledTimes(1);
  });

  it('解析失败时把原因留在芯片上，且不计入可发送附件', async () => {
    uploadAttachment.mockRejectedValue(new Error('文件超过上传上限'));
    render(<ChatTab session="s-1" />);
    pickFiles([new File(['x'], 'big.zip')]);

    expect(await screen.findByText('无法解析')).toBeTruthy();
    expect(screen.getByTitle('文件超过上传上限')).toBeTruthy();

    fireEvent.change(screen.getByRole('textbox', { name: '用户输入' }), {
      target: { value: '看下这个' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(send).toHaveBeenCalledWith('s-1', '看下这个', []);
  });

  it('移除芯片同时删掉服务端那份附件', async () => {
    const saved = attachment({ id: 'a'.repeat(16), name: 'run.log', size: 900, chars: 40 });
    uploadAttachment.mockResolvedValue(saved);
    render(<ChatTab session="s-1" />);
    pickFiles([new File(['x'], 'run.log')]);

    await waitFor(() => expect(screen.getByText('900 B · 40 字')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: '移除 run.log' }));

    expect(screen.queryByText('run.log')).toBeNull();
    expect(deleteAttachment).toHaveBeenCalledWith('a'.repeat(16));
  });

  it('只挂附件不写字也能发送，文件标签落在用户气泡里', async () => {
    uploadAttachment.mockResolvedValue(attachment({ name: 'run.log', size: 900, chars: 40 }));
    render(<ChatTab session="s-1" />);
    pickFiles([new File(['x'], 'run.log')]);

    await waitFor(() => expect(screen.getByText('900 B · 40 字')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(send).toHaveBeenCalledTimes(1);
    const [, text, attached] = send.mock.calls[0];
    expect(text).toBe('');
    expect((attached as Attachment[]).map((item) => item.name)).toEqual(['run.log']);
    // 输入区的芯片随发送清空，气泡里的标签重新渲染
    expect(document.querySelector('.attach-row')).toBeNull();
    expect(document.querySelector('.chat-attachments .attach-tag')?.textContent).toBe('run.log');
  });

  it('上传中的附件不随消息发出', async () => {
    uploadAttachment.mockReturnValue(new Promise<Attachment>(() => {}));
    render(<ChatTab session="s-1" />);
    pickFiles([new File(['x'], 'slow.log')]);

    expect(screen.getByText('上传中…')).toBeTruthy();
    submit('先问一句');
    expect(send).toHaveBeenCalledWith('s-1', '先问一句', []);
  });
});

afterEach(cleanup);
