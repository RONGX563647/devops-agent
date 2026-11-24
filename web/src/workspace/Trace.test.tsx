// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  fetchContext,
  fetchInputStages,
  fetchRun,
  fetchRunHealth,
  fetchRuns,
  fetchSpans,
  fetchToolSchemas,
} from '../api/client';
import type { Attachment, ChatMessage, InputStage, ParseStage, Run, RunHealth, Span } from '../types';
import { clearAllHistory, saveMessages } from './history';
import { RunTrace } from './RunTrace';
import { resetStore } from './store';
import { TraceTab } from './TraceTab';

vi.mock('../api/client', () => ({
  fetchRun: vi.fn(),
  fetchSpans: vi.fn(),
  fetchRunHealth: vi.fn(),
  fetchRuns: vi.fn(),
  fetchInputStages: vi.fn(),
  fetchContext: vi.fn(),
  fetchToolSchemas: vi.fn(),
}));

function makeRun(id: number, sessionId: string): Run {
  return {
    id,
    session_id: sessionId,
    domain: 'delivery',
    status: 'success',
    input: 'q',
    output: 'a',
    started_at: '2026-11-23T10:00:00',
    ended_at: null,
    duration_ms: 100,
    model: 'qwen',
    total_tokens: 10,
    health_score: 80,
    input_tokens: 5,
    output_tokens: 5,
  };
}

const span: Span = {
  id: 1,
  run_id: 7,
  type: 'model',
  name: 'chat',
  start_offset_ms: 0,
  duration_ms: 50,
  input: '',
  output: '',
  tokens: 5,
  status: 'ok',
  error: null,
};

const stage = (over: Partial<ParseStage> & { stage: string; label: string }): ParseStage => ({
  status: 'ok',
  summary: '',
  before: 0,
  after: 0,
  items: 0,
  ms: 1,
  detail: '',
  changed: false,
  failed: 0,
  ...over,
});

const persisted: InputStage[] = [
  { seq: 0, stage: 'normalize', label: '归一化', status: 'ok', changed: true, summary: 'CRLF → LF', before: 90, after: 80, items: 0, duration_ms: 1, detail: '', failed: 0 },
  { seq: 1, stage: 'budget', label: '预算裁剪', status: 'skip', changed: false, summary: '未超预算', before: 80, after: 80, items: 0, duration_ms: 0, detail: '', failed: 0 },
];

const health: RunHealth = {
  run_id: 7,
  score: 80,
  dimensions: { execution: 90, efficiency: 70, quality: 80, safety: 85 },
};

/** 一份已解析完成的附件：轨迹只关心名字与体积，正文留在服务端。 */
const uploaded: Attachment = {
  id: 'a1b2c3d4e5f60718',
  name: 'run.log',
  size: 900,
  suffix: '.log',
  chars: 40,
  truncated: false,
  status: 'ok',
};

function wrap(node: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{node}</QueryClientProvider>);
}

beforeEach(() => {
  resetStore();
  clearAllHistory();
  vi.mocked(fetchRun).mockResolvedValue(makeRun(7, 's-one'));
  vi.mocked(fetchSpans).mockResolvedValue([span]);
  vi.mocked(fetchRunHealth).mockResolvedValue(health);
  vi.mocked(fetchInputStages).mockResolvedValue(persisted);
  vi.mocked(fetchToolSchemas).mockResolvedValue([]);
  vi.mocked(fetchContext).mockResolvedValue({
    profile: 'workspace',
    profile_label: '工作区可写',
    system_prompt: '你是 DevOps 助手。',
    system_prompt_chars: 12,
    system_prompt_tokens: 7,
    tools: [
      {
        name: 'Bash',
        description: '执行命令',
        schema_chars: 120,
        tokens: 30,
        parameters: { type: 'object', properties: { cmd: { type: 'string' } } },
      },
    ],
    tools_chars: 120,
    tools_tokens: 30,
    prefix_tokens: 37,
    prefix_fingerprint: 'a1b2c3d4',
    policy_note: '本会话可在工作区内建/删/改文件。',
    policy_note_tokens: 12,
    workdir: '/workspace',
  });
  vi.mocked(fetchRuns).mockResolvedValue({
    items: [makeRun(7, 's-one'), makeRun(8, 's-two')],
    total: 2,
    limit: 100,
    offset: 0,
  });
});
afterEach(cleanup);

