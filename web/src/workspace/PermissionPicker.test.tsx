// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  PermissionApprovals,
  PermissionCatalog,
  PermissionKey,
  PermissionMeta,
  PermissionScope,
} from '../types';
import { FALLBACK_PROFILES, PermissionPicker } from './PermissionPicker';

const fetchPermissions = vi.fn();
const deletePermissionRule = vi.fn();
const createPermissionRule = vi.fn();

vi.mock('../api/client', () => ({
  fetchPermissions: () => fetchPermissions(),
  deletePermissionRule: (tool: string, pattern: string | null) =>
    deletePermissionRule(tool, pattern),
  createPermissionRule: (payload: unknown) => createPermissionRule(payload),
}));

function meta(
  key: PermissionKey,
  label: string,
  scope: PermissionScope,
  approvals: PermissionApprovals,
  over: Partial<PermissionMeta> = {},
): PermissionMeta {
  return {
    key,
    label,
    note: `${label}的说明`,
    scope,
    approvals,
    combo: `${scope}+${approvals}`,
    tools: scope === 'readonly' ? ['bash', 'read', 'glob', 'grep'] : ['bash', 'write'],
    write: scope !== 'readonly',
    auto_allow: ['ls -la'],
    will_ask: ['git commit -m x'],
    will_deny: ['cat .env'],
    ...over,
  };
}

/** 后端开放的四档：默认不放 container。 */
const AVAILABLE: PermissionMeta[] = [
  meta('readonly', '只读', 'readonly', 'never'),
  meta('workspace', '工作区可写', 'workspace', 'on-risk', {
    auto_allow: ['ls -la', 'pytest -q'],
    will_ask: ['git commit -m x'],
  }),
  meta('cautious', '逐个确认', 'workspace', 'always'),
  meta('unattended', '无人值守', 'workspace', 'never'),
];

const ALL: PermissionMeta[] = [
  ...AVAILABLE,
  meta('container', '全放开', 'container', 'never'),
];

