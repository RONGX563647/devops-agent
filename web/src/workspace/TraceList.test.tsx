// @vitest-environment jsdom
import { useState } from 'react';
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
    // 空对象 = 全部展开；「只展开最后一轮」是上层（TraceTab）的默认策略
    collapsed: {} as Record<string, boolean>,
    visibleTurns: 5 as number | null,
    onToggleTurn: vi.fn(),
    onLoadMore: vi.fn(),
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

  it('点轮次标题是选中这一轮，点箭头才是折叠', () => {
    const { props } = renderList({ mode: 'turn' });
    expect(screen.getByText(/command: ls -la/)).toBeTruthy();

    // 标题 = 选中：右侧要跟着换成这一轮，所以不能同时把整轮收起来
    fireEvent.click(screen.getByRole('button', { name: /第 1 轮/ }));
    expect(props.onSelectTurn).toHaveBeenCalledTimes(1);
    expect(props.onSelectTurn).toHaveBeenCalledWith(expect.objectContaining({ id: 'u0', index: 1 }));
    expect(props.onToggleTurn).not.toHaveBeenCalled();

    // 折叠交给左侧箭头，两个动作互不覆盖（每轮各有一个箭头，取第一轮）
    fireEvent.click(screen.getAllByRole('button', { name: '折叠本轮' })[0]);
    expect(props.onToggleTurn).toHaveBeenCalledWith('u0');
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

  it('轮次很多时先只渲染最近 5 轮，可放开显示全部', () => {
    const many = conversation(
      Array.from({ length: 7 }, (_, i) => ({
        question: `问题 ${i + 1}`,
        at: `2026-11-23T10:0${i}:00.000Z`,
        steps: [tool({ id: `t${i}`, toolName: 'Bash', at: `2026-11-23T10:0${i}:01.000Z` })],
      })),
    );
    const turns = turnsFromMessages(many);

    /** 最小受控壳：按钮只负责把「最近 5 轮」放开成全部，验证回调确实被接上了。 */
    function Harness() {
      const [visible, setVisible] = useState<number | null>(5);
      return (
        <TraceList
          turns={turns}
          mode="turn"
          query=""
          activeStepId={null}
          activeTurnId={null}
          context={null}
          collapsed={{}}
          visibleTurns={visible}
          onToggleTurn={vi.fn()}
          onLoadMore={() => setVisible(null)}
          onSelectStep={vi.fn()}
          onSelectTurn={vi.fn()}
        />
      );
    }

    render(<Harness />);
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

  const MODEL_TURNS = turnsFromMessages(
    conversation([
      {
        question: '解释一下部署失败',
        at: '2026-11-23T10:00:00.000Z',
        text: '镜像拉取超时。',
        steps: [
          {
            id: 'model:1',
            kind: 'model',
            label: '模型调用 · qwen3.8',
            detail: '',
            state: 'success',
            at: '2026-11-23T10:00:01.000Z',
            endedAt: '2026-11-23T10:00:11.000Z',
            tokensIn: 1_000,
            tokensOut: 50,
            cacheTokens: 800,
          },
          tool({
            id: 't1',
            toolName: 'Bash',
            at: '2026-11-23T10:00:11.000Z',
            endedAt: '2026-11-23T10:00:13.000Z',
            args: '{"command":"kubectl describe pod"}',
            result: 'ImagePullBackOff',
          }),
        ],
      },
    ]),
  );

  it('时长视图纳入模型调用：泳道里有长条，列表里就必须有对应行', () => {
    const { container } = renderList({ turns: MODEL_TURNS, mode: 'time' });
    const rows = Array.from(container.querySelectorAll('.trace-step'));
    // 10s 的模型调用排在 2s 的 Bash 之前，且带「模型」徽标
    expect(rows[0].textContent).toContain('qwen3.8');
    expect(rows[0].textContent).toContain('10.0s');
    expect(rows[0].querySelector('.trace-badge')?.textContent).toBe('模型');
    expect(rows[1].textContent).toContain('Bash');
    // 占比列与帕累托合计都在
    expect(container.textContent).toContain('83%');
    expect(container.textContent).toContain('前 2 步占 100%');
  });

  it('估算值沉底并带 ≈，不抢「最慢」的第一行', () => {
    const turns = turnsFromMessages([
      { id: 'u1', role: 'user', text: 'q', at: '2026-11-23T10:00:00.000Z', steps: [] },
      {
        id: 'a1',
        role: 'assistant',
        text: '',
        at: '2026-11-23T10:00:01.000Z',
        endedAt: '2026-11-23T10:00:30.000Z',
        steps: [
          // 没有 endedAt：只能按「下一步时间 − 本步时间」插出 19s
          tool({ id: 'slow', toolName: 'Bash', at: '2026-11-23T10:00:01.000Z' }),
          {
            id: 'model:1',
            kind: 'model',
            label: '模型调用 · qwen3.8',
            detail: '',
            state: 'success',
            at: '2026-11-23T10:00:20.000Z',
            endedAt: '2026-11-23T10:00:22.000Z',
          },
        ],
      },
    ]);
    const { container } = renderList({ turns, mode: 'time' });
    const rows = Array.from(container.querySelectorAll('.trace-step'));
    // 22s 的模型不是最慢：插值出来的 19s 更大，但必须沉底
    expect(rows[0].textContent).toContain('qwen3.8');
    expect(rows[rows.length - 1].querySelector('.trace-dur')?.textContent).toBe('≈19.0s');
    expect(container.textContent).toContain('1 条估算未计入');
  });

  it('调用视图按模型/工具分组，点一行就地展开每一次调用', () => {
    const { container, props } = renderList({ turns: MODEL_TURNS, mode: 'call' });
    const heads = Array.from(container.querySelectorAll('.trace-call-head'));
    expect(heads.map((head) => head.textContent)).toEqual([
      expect.stringContaining('qwen3.8'),
      expect.stringContaining('Bash'),
    ]);
    // 单次调用没有「分位」可言，p95 不显示
    expect(heads[0].textContent).toContain('1 次 · 合计 10.0s · 均值 10.0s · 最慢 10.0s');

    fireEvent.click(heads[1]);
    const item = container.querySelector('.trace-call-item');
    expect(item?.textContent).toContain('第 1 轮');
    expect(item?.textContent).toContain('ImagePullBackOff');
    fireEvent.click(item!);
    // 展开的条目点一下就选中左侧详情
    expect(props.onSelectStep).toHaveBeenCalledTimes(1);
  });

  it('全部折叠由上层控制：收起后只留轮次头', () => {
    const turns = turnsFromMessages(TWO_TURNS);
    const collapsed = Object.fromEntries(turns.map((turn) => [turn.id, true]));
    renderList({ turns, collapsed });
    expect(screen.queryByText(/command: ls -la/)).toBeNull();
    // 折叠态也要能判断值不值得展开：步数、工具数、耗时、最慢一步都在头上
    expect(screen.getByText(/3 步 · 2 工具/)).toBeTruthy();
    expect(screen.getByText(/最慢 Edit 1.0s/)).toBeTruthy();
  });

  it('每轮第 0 步给出上下文：默认收起，点开看构成、系统提示词与工具 schema', () => {
    const { container } = renderList({
      context: {
        profile: 'workspace',
        profile_label: '工作区可写',
        system_prompt: '你是 DevOps 助手。',
        system_prompt_chars: 10,
        system_prompt_tokens: 7,
        tools: [
          {
            name: 'Bash',
            description: '执行命令',
            schema_chars: 900,
            tokens: 250,
            parameters: { type: 'object', properties: { command: { type: 'string' } } },
          },
        ],
        tools_chars: 900,
        tools_tokens: 250,
        prefix_tokens: 257,
        prefix_fingerprint: 'a1b2c3d4',
        policy_note: '本会话权限：工作区可写。',
        policy_note_tokens: 9,
        workdir: '/workspace',
      },
    });
    const ctx = container.querySelector('.trace-step[data-kind="context"]');
    expect(ctx?.textContent).toContain('上下文');
    // 行内只报模型回传的精确首枪 token 与工具数，不再估算历史/前缀
    expect(ctx?.textContent).toContain('首枪 0 tok');
    expect(ctx?.textContent).toContain('工具 1 个');
    expect(ctx?.getAttribute('aria-expanded')).toBe('false');

    fireEvent.click(ctx!);
    expect(container.textContent).toContain('你是 DevOps 助手。');
    expect(container.textContent).toContain('系统提示词');
    expect(container.textContent).toContain('指纹 a1b2c3d4');
    // 工具能点开看完整 schema，而不是只给一个名字
    expect(container.querySelector('.ctx-comp-bar')).toBeTruthy();
    expect(container.querySelector('.ctx-tool code')?.textContent).toBe('Bash');
    expect(container.textContent).toContain('900 字');
  });

  it('上下文构成只用回传的精确值：命中缓存 + 本次计算，不倒推历史', () => {
    const { container } = renderList({
      turns: MODEL_TURNS,
      context: {
        profile: 'workspace',
        profile_label: '工作区可写',
        system_prompt: '你是 DevOps 助手。',
        system_prompt_chars: 10,
        system_prompt_tokens: 7,
        tools: [],
        tools_chars: 0,
        tools_tokens: 0,
        prefix_tokens: 500,
        prefix_fingerprint: 'a1b2c3d4',
        policy_note: '工作区可写。',
        policy_note_tokens: 9,
        workdir: '/workspace',
      },
    });
    const ctx = container.querySelector('.trace-step[data-kind="context"]');
    // firstCall = 首枪 1000 tok，其中命中 800 tok
    expect(ctx?.textContent).toContain('首枪 1.0k tok');
    expect(ctx?.textContent).toContain('命中 80%');
    // 不再出现「历史 ≈」这种残差估算
    expect(ctx?.textContent).not.toContain('历史');
    fireEvent.click(ctx!);
    const bar = container.querySelector('.ctx-comp-bar');
    expect(bar?.getAttribute('aria-label')).toBe('首枪输入 1000 token，其中命中缓存 800');
    const legend = container.querySelector('.ctx-legend')?.textContent ?? '';
    expect(legend).toContain('命中缓存 800');
    expect(legend).toContain('本次计算 200');
    expect(legend).not.toContain('系统提示');
    expect(legend).not.toContain('档位约束');
  });
});

