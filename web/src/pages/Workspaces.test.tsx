// @vitest-environment jsdom
/** 工作区页：列表 → 详情 → 新建的交互契约。
 *
 * 这些用例盯的是「界面有没有把能力说清楚」：
 * - 列表按来源区分本地 / Git，进页面默认选中第一条；
 * - 本地新建走目录浏览，选中哪一层就提交哪一层，名称跟着末段自动带出；
 * - Git 新建只发远端地址，分支为空时不下发空串；
 * - 白名单为空时本地入口直接禁用，而不是让人填完表单才收到 400。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { WorkspaceControl, WorkspaceSummary } from '../types';
import { Workspaces } from './Workspaces';

const listWorkspaces = vi.fn();
const loadControl = vi.fn();
const loadWorkspace = vi.fn();
const createWorkspace = vi.fn();
const archive = vi.fn();
const restore = vi.fn();
const remove = vi.fn();
const browse = vi.fn();
const loadMemory = vi.fn();

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return {
    ...actual,
    fetchWorkspaces: (...args: unknown[]) => listWorkspaces(...args),
    fetchWorkspaceControl: (...args: unknown[]) => loadControl(...args),
    fetchWorkspace: (...args: unknown[]) => loadWorkspace(...args),
    createWorkspace: (...args: unknown[]) => createWorkspace(...args),
    archiveWorkspace: (...args: unknown[]) => archive(...args),
    restoreWorkspace: (...args: unknown[]) => restore(...args),
    deleteWorkspace: (...args: unknown[]) => remove(...args),
    browseWorkspaceDirs: (...args: unknown[]) => browse(...args),
    fetchWorkspaceMemory: (...args: unknown[]) => loadMemory(...args),
  };
});

const CONTROL: WorkspaceControl = {
  enabled: true,
  allow_local_import: true,
  allow_git_import: true,
  allowed_local_roots: ['/srv/code'],
  git_available: true,
  max_workspaces: 10,
  default_profile: 'workspace',
};

const LOCAL: WorkspaceSummary = {
  id: 'ws-local',
  name: 'webpanel',
  owner: 'local',
  root_path: '/srv/code/webpanel',
  default_profile: 'workspace',
  status: 'active',
  created_at: '2026-11-01T08:00:00Z',
  updated_at: '2026-11-28T06:00:00Z',
  archived_at: null,
  source: 'local',
  managed: false,
  remote_url: '',
  branch: 'main',
  base_commit: 'abc1234567',
  detected_git: true,
  session_count: 4,
  long_term_count: 12,
};

const GIT: WorkspaceSummary = {
  ...LOCAL,
  id: 'ws-git',
  name: 'devops-agent',
  root_path: '/srv/managed/devops-agent',
  source: 'git',
  managed: true,
  remote_url: 'https://github.com/acme/devops-agent.git',
  branch: 'main',
  session_count: 0,
  long_term_count: 2,
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Workspaces />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  // jsdom 31 还没有 HTMLDialogElement.showModal/close：补最小实现，让「取消」走 close 事件。
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
  loadControl.mockResolvedValue(CONTROL);
  listWorkspaces.mockResolvedValue({ items: [LOCAL, GIT] });
  loadWorkspace.mockImplementation((id: string) =>
    Promise.resolve(id === GIT.id ? GIT : LOCAL),
  );
  browse.mockImplementation((path: string) =>
    Promise.resolve(
      path === '/srv/code/repo-a'
        ? { root: '/srv/code/repo-a', items: [], parent: '/srv/code', roots: ['/srv/code'] }
        : {
            root: '/srv/code',
            items: [{ name: 'repo-a', path: '/srv/code/repo-a' }],
            parent: null,
            roots: ['/srv/code'],
          },
    ),
  );
  createWorkspace.mockResolvedValue(GIT);
  loadMemory.mockResolvedValue({
    workspace_id: LOCAL.id,
    scope: 'workspace',
    scope_id: `workspace:${LOCAL.id}`,
    items: [
      {
        id: 7,
        kind: 'decision',
        text: '时间一律按 UTC 存、展示时转本地时区',
        hits: 3,
        created_at: '2026-11-20T08:00:00Z',
      },
    ],
    total: 12,
    limit: 5,
    offset: 0,
  });
  archive.mockResolvedValue({ ...LOCAL, status: 'archived' });
  restore.mockResolvedValue(LOCAL);
  remove.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('工作区页', () => {
  it('列表区分本地与 Git，进页面默认选中第一条', async () => {
    renderPage();
    expect(await screen.findByText('原地引用 · 4 个会话')).toBeTruthy();
    expect(screen.getByText('受管克隆 · main · 0 个会话')).toBeTruthy();
    await waitFor(() => expect(loadWorkspace).toHaveBeenCalledWith(LOCAL.id));
    expect(await screen.findByRole('heading', { name: /webpanel/ })).toBeTruthy();
    expect(await screen.findByText('时间一律按 UTC 存、展示时转本地时区')).toBeTruthy();
    expect(screen.getByText('决策')).toBeTruthy();
    expect(screen.getByText('最近 1 条 / 共 12 条')).toBeTruthy();
  });

  it('本地新建：选中子目录后提交该路径，名称自动带出', async () => {
    renderPage();
    await screen.findByText('原地引用 · 4 个会话');
    fireEvent.click(screen.getByTitle('新建工作区'));
    expect(await screen.findByRole('heading', { name: '新建工作区' })).toBeTruthy();
    // 选择器落到第一个白名单根
    await screen.findByText('/srv/code');
    const path = document.querySelector<HTMLInputElement>('#wsp-path');
    expect(path?.value).toBe('/srv/code');
    // 初始名称由白名单根末段带出
    expect(screen.getByLabelText<HTMLInputElement>('名称').value).toBe('code');
    // 进入子目录
    fireEvent.click(await screen.findByText('repo-a'));
    await waitFor(() =>
      expect(document.querySelector<HTMLInputElement>('#wsp-path')?.value).toBe(
        '/srv/code/repo-a',
      ),
    );
    expect(screen.getByLabelText<HTMLInputElement>('名称').value).toBe('repo-a');
    fireEvent.click(screen.getByText('添加工作区'));
    await waitFor(() =>
      expect(createWorkspace).toHaveBeenCalledWith({
        name: 'repo-a',
        source: { kind: 'local', path: '/srv/code/repo-a' },
      }),
    );
  });

  it('Git 新建：URL 带出仓库名，分支写进请求', async () => {
    listWorkspaces.mockResolvedValue({ items: [] });
    renderPage();
    fireEvent.click(await screen.findByText('Git 地址'));
    fireEvent.change(await screen.findByLabelText('Git 地址'), {
      target: { value: 'https://github.com/acme/demo.git' },
    });
    fireEvent.change(screen.getByLabelText('分支'), { target: { value: 'release/1.0' } });
    expect(screen.getByLabelText<HTMLInputElement>('名称').value).toBe('demo');
    fireEvent.click(screen.getByText('克隆并添加'));
    await waitFor(() =>
      expect(createWorkspace).toHaveBeenCalledWith({
        name: 'demo',
        source: {
          kind: 'git',
          url: 'https://github.com/acme/demo.git',
          branch: 'release/1.0',
          credential_ref: undefined,
        },
      }),
    );
  });

  it('白名单为空：本地入口禁用并指向设置页', async () => {
    listWorkspaces.mockResolvedValue({ items: [] });
    loadControl.mockResolvedValue({ ...CONTROL, allowed_local_roots: [] });
    renderPage();
    const localOption = (await screen.findByText('本地目录')).closest('button');
    await waitFor(() => expect(localOption?.disabled).toBe(true));
    expect(screen.getByText('需要先在设置里配置白名单')).toBeTruthy();
    const link = screen.getByText('「设置 · 工作区」').closest('a');
    expect(link?.getAttribute('href')).toBe('/?tab=settings&domain=workspace');
  });

  it('归档调用后从活跃列表消失', async () => {
    renderPage();
    await screen.findByText('原地引用 · 4 个会话');
    await screen.findByRole('heading', { name: /webpanel/ });
    listWorkspaces.mockResolvedValue({ items: [GIT] });
    fireEvent.click(screen.getByText('归档'));
    await waitFor(() => expect(archive).toHaveBeenCalledWith(LOCAL.id));
    await waitFor(() => expect(screen.queryByText('webpanel')).toBeNull());
  });
});
