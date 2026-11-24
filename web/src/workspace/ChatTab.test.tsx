// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Attachment, AwaitingState, ChatMessage, PendingCall } from '../types';
import { attachMeta, ChatTab, formatClock, formatSize, shouldStickToBottom } from './ChatTab';

const send = vi.fn();
const stop = vi.fn();
const setPermission = vi.fn();
const confirmByChoice = vi.fn();
const refreshPending = vi.fn();
const onFirstSend = vi.fn();
const uploadAttachment = vi.fn();
const deleteAttachment = vi.fn();
const fetchPermissions = vi.fn();

vi.mock('../api/client', () => ({
  uploadAttachment: (file: File) => uploadAttachment(file),
  deleteAttachment: (id: string) => deleteAttachment(id),
  fetchPermissions: () => fetchPermissions(),
  // 选择器里的规则增删在 ChatTab 这层不参与断言，但必须存在（否则渲染即报错）
  createPermissionRule: () => Promise.resolve([]),
  deletePermissionRule: () => Promise.resolve([]),
}));

/** 服务端开放的档位：不含 container，和后端默认一致。 */
const AVAILABLE = [
  meta('readonly', '只读', 'readonly', 'never'),
  meta('workspace', '工作区可写', 'workspace', 'on-risk'),
  meta('cautious', '逐个确认', 'workspace', 'always'),
  meta('unattended', '无人值守', 'workspace', 'never'),
];

function meta(
  key: string,
  label: string,
  scope: 'readonly' | 'workspace' | 'container',
  approvals: 'always' | 'on-risk' | 'never',
) {
  return {
    key,
    label,
    note: `${label}的说明`,
    scope,
    approvals,
    combo: `${scope}+${approvals}`,
    tools: ['bash', 'read', 'glob', 'grep'],
    write: scope !== 'readonly',
    auto_allow: ['ls -la'],
    will_ask: ['git commit -m x'],
    will_deny: ['cat .env'],
  };
}

/** 一条待确认调用；input 是服务端给的展示文本。 */
const CALLS: PendingCall[] = [
  { tool_call_id: 't1', name: 'Bash', input: 'rm -rf build', risk: 'danger' },
];

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
  permission: 'workspace' as ChatMessage['permission'] & string,
  awaiting: null as AwaitingState | null,
};

vi.mock('./store', () => ({
  useSession: () => snapshot,
  send: (
    session: string,
    text: string,
    attachments: Attachment[] = [],
    permission?: string,
    webSearch = false,
  ) => {
    send(session, text, attachments, permission, webSearch);
    const now = new Date().toISOString();
    snapshot.messages = [
      ...snapshot.messages,
      {
        id: `u-${snapshot.messages.length}`,
        role: 'user',
        text,
        at: now,
        steps: [],
        attachments,
        permission: snapshot.permission,
      },
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
  setPermission: (session: string, key: string) => setPermission(session, key),
  confirm: (session: string, decisions: unknown[]) => confirmByChoice(session, decisions),
  refreshPending: (session: string) => {
    refreshPending(session);
    return Promise.resolve();
  },
}));

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

/** 统一套一层 QueryProvider：档位选择器依赖 react-query。 */
function tab(props: Partial<Parameters<typeof ChatTab>[0]> = {}): ReactElement {
  return (
    <QueryClientProvider client={queryClient}>
      <ChatTab session="s-1" {...props} />
    </QueryClientProvider>
  );
}

function assistant(): ChatMessage {
  return snapshot.messages[snapshot.messages.length - 1];
}

function reset() {
  send.mockClear();
  stop.mockClear();
  setPermission.mockClear();
  confirmByChoice.mockClear();
  refreshPending.mockClear();
  onFirstSend.mockClear();
  uploadAttachment.mockReset();
  deleteAttachment.mockReset();
  deleteAttachment.mockResolvedValue(undefined);
  fetchPermissions.mockReset();
  fetchPermissions.mockResolvedValue({
    default: 'workspace',
    available: AVAILABLE,
    scopes: ['readonly', 'workspace', 'container'],
    approvals: ['always', 'on-risk', 'never'],
    rules: [],
    rules_file: 'config/permissions.local.yaml',
    trust: { mode: 'permissive', workdirs: [] },
    fatigue: [],
  });
  queryClient.clear();
  snapshot.messages = [];
  snapshot.running = false;
  snapshot.loading = false;
  snapshot.eventCount = 0;
  snapshot.inputTokens = 0;
  snapshot.outputTokens = 0;
  snapshot.runId = null;
  snapshot.error = null;
  snapshot.modelName = '';
  snapshot.permission = 'workspace';
  snapshot.awaiting = null;
}

function submit(text: string) {
  const box = screen.getByRole('textbox', { name: '用户输入' });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: /发送/ }));
}

