// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PermissionCatalog, PermissionKey, PermissionMeta } from '../types';
import { FALLBACK_PROFILES, FALLBACK_LABEL, PermissionPicker } from './PermissionPicker';

const fetchPermissions = vi.fn();

vi.mock('../api/client', () => ({ fetchPermissions: () => fetchPermissions() }));

/** 后端开放的四档：默认不放 unrestricted。 */
const AVAILABLE: PermissionMeta[] = [
  { key: 'readonly', label: '只读', note: '只能读文件', fallback: 'deny', write: false },
  { key: 'workspace', label: '工作区可写', note: '可改工作区文件', fallback: 'ask', write: false },
  { key: 'execute', label: '可执行', note: '白名单命令可直接跑', fallback: 'ask', write: false },
  { key: 'auto', label: '无人值守', note: '定时任务用', fallback: 'deny', write: false },
];

const ALL: PermissionMeta[] = [
  ...AVAILABLE,
  { key: 'unrestricted', label: '全放开', note: '跳过所有逐次确认', fallback: 'hard_deny', write: true },
];

function catalog(available: PermissionMeta[] = AVAILABLE): PermissionCatalog {
  return { default: 'workspace', available };
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function mount(
  current: PermissionKey = 'workspace',
  onChange = vi.fn(),
  extra: { disabled?: boolean } = {},
): { rerender: (node: ReactElement) => void; onChange: ReturnType<typeof vi.fn> } {
  const node = (next: PermissionKey) => (
    <QueryClientProvider client={queryClient}>
      <PermissionPicker current={next} onChange={onChange} disabled={extra.disabled} />
    </QueryClientProvider>
  );
  const { rerender } = render(node(current));
  return { rerender: (next: ReactElement) => rerender(next), onChange };
}

/** 打开弹层：按钮是唯一的 aria-haspopup=menu 入口。 */
function open() {
  fireEvent.click(screen.getByRole('button', { name: '选择权限档位' }));
}

beforeEach(() => {
  fetchPermissions.mockReset();
  fetchPermissions.mockResolvedValue(catalog());
  queryClient.clear();
});

afterEach(cleanup);

describe('PermissionPicker', () => {
  it('按钮显示当前档位名与兜底动作提示', async () => {
    mount('execute');
    const button = screen.getByRole('button', { name: '选择权限档位' });
    await waitFor(() => expect(button.textContent).toContain('可执行'));
    // title 直接给这档的说明，不用点开也知道含义
    expect(button.getAttribute('title')).toBe('白名单命令可直接跑');
    expect(button.getAttribute('aria-expanded')).toBe('false');
  });

  it('展开后列出全部开放档位，并标出当前项', async () => {
    mount('workspace');
    await waitFor(() => expect(fetchPermissions).toHaveBeenCalled());
    open();

    const items = await screen.findAllByRole('menuitemradio');
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining('只读'),
      expect.stringContaining('工作区可写'),
      expect.stringContaining('可执行'),
      expect.stringContaining('无人值守'),
    ]);
    expect(items[1].getAttribute('aria-checked')).toBe('true');
    expect(items[0].getAttribute('aria-checked')).toBe('false');
    // 兜底动作要有中文标签，用户才能判断越界后果
    expect(screen.getAllByText(FALLBACK_LABEL.ask).length).toBeGreaterThan(0);
  });

  it('选普通档位立即生效并收起弹层', async () => {
    const { onChange } = mount('workspace');
    await waitFor(() => expect(fetchPermissions).toHaveBeenCalled());
    open();
    fireEvent.click(await screen.findByRole('menuitemradio', { name: /可执行/ }));

    expect(onChange).toHaveBeenCalledWith('execute');
    expect(screen.queryByRole('menu', { name: '权限档位' })).toBeNull();
  });

  it('放开档要二次确认：取消不切换', async () => {
    fetchPermissions.mockResolvedValue(catalog(ALL));
    const { onChange } = mount('workspace');
    open();
    fireEvent.click(await screen.findByRole('menuitemradio', { name: /全放开/ }));

    expect(screen.getByText(/跳过所有逐次确认/)).toBeTruthy();
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(onChange).not.toHaveBeenCalled();
    // 二次确认面板整块消失（注意放开档的说明文字里也有「跳过…」，不能按文案判）
    expect(document.querySelector('.perm-confirm')).toBeNull();
    // 取消只退回到列表，弹层还开着
    expect(screen.getByRole('menu', { name: '权限档位' })).toBeTruthy();
  });

  it('放开档二次确认后才真的切换', async () => {
    fetchPermissions.mockResolvedValue(catalog(ALL));
    const { onChange } = mount('workspace');
    open();
    fireEvent.click(await screen.findByRole('menuitemradio', { name: /全放开/ }));
    fireEvent.click(screen.getByRole('button', { name: '仍要切换' }));

    expect(onChange).toHaveBeenCalledWith('unrestricted');
    expect(screen.queryByRole('menu', { name: '权限档位' })).toBeNull();
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
    mount('workspace', vi.fn(), { disabled: true });
    const button = screen.getByRole('button', { name: '选择权限档位' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(screen.queryByRole('menu', { name: '权限档位' })).toBeNull();
  });

  it('后端还没回时用兜底列表，且不含放开档', () => {
    fetchPermissions.mockReturnValue(new Promise<PermissionCatalog>(() => {}));
    mount('workspace');
    open();

    const items = screen.getAllByRole('menuitemradio');
    expect(items).toHaveLength(FALLBACK_PROFILES.length);
    expect(items.some((item) => item.textContent?.includes('全放开'))).toBe(false);
    expect(items.map((item) => item.getAttribute('aria-checked'))).toEqual(['false', 'true', 'false']);
  });

  it('当前档位不在开放集里时回落到第一项，不显示失效名字', () => {
    fetchPermissions.mockReturnValue(new Promise<PermissionCatalog>(() => {}));
    mount('unrestricted');
    // 兜底列表没有 unrestricted，按钮应显示第一档而不是空白
    expect(screen.getByRole('button', { name: '选择权限档位' }).textContent).toContain('只读');
  });
});
