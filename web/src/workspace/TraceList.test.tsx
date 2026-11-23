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
    mode: 'time' as const,
    query: '',
    activeStepId: null,
    activeTurnId: null,
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

  it('轮次模式默认折叠除最后一轮之外的轮次，点开才展开', () => {
    renderList({ mode: 'turn' });
    expect(screen.queryByText(/command: ls -la/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /第 1 轮/ }));
    expect(screen.getByText(/command: ls -la/)).toBeTruthy();
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

  it('每一步都能就地展开原文，再点收起', () => {
    const { container } = renderList();
    // 默认收起：只有行内摘要，没有原文块
    expect(container.querySelector('.trace-detail')).toBeNull();

    const toggles = screen.getAllByRole('button', { name: /展开 Bash 原文/ });
    fireEvent.click(toggles[0]);
    const detail = container.querySelector('.trace-detail');
    expect(detail?.textContent).toContain('"command": "ls -la"');
    expect(detail?.textContent).toContain('a.txt');

    fireEvent.click(screen.getByRole('button', { name: /收起 Bash 原文/ }));
    expect(container.querySelector('.trace-detail')).toBeNull();
  });

  it('展开不改变选中，选中也不抢展开', () => {
    const { props } = renderList();
    fireEvent.click(screen.getAllByRole('button', { name: /展开 Edit 原文/ })[0]);
    expect(props.onSelectStep).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText(/command: ls -la/));
    expect(props.onSelectStep).toHaveBeenCalledTimes(1);
  });
});
