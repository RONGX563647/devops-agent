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
  timing: { start: 0, end: 4_400, durationMs: 4_400, source: 'measured' },
  durationMs: 4_400,
  measured: true,
  share: 0.07,
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
  measuredMs: 30_000,
  wallMs: 30_000,
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
    expect(screen.getByText('占本轮实测')).toBeTruthy();
    expect(screen.getByText('占全会话实测')).toBeTruthy();
  });

  it('Schema 页签读工具注册表', async () => {
    wrap(<TraceInspector step={STEP} turn={TURN} totalMs={60_000} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Schema' }));
    expect(await screen.findByText('执行 shell 命令并返回输出。')).toBeTruthy();
    expect(screen.getByText(/"command"/)).toBeTruthy();
  });

  it('没有选中步骤时给出引导而不是空白面板', () => {
    wrap(<TraceInspector step={null} turn={null} totalMs={0} />);
    expect(screen.getByText('选中一轮或一条步骤')).toBeTruthy();
  });

  it('只选中轮次时给整轮概要，并多一个上下文页签', () => {
    wrap(<TraceInspector step={null} turn={TURN} totalMs={60_000} turns={[TURN]} />);
    // 轮级头部：不再是「选中一条步骤」的空态
    expect(screen.getByText('第 3 轮 · 7 步')).toBeTruthy();
    expect(screen.getAllByText('构建一下').length).toBeGreaterThan(0);

    const tab = screen.getByRole('tab', { name: '上下文' });
    fireEvent.click(tab);
    expect(screen.getByText('历史上下文')).toBeTruthy();
    // 只有第一轮：应当说清没有历史，而不是留一片空白
    expect(screen.getByText(/这是第一轮/)).toBeTruthy();
  });

  it('上下文页签按轮分开列出历史原文', () => {
    const earlier = {
      ...TURN,
      id: 'u0',
      index: 1,
      question: '先看看目录',
      userText: '先看看目录',
      items: [
        {
          ...STEP,
          id: 'u0:text',
          kind: 'text' as const,
          title: '模型输出',
          synthetic: true,
          text: '目录里有三个文件。',
        },
      ],
    };
    wrap(
      <TraceInspector
        step={null}
        turn={TURN}
        totalMs={60_000}
        turns={[earlier, TURN]}
      />,
    );
    fireEvent.click(screen.getByRole('tab', { name: '上下文' }));
    expect(screen.getByText('1 轮')).toBeTruthy();
    // 历史那一轮的问题与助手原文都在（收起时也留在 DOM 里，展开即读）
    expect(screen.getAllByText('先看看目录').length).toBeGreaterThan(0);
    expect(screen.getByText('目录里有三个文件。')).toBeTruthy();
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

  it('点调用一级节点：右侧给失败率与失败原因分类，可跳到具体那一步', () => {
    const failed: TraceStep = {
      ...STEP,
      id: 'tool:fail',
      status: 'error',
      result: 'Command failed: npm test\n退出码 1',
      resultSummary: 'Command failed: npm test',
    };
    const group = {
      key: 'tool:Bash',
      type: 'tool' as const,
      name: 'Bash',
      calls: [
        { step: STEP, turnIndex: 1 },
        { step: failed, turnIndex: 2 },
      ],
      total: 5_400,
      failed: 1,
      slowest: STEP,
    };
    const onSelectStep = vi.fn();
    wrap(
      <TraceInspector
        step={null}
        turn={null}
        group={group}
        totalMs={60_000}
        onSelectStep={onSelectStep}
      />,
    );
    expect(screen.getAllByText(/工具调用组/).length).toBeGreaterThan(0);
    expect(screen.getByText(/失败率 50%/)).toBeTruthy();
    // 原因按报错首行归并，不把整段返回原文塞进统计
    expect(screen.getByText('Command failed: npm test')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '查看' }));
    expect(onSelectStep).toHaveBeenCalledWith(failed);
  });
});