describe('RunTrace', () => {
  it('渲染运行概览、健康分拆解与瀑布图', async () => {
    wrap(<RunTrace runId={7} />);
    expect(await screen.findByText('Trace · run #7')).toBeTruthy();
    expect(screen.getByText('总分 80')).toBeTruthy();
    expect(screen.getByText(/模型调用 · chat/)).toBeTruthy();
    expect(screen.getByText('50 ms')).toBeTruthy();
  });

  it('点瀑布图一行，右侧看落库的入参与返回', async () => {
    vi.mocked(fetchSpans).mockResolvedValue([
      { ...span, input: '{"prompt":"定位"}', output: '镜像拉取超时' },
    ]);
    wrap(<RunTrace runId={7} />);
    const row = await screen.findByRole('button', { name: /模型调用 · chat/ });
    fireEvent.click(row);
    expect(await screen.findByText(/prompt: 定位/)).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: '结果' }));
    expect(screen.getByText(/镜像拉取超时/)).toBeTruthy();
  });

  it('运行记录页展示落库的完整解析链（含无改动阶段）', async () => {
    wrap(<RunTrace runId={7} />);
    expect(await screen.findByText('输入解析链')).toBeTruthy();
    expect(await screen.findByText('归一化')).toBeTruthy();
    expect(screen.getByText('预算裁剪')).toBeTruthy();
    expect(screen.getByText('无改动')).toBeTruthy();
  });

  it('run 不存在时显示错误', async () => {
    vi.mocked(fetchRun).mockRejectedValue(new Error('404'));
    wrap(<RunTrace runId={99} />);
    expect(await screen.findByText(/加载失败/)).toBeTruthy();
  });
});

