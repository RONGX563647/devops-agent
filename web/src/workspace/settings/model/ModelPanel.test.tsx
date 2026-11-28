// @vitest-environment jsdom
/**
 * 模型参数页（SPEC-0022 §7）：三栏、保存范围、危险确认、预览数字。
 *
 * 这些用例盯的是「界面有没有撒谎」：
 * - 右栏数字必须来自后端 `derived`（前端自己乘一次就会和后端口径分家，A-11）；
 * - 深链 `field=top_p` 要真的把右栏和那一行对上（A-8），否则收藏链接等于无效；
 * - 预设只填草稿，危险项必须先确认——两点都是「不让人在不知情时落盘」。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  clearSessionModelConfig,
  fetchModelConfig,
  fetchModelLayers,
  fetchProviderModels,
  fetchProviders,
  patchModelConfig,
  patchSessionModelConfig,
  previewModelConfig,
} from '../../../api/client';
import { modelConfig } from '../fixtures';
import { ModelPanel } from './ModelPanel';

vi.mock('../../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../../api/client')>('../../../api/client');
  return {
    ...actual,
    fetchModelConfig: vi.fn(),
    fetchModelLayers: vi.fn(),
    fetchProviders: vi.fn(),
    fetchProviderModels: vi.fn(),
    refreshProviderModels: vi.fn(),
    patchModelConfig: vi.fn(),
    patchSessionModelConfig: vi.fn(),
    clearSessionModelConfig: vi.fn(),
    previewModelConfig: vi.fn(),
  };
});

const configMock = vi.mocked(fetchModelConfig);
const layersMock = vi.mocked(fetchModelLayers);
const providersMock = vi.mocked(fetchProviders);
const modelsMock = vi.mocked(fetchProviderModels);
const saveGlobal = vi.mocked(patchModelConfig);
const saveSession = vi.mocked(patchSessionModelConfig);
const clearSession = vi.mocked(clearSessionModelConfig);
const previewMock = vi.mocked(previewModelConfig);

const PROVIDERS = {
  default: 'dashscope',
  anonymous: false,
  enabled: true,
  items: [
    {
      id: 'dashscope',
      kind: 'openai_chat',
      label: '百炼',
      base_url_host: 'dashscope.aliyuncs.com',
      discover: 'models' as const,
      has_key: true,
      key_fingerprint: '70c4551b',
      models: 2,
      last_check: null,
    },
  ],
  kinds: [{ kind: 'openai_chat', family: 'openai' }],
};

function renderPanel(options: { session?: string; field?: string } = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const dirty = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <ModelPanel session={options.session ?? ''} initialField={options.field} onDirtyChange={dirty} />
    </QueryClientProvider>,
  );
  return dirty;
}

beforeEach(() => {
  configMock.mockReset();
  layersMock.mockReset();
  providersMock.mockReset();
  modelsMock.mockReset();
  saveGlobal.mockReset();
  saveSession.mockReset();
  clearSession.mockReset();
  previewMock.mockReset();

  configMock.mockResolvedValue(modelConfig());
  layersMock.mockResolvedValue({
    version: 7,
    file_path: 'E:/repo/config/model.local.yaml',
    layers: [
      { id: 'file', label: 'config/model.local.yaml', writable: true, values: { model: 'qwen3-max' } },
    ],
    effective: modelConfig().effective,
    sources: {},
  });
  providersMock.mockResolvedValue(PROVIDERS);
  modelsMock.mockResolvedValue({
    provider: 'dashscope',
    fetched_at: '2026-11-28T00:00:00Z',
    stale: false,
    code: 'ok',
    http_status: 200,
    total: 2,
    items: [],
  });
  saveGlobal.mockResolvedValue({ ...modelConfig(), affected: { applied: 1, deferred: 0 } });
  saveSession.mockResolvedValue({
    ...modelConfig(),
    overrides: {},
    effective_from: '下一次模型调用',
  });
  clearSession.mockResolvedValue({ ...modelConfig(), overrides: {} });
  previewMock.mockResolvedValue({ ...modelConfig(), errors: [] });
});

afterEach(cleanup);

describe('三栏与说明', () => {
  it('左栏五组、中栏表单、右栏默认讲上下文窗口', async () => {
    renderPanel();
    await screen.findByLabelText('上下文窗口');
    for (const label of ['连接', '采样', '执行', '上下文压缩', '高级与只读']) {
      expect(screen.getByRole('button', { name: label })).toBeTruthy();
    }
    const doc = screen.getByLabelText('参数说明');
    expect(doc.textContent).toContain('上下文窗口');
    // 四段式：是什么 / 改了会怎样 / 谁在压着它
    expect(doc.textContent).toContain('是什么');
    expect(doc.textContent).toContain('改了会怎样');
    expect(doc.textContent).toContain('谁在压着它');
    expect(doc.textContent).toContain('高风险');
  });

  it('只读项列出来但不可编辑，并说明在环境变量里改', async () => {
    renderPanel();
    await screen.findByLabelText('上下文窗口');
    expect(screen.getByText('api_key')).toBeTruthy();
    expect(screen.getByText(/DEVOPS_AGENT_API_KEY/)).toBeTruthy();
    expect(screen.queryByLabelText('api_key')).toBeNull();
  });

  it('深链 field=top_p：右栏换成 top_p，且那一行高亮（A-8）', async () => {
    renderPanel({ field: 'top_p' });
    await screen.findByLabelText('top_p');
    const doc = screen.getByLabelText('参数说明');
    expect(doc.textContent).toContain('核采样 top_p');
    // 高亮只应落在一行上，否则「点了没反应」变成「到处都在闪」
    const highlighted = document.querySelectorAll('.mcd-row.is-highlight');
    expect(highlighted).toHaveLength(1);
    expect(highlighted[0].textContent).toContain('top_p');
  });

  it('点某一行的来源徽标，右栏跟着切换', async () => {
    renderPanel();
    await screen.findByLabelText('上下文窗口');
    fireEvent.click(screen.getByRole('button', { name: '迭代上限 的来源' }));
    expect(screen.getByLabelText('参数说明').textContent).toContain('迭代上限');
  });
});

describe('实时影响（口径只在后端）', () => {
  it('改迭代上限后右栏用后端给的 worst_case_attempts，并标「预览值」（A-11）', async () => {
    previewMock.mockResolvedValue({
      ...modelConfig(),
      derived: {
        compression_threshold: 104858,
        worst_case_attempts: 12,
        context_uncalibrated: false,
        context_source: 'discovered',
        context_card_may_be_stale: false,
      },
      errors: [],
    });
    renderPanel({ field: 'max_iters' });
    const input = await screen.findByLabelText('迭代上限');
    fireEvent.change(input, { target: { value: '5' } });

    await waitFor(() => expect(previewMock).toHaveBeenCalled());
    expect(previewMock.mock.calls[0][0]).toEqual({ max_iters: 5 });
    // 若前端自己乘 (1+3)×(1+3)=16，这里就会是 16 而不是 12
    expect(await screen.findByText(/发 12 次模型请求/)).toBeTruthy();
    expect(screen.getByText('预览值')).toBeTruthy();
  });

  it('预览回来的字段级错误出现在右栏「这一项不合法」', async () => {
    previewMock.mockResolvedValue({
      ...modelConfig(),
      errors: [{ field: 'context_size', reason: '不能小于 8192' }],
    });
    renderPanel();
    const input = await screen.findByLabelText('上下文窗口');
    fireEvent.change(input, { target: { value: '4096' } });

    expect(await screen.findByText('这一项不合法')).toBeTruthy();
    expect(screen.getByText('不能小于 8192')).toBeTruthy();
  });

  it('本地越界先禁用保存（服务端仍是唯一裁判）', async () => {
    renderPanel();
    const input = await screen.findByLabelText('上下文窗口');
    fireEvent.change(input, { target: { value: '4096' } });
    expect(screen.getByText(/context_size 不能小于 8192/)).toBeTruthy();
    expect((screen.getByRole('button', { name: '保存为全局默认' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });
});

describe('保存范围与危险确认', () => {
  it('预设只填草稿：立刻显示未保存项，且没有写任何东西', async () => {
    renderPanel();
    await screen.findByLabelText('上下文窗口');
    fireEvent.click(screen.getByRole('button', { name: '省钱' }));
    expect(screen.getByText('未保存 4 项')).toBeTruthy();
    expect(saveGlobal).not.toHaveBeenCalled();
    // 预设不动模型名：模型是用户显式选的，不该被「省钱」顺手改掉
    expect((screen.getByLabelText('模型名') as HTMLInputElement).value).toBe('qwen3-max');
  });

  it('改重试次数属于危险项：先勾「我了解上面的影响」才允许保存', async () => {
    renderPanel();
    await screen.findByLabelText('上下文窗口');
    fireEvent.click(screen.getByRole('button', { name: '省钱' }));
    expect(screen.getByText(/重试次数变了/)).toBeTruthy();

    const save = screen.getByRole('button', { name: '保存为全局默认' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);

    fireEvent.click(screen.getByLabelText('我了解上面的影响'));
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(saveGlobal).toHaveBeenCalledTimes(1));
    // 乐观并发：version 必须取自当前快照
    expect(saveGlobal.mock.calls[0][1]).toBe(7);
    expect(await screen.findByText(/1 个会话立即生效/)).toBeTruthy();
  });

  it('没有会话时「本次会话」不可点，避免改了个不存在的会话', async () => {
    renderPanel();
    await screen.findByLabelText('上下文窗口');
    expect((screen.getByRole('button', { name: '本次会话' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it('会话范围常驻警示条，保存走会话端点', async () => {
    renderPanel({ session: 's1' });
    await screen.findByLabelText('上下文窗口');
    fireEvent.click(screen.getByRole('button', { name: '本次会话' }));
    expect(screen.getByText(/仅当前会话生效，换会话即回到全局默认/)).toBeTruthy();

    // 换范围会重新取一次配置，表单要先回来
    const input = await screen.findByLabelText('上下文窗口');
    fireEvent.change(input, { target: { value: '65536' } });
    fireEvent.click(screen.getByLabelText('我了解上面的影响'));
    fireEvent.click(screen.getByRole('button', { name: '保存到本次会话' }));
    await waitFor(() => expect(saveSession).toHaveBeenCalledWith('s1', { context_size: 65536 }));
    expect(saveGlobal).not.toHaveBeenCalled();
  });

  it('会话里已有的覆盖被重置时发 null，而不是写回继承值', async () => {
    configMock.mockResolvedValue(modelConfig({ overrides: { temperature: 0.5 } }));
    renderPanel({ session: 's1' });
    await screen.findByLabelText('上下文窗口');
    fireEvent.click(screen.getByRole('button', { name: '本次会话' }));
    // 先改一次才出现「撤销」：草稿为空时没有可撤销的东西
    fireEvent.change(await screen.findByLabelText('温度'), { target: { value: '0.3' } });
    fireEvent.click(screen.getByRole('button', { name: '重置温度为继承' }));
    fireEvent.click(screen.getByRole('button', { name: '保存到本次会话' }));
    await waitFor(() => expect(saveSession).toHaveBeenCalledWith('s1', { temperature: null }));
  });

  it('切保存范围时草稿会被丢弃，必须先确认', async () => {
    renderPanel({ session: 's1' });
    await screen.findByLabelText('上下文窗口');
    fireEvent.change(screen.getByLabelText('上下文窗口'), { target: { value: '65536' } });

    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    fireEvent.click(screen.getByRole('button', { name: '本次会话' }));
    expect(confirm).toHaveBeenCalled();
    // 取消后仍在全局范围，草稿还在
    expect(screen.getByText('未保存 1 项')).toBeTruthy();
    expect(screen.queryByText(/仅当前会话生效/)).toBeNull();
    confirm.mockRestore();
  });

  it('清空本会话覆盖只打会话端点', async () => {
    renderPanel({ session: 's1' });
    await screen.findByLabelText('上下文窗口');
    fireEvent.click(screen.getByRole('button', { name: '本次会话' }));
    fireEvent.click(screen.getByRole('button', { name: '清空本会话覆盖' }));
    await waitFor(() => expect(clearSession).toHaveBeenCalledWith('s1'));
  });

  it('读取失败时给出可重试的错误块', async () => {
    configMock.mockRejectedValue(new Error('500'));
    renderPanel();
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy();
  });
});