/** 造一个挂起态：卡片挂在助手消息上，发送按钮看快照里的 awaiting。 */
function park(calls: PendingCall[] = CALLS) {
  const awaiting: AwaitingState = { runId: 12, calls, expiresAt: null };
  const last = assistant();
  if (last) last.awaiting = awaiting;
  snapshot.awaiting = awaiting;
  snapshot.running = false;
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
    render(tab());
    expect(screen.getByText('开始一次会话')).toBeTruthy();
    expect(screen.getByRole('button', { name: /发送/ })).toBeTruthy();
  });

  it('点击示例问题直接发起会话', () => {
    render(tab());
    fireEvent.click(screen.getByRole('button', { name: /CI 流程是怎么跑的/ }));
    expect(send).toHaveBeenCalledWith('s-1', '这个仓库的 CI 流程是怎么跑的？', [], 'workspace', false);
    expect(screen.getByText('这个仓库的 CI 流程是怎么跑的？')).toBeTruthy();
  });

  it('提交后追加用户消息并调用 send', () => {
    render(tab());
    submit('你好');
    expect(screen.getByText('你好')).toBeTruthy();
    expect(send).toHaveBeenCalledWith('s-1', '你好', [], 'workspace', false);
  });

  it('草稿会话首次发送时通知外层落到 URL，不再固定 live-1', () => {
    render(tab({ session: 'draft-1', onFirstSend }));
    submit('hi');
    expect(onFirstSend).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith('draft-1', 'hi', [], 'workspace', false);
  });

  it('首 token 未到时不留空气泡', () => {
    const { rerender } = render(tab());
    submit('hi');
    rerender(tab());
    expect(document.querySelector('.chat-msg.assistant .chat-bubble')).toBeNull();
    expect(screen.getByText('正在处理…')).toBeTruthy();
  });

  it('收到文本即完整渲染，不做逐字揭示', () => {
    const { rerender } = render(tab());
    submit('hi');
    assistant().text = '持续交付是一种工程实践';
    rerender(tab());
    expect(screen.getByText('持续交付是一种工程实践')).toBeTruthy();
    expect(document.querySelector('.chat-cursor')).toBeNull();
  });

  it('流结束后保留全文', () => {
    const { rerender } = render(tab());
    submit('hi');
    assistant().text = '流水线失败常见于测试阶段';
    rerender(tab());
    snapshot.running = false;
    assistant().pending = false;
    rerender(tab());
    expect(screen.getByText('流水线失败常见于测试阶段')).toBeTruthy();
  });

  it('助手消息带复制与重试操作条，重试复用原问题', async () => {
    const { rerender } = render(tab());
    submit('hi');
    assistant().text = '这是答案';
    assistant().pending = false;
    assistant().endedAt = new Date(2026, 10, 23, 9, 5).toISOString();
    snapshot.running = false;
    rerender(tab());

    expect(await screen.findByText('这是答案')).toBeTruthy();
    expect(screen.getByRole('button', { name: '复制' })).toBeTruthy();
    expect(screen.getByText('09:05')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(send).toHaveBeenCalledTimes(2);
    expect(send).toHaveBeenLastCalledWith('s-1', 'hi', [], 'workspace', false);
  });

  it('回答按 Markdown 渲染，而不是整段纯文本', async () => {
    const { rerender } = render(tab());
    submit('hi');
    assistant().text = '## 结论\n\n- **先看日志**\n- 再跑 `ruff`';
    rerender(tab());

    expect(await screen.findByRole('heading', { level: 2, name: '结论' })).toBeTruthy();
    expect(document.querySelector('.md strong')?.textContent).toBe('先看日志');
    expect(document.querySelector('.md code')?.textContent).toBe('ruff');
  });

  it('过程步骤显示在回答上方，工具行可点开返回', async () => {
    const { rerender } = render(tab());
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
    rerender(tab());

    expect(await screen.findByText('开始回复')).toBeTruthy();
    const line = screen.getByRole('button', { name: /调用工具 · bash/ });
    expect(line.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(line);
    expect(document.querySelector('.step-result')?.textContent).toBe('ls -la');
    expect(screen.getByText('看完了')).toBeTruthy();
  });

  it('运行失败时给出中断提示与重试入口', async () => {
    const { rerender } = render(tab());
    submit('hi');
    assistant().error = '流式请求失败 500';
    snapshot.running = false;
    rerender(tab());

    expect(await screen.findByText('中断')).toBeTruthy();
    expect(screen.getByText('流式请求失败 500')).toBeTruthy();
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy();
  });

  it('中文输入法组词中的 Enter 不发送，组词结束后才发送', () => {
    render(tab());
    const box = screen.getByRole('textbox', { name: '用户输入' });
    fireEvent.change(box, { target: { value: '你好' } });

    fireEvent.keyDown(box, { key: 'Enter', isComposing: true });
    expect(send).not.toHaveBeenCalled();

    fireEvent.keyDown(box, { key: 'Enter', isComposing: false });
    expect(send).toHaveBeenCalledWith('s-1', '你好', [], 'workspace', false);
  });

  it('生成中仍可输入，但发送按钮禁用、显示停止按钮', () => {
    const { rerender } = render(tab());
    submit('hi');
    rerender(tab());

    const box = screen.getByRole('textbox', { name: '用户输入' }) as HTMLTextAreaElement;
    expect(box.disabled).toBe(false);
    fireEvent.change(box, { target: { value: '下一句' } });
    expect(box.value).toBe('下一句');
    expect((screen.getByRole('button', { name: /发送/ }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: '停止' }));
    expect(stop).toHaveBeenCalledWith('s-1');
  });

  it('刷新回来后自己去把挂起的确认取回来', () => {
    render(tab());
    expect(refreshPending).toHaveBeenCalledWith('s-1');
  });
});

