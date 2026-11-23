// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage, ProcessEntry } from '../types';
import { turnsFromMessages } from './trace';
import { TraceList } from './TraceList';

function tool(partial: Partial<ProcessEntry> & { id: string }): ProcessEntry {
  return {
    kind: 'tool',
    label: '调用工具 · Bash',
    detail: '',
    state: 'success',
    at: '',
    ...partial,
  };
}

function conversation(turns: { question: string; at: string; steps: ProcessEntry[]; text?: string }[]): ChatMessage[] {
  const messages: ChatMessage[] = [];
  for (const [i, turn] of turns.entries()) {
    messages.push({ id: `u${i}`, role: 'user', text: turn.question, at: turn.at, steps: [] });
    messages.push({
      id: `a${i}`,
      role: 'assistant',
      text: turn.text ?? '',
      at: turn.at,
      endedAt: new Date(new Date(turn.at).getTime() + 60_000).toISOString(),
      steps: turn.steps,
    });
  }
  return messages;
}

const TWO_TURNS = conversation([
  {
    question: '为什么部署失败？',
    at: '2026-11-23T10:00:00.000Z',
    steps: [
      tool({
        id: 't1',
        toolName: 'Bash',
        at: '2026-11-23T10:00:01.000Z',
        endedAt: '2026-11-23T10:00:03.000Z',
        args: '{"command":"ls -la"}',
        result: 'a.txt\nb.txt',
      }),
    ],
  },
  {
    question: '跑一下测试',
    at: '2026-11-23T10:01:00.000Z',
    steps: [
      tool({
        id: 't2',
        toolName: 'Edit',
        at: '2026-11-23T10:01:01.000Z',
        endedAt: '2026-11-23T10:01:02.000Z',
        args: '{"file_path":"a.ts","old_string":"old();","new_string":"new();"}',
        result: '已更新',
      }),
      tool({
        id: 't3',
        toolName: 'Bash',
        at: '2026-11-23T10:01:02.000Z',
        endedAt: '2026-11-23T10:01:03.000Z',
        state: 'error',
        args: '{"command":"npm test"}',
        result: 'FAIL 2 tests',
      }),
    ],
    text: '测试挂了。',
  },
]);

function renderList(overrides: Partial<React.ComponentProps<typeof TraceList>> = {}) {
  const props = {
    turns: turnsFromMessages(TWO_TURNS),
    mode: 'turn' as const,
    query: '',
    activeStepId: null,
    activeTurnId: null,
    context: null,
    onSelectStep: vi.fn(),
    onSelectTurn: vi.fn(),
    ...overrides,
  };
  return { props, ...render(<TraceList {...props} />) };
}

afterEach(cleanup);

