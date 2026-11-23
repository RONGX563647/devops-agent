// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { deleteSecret, fetchSecrets } from '../api/client';
import type { SecretMeta } from '../types';
import { SecretsTab } from './SecretsTab';

vi.mock('../api/client', () => ({
  fetchSecrets: vi.fn(),
  deleteSecret: vi.fn(),
}));

const fetchMock = vi.mocked(fetchSecrets);
const deleteMock = vi.mocked(deleteSecret);

function secret(over: Partial<SecretMeta> = {}): SecretMeta {
  return {
    name: 'openai_key',
    hint: 'sk-…f3a9',
    source: 'input',
    created_at: '2026-11-23T09:00:00',
    last_used_at: null,
    uses: 0,
    ...over,
  };
}

/** 每个用例独立 QueryClient：缓存互不影响，失败也不重试以免拖慢。 */
function renderTab() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SecretsTab />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  deleteMock.mockReset();
  deleteMock.mockResolvedValue(undefined);
});

afterEach(cleanup);

describe('SecretsTab', () => {
  it('保险箱为空时给出去哪产生密钥的引导', async () => {
    fetchMock.mockResolvedValue([]);
    renderTab();

    expect(await screen.findByText(/保险箱是空的/)).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('只展示元信息，不出现明文列', async () => {
    fetchMock.mockResolvedValue([secret({ uses: 3, last_used_at: '2026-11-23T10:30:00' })]);
    renderTab();

    expect(await screen.findByText('openai_key')).toBeTruthy();
    expect(screen.getByText('sk-…f3a9')).toBeTruthy();
    expect(screen.getByText('3')).toBeTruthy();
    // 表头固定六列：名字 / 提示 / 来源 / 使用次数 / 最近使用 / 操作
    const heads = [...document.querySelectorAll('.secrets-table thead th')].map((th) =>
      th.textContent?.trim(),
    );
    expect(heads).toEqual(['名字', '提示', '来源', '使用次数', '最近使用', '']);
    expect(screen.queryByText(/明文值/)).toBeNull();
  });

  it('从未使用过的密钥在「最近使用」显示占位符', async () => {
    fetchMock.mockResolvedValue([secret()]);
    renderTab();
    expect(await screen.findByText('—')).toBeTruthy();
  });

  it('点删除按钮按名字删掉对应密钥', async () => {
    fetchMock.mockResolvedValue([secret({ name: 'aws_key' })]);
    renderTab();

    fireEvent.click(await screen.findByRole('button', { name: '删除' }));
    // mutationFn 会额外收到 react-query 的上下文，只断言第一个入参
    await waitFor(() => expect(deleteMock.mock.calls[0]?.[0]).toBe('aws_key'));
  });

  it('读取失败时提示密码库不可用', async () => {
    fetchMock.mockRejectedValue(new Error('vault broken'));
    renderTab();

    expect(await screen.findByText(/读取失败：密码库不可用/)).toBeTruthy();
  });
});