describe('ChatTab 权限', () => {
  it('挂起等确认时即使有内容也不让发，并给出提示', () => {
    const { rerender } = render(tab());
    submit('hi');
    park();
    rerender(tab());

    const box = screen.getByRole('textbox', { name: '用户输入' });
    fireEvent.change(box, { target: { value: '下一句' } });
    rerender(tab());

    expect((screen.getByRole('button', { name: '发送' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('等待你确认上面的操作')).toBeTruthy();
    expect(screen.getByRole('group', { name: '等待确认的操作' })).toBeTruthy();
  });

  it('点「这一次」把这一批调用一起答复，且不落盘', () => {
    const { rerender } = render(tab());
    submit('hi');
    park();
    rerender(tab());

    fireEvent.click(screen.getByRole('button', { name: '这一次' }));
    expect(confirmByChoice).toHaveBeenCalledWith('s-1', [
      { tool_call_id: 't1', confirmed: true, scope: 'once' },
    ]);
  });

  it('「本会话」只写会话规则，「以后都这样」额外带 persist 落盘', () => {
    const { rerender } = render(tab());
    submit('hi');
    park([{ tool_call_id: 't1', name: 'Bash', input: 'pytest -q', risk: 'exec' }]);
    rerender(tab());

    fireEvent.click(screen.getByRole('button', { name: '本会话' }));
    expect(confirmByChoice).toHaveBeenLastCalledWith('s-1', [
      { tool_call_id: 't1', confirmed: true, scope: 'prefix' },
    ]);

    rerender(tab());
    fireEvent.click(screen.getByRole('button', { name: '以后都这样' }));
    expect(confirmByChoice).toHaveBeenLastCalledWith('s-1', [
      { tool_call_id: 't1', confirmed: true, scope: 'prefix', persist: true },
    ]);
  });

  it('拒绝时把 confirmed 置否，scope 回落 once', () => {
    const { rerender } = render(tab());
    submit('hi');
    park([{ tool_call_id: 't2', name: 'Write', input: '/etc/hosts', risk: 'write' }]);
    rerender(tab());

    fireEvent.click(screen.getByRole('button', { name: '拒绝' }));
    expect(confirmByChoice).toHaveBeenCalledWith('s-1', [
      { tool_call_id: 't2', confirmed: false, scope: 'once' },
    ]);
  });

  it('选档位交给 store，而不是直接改本地快照', async () => {
    render(tab());
    fireEvent.click(screen.getByRole('button', { name: '选择权限档位' }));
    fireEvent.click(await screen.findByRole('menuitemradio', { name: '逐个确认' }));
    expect(setPermission).toHaveBeenCalledWith('s-1', 'cautious');
  });

  it('用户气泡标出这一轮用的档位，默认档不标', () => {
    const { rerender } = render(tab());
    submit('hi');
    rerender(tab());
    expect(document.querySelector('.perm-tag')).toBeNull();

    snapshot.messages[0].permission = 'readonly';
    rerender(tab());
    expect(document.querySelector('.perm-tag')?.textContent).toBe('只读');
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
    render(tab());
    pickFiles([new File(['hello'], 'notes.md')]);

    expect(screen.getByText('notes.md')).toBeTruthy();
    expect(await screen.findByText('2.0 KB · 1200 字')).toBeTruthy();
    expect(uploadAttachment).toHaveBeenCalledTimes(1);
  });

  it('解析失败时把原因留在芯片上，且不计入可发送附件', async () => {
    uploadAttachment.mockRejectedValue(new Error('文件超过上传上限'));
    render(tab());
    pickFiles([new File(['x'], 'big.zip')]);

    expect(await screen.findByText('无法解析')).toBeTruthy();
    expect(screen.getByTitle('文件超过上传上限')).toBeTruthy();

    fireEvent.change(screen.getByRole('textbox', { name: '用户输入' }), {
      target: { value: '看下这个' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(send).toHaveBeenCalledWith('s-1', '看下这个', [], 'workspace', false);
  });

  it('移除芯片同时删掉服务端那份附件', async () => {
    const saved = attachment({ id: 'a'.repeat(16), name: 'run.log', size: 900, chars: 40 });
    uploadAttachment.mockResolvedValue(saved);
    render(tab());
    pickFiles([new File(['x'], 'run.log')]);

    await waitFor(() => expect(screen.getByText('900 B · 40 字')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: '移除 run.log' }));

    expect(screen.queryByText('run.log')).toBeNull();
    expect(deleteAttachment).toHaveBeenCalledWith('a'.repeat(16));
  });

  it('只挂附件不写字也能发送，文件标签落在用户气泡里', async () => {
    uploadAttachment.mockResolvedValue(attachment({ name: 'run.log', size: 900, chars: 40 }));
    render(tab());
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
    render(tab());
    pickFiles([new File(['x'], 'slow.log')]);

    expect(screen.getByText('上传中…')).toBeTruthy();
    submit('先问一句');
    expect(send).toHaveBeenCalledWith('s-1', '先问一句', [], 'workspace', false);
  });
});

describe('输入区链接芯片', () => {
  it('输入里的链接实时渲染成可点芯片，GitHub 仓库带图标', () => {
    render(tab());
    fireEvent.change(screen.getByRole('textbox', { name: '用户输入' }), {
      target: {
        value: '看看 https://github.com/RONGX563647/devops-agent 和 https://example.com/docs?x=1。',
      },
    });

    const github = screen.getByRole('link', { name: 'RONGX563647/devops-agent' });
    expect(github.getAttribute('href')).toBe('https://github.com/RONGX563647/devops-agent');
    expect(github.getAttribute('target')).toBe('_blank');
    expect(github.getAttribute('rel')).toBe('noreferrer noopener');
    expect(github.className).toContain('is-github');

    const web = screen.getByRole('link', { name: 'example.com/docs' });
    expect(web.getAttribute('href')).toBe('https://example.com/docs?x=1');
    expect(web.className).not.toContain('is-github');
  });

  it('清空输入后芯片随之消失', () => {
    render(tab());
    const box = screen.getByRole('textbox', { name: '用户输入' });
    fireEvent.change(box, { target: { value: 'https://example.com' } });
    expect(screen.getByRole('link', { name: 'example.com' })).toBeTruthy();

    fireEvent.change(box, { target: { value: '没有链接了' } });
    expect(document.querySelector('.link-row')).toBeNull();
  });
});

describe('联网搜索开关', () => {
  it('默认关闭，点开后随本轮请求带上 web_search', () => {
    render(tab());
    const toggle = screen.getByRole('button', { name: '联网搜索' });
    expect(toggle.getAttribute('aria-pressed')).toBe('false');

    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-pressed')).toBe('true');

    fireEvent.change(screen.getByRole('textbox', { name: '用户输入' }), {
      target: { value: '查下最新版本' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(send).toHaveBeenCalledWith('s-1', '查下最新版本', [], 'workspace', true);
  });

  it('默认发送时不带搜索开关', () => {
    render(tab());
    fireEvent.change(screen.getByRole('textbox', { name: '用户输入' }), {
      target: { value: '解释下这段代码' },
    });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));

    expect(send).toHaveBeenCalledWith('s-1', '解释下这段代码', [], 'workspace', false);
  });
});

afterEach(cleanup);