describe('TraceList', () => {
  it('一步一行：工具名、入参摘要、返回摘要与真实耗时都在行里', () => {
    renderList();
    expect(screen.getAllByText('Bash')).toHaveLength(2);
    expect(screen.getByText(/command: ls -la/)).toBeTruthy();
    expect(screen.getByText('2.0s')).toBeTruthy();
    // 轮次分隔带步数与工具数
    expect(screen.getByText(/3 步 · 2 工具/)).toBeTruthy();
  });

  it('搜索只留命中步骤，并把命中片段标出来', () => {
    const { container } = renderList({ query: 'FAIL' });
    expect(screen.queryByText('Edit')).toBeNull();
    // 命中片段被 <mark> 拆开，按整行文本断言
    expect(container.textContent).toContain('FAIL 2 tests');
    expect(container.querySelector('mark')?.textContent).toBe('FAIL');
  });

  it('轮次默认展开，点标题收起整轮', () => {
    renderList({ mode: 'turn' });
    expect(screen.getByText(/command: ls -la/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /第 1 轮/ }));
    expect(screen.queryByText(/command: ls -la/)).toBeNull();
  });

  it('调用模式按工具聚合次数与总耗时', () => {
    renderList({ mode: 'call' });
    expect(screen.getByText(/2 次 · 合计 3.0s/)).toBeTruthy();
    expect(screen.getByText('Edit')).toBeTruthy();
  });

  it('写入类工具直接画 diff 的增删行', () => {
    renderList();
    expect(screen.getByText(/^- old\(\);$/)).toBeTruthy();
    expect(screen.getByText(/^\+ new\(\);$/)).toBeTruthy();
  });

  it('轮次很多时先只渲染最近 5 轮，可加载更早的', () => {
    const many = conversation(
      Array.from({ length: 7 }, (_, i) => ({
        question: `问题 ${i + 1}`,
        at: `2026-11-23T10:0${i}:00.000Z`,
        steps: [tool({ id: `t${i}`, toolName: 'Bash', at: `2026-11-23T10:0${i}:01.000Z` })],
      })),
    );
    renderList({ turns: turnsFromMessages(many) });
    expect(screen.getByText(/加载更早的 2 轮/)).toBeTruthy();
    fireEvent.click(screen.getByText(/加载更早的 2 轮/));
    expect(screen.getAllByText(/问题 1/).length).toBeGreaterThan(0);
  });

  it('每一步默认展开原文，点整行收起', () => {
    const { container } = renderList();
    const details = container.querySelectorAll('.trace-detail');
    expect(details.length).toBeGreaterThan(0);
    expect(container.textContent).toContain('"command": "ls -la"');

    // 点整行：收起这一步的原文
    fireEvent.click(screen.getByText(/command: ls -la/));
    expect(container.querySelectorAll('.trace-detail').length).toBe(details.length - 1);
  });

  it('点整行同时选中并收起', () => {
    const { props } = renderList();
    fireEvent.click(screen.getByText(/command: ls -la/));
    expect(props.onSelectStep).toHaveBeenCalledTimes(1);
  });

  it('轮次头部标出缓存命中率，没有缓存字段就不显示', () => {
    const withCache: ChatMessage[] = [
      { id: 'u1', role: 'user', text: '跑一下', at: '2026-11-23T10:00:00.000Z', steps: [] },
      {
        id: 'a1',
        role: 'assistant',
        text: '好了。',
        at: '2026-11-23T10:00:00.000Z',
        endedAt: '2026-11-23T10:00:10.000Z',
        inputTokens: 1_000,
        outputTokens: 100,
        cacheInputTokens: 790,
        steps: [tool({ id: 't9', toolName: 'Bash', at: '2026-11-23T10:00:01.000Z' })],
      },
    ];
    renderList({ turns: turnsFromMessages(withCache) });
    expect(screen.getByText('· 缓存 79%')).toBeTruthy();

    cleanup();
    renderList({
      turns: turnsFromMessages(
        conversation([
          { question: '再跑一下', at: '2026-11-23T10:01:00.000Z', steps: [tool({ id: 't10' })] },
        ]),
      ),
    });
    expect(screen.queryByText(/缓存 \d+%/)).toBeNull();
  });

  it('时长视图按耗时降序，并标出每一步属于第几轮', () => {
    const { container } = renderList({ mode: 'time' });
    const rows = Array.from(container.querySelectorAll('.trace-step'));
    const durations = rows.map((row) => row.querySelector('.trace-dur')?.textContent ?? '');
    // 2.0s 的 Bash 在 1.0s 的 Edit / npm test 之前
    expect(durations[0]).toBe('2.0s');
    expect(container.textContent).toContain('第 2 轮');
    expect(container.textContent).toContain('按耗时降序');
  });

  it('每轮第 0 步给出上下文：默认收起，点开看系统提示词与工具', () => {
    const { container } = renderList({
      context: {
        system_prompt: '你是 DevOps 助手。',
        system_prompt_chars: 10,
        tools: [{ name: 'Bash', description: '执行命令' }],
        workdir: '/workspace',
      },
    });
    const ctx = container.querySelector('.trace-step[data-kind="context"]');
    expect(ctx?.textContent).toContain('上下文');
    expect(ctx?.getAttribute('aria-expanded')).toBe('false');

    fireEvent.click(ctx!);
    expect(container.textContent).toContain('你是 DevOps 助手。');
    expect(container.textContent).toContain('系统提示词');
  });
});