describe('TraceTab', () => {
  it('会话轨迹由消息派生：轮列表 + 工具入参/返回详情', async () => {
    const messages: ChatMessage[] = [
      {
        id: 'u1',
        role: 'user',
        text: '为什么部署失败？',
        at: '2026-11-23T10:00:00.000Z',
        steps: [],
      },
      {
        id: 'a1',
        role: 'assistant',
        text: '看日志发现镜像拉取超时。',
        at: '2026-11-23T10:00:01.000Z',
        steps: [
          {
            id: 'tool:t1',
            kind: 'tool',
            label: '调用工具 · bash',
            detail: '',
            state: 'success',
            toolCallId: 't1',
            at: '2026-11-23T10:00:01.000Z',
            args: 'kubectl describe pod',
            result: 'ImagePullBackOff',
          },
        ],
        endedAt: '2026-11-23T10:00:04.000Z',
        runId: 7,
      },
    ];
    saveMessages('s-two', messages);
    wrap(<TraceTab session="s-two" />);

    expect(await screen.findByRole('heading', { name: '会话轨迹' })).toBeTruthy();
    // 轮次视图是默认入口；此刻再点「轮次」胶囊会全部折叠，再点一次才回到全展开
    fireEvent.click(screen.getByRole('tab', { name: '轮次' }));
    fireEvent.click(screen.getByRole('tab', { name: '轮次' }));
    // 轮次分隔 + 步骤行：入参摘要直接出现在行里，不用点开
    expect(screen.getAllByText(/为什么部署失败/).length).toBeGreaterThan(0);
    const rows = await screen.findAllByRole('button', { name: /bash/ });
    const tool = rows.find((row) => row.classList.contains('trace-step'))!;
    expect(tool.textContent).toContain('kubectl describe pod');
    fireEvent.click(tool);
    fireEvent.click(await screen.findByRole('tab', { name: '参数' }));
    expect((await screen.findAllByText(/kubectl describe pod/)).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('tab', { name: '结果' }));
    expect((await screen.findAllByText(/ImagePullBackOff/)).length).toBeGreaterThan(0);
  });

  it('解析步骤只展示生效的阶段，并说明其余无改动', async () => {
    const messages: ChatMessage[] = [
      { id: 'u1', role: 'user', text: '看下 ./README.md', at: '2026-11-23T10:00:00.000Z', steps: [] },
      {
        id: 'a1',
        role: 'assistant',
        text: '读完了。',
        at: '2026-11-23T10:00:01.000Z',
        steps: [
          {
            id: 'parse:input-parse',
            kind: 'parse',
            label: '输入解析',
            detail: '2/6 阶段生效 · 1 引用',
            state: 'success',
            at: '2026-11-23T10:00:01.000Z',
            parseStages: [
              stage({ stage: 'normalize', label: '归一化', changed: true, summary: 'CRLF → LF', before: 30, after: 28 }),
              stage({ stage: 'detect', label: '引用探测', changed: true, items: 1, summary: '本地路径 ×1' }),
              stage({ stage: 'expand', label: '引用展开', changed: true, summary: '1 条引用已展开', after: 120 }),
              stage({ stage: 'redact', label: '密钥脱敏', status: 'skip', summary: '未发现凭据' }),
              stage({ stage: 'budget', label: '预算裁剪', status: 'skip', summary: '未超预算' }),
              stage({ stage: 'compose', label: '组装', changed: true, summary: '追加 1 份引用材料' }),
            ],
          },
        ],
        endedAt: '2026-11-23T10:00:03.000Z',
      },
    ];
    saveMessages('s-parse', messages);
    wrap(<TraceTab session="s-parse" />);

    // 徽标已写作「输入」，标题不再重复同一个词，用行摘要定位
    const row = await screen.findByRole('button', { name: /阶段生效/ });
    fireEvent.click(row);
    // 解析步骤的结果页签就叫「阶段」
    fireEvent.click(await screen.findByRole('tab', { name: '阶段' }));
    expect(await screen.findByText('引用探测')).toBeTruthy();
    expect(screen.getByText('本地路径 ×1')).toBeTruthy();
    // 未生效的阶段不该占位置
    expect(screen.queryByText('预算裁剪')).toBeNull();
    expect(screen.getByText(/另有 2 项无改动/)).toBeTruthy();
    // 轮列表与父行摘要都带引用计数
    expect(screen.getAllByText(/1 引用/).length).toBeGreaterThan(0);
  });

  it('按会话过滤运行记录列表', async () => {
    const { container } = wrap(<TraceTab session="s-two" />);
    fireEvent.click(await screen.findByRole('button', { name: /运行记录/ }));
    await waitFor(() => {
      expect(container.querySelectorAll('tbody tr')).toHaveLength(1);
    });
    // 只有 s-two 的 run #8；s-one 的 run #7 不应出现
    expect(container.querySelector('tbody')?.textContent).toContain('#8');
    expect(container.querySelector('tbody')?.textContent).not.toContain('#7');
  });

  it('点击运行进入轨迹详情并可返回', async () => {
    const { container } = wrap(<TraceTab session={null} />);
    await waitFor(() => {
      expect(container.querySelectorAll('tbody tr').length).toBe(2);
    });
    fireEvent.click(container.querySelector('tbody tr')!);
    expect(await screen.findByText('Trace · run #7')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '返回列表' }));
    expect(screen.getByText('全部会话 · 点击运行查看轨迹与健康分')).toBeTruthy();
  });

  it('轮列表标注用户挂上的附件数', async () => {
    saveMessages('s-attach', [
      {
        id: 'u1',
        role: 'user',
        text: '看下 run.log',
        at: '2026-11-23T10:00:00.000Z',
        steps: [],
        attachments: [uploaded],
      },
      {
        id: 'a1',
        role: 'assistant',
        text: '看完了。',
        at: '2026-11-23T10:00:02.000Z',
        steps: [],
        endedAt: '2026-11-23T10:00:02.000Z',
      },
    ]);
    wrap(<TraceTab session="s-attach" />);

    // 附件数标在轮次头部，只有轮次视图才有
    fireEvent.click(await screen.findByRole('tab', { name: '轮次' }));
    expect(await screen.findByText(/1 附件/)).toBeTruthy();
  });

  /** 两轮会话：第一轮一次成功工具，第二轮一次失败工具。 */
  const twoTurns: ChatMessage[] = [
    { id: 'u1', role: 'user', text: '第一问', at: '2026-11-23T10:00:00.000Z', steps: [] },
    {
      id: 'a1',
      role: 'assistant',
      text: '看完了。',
      at: '2026-11-23T10:00:01.000Z',
      endedAt: '2026-11-23T10:00:03.000Z',
      steps: [
        {
          id: 'tool:t1',
          kind: 'tool',
          label: '调用工具 · Bash',
          detail: '',
          state: 'success',
          toolName: 'Bash',
          at: '2026-11-23T10:00:01.000Z',
          endedAt: '2026-11-23T10:00:02.000Z',
          args: '{"command":"ls -la"}',
          result: 'a.txt',
        },
      ],
    },
    { id: 'u2', role: 'user', text: '第二问', at: '2026-11-23T10:05:00.000Z', steps: [] },
    {
      id: 'a2',
      role: 'assistant',
      text: '测试挂了。',
      at: '2026-11-23T10:05:01.000Z',
      endedAt: '2026-11-23T10:05:04.000Z',
      steps: [
        {
          id: 'tool:t2',
          kind: 'tool',
          label: '调用工具 · Bash',
          detail: '',
          state: 'error',
          toolName: 'Bash',
          at: '2026-11-23T10:05:01.000Z',
          endedAt: '2026-11-23T10:05:03.000Z',
          args: '{"command":"npm test"}',
          result: 'FAIL 2 tests',
        },
      ],
    },
  ];

  it('默认只展开最后一轮：长会话进来不会被全展开劝退', async () => {
    saveMessages('s-default-collapse', twoTurns);
    const { container } = wrap(<TraceTab session="s-default-collapse" />);
    await screen.findByText(/第一问/);

    // 只看列表区：右侧检查面板会展示当前选中步骤的入参，不参与这条断言
    const list = container.querySelector('.trace-scroll')?.textContent ?? '';
    expect(list).toContain('command: npm test');
    expect(list).not.toContain('command: ls -la');
  });

  it('全部折叠 / 全部展开，以及胶囊 toggle 语义', async () => {
    saveMessages('s-collapse-actions', twoTurns);
    const { container } = wrap(<TraceTab session="s-collapse-actions" />);
    const list = () => container.querySelector('.trace-scroll')?.textContent ?? '';

    fireEvent.click(await screen.findByRole('button', { name: '全部展开' }));
    expect(list()).toContain('command: ls -la');

    fireEvent.click(screen.getByRole('button', { name: '全部折叠' }));
    expect(list()).not.toContain('command: ls -la');
    expect(list()).not.toContain('command: npm test');

    // 胶囊 toggle：全展开状态下再点「轮次」= 全部折叠，再点 = 全部展开
    fireEvent.click(screen.getByRole('button', { name: '全部展开' }));
    expect(list()).toContain('command: ls -la');
    const pill = screen.getByRole('tab', { name: '轮次' });
    fireEvent.click(pill);
    expect(list()).not.toContain('command: ls -la');
    fireEvent.click(pill);
    expect(list()).toContain('command: ls -la');
  });

  it('仅看异常只留出问题的轮次', async () => {
    saveMessages('s-only-errors', twoTurns);
    const { container } = wrap(<TraceTab session="s-only-errors" />);

    fireEvent.click(await screen.findByRole('button', { name: '仅看异常' }));
    const list = container.querySelector('.trace-scroll')?.textContent ?? '';
    expect(list).not.toContain('第一问');
    expect(list).toContain('第二问');
    expect(list).toContain('command: npm test');
  });

  it('点第几轮，右侧就换成第几轮的概要与上下文', async () => {
    saveMessages('s-pick-turn', twoTurns);
    const { container } = wrap(<TraceTab session="s-pick-turn" />);
    await screen.findByText(/第一问/);

    const inspector = () => container.querySelector('.inspector')?.textContent ?? '';
    // 默认落在「最慢的一步」（第二轮的工具），起点先确认
    expect(inspector()).toContain('第 2 轮');

    // 点标题 = 选中这一轮：右侧跟着换，而不是被默认步骤牵住
    fireEvent.click(screen.getByRole('button', { name: /第 1 轮/ }));
    expect(inspector()).toContain('第 1 轮 · 2 步');
    expect(inspector()).toContain('第一问');

    // 步骤模式也有「上下文」页签；第 1 轮没有历史，要说明而不是留白
    fireEvent.click(screen.getByRole('tab', { name: '上下文' }));
    expect(inspector()).toContain('历史上下文');
    expect(inspector()).toContain('这是第一轮');
  });

  it('折叠态写进 sessionStorage：刷新后不用重新点', async () => {
    saveMessages('s-persist', twoTurns);
    wrap(<TraceTab session="s-persist" />);
    fireEvent.click(await screen.findByRole('button', { name: '全部折叠' }));

    const raw = sessionStorage.getItem('trace:collapsed:s-persist');
    expect(raw).toBeTruthy();
    expect(Object.values(JSON.parse(raw!) as Record<string, boolean>)).toEqual([true, true]);

    // 重新挂载（等价于刷新）：折叠态从存储里恢复
    cleanup();
    wrap(<TraceTab session="s-persist" />);
    expect(await screen.findByText(/第一问/)).toBeTruthy();
    expect(screen.queryByText(/command: ls -la/)).toBeNull();
  });
});
