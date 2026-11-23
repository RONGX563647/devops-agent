// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchToolSchemas } from '../api/client';
import type { TraceStep } from '../types';
import { TraceInspector } from './TraceInspector';

vi.mock('../api/client', () => ({ fetchToolSchemas: vi.fn() }));

const STEP: TraceStep = {
  id: 'tool:t1',
  kind: 'tool',
  layer: 'tool',
  index: 4,
  title: 'Bash',
  text: '',
  at: 0,
  durationMs: 4_400,
  measured: true,
  status: 'success',
  toolName: 'Bash',
  toolCallId: 'tc_04',
  args: '{"command":"npm run build"}',
  result: 'built in 4.4s',
  argsSummary: 'command: npm run build',
  resultSummary: 'built in 4.4s',
  tokensIn: 1_200,
  tokensOut: 240,
};

const TURN = {
  id: 'u1',
  index: 3,
  question: '构建一下',
  userText: '构建一下',
  at: 0,
  startedAt: 0,
  endedAt: 30_000,
  steps: 7,
  tools: 2,
  failed: false,
  items: [],
  spans: [],
  tokens: { in: 0, out: 0, cache: 0 },
  errorCount: 0,
};

function wrap(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

beforeEach(() => {
  vi.mocked(fetchToolSchemas).mockResolvedValue([
    {
      type: 'function',
      function: {
        name: 'Bash',
        description: '执行 shell 命令并返回输出。',
        parameters: { type: 'object', properties: { command: { type: 'string' } } },
      },
    },
  ]);
});
afterEach(cleanup);

describe('TraceInspector', () => {
  it('概述给层级、状态、耗时占比与 token', () => {
    wrap(<TraceInspector step={STEP} turn={TURN} totalMs={60_000} />);
    expect(screen.getByText('工具 · Bash')).toBeTruthy();
    expect(screen.getByText('已完成')).toBeTruthy();
    expect(screen.getByText(/占本轮 15%/)).toBeTruthy();
    expect(screen.getByText(/入 1200 \/ 出 240/)).toBeTruthy();
    expect(screen.getByText('tc_04')).toBeTruthy();
  });

  it('参数页签把 JSON 展开成多行', () => {
    wrap(<TraceInspector step={STEP} turn={TURN} totalMs={60_000} />);
    fireEvent.click(screen.getByRole('tab', { name: '参数' }));
    expect(screen.getByText(/"command": "npm run build"/)).toBeTruthy();
  });

  it('计时页签标明来源与两个占比', () => {
    wrap(<TraceInspector step={STEP} turn={TURN} totalMs={60_000} />);
    fireEvent.click(screen.getByRole('tab', { name: '计时' }));
    expect(screen.getByText('事件时间戳')).toBeTruthy();
    expect(screen.getByText('占本轮')).toBeTruthy();
    expect(screen.getByText('占全会话')).toBeTruthy();
  });

  it('Schema 页签读工具注册表', async () => {
    wrap(<TraceInspector step={STEP} turn={TURN} totalMs={60_000} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Schema' }));
    expect(await screen.findByText('执行 shell 命令并返回输出。')).toBeTruthy();
    expect(screen.getByText(/"command"/)).toBeTruthy();
  });

  it('没有选中步骤时给出引导而不是空白面板', () => {
    wrap(<TraceInspector step={null} turn={null} totalMs={0} />);
    expect(screen.getByText('选中一条步骤')).toBeTruthy();
  });

  it('页签按步骤类型给：思考没有参数和 Schema，只留思考与计时', () => {
    const thinking: TraceStep = {
      ...STEP,
      id: 'think:t1',
      kind: 'text',
      layer: 'model',
      title: '思考',
      text: '先看 pod 事件，再查镜像仓库连通性。',
      toolName: undefined,
      toolCallId: undefined,
      args: undefined,
      result: undefined,
      argsSummary: undefined,
      resultSummary: undefined,
    };
    wrap(<TraceInspector step={thinking} turn={TURN} totalMs={60_000} />);
    expect(screen.getByRole('tab', { name: '概述' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: '思考' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: '计时' })).toBeTruthy();
    expect(screen.queryByRole('tab', { name: '参数' })).toBeNull();
    expect(screen.queryByRole('tab', { name: 'Schema' })).toBeNull();
    expect(screen.queryByRole('tab', { name: '结果' })).toBeNull();
  });

  it('工具步骤的页签叫「结果」，带改动的叫「改动」', () => {
    wrap(<TraceInspector step={STEP} turn={TURN} totalMs={60_000} />);
    expect(screen.getByRole('tab', { name: '结果' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Schema' })).toBeTruthy();

    cleanup();
    const edit: TraceStep = {
      ...STEP,
      id: 'tool:edit',
      title: 'Edit',
      toolName: 'Edit',
      diff: { oldLines: ['setTimeout(render, 16);'], newLines: ['requestAnimationFrame(render);'] },
    };
    wrap(<TraceInspector step={edit} turn={TURN} totalMs={60_000} />);
    fireEvent.click(screen.getByRole('tab', { name: '改动' }));
    expect(screen.getByText(/requestAnimationFrame/)).toBeTruthy();
  });
});
