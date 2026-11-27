// @vitest-environment jsdom
/** 模型设置抽屉：分类树、保存范围、来源链、危险确认、只读项。
 *
 * 这些用例盯的是「界面有没有撒谎」：
 * - 主按钮必须是「保存为全局默认」，会话保存降为次要并常驻提示「换会话失效」；
 * - 推算值直接用后端给的（前端自己乘一次就迟早和后端对不上）；
 * - 徽标点开要能看到每一层的原值，而不是一个孤零零的「配置文件」；
 * - 有未保存改动时 Esc 不能静默丢弃。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ModelConfig, ModelLayers, ModelValues } from '../types';
import { ModelConfigDrawer } from './model-config/Drawer';

const saveSession = vi.fn();
const saveDefaults = vi.fn();
const clearSession = vi.fn();
const loadLayers = vi.fn();

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  patchSessionModelConfig: (...args: unknown[]) => saveSession(...args),
  patchModelConfig: (...args: unknown[]) => saveDefaults(...args),
  clearSessionModelConfig: (...args: unknown[]) => clearSession(...args),
  fetchModelLayers: (...args: unknown[]) => loadLayers(...args),
}));

const VALUES: ModelValues = {
  model: 'qwen3.8-flash',
  context_size: 128_000,
  temperature: null,
  max_tokens: null,
  top_p: null,
  reasoning_effort: null,
  thinking_enable: null,
  parallel_tool_calls: true,
  max_iters: 12,
  model_max_retries: 3,
  agent_max_retries: 3,
  trigger_ratio: 0.8,
  reserve_ratio: 0.1,
  context_buffer_ratio: 0.2,
};

const FILE_PATH = 'E:/code/devops-agent/config/model.local.yaml';

function config(over: Partial<ModelConfig> = {}): ModelConfig {
  return {
    effective: VALUES,
    sources: {
      model: 'env',
      context_size: 'framework-default',
      temperature: 'framework-default',
      max_iters: 'yaml',
      model_max_retries: 'yaml',
      agent_max_retries: 'yaml',
    },
    bounds: {
      temperature: { kind: 'float', nullable: true, min: 0, max: 2, step: 0.1 },
      context_size: { kind: 'int', nullable: false, min: 8192, max: 2_000_000, step: 1024 },
      max_iters: { kind: 'int', nullable: false, min: 1, max: 200, step: 1 },
    },
    editable: ['model', 'temperature', 'context_size'],
    readonly: {
      api_key: '环境变量 DEVOPS_AGENT_API_KEY（重启生效）',
      stream: '固定 true',
    },
    derived: {
      compression_threshold: 102_400,
      worst_case_attempts: 16,
      context_uncalibrated: true,
    },
    warnings: [],
    version: 3,
    file_path: FILE_PATH,
    ...over,
  };
}

const layers: ModelLayers = {
  version: 3,
  file_path: FILE_PATH,
  layers: [
    { id: 'session', label: '本次会话', writable: true, values: { temperature: 0.2 } },
    {
      id: 'file',
      label: 'config/model.local.yaml',
      writable: true,
      path: FILE_PATH,
      values: { model: 'qwen3.8-flash' },
    },
    { id: 'stored', label: '数据库旧值（迁移残留）', writable: true, values: {} },
    {
      id: 'base',
      label: 'config/model.yaml / 环境变量 / 框架默认',
      writable: false,
      values: { context_size: 128_000 },
    },
  ],
  effective: VALUES,
  sources: {},
};

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function mount(cfg: ModelConfig | undefined, contextTokens: number | null = null) {
  render(
    <QueryClientProvider client={queryClient}>
      <ModelConfigDrawer
        session="s1"
        config={cfg}
        contextTokens={contextTokens}
        onChanged={() => undefined}
      />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole('button', { name: '模型设置' }));
  return document.querySelector('dialog.mcd') as HTMLDialogElement;
}

beforeEach(() => {
  saveSession.mockReset();
  saveDefaults.mockReset();
  clearSession.mockReset();
  loadLayers.mockReset();
  saveSession.mockResolvedValue({ effective_from: '下一次模型调用' });
  saveDefaults.mockResolvedValue({
    affected: { applied: 2, deferred: 1 },
    file_path: FILE_PATH,
  });
  clearSession.mockResolvedValue({});
  loadLayers.mockResolvedValue(layers);
});

afterEach(cleanup);

describe('ModelConfigDrawer', () => {
  it('标题下常驻显示落盘位置与会话覆盖条数', () => {
    mount(config({ overrides: { temperature: 0.2, max_iters: 30 } }));
    expect(screen.getByText(/本次会话覆盖 2 项（换会话失效）/)).toBeTruthy();
    expect(screen.getByText('config/model.local.yaml')).toBeTruthy();
    // 全路径放 title：鼠标悬停能确认写到哪
    expect(screen.getByTitle(FILE_PATH)).toBeTruthy();
  });

  it('左树切换分组：一次只显示一组的表单', () => {
    mount(config());
    expect(screen.getByLabelText('上下文窗口')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /采样/ }));
    expect(screen.getByLabelText('温度')).toBeTruthy();
    expect(screen.getByLabelText('top_p')).toBeTruthy();
    expect(screen.queryByLabelText('上下文窗口')).toBeNull();
  });

  it('点二级节点跳到对应字段并高亮', () => {
    mount(config());
    fireEvent.click(screen.getByRole('button', { name: /采样/ }));
    fireEvent.click(screen.getByRole('button', { name: '温度' }));
    expect(screen.getByRole('button', { name: '温度' }).getAttribute('aria-current')).toBe('true');
  });

  it('每行给出处：yaml / env / framework-default 徽标', () => {
    mount(config());
    expect(screen.getAllByText('环境变量').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: /采样/ }));
    // 来源是框架默认的项要显示成「未配置」，这是「该确认一下」的提示
    expect(screen.getAllByText('未配置').length).toBeGreaterThan(0);
  });

  it('上下文窗口未校准时给出警告', () => {
    mount(config());
    expect(screen.getByText(/是框架默认值，没有经过确认/)).toBeTruthy();
  });

  it('推算值直接用后端给的，前端不自己乘', () => {
    mount(
      config({
        derived: {
          compression_threshold: 77_777,
          worst_case_attempts: 99,
          context_uncalibrated: false,
        },
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: /执行/ }));
    // 若前端自己乘 (1+3)×(1+3)=16，这里就会是 16 而不是 99
    expect(screen.getByText(/最坏 99 次模型请求/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /上下文压缩/ }));
    expect(screen.getByText(/阈值 77,777 tokens/)).toBeTruthy();
  });

  it('主按钮是保存为全局默认，且写文件前先二次确认', async () => {
    mount(config());
    fireEvent.change(screen.getByLabelText('上下文窗口'), { target: { value: '64000' } });
    // 改窗口属于危险项，要先勾确认
    fireEvent.click(screen.getByLabelText('我了解上面的影响'));
    fireEvent.click(screen.getByRole('button', { name: '保存为全局默认' }));
    expect(screen.getByText(/将写入/)).toBeTruthy();
    expect(saveDefaults).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '确认写入' }));
    await waitFor(() => expect(saveDefaults).toHaveBeenCalledTimes(1));
    // 乐观并发：必须带上读到的 version
    expect(saveDefaults.mock.calls[0][1]).toBe(3);
    await waitFor(() => expect(screen.getByText(/换会话仍在/)).toBeTruthy());
  });

  it('仅本次会话走会话端点，不碰全局默认', async () => {
    mount(config());
    fireEvent.click(screen.getByRole('button', { name: /采样/ }));
    fireEvent.change(screen.getByLabelText('温度'), { target: { value: '0.3' } });
    fireEvent.click(screen.getByRole('button', { name: '仅本次会话' }));
    await waitFor(() => expect(saveSession).toHaveBeenCalledTimes(1));
    expect(saveSession.mock.calls[0][0]).toBe('s1');
    expect(saveSession.mock.calls[0][1]).toEqual({ temperature: 0.3 });
    expect(saveDefaults).not.toHaveBeenCalled();
  });

  it('越界时禁用保存（服务端仍是唯一裁判）', () => {
    mount(config());
    fireEvent.click(screen.getByRole('button', { name: /采样/ }));
    fireEvent.change(screen.getByLabelText('温度'), { target: { value: '3' } });
    expect(screen.getByText(/temperature 不能大于 2/)).toBeTruthy();
    const save = screen.getByRole('button', { name: '仅本次会话' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
  });

  it('改危险项要先勾选确认', () => {
    mount(config());
    fireEvent.change(screen.getByLabelText('模型名'), { target: { value: 'another-model' } });
    expect(screen.getByText(/下一次模型调用即生效/)).toBeTruthy();
    const save = screen.getByRole('button', { name: '保存为全局默认' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText('我了解上面的影响'));
    expect(save.disabled).toBe(false);
  });

  it('只读项不可编辑但可见，并说明在哪改', () => {
    mount(config());
    fireEvent.click(screen.getByRole('button', { name: /高级与只读/ }));
    expect(screen.queryByLabelText('api_key')).toBeNull();
    expect(screen.getAllByText('不可在界面修改').length).toBeGreaterThan(0);
    expect(screen.getByText(/DEVOPS_AGENT_API_KEY/)).toBeTruthy();
  });

  it('恢复本会话只清会话覆盖', async () => {
    mount(config());
    fireEvent.click(screen.getByRole('button', { name: '恢复本会话' }));
    await waitFor(() => expect(clearSession).toHaveBeenCalledWith('s1'));
  });

  it('草稿未保存时 Esc 不静默丢弃改动', () => {
    const dialog = mount(config());
    fireEvent.click(screen.getByRole('button', { name: /采样/ }));
    fireEvent.change(screen.getByLabelText('温度'), { target: { value: '0.3' } });
    const cancel = new Event('cancel', { cancelable: true });
    fireEvent(dialog, cancel);
    expect(cancel.defaultPrevented).toBe(true);
    expect(screen.getByText(/有未保存的改动/)).toBeTruthy();
  });

  it('点徽标加载并显示四层链路', async () => {
    mount(config());
    fireEvent.click(screen.getByRole('button', { name: '模型名 的来源' }));
    await waitFor(() => expect(loadLayers).toHaveBeenCalledWith('s1'));
    const chain = await screen.findByLabelText('模型名 的来源链');
    expect(chain.textContent).toContain('本次会话');
    expect(chain.textContent).toContain('config/model.local.yaml');
    expect(chain.textContent).toContain('只读');
  });

  it('清空上下文窗口后保存：发 null 删覆盖，而不是写回继承值', async () => {
    mount(config(), 0);
    fireEvent.change(screen.getByLabelText('上下文窗口'), { target: { value: '' } });
    fireEvent.click(screen.getByLabelText('我了解上面的影响'));
    fireEvent.click(screen.getByRole('button', { name: '仅本次会话' }));
    await waitFor(() => expect(saveSession).toHaveBeenCalledTimes(1));
    expect(saveSession.mock.calls[0][1]).toEqual({ context_size: null });
  });

  it('模型名保存前 trim 掉首尾空格', async () => {
    mount(config());
    const input = screen.getByLabelText('模型名');
    fireEvent.change(input, { target: { value: '  qwen3.8-flash  ' } });
    fireEvent.blur(input);
    fireEvent.click(screen.getByRole('button', { name: '仅本次会话' }));
    await waitFor(() => expect(saveSession).toHaveBeenCalledTimes(1));
    expect(saveSession.mock.calls[0][1]).toEqual({ model: 'qwen3.8-flash' });
  });

  it('清空模型名保存为 null（删覆盖，而不是写空模型名）', async () => {
    mount(config());
    fireEvent.change(screen.getByLabelText('模型名'), { target: { value: '' } });
    fireEvent.click(screen.getByLabelText('我了解上面的影响'));
    fireEvent.click(screen.getByRole('button', { name: '仅本次会话' }));
    await waitFor(() => expect(saveSession).toHaveBeenCalledTimes(1));
    expect(saveSession.mock.calls[0][1]).toEqual({ model: null });
  });
});
