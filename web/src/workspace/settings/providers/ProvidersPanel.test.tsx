// @vitest-environment jsdom
/**
 * 网关页（SPEC-0022 §6）：列表、增删改、探活。
 *
 * 三道闸都在这层被看见：
 * - 明文密钥不许贴进来（服务端硬拦，前端先说一次，省一次往返）；
 * - 删默认网关必须指定接任者，否则会话会指向一个不存在的家；
 * - 服务端 code 翻成人话，同时不吞掉认不出的码。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as client from '../../../api/client';
import {
  checkProvider,
  createProvider,
  deleteProvider,
  fetchProviderDraft,
  fetchProviders,
  updateProvider,
} from '../../../api/client';
import { PROVIDERS } from '../fixtures';
import { ProvidersPanel } from './ProvidersPanel';

const { ApiError } = client;
vi.mock('../../../api/client', async () => {
  const actual = await vi.importActual<typeof import('../../../api/client')>('../../../api/client');
  return {
    ...actual,
    fetchProviders: vi.fn(),
    fetchProviderDraft: vi.fn(),
    createProvider: vi.fn(),
    updateProvider: vi.fn(),
    deleteProvider: vi.fn(),
    checkProvider: vi.fn(),
  };
});

const listMock = vi.mocked(fetchProviders);
const draftMock = vi.mocked(fetchProviderDraft);
const createMock = vi.mocked(createProvider);
const updateMock = vi.mocked(updateProvider);
const deleteMock = vi.mocked(deleteProvider);
const checkMock = vi.mocked(checkProvider);

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const dirty = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <ProvidersPanel onDirtyChange={dirty} />
    </QueryClientProvider>,
  );
  return dirty;
}

beforeEach(() => {
  listMock.mockReset();
  draftMock.mockReset();
  createMock.mockReset();
  updateMock.mockReset();
  deleteMock.mockReset();
  checkMock.mockReset();

  listMock.mockResolvedValue(PROVIDERS);
  draftMock.mockResolvedValue({
    id: 'dashscope',
    kind: 'openai_chat',
    label: '百炼',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    api_key_ref: 'DASHSCOPE_API_KEY',
    discover: 'models',
  });
  checkMock.mockResolvedValue({
    id: 'dashscope',
    code: 'ok',
    http_status: 200,
    total: 3,
    latency_ms: 123,
  });
  deleteMock.mockResolvedValue({ version: 'a1b2c3d4e5f6' });
});

afterEach(cleanup);

describe('网关清单', () => {
  it('列出网关，标出默认、缺 key 与模型数', async () => {
    renderPanel();
    expect(await screen.findByText('百炼')).toBeTruthy();
    expect(screen.getByText('默认')).toBeTruthy();
    expect(screen.getByText('已配 key')).toBeTruthy();
    expect(screen.getByText('缺 key')).toBeTruthy();
    expect(screen.getByText(/53 个模型/)).toBeTruthy();
    expect(screen.getByText(/还没有模型列表/)).toBeTruthy();
  });

  it('明说保存会重排文件、密钥只写变量名（不假装能保住手写注释）', async () => {
    renderPanel();
    await screen.findByText('百炼');
    expect(screen.getByText(/手写注释会丢失/)).toBeTruthy();
    expect(screen.getByText(/密钥只写变量名/)).toBeTruthy();
  });

  it('探活只读：显示状态与耗时', async () => {
    renderPanel();
    await screen.findByText('百炼');
    fireEvent.click(screen.getAllByRole('button', { name: '探活' })[0]);
    await waitFor(() => expect(checkMock).toHaveBeenCalledWith('dashscope'));
    expect(await screen.findByText(/探活 可达 123ms \/ 3 个/)).toBeTruthy();
  });
});

describe('新增与编辑', () => {
  it('新增时打开表单，并把未保存状态报给外壳', async () => {
    const dirty = renderPanel();
    await screen.findByText('百炼');
    fireEvent.click(screen.getByRole('button', { name: '新增网关' }));
    expect(await screen.findByRole('heading', { name: '新增网关' })).toBeTruthy();
    expect(dirty).toHaveBeenCalledWith(true);

    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => expect(dirty).toHaveBeenCalledWith(false));
  });

  it('明文密钥在前端就被拦下，不发出请求（A-5）', async () => {
    renderPanel();
    await screen.findByText('百炼');
    fireEvent.click(screen.getByRole('button', { name: '新增网关' }));
    await screen.findByRole('heading', { name: '新增网关' });

    fireEvent.change(document.querySelector('input[pattern]') as HTMLInputElement, {
      target: { value: 'proxy' },
    });
    fireEvent.change(screen.getByPlaceholderText(/DASHSCOPE_API_KEY/), {
      target: { value: 'sk-abcdef0123456789' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存到 providers.yaml' }));

    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByText(/不要粘贴密钥/)).toBeTruthy();
    expect(createMock).not.toHaveBeenCalled();
  });

  it('服务端 409 翻成「文件被别处改过」，不暴露 code', async () => {
    createMock.mockRejectedValue(
      new ApiError('conflict', 409, { code: 'providers_version_conflict' }),
    );
    renderPanel();
    await screen.findByText('百炼');
    fireEvent.click(screen.getByRole('button', { name: '新增网关' }));
    await screen.findByRole('heading', { name: '新增网关' });
    fireEvent.change(document.querySelector('input[pattern]') as HTMLInputElement, {
      target: { value: 'proxy' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存到 providers.yaml' }));

    expect(await screen.findByText(/文件已被别处改过/)).toBeTruthy();
  });

  it('编辑时带上内容版本号做乐观锁（id 不可改）', async () => {
    updateMock.mockResolvedValue(PROVIDERS);
    renderPanel();
    await screen.findByText('百炼');
    fireEvent.click(screen.getAllByRole('button', { name: '编辑' })[0]);
    const idInput = (await screen.findByDisplayValue('dashscope')) as HTMLInputElement;
    expect(idInput.disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '保存到 providers.yaml' }));
    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    expect(updateMock.mock.calls[0][1].expected_version).toBe('1f3a9c2b7e04');
  });
});

describe('删除', () => {
  it('删默认网关必须先选接任者，且把接任者一起发出去（A-7）', async () => {
    renderPanel();
    await screen.findByText('百炼');
    fireEvent.click(screen.getAllByRole('button', { name: '删除' })[0]);
    expect(await screen.findByText(/这是默认网关，删除后默认项交给/)).toBeTruthy();
    const picker = (await screen.findByLabelText('接任的默认网关')) as HTMLSelectElement;
    expect(picker.value).toBe('vllm');

    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await waitFor(() =>
      expect(deleteMock).toHaveBeenCalledWith('dashscope', '1f3a9c2b7e04', 'vllm'),
    );
  });

  it('删非默认网关不带 reassign', async () => {
    renderPanel();
    await screen.findByText('百炼');
    fireEvent.click(screen.getAllByRole('button', { name: '删除' })[1]);
    expect(await screen.findByText(/文件会立即重写/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith('vllm', '1f3a9c2b7e04', undefined));
  });

  it('取消删除不动文件', async () => {
    renderPanel();
    await screen.findByText('百炼');
    fireEvent.click(screen.getAllByRole('button', { name: '删除' })[1]);
    fireEvent.click(await screen.findByRole('button', { name: '取消' }));
    expect(screen.queryByRole('button', { name: '确认删除' })).toBeNull();
    expect(deleteMock).not.toHaveBeenCalled();
  });
});
