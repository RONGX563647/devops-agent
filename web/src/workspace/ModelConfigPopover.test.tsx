// @vitest-environment jsdom
/** 模型设置面板：来源徽标、两个保存按钮、危险确认、越界禁用、只读项。

这些用例盯的是「界面有没有撒谎」：
- 推算值必须用后端给的（前端自己乘一次就迟早和后端对不上）；
- 「保存到本会话」与「设为部署默认」必须走不同端点，且后者要二次确认；
- 越界时保存要禁用，但服务端仍是唯一裁判。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ModelConfig, ModelValues } from '../types';
import { ModelConfigPopover } from './model-config/Popover';

const saveSession = vi.fn();
const saveDefaults = vi.fn();
const clearSession = vi.fn();

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  patchSessionModelConfig: (...args: unknown[]) => saveSession(...args),
  patchModelConfig: (...args: unknown[]) => saveDefaults(...args),
  clearSessionModelConfig: (...args: unknown[]) => clearSession(...args),
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
    ...over,
  };
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function mount(cfg: ModelConfig | undefined, contextTokens: number | null = null) {
  render(
    <QueryClientProvider client={queryClient}>
      <ModelConfigPopover
        session="s1"
        config={cfg}
        contextTokens={contextTokens}
        onChanged={() => undefined}
      />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole('button', { name: '模型设置' }));
}

beforeEach(() => {
  saveSession.mockReset();
  saveDefaults.mockReset();
  clearSession.mockReset();
  saveSession.mockResolvedValue({ effective_from: '下一次模型调用' });
  saveDefaults.mockResolvedValue({ affected: { applied: 2, deferred: 1 } });
  clearSession.mockResolvedValue({});
});

afterEach(cleanup);

describe('ModelConfigPopover', () => {
  it('每行给出处：yaml / env / framework-default 徽标', () => {
    mount(config());
    expect(screen.getAllByText('环境变量').length).toBeGreaterThan(0);
    expect(screen.getAllByText('配置文件').length).toBeGreaterThan(0);
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
    // 若前端自己乘 (1+3)×(1+3)=16，这里就会是 16 而不是 99
    expect(screen.getByText(/最坏 99 次模型请求/)).toBeTruthy();
    expect(screen.getByText(/阈值 77,777 tokens/)).toBeTruthy();
  });

  it('有会话覆盖时标题标出条数', () => {
    mount(config({ overrides: { temperature: 0.2, max_iters: 30 } }));
    expect(screen.getByText('本会话 2 项')).toBeTruthy();
  });

  it('保存到本会话不碰部署默认', async () => {
    mount(config());
    fireEvent.change(screen.getByLabelText('温度'), { target: { value: '0.3' } });
    fireEvent.click(screen.getByRole('button', { name: '保存到本会话' }));
    await waitFor(() => expect(saveSession).toHaveBeenCalledTimes(1));
    expect(saveSession.mock.calls[0][0]).toBe('s1');
    expect(saveSession.mock.calls[0][1]).toEqual({ temperature: 0.3 });
    expect(saveDefaults).not.toHaveBeenCalled();
  });

  it('设为部署默认先二次确认，确认后才写', async () => {
    mount(config());
    fireEvent.change(screen.getByLabelText('温度'), { target: { value: '0.4' } });
    fireEvent.click(screen.getByRole('button', { name: '设为部署默认' }));
    expect(screen.getByText(/将影响所有会话/)).toBeTruthy();
    expect(saveDefaults).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '确认写入' }));
    await waitFor(() => expect(saveDefaults).toHaveBeenCalledTimes(1));
    // 乐观并发：必须带上读到的 version
    expect(saveDefaults.mock.calls[0][1]).toBe(3);
  });

  it('越界时禁用保存（服务端仍是唯一裁判）', () => {
    mount(config());
    fireEvent.change(screen.getByLabelText('温度'), { target: { value: '3' } });
    expect(screen.getByText(/temperature 不能大于 2/)).toBeTruthy();
    const save = screen.getByRole('button', { name: '保存到本会话' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
  });

  it('改危险项要先勾选确认', () => {
    mount(config());
    fireEvent.change(screen.getByLabelText('模型名'), { target: { value: 'another-model' } });
    expect(screen.getByText(/下一次模型调用即生效/)).toBeTruthy();
    const save = screen.getByRole('button', { name: '保存到本会话' }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText('我了解上面的影响'));
    expect(save.disabled).toBe(false);
  });

  it('只读项不可编辑但可见，并说明在哪改', () => {
    mount(config());
    // 只读行只有说明文字，没有输入框
    expect(screen.queryByLabelText('api_key')).toBeNull();
    expect(screen.getAllByText('不可在界面修改').length).toBeGreaterThan(0);
    expect(screen.getByText(/DEVOPS_AGENT_API_KEY/)).toBeTruthy();
  });

  it('恢复默认只清本会话', async () => {
    mount(config());
    fireEvent.click(screen.getByRole('button', { name: '恢复默认' }));
    await waitFor(() => expect(clearSession).toHaveBeenCalledWith('s1'));
  });

  it('清空上下文窗口后保存：发 null 删覆盖，而不是写回继承值', async () => {
    mount(config(), 0);
    fireEvent.change(screen.getByLabelText('上下文窗口'), { target: { value: '' } });
    // 改窗口属于危险项，要先勾确认；重点是请求体里不能出现继承来的 128000
    fireEvent.click(screen.getByLabelText('我了解上面的影响'));
    fireEvent.click(screen.getByRole('button', { name: '保存到本会话' }));
    await waitFor(() => expect(saveSession).toHaveBeenCalledTimes(1));
    expect(saveSession.mock.calls[0][1]).toEqual({ context_size: null });
  });

  it('Esc 关闭面板（键盘用户不能只能靠点外面）', () => {
    mount(config());
    expect(screen.queryByRole('dialog', { name: '模型设置' })).toBeTruthy();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: '模型设置' })).toBeNull();
  });

  it('模型名保存前 trim 掉首尾空格', async () => {
    mount(config());
    const input = screen.getByLabelText('模型名');
    fireEvent.change(input, { target: { value: '  qwen3.8-flash  ' } });
    // 离开输入框时才归一：空白消除后与生效值一致，危险提示也随之消失
    fireEvent.blur(input);
    fireEvent.click(screen.getByRole('button', { name: '保存到本会话' }));
    await waitFor(() => expect(saveSession).toHaveBeenCalledTimes(1));
    expect(saveSession.mock.calls[0][1]).toEqual({ model: 'qwen3.8-flash' });
  });

  it('清空模型名保存为 null（删覆盖，而不是写空模型名）', async () => {
    mount(config());
    fireEvent.change(screen.getByLabelText('模型名'), { target: { value: '' } });
    fireEvent.click(screen.getByLabelText('我了解上面的影响'));
    fireEvent.click(screen.getByRole('button', { name: '保存到本会话' }));
    await waitFor(() => expect(saveSession).toHaveBeenCalledTimes(1));
    expect(saveSession.mock.calls[0][1]).toEqual({ model: null });
  });
});
