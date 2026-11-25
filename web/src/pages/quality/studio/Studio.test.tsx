// @vitest-environment jsdom
/**
 * 用例与模型视图：配置保存、用例编辑、以及两类「不该悄悄通过」的失败。
 *
 * 这里刻意不测样式，只测契约：保存时发出去的是什么、非法输入有没有被挡住、
 * 服务端拒绝时错误有没有显示出来。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  deleteOpenJudgeCase,
  fetchOpenJudgeCaseDocument,
  fetchOpenJudgeLibrary,
  fetchOpenJudgeSettings,
  patchOpenJudgeSettings,
  restoreOpenJudgeCase,
  saveOpenJudgeCase,
} from '../../../api/client';
import { Studio } from './Studio';

vi.mock('../../../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number;
    detail: unknown;
    constructor(message: string, status: number, detail: unknown) {
      super(message);
      this.status = status;
      this.detail = detail;
    }
  },
  fetchOpenJudgeSettings: vi.fn(),
  patchOpenJudgeSettings: vi.fn(),
  fetchOpenJudgeLibrary: vi.fn(),
  fetchOpenJudgeCaseDocument: vi.fn(),
  saveOpenJudgeCase: vi.fn(),
  deleteOpenJudgeCase: vi.fn(),
  restoreOpenJudgeCase: vi.fn(),
}));

const mockSettings = vi.mocked(fetchOpenJudgeSettings);
const mockPatch = vi.mocked(patchOpenJudgeSettings);
const mockLibrary = vi.mocked(fetchOpenJudgeLibrary);
const mockDoc = vi.mocked(fetchOpenJudgeCaseDocument);
const mockSave = vi.mocked(saveOpenJudgeCase);
const mockDelete = vi.mocked(deleteOpenJudgeCase);
const mockRestore = vi.mocked(restoreOpenJudgeCase);

const LIMITS = {
  max_concurrency: 4,
  min_timeout: 30,
  max_timeout: 900,
  max_limit: 30,
  tracks: ['A', 'B'],
  risks: ['low'],
  permissions: ['readonly'],
};

function settingsPayload() {
  return {
    values: {
      target_model: '',
      judge_model: '',
      judge: false,
      concurrency: 2,
      timeout: 180,
    },
    effective: { target_model: 'deepseek-v4.1-flash', judge_model: 'qwen3.8-flash' },
    limits: LIMITS,
  };
}

function libraryPayload() {
  return {
    items: [
      {
        id: 'a-1',
        track: 'A',
        risk: 'low',
        permission: 'readonly',
        subject: 'repo_qa',
        fixture: 'python-basic',
        offline: false,
        source: 'repo' as const,
        edited: false,
      },
      {
        id: 'a-2',
        track: 'A',
        risk: 'low',
        permission: 'readonly',
        subject: 'repo_qa',
        fixture: 'python-basic',
        offline: false,
        source: 'overlay' as const,
        edited: true,
      },
    ],
    total: 2,
    suite: { cases: 2, suite_sha: 'abcdef1234567890', overlay: 1, deleted: 0, dir: '/tmp/x' },
    thresholds: { A: { pass_rate: 0.6 } },
    limits: LIMITS,
  };
}

function caseDoc(raw: Record<string, unknown>) {
  return { case: { _source: 'repo' as const, _edited: false, ...raw }, limits: LIMITS };
}

function renderStudio() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Studio />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockSettings.mockResolvedValue(settingsPayload());
  mockLibrary.mockResolvedValue(libraryPayload());
  mockDoc.mockResolvedValue(caseDoc({ id: 'a-1', track: 'A', turns: ['你好'] }));
  mockPatch.mockResolvedValue(settingsPayload());
  mockSave.mockResolvedValue(caseDoc({ id: 'a-1', track: 'A', turns: ['改过'] }));
  mockDelete.mockResolvedValue({ id: 'a-1', suite: libraryPayload().suite });
  mockRestore.mockResolvedValue(caseDoc({ id: 'a-1', track: 'A', turns: ['你好'] }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('评测模型配置', () => {
  it('显示解析后的生效模型，避免「没填但在跑」说不清', async () => {
    renderStudio();
    expect(await screen.findByText(/实际生效：deepseek/)).toBeTruthy();
    expect(screen.getByText(/实际生效：qwen3/)).toBeTruthy();
  });

  it('保存时把表单值发给 PATCH', async () => {
    renderStudio();
    // 等配置真正加载完：加载中按钮是禁用的，先点等于什么都没做。
    await screen.findByText(/实际生效：deepseek/);
    const input = (await screen.findByLabelText('被测模型')) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'my-eval-model' } });
    fireEvent.click(screen.getByText('保存配置'));

    await waitFor(() => expect(mockPatch).toHaveBeenCalled());
    expect(mockPatch.mock.calls[0][0]).toMatchObject({ target_model: 'my-eval-model' });
  });

  it('服务端 422 时把错误显示出来', async () => {
    const { ApiError } = await import('../../../api/client');
    mockPatch.mockRejectedValueOnce(new ApiError('bad', 422, 'concurrency 必须在 1..4 之间'));
    renderStudio();
    await screen.findByText(/实际生效：deepseek/);
    fireEvent.click(screen.getByText('保存配置'));
    expect(await screen.findByText(/concurrency 必须在 1\.\.4/)).toBeTruthy();
  });
});

describe('题库列表', () => {
  it('区分仓库原版与本地改动', async () => {
    renderStudio();
    expect(await screen.findByText('仓库')).toBeTruthy();
    expect(screen.getByText('改过')).toBeTruthy();
    expect(screen.getByText(/本地改动 1 条/)).toBeTruthy();
  });

  it('点一条用例会打开编辑器', async () => {
    renderStudio();
    fireEvent.click(await screen.findByText('a-1'));
    await waitFor(() => expect(mockDoc).toHaveBeenCalledWith('a-1'));
    expect(await screen.findByLabelText('用例 JSON')).toBeTruthy();
  });
});

describe('用例编辑器', () => {
  async function openEditor() {
    renderStudio();
    fireEvent.click(await screen.findByText('a-1'));
    return (await screen.findByLabelText('用例 JSON')) as HTMLTextAreaElement;
  }

  it('改完保存会把解析后的对象发出去', async () => {
    const area = await openEditor();
    fireEvent.change(area, {
      target: { value: JSON.stringify({ id: 'a-1', track: 'A', turns: ['改过'] }) },
    });
    fireEvent.click(screen.getByText('保存'));
    await waitFor(() => expect(mockSave).toHaveBeenCalled());
    const [id, raw] = mockSave.mock.calls[0];
    expect(id).toBe('a-1');
    expect(raw).toMatchObject({ turns: ['改过'] });
  });

  it('非法 JSON 不发请求，只报解析失败', async () => {
    const area = await openEditor();
    fireEvent.change(area, { target: { value: '{ 这不是 JSON' } });
    fireEvent.click(screen.getByText('保存'));
    expect(await screen.findByText(/JSON 解析失败/)).toBeTruthy();
    expect(mockSave).not.toHaveBeenCalled();
  });

  it('服务端拒绝结构错误时显示原因', async () => {
    const { ApiError } = await import('../../../api/client');
    mockSave.mockRejectedValueOnce(new ApiError('bad', 422, 'a-1: 未知赛道 Z'));
    const area = await openEditor();
    fireEvent.change(area, { target: { value: JSON.stringify({ id: 'a-1', track: 'Z' }) } });
    fireEvent.click(screen.getByText('保存'));
    expect(await screen.findByText(/未知赛道/)).toBeTruthy();
  });

  it('隐藏与恢复都走对应接口', async () => {
    await openEditor();
    fireEvent.click(screen.getByText('从题库隐藏'));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith('a-1'));

    fireEvent.click(screen.getByText('恢复仓库版本'));
    await waitFor(() => expect(mockRestore).toHaveBeenCalledWith('a-1'));
  });
});
