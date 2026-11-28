// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Run } from '../types';
import { aggregateSessions, groupOf, toSessionTitle } from './SessionSidebar';
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

function mockRuns(runs: Run[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      body: null,
      json: async () => ({ items: runs, total: runs.length, limit: 200, offset: 0 }),
    })),
  );
}

function mockApi(payload: { runs: Run[]; board?: unknown; control?: unknown }) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.includes('/api/workspaces/sessions')
        ? (payload.board ?? { items: [], unbound: [] })
        : url.includes('/api/workspaces/control')
          ? (payload.control ?? null)
          : { items: payload.runs, total: payload.runs.length, limit: 200, offset: 0 };
      return { ok: true, body: null, json: async () => body };
    }),
  );
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
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  mockRuns([
    makeRun(1, 's-one', 'success', '2026-11-23T10:00:01', '会话一的第一个问题'),
    makeRun(2, 's-one', 'failed', '2026-11-23T10:00:02', '后续的短问题'),
    makeRun(3, 's-two', 'success', '2026-11-22T09:00:00', '会话二的问题'),
  ]);
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

describe('左侧分组', () => {
  it('按工作区分组：工作区头可新开会话，会话照常在底下点开', async () => {
    const run = makeRun(1, 's-one', 'success', '2026-11-28T10:00:01', '修 CI 流水线为什么失败');
    mockApi({
      runs: [run],
      control: {
        enabled: true,
        allow_local_import: true,
        allow_git_import: false,
        allowed_local_roots: ['/code'],
        git_available: false,
        max_workspaces: 20,
        default_profile: 'workspace',
      },
      board: {
        items: [
          {
            id: 'ws_a',
            name: 'webpanel',
            root_path: '/code/webpanel',
            status: 'active',
            session_count: 1,
            sessions: [
              {
                session_id: 's-one',
                title: '修 CI 流水线为什么失败',
                last_at: '2026-11-28T10:00:01',
                run_count: 1,
                failed_count: 0,
                bound_at: '2026-11-28T09:00:00',
                bound_by: 'local',
              },
            ],
          },
        ],
        unbound: [],
      },
    });
    renderWorkspace('/?session=s-one');

    expect(await screen.findByText('webpanel')).toBeTruthy();
    expect(screen.getByRole('button', { name: '新建工作区' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /在「webpanel」中新开会话/ }));
    expect(screen.getByRole('heading', { name: '新会话' })).toBeTruthy();
  });

  it('左栏的 + 直接打开新建工作区对话框', async () => {
    mockApi({
      runs: [],
      control: {
        enabled: true,
        allow_local_import: true,
        allow_git_import: false,
        allowed_local_roots: ['/code'],
        git_available: false,
        max_workspaces: 20,
        default_profile: 'workspace',
      },
      board: { items: [], unbound: [] },
    });
    renderWorkspace();
    const trigger = await screen.findByRole('button', { name: '新建工作区' });
    // 能力自述没回来之前按钮是禁用的：点它不会开对话框，先等它可用。
    await vi.waitFor(() => expect(trigger.hasAttribute('disabled')).toBe(false));
    fireEvent.click(trigger);
    expect(await screen.findByRole('heading', { name: '新建工作区' })).toBeTruthy();
  });

  it('工作区接口不可用时不显示工作区入口，会话列表照常', async () => {
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
    expect(await screen.findByRole('button', { name: /会话一的第一个问题/ })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '新建工作区' })).toBeNull();
  });
});

describe('Workspace', () => {
  it('渲染七个 tab 且默认选中 chat', () => {
    renderWorkspace();
    expect(screen.getAllByRole('tab')).toHaveLength(7);
    expect(screen.getByRole('tab', { name: '对话' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('tab', { name: '密钥' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: '设置' })).toBeTruthy();
    expect(screen.getByText('开始一次会话')).toBeTruthy();
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
    expect((await screen.findByRole('tab', { name: '成本' })).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('tab', { name: '指标' }).getAttribute('aria-selected')).toBe('false');
  });

  it('点击会话切换标题并选中态', async () => {
    renderWorkspace();
    fireEvent.click(await screen.findByRole('button', { name: /会话一的第一个问题/ }));
    expect(screen.getByRole('heading', { name: '会话一的第一个问题' })).toBeTruthy();
  });

  it('搜索框按标题过滤会话', async () => {
    renderWorkspace();
    await screen.findByRole('button', { name: /会话一的第一个问题/ });
    fireEvent.change(screen.getByRole('searchbox', { name: '搜索会话' }), {
      target: { value: '会话二' },
    });
    expect(screen.queryByRole('button', { name: /会话一的第一个问题/ })).toBeNull();
    expect(screen.getByRole('button', { name: /会话二的问题/ })).toBeTruthy();
  });

  it('新会话按钮清空当前选择', async () => {
    renderWorkspace('/?session=s-two');
    expect(await screen.findByRole('heading', { name: '会话二的问题' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /新会话/ }));
    expect(screen.getByRole('heading', { name: '新会话' })).toBeTruthy();
  });
});
