// @vitest-environment jsdom
/**
 * 设置页外壳：两段左树、按 section 落内容、离开前确认、首次引导。
 *
 * 旧用例（`SettingsTab.test.tsx` 10 条）保留的是**行为契约**（保存只发改动项、
 * 重置发 null、只读域不可写、漂移提示），换掉的是入口形状：默认分区从
 * 「第一个域」变成「模型参数」，`initialDomain` 变成 `initialSection`。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  exportConfigYaml,
  fetchConfigDomain,
  fetchConfigIndex,
  fetchModelConfig,
  fetchModelLayers,
  fetchProviders,
  patchConfigDomain,
  resetConfigDomain,
} from '../../api/client';
import type { ConfigDomain } from '../../types';
import { CONFIG_INDEX, PROVIDERS, domain, modelConfig } from './fixtures';
import { SettingsTab } from './SettingsTab';

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client');
  // `...actual` 保住 ApiError 等类；列出的这些一律换成桩，
  // 免得漏一个就在 import 期炸「No export is defined on the mock」。
  return {
    ...actual,
    fetchConfigIndex: vi.fn(),
    fetchConfigDomain: vi.fn(),
    patchConfigDomain: vi.fn(),
    resetConfigDomain: vi.fn(),
    exportConfigYaml: vi.fn(),
    fetchModelConfig: vi.fn(),
    fetchModelLayers: vi.fn(),
    patchModelConfig: vi.fn(),
    patchSessionModelConfig: vi.fn(),
    clearSessionModelConfig: vi.fn(),
    previewModelConfig: vi.fn(),
    fetchProviders: vi.fn(),
    fetchProviderModels: vi.fn(),
    refreshProviderModels: vi.fn(),
    fetchProviderDraft: vi.fn(),
    fetchModels: vi.fn(),
    createProvider: vi.fn(),
    updateProvider: vi.fn(),
    deleteProvider: vi.fn(),
    checkProvider: vi.fn(),
  };
});

const indexMock = vi.mocked(fetchConfigIndex);
const domainMock = vi.mocked(fetchConfigDomain);
const patchMock = vi.mocked(patchConfigDomain);
const resetMock = vi.mocked(resetConfigDomain);
const exportMock = vi.mocked(exportConfigYaml);
const modelMock = vi.mocked(fetchModelConfig);
const layersMock = vi.mocked(fetchModelLayers);
const providersMock = vi.mocked(fetchProviders);

function healthDomain(): ConfigDomain {
  return domain();
}

function memoryDomain(): ConfigDomain {
  return domain({
    domain: 'memory',
    title: '记忆策略',
    values: {
      short_term: { max_turns: 12, tool_output_chars: 4000 },
      long_term: { scope: 'session' },
    },
    defaults: {
      short_term: { max_turns: 12, tool_output_chars: 4000 },
      long_term: { scope: 'session' },
    },
    sources: {
      'short_term.max_turns': 'db',
      'short_term.tool_output_chars': 'yaml',
      'long_term.scope': 'env',
    },
    bounds: { 'short_term.max_turns': { min: 1, max: 200 } },
    env_locked: ['long_term.scope'],
    hot: 'next_turn',
    version: 2,
  });
}

function policyDomain(): ConfigDomain {
  return domain({
    domain: 'policy',
    title: '确认协议（只读）',
    values: { confirm: { display_limit: 2000 } },
    defaults: { confirm: { display_limit: 2000 } },
    sources: { 'confirm.display_limit': 'yaml' },
    bounds: {},
    writable: false,
    hot: 'restart',
    note: '高危正则只能改 git',
    version: 1,
  });
}

function renderTab(options: { section?: string; field?: string; entry?: string } = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const { section, field, entry } = options;
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry ?? '/?tab=settings']}>
        <SettingsTab initialSection={section} initialField={field} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  indexMock.mockReset();
  domainMock.mockReset();
  patchMock.mockReset();
  resetMock.mockReset();
  exportMock.mockReset();
  modelMock.mockReset();
  layersMock.mockReset();
  providersMock.mockReset();

  indexMock.mockResolvedValue(CONFIG_INDEX);
  domainMock.mockImplementation(async (name: string) =>
    name === 'memory' ? memoryDomain() : name === 'policy' ? policyDomain() : healthDomain(),
  );
  modelMock.mockResolvedValue(modelConfig());
  layersMock.mockResolvedValue({
    version: 7,
    file_path: 'E:/repo/config/model.local.yaml',
    layers: [],
    effective: modelConfig().effective,
    sources: {},
  });
  providersMock.mockResolvedValue(PROVIDERS);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe('左树与默认分区', () => {
  it('默认落在「模型参数」，左树按性质分三段', async () => {
    renderTab();
    // 默认分区必须是模型参数（改前默认是「第一个域」，正是「找不到模型配置」的原因）
    expect(await screen.findByRole('heading', { name: '模型参数' })).toBeTruthy();
    expect(screen.getByText('模型与网关')).toBeTruthy();
    expect(screen.getByText('运行策略')).toBeTruthy();
    expect(screen.getByText('安全边界')).toBeTruthy();
  });

  it('旧深链 `domain=health` 落到「健康分口径」而不是白屏（A-9）', async () => {
    renderTab({ section: 'health' });
    expect(await screen.findByRole('heading', { name: '健康分口径' })).toBeTruthy();
    expect(screen.getByRole('button', { name: /健康分口径/ }).getAttribute('aria-current')).toBe(
      'page',
    );
  });

  it('认不出的 section 回到默认分区而不是白屏', async () => {
    renderTab({ section: 'nope' });
    expect(await screen.findByRole('heading', { name: '模型参数' })).toBeTruthy();
  });

  it('切域会重新拉取对应域', async () => {
    renderTab({ section: 'health' });
    await screen.findByRole('heading', { name: '健康分口径' });
    fireEvent.click(screen.getByRole('button', { name: /记忆策略/ }));
    await waitFor(() => expect(domainMock).toHaveBeenCalledWith('memory'));
  });
});

describe('运行策略域', () => {
  it('标签是中文名，键名降为次要信息', async () => {
    renderTab({ section: 'health' });
    // 「读列名」是旧版的根因：`latency_baseline_ms` 直接当标签
    expect(await screen.findByText('延迟基线')).toBeTruthy();
    expect(screen.getByText('latency_baseline_ms')).toBeTruthy();
  });

  it('按 yaml 段分组、默认只展开前两组，组名说中文', async () => {
    renderTab({ section: 'memory' });
    expect(await screen.findByText('短期记忆')).toBeTruthy();
    expect(screen.getByText('长期记忆')).toBeTruthy();
    expect(screen.getAllByText('下一轮生效').length).toBeGreaterThan(0);
  });

  it('组内搜索按时中文名过滤，没有匹配时明确说', async () => {
    renderTab({ section: 'memory' });
    await screen.findByText('短期记忆');
    fireEvent.change(screen.getByLabelText('搜索参数'), { target: { value: '保留轮数' } });
    expect(screen.getByText('short_term.max_turns')).toBeTruthy();
    expect(screen.queryByText('long_term.scope')).toBeNull();

    fireEvent.change(screen.getByLabelText('搜索参数'), { target: { value: '不存在的键' } });
    expect(screen.getByText('没有匹配的参数。')).toBeTruthy();
  });

  it('来源用文字徽标，不用颜色小方块', async () => {
    renderTab({ section: 'health' });
    expect((await screen.findAllByText('yaml 基线')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('代码默认').length).toBeGreaterThan(0);
    expect(screen.getAllByText('配置库').length).toBeGreaterThan(0);
  });

  it('改动后显示未保存，保存只发改动项并带版本号', async () => {
    patchMock.mockResolvedValue(healthDomain());
    renderTab({ section: 'health' });
    const field = await screen.findByLabelText('latency_baseline_ms');
    fireEvent.change(field, { target: { value: '1500' } });
    expect(screen.getByText('未保存 1 项')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(patchMock).toHaveBeenCalledWith('health', { latency_baseline_ms: 1500 }, 1));
  });

  it('单行重置发 null（删掉覆盖、回落到下层）', async () => {
    patchMock.mockResolvedValue(healthDomain());
    renderTab({ section: 'health' });
    await screen.findByLabelText('weights.quality');
    fireEvent.click(screen.getByRole('button', { name: '重置 weights.quality' }));
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(patchMock).toHaveBeenCalledWith('health', { 'weights.quality': null }, 1));
  });

  it('环境变量锁定的键禁用输入，并说明锁在谁手里', async () => {
    renderTab({ section: 'memory' });
    const input = (await screen.findByLabelText('long_term.scope')) as HTMLInputElement;
    expect(input.disabled).toBe(true);
    expect(screen.getByText(/已被环境变量锁定/)).toBeTruthy();
  });

  it('只读域不画输入框，显示文本与「只读」徽标，并说明去哪里改（A-17）', async () => {
    renderTab({ section: 'policy' });
    expect(await screen.findByText(/本域只读/)).toBeTruthy();
    // 灰掉的输入框看起来像坏了：只读用文本 + 徽标
    expect(screen.queryByLabelText('confirm.display_limit')).toBeNull();
    expect(document.querySelectorAll('.cfg-readonly-badge')).toHaveLength(1);
    expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('默认值升级漂移时给出提示', async () => {
    domainMock.mockResolvedValue(domain({ drifted: ['latency_baseline_ms'] }));
    renderTab({ section: 'health' });
    expect(await screen.findByText(/代码默认值已升级/)).toBeTruthy();
  });

  it('没有覆盖时导出与恢复全部都不可点', async () => {
    domainMock.mockResolvedValue(
      domain({
        sources: { 'weights.execution': 'yaml', 'weights.quality': 'yaml', latency_baseline_ms: 'yaml' },
      }),
    );
    renderTab({ section: 'health' });
    await screen.findByLabelText('latency_baseline_ms');
    expect((screen.getByRole('button', { name: '导出 yaml' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect((screen.getByRole('button', { name: '恢复全部' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it('读取失败给出可重试的错误块，而不是空白', async () => {
    domainMock.mockRejectedValue(new Error('500'));
    renderTab({ section: 'health' });
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy();
  });

  it('后端不可用时给出可行动的提示', async () => {
    indexMock.mockRejectedValue(new Error('404'));
    renderTab();
    expect(await screen.findByText(/后端可能还是旧版本/)).toBeTruthy();
  });
});

describe('离开确认与引导', () => {
  it('有未保存改动时切分区先确认，取消则留在原页（A-16）', async () => {
    renderTab({ section: 'health' });
    const field = await screen.findByLabelText('latency_baseline_ms');
    fireEvent.change(field, { target: { value: '1500' } });

    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    fireEvent.click(screen.getByRole('button', { name: /记忆策略/ }));
    expect(confirm).toHaveBeenCalled();
    // 取消后仍停在健康分口径：不能一边问一边把草稿丢了
    expect(screen.getByRole('heading', { name: '健康分口径' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: '记忆策略' })).toBeNull();

    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole('button', { name: /记忆策略/ }));
    await waitFor(() => expect(domainMock).toHaveBeenCalledWith('memory'));
    confirm.mockRestore();
  });

  it('首次引导出现，点「不再提示」后置位且刷新不再出现（A-15）', async () => {
    const first = renderTab({ section: 'health' });
    expect(await screen.findByLabelText('首次使用引导')).toBeTruthy();
    expect(screen.getByText('第 1 / 3 步')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '不再提示' }));
    expect(screen.queryByLabelText('首次使用引导')).toBeNull();
    expect(localStorage.getItem('cfg.tour.v1')).toBe('done');
    first.unmount();

    // 重挂一次：同一个 localStorage 指纹下不该再打扰
    renderTab({ section: 'health' });
    await screen.findByLabelText('latency_baseline_ms');
    expect(screen.queryByLabelText('首次使用引导')).toBeNull();
  });
});
