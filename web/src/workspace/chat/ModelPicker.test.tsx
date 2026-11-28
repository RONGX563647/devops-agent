// @vitest-environment jsdom
/**
 * 会话内模型选择器（SPEC-0022 §5）：一个 chip、一份跨网关列表。
 *
 * 盯两件事：
 * - 用户想的是「换个模型」，所以列表按网关分组但只有一层（A-1）；
 * - 服务端拒绝「这个网关没有这个模型」时，要把候选直接摆出来让人改（A-3），
 *   而不是只弹一句「保存失败」。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as client from '../../api/client';
import { fetchModels, patchSessionModelConfig } from '../../api/client';
import { AGGREGATE, modelConfig } from '../settings/fixtures';
import { ModelPicker } from './ModelPicker';

const { ApiError } = client;

vi.mock('../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client');
  return {
    ...actual,
    fetchModels: vi.fn(),
    patchSessionModelConfig: vi.fn(),
  };
});

const modelsMock = vi.mocked(fetchModels);
const saveMock = vi.mocked(patchSessionModelConfig);

function renderPicker(onChanged = vi.fn()) {
  render(
    <MemoryRouter initialEntries={['/?tab=chat&session=s1']}>
      <ModelPicker session="s1" config={modelConfig()} onChanged={onChanged} />
    </MemoryRouter>,
  );
  return onChanged;
}

beforeEach(() => {
  modelsMock.mockReset();
  saveMock.mockReset();
  modelsMock.mockResolvedValue(AGGREGATE);
  saveMock.mockResolvedValue({ ...modelConfig(), overrides: {} });
});

afterEach(cleanup);

describe('chip', () => {
  it('显示当前模型与窗口，点开才去向网关要列表', async () => {
    renderPicker();
    expect(screen.getByText('qwen3-max')).toBeTruthy();
    expect(screen.getByText('131,072')).toBeTruthy();
    // 打开前不打上游：一个 chip 不该在页面加载时惊动所有网关
    expect(modelsMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '选择模型' }));
    await waitFor(() => expect(modelsMock).toHaveBeenCalledTimes(1));
  });
});

/** 列表项里的模型名会与 chip 里的当前模型重名，所以按 role=option 取。 */
function option(name: string): HTMLElement {
  const found = screen
    .getAllByRole('option')
    .find((el) => el.textContent?.includes(name));
  if (!found) throw new Error(`没有找到模型项 ${name}`);
  return found;
}

describe('跨网关列表', () => {
  it('按网关分组，组头给出可用数与显示名（A-1）', async () => {
    renderPicker();
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }));
    expect(await screen.findByText('qwen-flash')).toBeTruthy();

    // 组头 + 底部「当前网关」都会写网管名，所以按出现次数断言
    expect(screen.getAllByText('百炼').length).toBeGreaterThan(0);
    expect(screen.getAllByText('内网 vLLM').length).toBeGreaterThan(0);
    expect(screen.getByText('2 个可用')).toBeTruthy();
    expect(screen.getAllByRole('option')).toHaveLength(3);
    // 窗口未知的项不留空，也不假装是 0
    expect(screen.getByText('窗口未知')).toBeTruthy();
  });

  it('搜索按模型名过滤，无结果时明说可以手填', async () => {
    renderPicker();
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }));
    await screen.findByText('qwen-flash');

    fireEvent.change(screen.getByLabelText('搜索模型'), { target: { value: 'glm' } });
    expect(screen.getAllByRole('option')).toHaveLength(1);
    expect(option('glm-4')).toBeTruthy();

    fireEvent.change(screen.getByLabelText('搜索模型'), { target: { value: 'zzz' } });
    expect(screen.getByText(/没有匹配的模型。可以手动输入名字。/)).toBeTruthy();
  });

  it('某个网关读不到时说出来，不静默少一组', async () => {
    modelsMock.mockResolvedValue({
      ...AGGREGATE,
      providers: [
        AGGREGATE.providers[0],
        { ...AGGREGATE.providers[1], code: 'unavailable', total: 0 },
      ],
    });
    renderPicker();
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }));
    expect(await screen.findByText(/1 个网关的模型没取到/)).toBeTruthy();
  });

  it('全部网关不可达时说「读不到」，而不是「没有模型」（A-13）', async () => {
    modelsMock.mockRejectedValue(new ApiError('unavailable', 503, null));
    renderPicker();
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }));
    expect(await screen.findByText(/读不到模型列表/)).toBeTruthy();
    expect(screen.queryByText(/没有匹配的模型/)).toBeNull();
  });
});

describe('切换与拒绝', () => {
  it('选中即提交：一次请求同时带 provider 与 model', async () => {
    const onChanged = renderPicker();
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }));
    fireEvent.click(await screen.findByText('glm-4'));

    await waitFor(() => expect(saveMock).toHaveBeenCalledWith('s1', { provider: 'vllm', model: 'glm-4' }));
    expect(onChanged).toHaveBeenCalled();
  });

  it('被拒时列出该网关的候选，点一个就重试（A-3）', async () => {
    saveMock
      .mockRejectedValueOnce(
        new ApiError('bad request', 400, {
          code: 'model_not_in_provider',
          provider: 'dashscope',
          model: 'qwen3-max',
          available: ['glm-4', 'glm-4-flash'],
        }),
      )
      .mockResolvedValueOnce({ ...modelConfig(), overrides: {} });

    renderPicker();
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }));
    await screen.findByText('qwen-flash');
    fireEvent.click(option('qwen3-max'));

    expect(await screen.findByText(/「qwen3-max」不在这个网关上，换一个：/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'glm-4-flash' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'glm-4-flash' }));
    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(2));
    // 重试用的是当前网关 + 候选名，不是原来那个错的组合
    expect(saveMock.mock.calls[1][1]).toEqual({ provider: 'dashscope', model: 'glm-4-flash' });
  });

  it('手填模型名也能提交：网关的 /models 常常不全', async () => {
    renderPicker();
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }));
    await screen.findByText('qwen-flash');
    fireEvent.click(screen.getByRole('button', { name: '手动输入模型名…' }));

    fireEvent.change(screen.getByLabelText('手动输入模型名'), {
      target: { value: '  qwen3-coder  ' },
    });
    fireEvent.click(screen.getByRole('button', { name: '使用' }));
    await waitFor(() =>
      expect(saveMock).toHaveBeenCalledWith('s1', { provider: 'dashscope', model: 'qwen3-coder' }),
    );
  });

  it('底部链接指向设置页的模型参数分区', async () => {
    renderPicker();
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }));
    const link = await screen.findByRole('link', { name: '模型参数 ›' });
    expect(link.getAttribute('href')).toBe('/?tab=settings&section=model');
  });
});
