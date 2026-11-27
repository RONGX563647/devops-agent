// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  exportConfigYaml,
  fetchConfigDomain,
  fetchConfigIndex,
  patchConfigDomain,
  resetConfigDomain,
} from '../../api/client';
import type { ConfigDomain, ConfigIndex } from '../../types';
import { SettingsTab } from './SettingsTab';

vi.mock('../../api/client', () => ({
  fetchConfigIndex: vi.fn(),
  fetchConfigDomain: vi.fn(),
  patchConfigDomain: vi.fn(),
  resetConfigDomain: vi.fn(),
  exportConfigYaml: vi.fn(),
}));

const indexMock = vi.mocked(fetchConfigIndex);
const domainMock = vi.mocked(fetchConfigDomain);
const patchMock = vi.mocked(patchConfigDomain);
const resetMock = vi.mocked(resetConfigDomain);
const exportMock = vi.mocked(exportConfigYaml);

const INDEX: ConfigIndex = {
  mode: 'db',
  db_path: 'E:/repo/workspace/config.db',
  domains: [
    {
      domain: 'input',
      title: '输入管线',
      writable: true,
      hot: 'immediate',
      note: '抓取超时与体积上限',
      managed_by: '',
      version: 1,
      overrides: 0,
      env_locked: [],
    },
    {
      domain: 'memory',
      title: '记忆策略',
      writable: true,
      hot: 'next_turn',
      note: '三层记忆',
      managed_by: '',
      version: 2,
      overrides: 0,
      env_locked: ['long_term.scope'],
    },
    {
      domain: 'policy',
      title: '确认协议（只读）',
      writable: false,
      hot: 'restart',
      note: '高危正则只能改 git',
      managed_by: '',
      version: 0,
      overrides: 0,
      env_locked: [],
    },
  ],
};

function inputDomain(over: Partial<ConfigDomain> = {}): ConfigDomain {
  return {
    domain: 'input',
    title: '输入管线',
    values: {
      max_references: 5,
      fetch_timeout_seconds: 5,
      allow_private_addresses: false,
      allowed_ports: [80, 443],
    },
    defaults: {
      max_references: 5,
      fetch_timeout_seconds: 5,
      allow_private_addresses: false,
      allowed_ports: [80, 443],
    },
    sources: {
      max_references: 'yaml',
      fetch_timeout_seconds: 'yaml',
      allow_private_addresses: 'yaml',
      allowed_ports: 'yaml',
    },
    bounds: {
      max_references: { min: 1, max: 50 },
      fetch_timeout_seconds: { min_exclusive: 0, max: 60 },
    },
    editable: ['max_references', 'fetch_timeout_seconds'],
    env_locked: [],
    writable: true,
    managed_by: '',
    hot: 'immediate',
    note: '抓取超时与体积上限',
    version: 1,
    drifted: [],
    mode: 'db',
    db_path: 'E:/repo/workspace/config.db',
    ...over,
  };
}

function memoryDomain(): ConfigDomain {
  return inputDomain({
    domain: 'memory',
    title: '记忆策略',
    values: { short_term: { max_turns: 12 }, long_term: { scope: 'session' } },
    defaults: { short_term: { max_turns: 12 }, long_term: { scope: 'session' } },
    sources: { 'short_term.max_turns': 'yaml', 'long_term.scope': 'env' },
    bounds: { 'short_term.max_turns': { min: 1, max: 200 } },
    env_locked: ['long_term.scope'],
    hot: 'next_turn',
    version: 2,
  });
}

function policyDomain(): ConfigDomain {
  return inputDomain({
    domain: 'policy',
    title: '确认协议（只读）',
    values: { confirm: { display_limit: 2000 } },
    defaults: { confirm: { display_limit: 2000 } },
    sources: { 'confirm.display_limit': 'yaml' },
    bounds: {},
    writable: false,
    hot: 'restart',
    note: '高危正则只能改 git',
    version: 0,
  });
}

function renderTab(initialDomain?: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SettingsTab initialDomain={initialDomain} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  indexMock.mockReset();
  domainMock.mockReset();
  patchMock.mockReset();
  resetMock.mockReset();
  exportMock.mockReset();
  indexMock.mockResolvedValue(INDEX);
  domainMock.mockImplementation(async (domain: string) =>
    domain === 'memory' ? memoryDomain() : domain === 'policy' ? policyDomain() : inputDomain(),
  );
});

afterEach(cleanup);

describe('SettingsTab', () => {
  it('列出所有域并默认展示第一个域的值', async () => {
    renderTab();
    expect((await screen.findAllByText('输入管线')).length).toBeGreaterThan(0);
    expect((await screen.findByLabelText('max_references') as HTMLInputElement).value).toBe('5');
    expect(screen.getAllByText('yaml 基线').length).toBe(4);
  });

  it('按第一段路径分组，并标出生效时机', async () => {
    renderTab('memory');
    expect(await screen.findByText('short_term')).toBeTruthy();
    expect(screen.getAllByText('下一轮生效').length).toBeGreaterThan(0);
  });

  it('改动后显示未保存，保存只发改动项并带版本号', async () => {
    patchMock.mockResolvedValue(inputDomain({ version: 2 }));
    renderTab();
    const field = await screen.findByLabelText('max_references');
    fireEvent.change(field, { target: { value: '9' } });
    expect(screen.getByText('未保存 1 项')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(patchMock).toHaveBeenCalledWith('input', { max_references: 9 }, 1));
  });

  it('单行重置发 null（删掉覆盖、回落到下层）', async () => {
    patchMock.mockResolvedValue(inputDomain({ version: 2 }));
    renderTab();
    await screen.findByLabelText('max_references');
    fireEvent.click(screen.getByRole('button', { name: '重置 max_references' }));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(patchMock).toHaveBeenCalledWith('input', { max_references: null }, 1));
  });

  it('环境变量锁定的键禁用输入', async () => {
    renderTab('memory');
    expect((await screen.findByLabelText('long_term.scope') as HTMLInputElement).disabled).toBe(true);
  });

  it('只读域不允许保存，并说明去哪里改', async () => {
    renderTab('policy');
    expect(await screen.findByText(/本域只读/)).toBeTruthy();
    expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('默认值升级漂移时给出提示', async () => {
    domainMock.mockResolvedValue(inputDomain({ drifted: ['max_references'] }));
    renderTab();
    expect(await screen.findByText(/代码默认值已升级/)).toBeTruthy();
  });

  it('没有覆盖时导出与恢复全部都不可点', async () => {
    renderTab();
    await screen.findByLabelText('max_references');
    expect((screen.getByRole('button', { name: '导出 yaml' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect((screen.getByRole('button', { name: '恢复全部' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it('切域会重新拉取对应域', async () => {
    renderTab();
    fireEvent.click(await screen.findByRole('button', { name: /记忆策略/ }));
    await waitFor(() => expect(domainMock).toHaveBeenCalledWith('memory'));
  });

  it('后端不可用时给出可行动的提示', async () => {
    indexMock.mockRejectedValue(new Error('404'));
    renderTab();
    expect(await screen.findByText(/后端可能还是旧版本/)).toBeTruthy();
  });
});
