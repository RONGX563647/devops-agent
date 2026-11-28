// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Run } from '../types';
import { groupOf, toSessionTitle } from './SessionSidebar';
import { aggregateSessions } from './sidebar-sessions';
import { Workspace, parseTab } from './Workspace';

function makeRun(
  id: number,
  sessionId: string,
  status: Run['status'] = 'success',
  startedAt = `2026-11-23T10:00:${String(id).padStart(2, '0')}`,
  input = 'q',
): Run {
  return {
    id,
    session_id: sessionId,
    domain: 'delivery',
    status,
    input,
    output: 'a',
    started_at: startedAt,
    ended_at: null,
    duration_ms: 100,
    model: 'qwen',
    total_tokens: 10,
    health_score: 80,
    input_tokens: 5,
    output_tokens: 5,
  };
}

const CONTROL = {
  enabled: true,
  allow_local_import: true,
  allow_git_import: false,
  allowed_local_roots: ['/code'],
  git_available: false,
  max_workspaces: 20,
  default_profile: 'workspace',
};

function sessionItem(
  id: string,
  title: string,
  lastAt = '2026-11-28T10:00:01',
  extra: Record<string, unknown> = {},
) {
  return {
    session_id: id,
    title,
    last_at: lastAt,
    run_count: 1,
    failed_count: 0,
    bound_at: '2026-11-26T09:00:00',
    bound_by: 'local',
    archived_at: '',
    ...extra,
  };
}

function defaultBoard() {
  return {
    items: [
      {
        id: 'ws_a',
        name: 'webpanel',
        root_path: '/code/webpanel',
        status: 'active',
        session_count: 2,
        sessions: [
          sessionItem('s-one', '会话一的第一个问题'),
          sessionItem('s-two', '会话二的问题', '2026-11-22T09:00:00'),
        ],
      },
    ],
    unbound: [],
  };
}

interface ApiPayload {
  runs?: Run[];
  board?: unknown;
  control?: unknown;
  session?: unknown;
}