function catalog(available: PermissionMeta[] = AVAILABLE): PermissionCatalog {
  return {
    default: 'workspace',
    available,
    scopes: ['readonly', 'workspace', 'container'],
    approvals: ['always', 'on-risk', 'never'],
    rules: [],
    rules_file: 'config/permissions.local.yaml',
    trust: { mode: 'permissive', workdirs: [] },
    fatigue: [],
  };
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function mount(
  current: PermissionKey = 'workspace',
  extra: { disabled?: boolean } = {},
): { onChange: ReturnType<typeof vi.fn>; rerender: (next: PermissionKey) => void } {
  const onChange = vi.fn();
  const node = (next: PermissionKey): ReactElement => (
    <QueryClientProvider client={queryClient}>
      <PermissionPicker current={next} onChange={onChange} disabled={extra.disabled} />
    </QueryClientProvider>
  );
  const { rerender } = render(node(current));
  return { onChange, rerender: (next: PermissionKey) => rerender(node(next)) };
}

/** 打开弹层：按钮是唯一的 aria-haspopup=menu 入口。 */
function open() {
  fireEvent.click(screen.getByRole('button', { name: '选择权限档位' }));
}

beforeEach(() => {
  fetchPermissions.mockReset();
  fetchPermissions.mockResolvedValue(catalog());
  deletePermissionRule.mockReset();
  deletePermissionRule.mockResolvedValue([]);
  createPermissionRule.mockReset();
  queryClient.clear();
});

afterEach(cleanup);

describe('PermissionPicker', () => {
  it('按钮显示当前档位名，title 给出该档说明', async () => {
    mount('workspace');
    const button = screen.getByRole('button', { name: '选择权限档位' });
    await waitFor(() => expect(button.textContent).toContain('工作区可写'));
    // 兜底列表的说明先顶上，服务端返回后换成服务端那句（判定与文案必须同源）
    await waitFor(() => expect(button.getAttribute('title')).toBe('工作区可写的说明'));
    expect(button.getAttribute('aria-expanded')).toBe('false');
  });

  it('展开后是两个维度，而不是一列档位', async () => {
    mount('workspace');
    await waitFor(() => expect(fetchPermissions).toHaveBeenCalled());
    open();

    expect(screen.getByText('能力范围')).toBeTruthy();
    expect(screen.getByText('打扰程度')).toBeTruthy();

    const scopes = await screen.findAllByRole('menuitemradio', { name: /只读|工作区|不限目录/ });
    expect(scopes.map((item) => item.textContent)).toEqual(['只读', '工作区', '不限目录']);
    // 当前档是 workspace+on-risk：两行各高亮一格
    const checked = screen
      .getAllByRole('menuitemradio')
      .filter((item) => item.getAttribute('aria-checked') === 'true');
    expect(checked.map((item) => item.textContent)).toEqual(['工作区', '遇到风险才问']);
  });

  it('只读档没有「什么都问」的组合：那一格置灰', async () => {
    mount('readonly');
    await waitFor(() => expect(fetchPermissions).toHaveBeenCalled());
    open();
    const always = await screen.findByRole('menuitemradio', { name: '逐个确认' });
    expect((always as HTMLButtonElement).disabled).toBe(true);
  });

  it('换打扰程度立即生效并收起弹层', async () => {
    const { onChange } = mount('workspace');
    await waitFor(() => expect(fetchPermissions).toHaveBeenCalled());
    open();
    fireEvent.click(await screen.findByRole('menuitemradio', { name: '逐个确认' }));

    expect(onChange).toHaveBeenCalledWith('cautious');
    expect(screen.queryByRole('menu', { name: '权限档位' })).toBeNull();
  });

  it('选不限目录要二次确认：取消不切换', async () => {
    fetchPermissions.mockResolvedValue(catalog(ALL));
    // 从「不再询问」起步：不限目录目前只有 container+never 这一形态
    const { onChange } = mount('unattended');
    await waitFor(() => expect(fetchPermissions).toHaveBeenCalled());
    open();
    fireEvent.click(await screen.findByRole('menuitemradio', { name: '不限目录' }));

    expect(screen.getByText(/不受工作目录限制、也不再逐次确认/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(onChange).not.toHaveBeenCalled();
    expect(document.querySelector('.perm-confirm')).toBeNull();
  });

  it('不限目录二次确认后才真的切换', async () => {
    fetchPermissions.mockResolvedValue(catalog(ALL));
    const { onChange } = mount('unattended');
    await waitFor(() => expect(fetchPermissions).toHaveBeenCalled());
    open();
    fireEvent.click(await screen.findByRole('menuitemradio', { name: '不限目录' }));
    fireEvent.click(screen.getByRole('button', { name: '仍要切换' }));

    expect(onChange).toHaveBeenCalledWith('container');
  });

  it('预览直接说清不问 / 会问 / 拒绝，不用试一次才知道', async () => {
    mount('workspace');
    await waitFor(() => expect(fetchPermissions).toHaveBeenCalled());
    open();

    expect(await screen.findByText('不问')).toBeTruthy();
    expect(screen.getByText('会问')).toBeTruthy();
    expect(screen.getByText('拒绝')).toBeTruthy();
    expect(screen.getByText(/pytest -q/)).toBeTruthy();
    expect(screen.getByText(/cat \.env/)).toBeTruthy();
  });

  it('已沉淀规则可逐条删除', async () => {
    fetchPermissions.mockResolvedValue({
      ...catalog(),
      rules: [
        {
          tool: 'Bash',
          pattern: 'pytest:*',
          behavior: 'allow',
          source: 'user',
          scope: 'workspace',
          origin: 'pytest -q',
          created_at: '2026-11-24T00:00:00Z',
        },
      ],
    });
    mount('workspace');
    await waitFor(() => expect(fetchPermissions).toHaveBeenCalled());
    open();

    fireEvent.click(await screen.findByRole('button', { name: '撤销规则 pytest:*' }));
    // 乐观副作用是异步的：等 mutationFn 真的跑到才算数
    await waitFor(() => expect(deletePermissionRule).toHaveBeenCalledWith('Bash', 'pytest:*'));
  });

  it('被反复询问时给出「以后都允许」入口', async () => {
    fetchPermissions.mockResolvedValue({
      ...catalog(),
      fatigue: [{ tool: 'Bash', prefix: 'pytest:*', count: 3 }],
    });
    mount('workspace');
    await waitFor(() => expect(fetchPermissions).toHaveBeenCalled());
    open();

    expect(await screen.findByText(/已被问 3 次/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '以后都允许' }));
    await waitFor(() =>
      expect(createPermissionRule).toHaveBeenCalledWith({ tool: 'Bash', pattern: 'pytest:*' }),
    );
  });

  it('点弹层外面收起，不触发切换', async () => {
    const { onChange } = mount('workspace');
    await waitFor(() => expect(fetchPermissions).toHaveBeenCalled());
    open();
    expect(screen.getByRole('menu', { name: '权限档位' })).toBeTruthy();

    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole('menu', { name: '权限档位' })).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('执行中禁用，点不开菜单', () => {
    mount('workspace', { disabled: true });
    const button = screen.getByRole('button', { name: '选择权限档位' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(screen.queryByRole('menu', { name: '权限档位' })).toBeNull();
  });

  it('后端还没回时用兜底列表，且不含全放开档', () => {
    fetchPermissions.mockReturnValue(new Promise<PermissionCatalog>(() => {}));
    mount('workspace');
    open();

    const scopes = screen.getAllByRole('menuitemradio', { name: /只读|工作区|不限目录/ });
    expect(scopes).toHaveLength(3);
    // 兜底列表没有 container，所以「不限目录」置灰
    expect((scopes[2] as HTMLButtonElement).disabled).toBe(true);
    expect(FALLBACK_PROFILES.some((item) => item.scope === 'container')).toBe(false);
  });

  it('当前档位不在开放集里时回落到第一项，不显示失效名字', () => {
    fetchPermissions.mockReturnValue(new Promise<PermissionCatalog>(() => {}));
    mount('container');
    expect(screen.getByRole('button', { name: '选择权限档位' }).textContent).toContain('只读');
  });
});
