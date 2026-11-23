// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AnswerItem, PendingQuestion } from '../types';
import { QuestionCard } from './QuestionCard';

afterEach(cleanup);

const CALLS: PendingQuestion[] = [
  {
    tool_call_id: 'q1',
    name: 'AskUser',
    questions: [
      {
        question: '用哪个库？',
        header: 'Library',
        options: [
          { label: 'httpx', description: 'async 优先' },
          { label: 'requests', description: '同步惯用' },
        ],
        multi_select: false,
      },
      {
        question: '要开哪些能力？',
        header: 'Features',
        options: [
          { label: '缓存', description: '命中率' },
          { label: '重试', description: '失败重放' },
        ],
        multi_select: true,
      },
    ],
  },
];

function renderCard(over: Partial<Parameters<typeof QuestionCard>[0]> = {}) {
  const onSubmit = vi.fn<(answers: AnswerItem[]) => void>();
  const onCancel = vi.fn();
  render(
    <QuestionCard
      questions={CALLS}
      expiresAt={null}
      onSubmit={onSubmit}
      onCancel={onCancel}
      {...over}
    />,
  );
  return { onSubmit, onCancel };
}

function submitButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: '提交回答' }) as HTMLButtonElement;
}

describe('QuestionCard', () => {
  it('未答完时提交禁用，答完后提交单选项与多选项', () => {
    const { onSubmit } = renderCard();
    expect(submitButton().disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: /httpx/ }));
    // 只回答了第一题，仍然不能提交
    expect(submitButton().disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: /缓存/ }));
    fireEvent.click(screen.getByRole('button', { name: /重试/ }));
    expect(submitButton().disabled).toBe(false);

    fireEvent.click(submitButton());
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0]).toEqual([
      {
        tool_call_id: 'q1',
        answers: [
          { question: '用哪个库？', selected: ['httpx'], other: null },
          { question: '要开哪些能力？', selected: ['缓存', '重试'], other: null },
        ],
      },
    ]);
  });

  it('单选再点一次取消选择', () => {
    renderCard();
    const httpx = screen.getByRole('button', { name: /httpx/ });
    fireEvent.click(httpx);
    expect(httpx.className).toContain('active');
    fireEvent.click(httpx);
    expect(httpx.className).not.toContain('active');
    expect(submitButton().disabled).toBe(true);
  });

  it('只填 Other 也能提交，并去掉首尾空格', () => {
    const { onSubmit } = renderCard();
    const [first, second] = screen.getAllByPlaceholderText('其他（可留空）');
    fireEvent.change(first, { target: { value: '  aiohttp  ' } });
    fireEvent.change(second, { target: { value: '都不需要' } });
    expect(submitButton().disabled).toBe(false);

    fireEvent.click(submitButton());
    const answers = onSubmit.mock.calls[0][0][0].answers;
    expect(answers[0]).toEqual({ question: '用哪个库？', selected: [], other: 'aiohttp' });
    expect(answers[1]).toEqual({ question: '要开哪些能力？', selected: [], other: '都不需要' });
  });

  it('取消本轮触发 onCancel', () => {
    const { onCancel } = renderCard();
    fireEvent.click(screen.getByRole('button', { name: '取消本轮' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('busy 时禁用选项与提交', () => {
    renderCard({ busy: true });
    expect((screen.getByRole('button', { name: /httpx/ }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(submitButton().disabled).toBe(true);
  });
});