function mockApi(payload: ApiPayload = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    let body: unknown = { items: payload.runs ?? [], total: 0, limit: 200, offset: 0 };
    if (url.includes('/api/workspaces/control')) body = payload.control ?? CONTROL;
    else if (/\/api\/workspaces\/sessions\/[^?]+/.test(url)) {
      body = payload.session ?? sessionItem('s-one', '会话一的第一个问题');
    } else if (url.includes('/api/workspaces/sessions')) body = payload.board ?? defaultBoard();
    // init 只用于让 mock.calls 推成 [input, init?]：断言里要读 method 与 body
    void init;
    return { ok: true, body: null, json: async () => body };
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** 侧栏作用域：轨迹刻度条的按钮也用会话标题做 aria-label，全局查询会撞车。 */
function sidebar() {
  return within(screen.getByRole('navigation', { name: '工作区与会话' }));
}

function renderWorkspace(entry = '/?tab=chat') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <Workspace />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  mockApi({
    runs: [
      makeRun(1, 's-one', 'success', '2026-11-23T10:00:01', '会话一的第一个问题'),
      makeRun(2, 's-one', 'failed', '2026-11-23T10:00:02', '后续的短问题'),
      makeRun(3, 's-two', 'success', '2026-11-22T09:00:00', '会话二的问题'),
    ],
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('parseTab', () => {
  it('合法 key 原样返回，非法与空值回退 chat', () => {
    expect(parseTab('metrics')).toBe('metrics');
    expect(parseTab('trace')).toBe('trace');
    expect(parseTab('bogus')).toBe('chat');
    expect(parseTab(null)).toBe('chat');
  });
});

describe('toSessionTitle', () => {
  it('压成单行并按长度截断', () => {
    expect(toSessionTitle('  多行\n输入  ')).toBe('多行 输入');
    expect(toSessionTitle('一二三四五六七八九十一二三四五六七八九十超出部分')).toBe(
      '一二三四五六七八九十一二三四五六七八九十…',
    );
    expect(toSessionTitle('   ')).toBe('未命名会话');
  });
});

describe('groupOf', () => {
  const now = new Date(2026, 10, 23, 12, 0, 0);

  it('按本地日界分今天/昨天/更早', () => {
    expect(groupOf(new Date(2026, 10, 23, 8, 0, 0).toISOString(), now)).toBe('今天');
    expect(groupOf(new Date(2026, 10, 22, 23, 0, 0).toISOString(), now)).toBe('昨天');
    expect(groupOf(new Date(2026, 10, 20, 10, 0, 0).toISOString(), now)).toBe('更早');
    expect(groupOf('bad', now)).toBe('更早');
  });
});

describe('aggregateSessions', () => {
  it('按 session_id 聚合、统计失败数并按最新时间排序', () => {
    const sessions = aggregateSessions([
      makeRun(1, 's-one'),
      makeRun(2, 's-one', 'failed'),
      makeRun(3, 's-two', 'success', '2026-11-22T09:00:00'),
    ]);
    expect(sessions).toHaveLength(2);
    expect(sessions[0].id).toBe('s-one');
    expect(sessions[0].runCount).toBe(2);
    expect(sessions[0].failedCount).toBe(1);
    expect(sessions[1].id).toBe('s-two');
  });

  it('标题取最早一条用户输入，不被后续短问题覆盖', () => {
    const sessions = aggregateSessions([
      makeRun(1, 's-one', 'success', '2026-11-23T10:00:01', '会话主题：CI 流水线为什么失败'),
      makeRun(2, 's-one', 'success', '2026-11-23T10:05:00', '继续'),
    ]);
    expect(sessions[0].title).toBe('会话主题：CI 流水线为什么失败');
  });
});

describe('左侧工作区面板', () => {
  it('工作区底下挂会话，列表里没有「新会话」入口', async () => {
    renderWorkspace('/?session=s-one');

    expect(await screen.findByRole('button', { name: 'webpanel' })).toBeTruthy();
    expect(sidebar().getByRole('button', { name: /^会话一的第一个问题/ })).toBeTruthy();
    expect(screen.queryByText('全部会话')).toBeNull();
  });

  it('点工作区里的 + 才开新会话：先出现草稿行，对话区可以输入', async () => {
    renderWorkspace();

    fireEvent.click(await screen.findByRole('button', { name: /在「webpanel」中新建会话/ }));

    // 草稿行只存在于界面：标题是「新会话」，右侧标「未发送」
    expect(sidebar().getByRole('button', { name: /^新会话/ })).toBeTruthy();
    expect(sidebar().getByText('未发送')).toBeTruthy();
    expect(screen.getByRole('heading', { name: '开始一次会话' })).toBeTruthy();
    expect(
      (screen.getByRole('textbox', { name: '用户输入' }) as HTMLTextAreaElement).disabled,
    ).toBe(false);
  });

  it('没有选中工作区时对话区要求先选工作区，输入框禁用', async () => {
    renderWorkspace();
    expect(await screen.findByRole('heading', { name: '先选择一个工作区' })).toBeTruthy();
    expect(
      (screen.getByRole('textbox', { name: '用户输入' }) as HTMLTextAreaElement).disabled,
    ).toBe(true);
  });

  it('工作区行可折叠', async () => {
    renderWorkspace('/?session=s-one');
    const head = await screen.findByRole('button', { name: 'webpanel' });
    expect(head.getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(head);
    expect(sidebar().queryByRole('button', { name: /^会话一的第一个问题/ })).toBeNull();
    expect(head.getAttribute('aria-expanded')).toBe('false');
  });

  it('工作区菜单可以改名，提交后发 PATCH', async () => {
    const fetchMock = mockApi({ runs: [] });
    renderWorkspace('/?session=s-one');

    fireEvent.click(await screen.findByRole('button', { name: '「webpanel」的更多操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '重命名' }));

    const input = screen.getByRole('textbox', { name: '重命名工作区「webpanel」' });
    fireEvent.change(input, { target: { value: '新名字' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await vi.waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) =>
        String(url).includes('/api/workspaces/ws_a'),
      );
      expect(call).toBeTruthy();
      expect((call![1] as RequestInit).method).toBe('PATCH');
      expect(JSON.parse(String((call![1] as RequestInit).body))).toEqual({ name: '新名字' });
    });
  });

  it('工作区菜单可以归档', async () => {
    const fetchMock = mockApi({ runs: [] });
    renderWorkspace('/?session=s-one');

    fireEvent.click(await screen.findByRole('button', { name: '「webpanel」的更多操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '归档工作区' }));

    await vi.waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) =>
        String(url).includes('/api/workspaces/ws_a/archive'),
      );
      expect(call).toBeTruthy();
    });
  });

  it('会话菜单可以改名与归档', async () => {
    const fetchMock = mockApi({ runs: [] });
    renderWorkspace('/?session=s-one');

    fireEvent.click(
      await screen.findByRole('button', { name: '「会话一的第一个问题」的更多操作' }),
    );
    fireEvent.click(screen.getByRole('menuitem', { name: '重命名' }));
    const input = screen.getByRole('textbox', { name: '重命名会话「会话一的第一个问题」' });
    fireEvent.change(input, { target: { value: 'CI 排查' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    await vi.waitFor(() => {
      const call = fetchMock.mock.calls.find(
        ([url, init]) =>
          String(url).includes('/api/workspaces/sessions/s-one') &&
          (init as RequestInit | undefined)?.method === 'PATCH',
      );
      expect(JSON.parse(String((call![1] as RequestInit).body))).toEqual({ title: 'CI 排查' });
    });

    fireEvent.click(screen.getByRole('button', { name: '「会话一的第一个问题」的更多操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '归档会话' }));
    await vi.waitFor(() => {
      const calls = fetchMock.mock.calls.filter(
        ([url, init]) =>
          String(url).includes('/api/workspaces/sessions/s-one') &&
          (init as RequestInit | undefined)?.method === 'PATCH',
      );
      expect(JSON.parse(String((calls.at(-1)![1] as RequestInit).body))).toEqual({
        archived: true,
      });
    });
  });

  it('搜索图标展开输入框并按标题过滤会话', async () => {
    renderWorkspace('/?session=s-one');
    await screen.findByRole('button', { name: 'webpanel' });

    fireEvent.click(screen.getByRole('button', { name: '搜索会话' }));
    fireEvent.change(screen.getByRole('searchbox', { name: '搜索会话' }), {
      target: { value: '会话二' },
    });
    expect(sidebar().queryByRole('button', { name: /^会话一的第一个问题/ })).toBeNull();
    expect(sidebar().getByRole('button', { name: /^会话二的问题/ })).toBeTruthy();
  });

  it('未归属的老会话默认收起，点开关才展开', async () => {
    const board = defaultBoard();
    mockApi({ runs: [], board: { ...board, unbound: [sessionItem('loose', '历史会话')] } });
    renderWorkspace('/?session=s-one');

    const toggle = await screen.findByRole('button', { name: /未归属会话/ });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(sidebar().queryByText('历史会话')).toBeNull();

    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(sidebar().getByText('历史会话')).toBeTruthy();
  });

  it('左栏 + 直接打开新建工作区对话框', async () => {
    mockApi({ runs: [], board: { items: [], unbound: [] } });
    renderWorkspace();
    const trigger = await screen.findByRole('button', { name: '新建工作区' });
    // 能力自述没回来之前按钮是禁用的：点它不会开对话框，先等它可用。
    await vi.waitFor(() => expect(trigger.hasAttribute('disabled')).toBe(false));
    fireEvent.click(trigger);
    expect(await screen.findByRole('heading', { name: '新建工作区' })).toBeTruthy();
  });

  it('工作区接口不可用时不显示分组，会话列表照常', async () => {
    const run = makeRun(1, 's-one', 'success', '2026-11-28T10:00:01', '会话一的第一个问题');
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes('/api/workspaces')) {
          return { ok: false, body: null, json: async () => ({}) };
        }
        return {
          ok: true,
          body: null,
          json: async () => ({ items: [run], total: 1, limit: 200, offset: 0 }),
        };
      }),
    );
    renderWorkspace();
    expect(await screen.findByRole('button', { name: /^会话一的第一个问题/ })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '新建工作区' })).toBeNull();
    expect(screen.getByText(/只读会话列表/)).toBeTruthy();
  });
});

describe('Workspace', () => {
  it('渲染七个 tab 且默认选中 chat', () => {
    renderWorkspace();
    expect(screen.getAllByRole('tab')).toHaveLength(7);
    expect(screen.getByRole('tab', { name: '对话' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('tab', { name: '密钥' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: '设置' })).toBeTruthy();
  });

  it('默认标题为新会话，并显示会话与运行统计 chip', async () => {
    renderWorkspace();
    expect(screen.getByRole('heading', { name: '新会话' })).toBeTruthy();
    expect(await screen.findByText('2 个会话 · 3 次运行 · 1 次失败')).toBeTruthy();
  });

  it('从 URL 读取初始 tab，点击切换到指标', async () => {
    renderWorkspace('/?tab=metrics');
    expect(screen.getByRole('tab', { name: '指标' }).getAttribute('aria-selected')).toBe('true');
    fireEvent.click(screen.getByRole('tab', { name: '成本' }));
    expect((await screen.findByRole('tab', { name: '成本' })).getAttribute('aria-selected')).toBe(
      'true',
    );
    expect(screen.getByRole('tab', { name: '指标' }).getAttribute('aria-selected')).toBe('false');
  });

  it('点击会话切换标题并选中态', async () => {
    renderWorkspace();
    await screen.findByRole('button', { name: 'webpanel' });
    fireEvent.click(sidebar().getByRole('button', { name: /^会话一的第一个问题/ }));
    expect(await screen.findByRole('heading', { name: '会话一的第一个问题' })).toBeTruthy();
  });
});
