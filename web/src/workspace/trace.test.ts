import { describe, expect, it } from 'vitest';
import type { Attachment, ChatMessage, ParseStage } from '../types';
import { buildLanes, turnsFromMessages } from './traceData/build';
import { unionLength } from './traceData/format';
import { durationRows, percentile, topShare, traceStats } from './traceData/stats';

function user(id: string, text: string, at: string): ChatMessage {
  return { id, role: 'user', text, at, steps: [] };
}

/** 一份已解析完成的附件：轨迹只关心名字与体积，正文留在服务端。 */
const uploaded: Attachment = {
  id: 'a1b2c3d4e5f60718',
  name: 'run.log',
  size: 900,
  suffix: '.log',
  chars: 40,
  truncated: false,
  status: 'ok',
};

describe('turnsFromMessages', () => {
  it('按用户消息切分轮次，统计工具与耗时', () => {
    const messages: ChatMessage[] = [
      user('u1', '为什么部署失败？', '2026-11-23T10:00:00.000Z'),
      {
        id: 'a1',
        role: 'assistant',
        text: '镜像拉取超时。',
        at: '2026-11-23T10:00:01.000Z',
        endedAt: '2026-11-23T10:00:05.000Z',
        steps: [
          {
            id: 'tool:t1',
            kind: 'tool',
            label: '调用工具 · bash',
            detail: '',
            state: 'success',
            at: '2026-11-23T10:00:01.000Z',
            args: 'kubectl',
            result: 'timeout',
          },
          {
            id: 'model:1',
            kind: 'lifecycle',
            label: '模型调用',
            detail: '',
            state: 'success',
            at: '2026-11-23T10:00:02.000Z',
          },
        ],
      },
      user('u2', '继续', '2026-11-23T10:01:00.000Z'),
      {
        id: 'a2',
        role: 'assistant',
        text: '好的。',
        at: '2026-11-23T10:01:01.000Z',
        endedAt: '2026-11-23T10:01:03.000Z',
        steps: [],
      },
    ];

    const turns = turnsFromMessages(messages);
    expect(turns).toHaveLength(2);
    expect(turns[0].index).toBe(1);
    expect(turns[0].question).toBe('为什么部署失败？');
    expect(turns[0].tools).toBe(1);
    // lifecycle 不作为轨迹条目；工具 + 正文
    expect(turns[0].items.map((item) => item.kind)).toEqual(['tool', 'text']);
    expect(turns[0].items[0]).toMatchObject({
      title: 'bash',
      layer: 'tool',
      args: 'kubectl',
      result: 'timeout',
    });
    expect(turns[0].endedAt).toBe(new Date('2026-11-23T10:00:05.000Z').getTime());
    // 第一步耗时 = 下一步时间 - 当前时间（工具 1s 后是正文）
    expect(turns[0].items[0].durationMs).toBe(4000);
    expect(turns[1].steps).toBe(1);
  });

  it('错误与主动停止标记该轮为异常', () => {
    const turns = turnsFromMessages([
      user('u1', 'q', '2026-11-23T10:00:00.000Z'),
      {
        id: 'a1',
        role: 'assistant',
        text: '（已停止）',
        at: '2026-11-23T10:00:01.000Z',
        steps: [],
        stopped: true,
      },
      user('u2', 'q2', '2026-11-23T10:02:00.000Z'),
      {
        id: 'a2',
        role: 'assistant',
        text: '',
        at: '2026-11-23T10:02:01.000Z',
        steps: [],
        error: '网络错误',
      },
    ]);
    expect(turns[0].failed).toBe(true);
    expect(turns[1].failed).toBe(true);
    expect(turns[1].items.some((item) => item.kind === 'error')).toBe(true);
  });

  it('解析步骤映射为 parse，并汇总引用与降级', () => {
    const stages: ParseStage[] = [
      { stage: 'detect', label: '引用探测', status: 'ok', summary: 'URL ×1', before: 0, after: 0, items: 1, ms: 0, detail: '', changed: true, failed: 0 },
      { stage: 'redact', label: '密钥脱敏', status: 'ok', summary: '1 处', before: 0, after: 0, items: 1, ms: 0, detail: '', changed: true, failed: 0 },
      { stage: 'expand', label: '引用展开', status: 'ok', summary: '1 条成功 · 1 条降级', before: 0, after: 10, items: 2, ms: 0, detail: '', changed: true, failed: 1 },
    ];
    const turns = turnsFromMessages([
      user('u1', '看下 README', '2026-11-23T10:00:00.000Z'),
      {
        id: 'a1',
        role: 'assistant',
        text: '好的。',
        at: '2026-11-23T10:00:01.000Z',
        endedAt: '2026-11-23T10:00:02.000Z',
        steps: [
          {
            id: 'parse:input-parse',
            kind: 'parse',
            label: '输入解析',
            detail: '3/6 阶段生效',
            state: 'success',
            at: '2026-11-23T10:00:01.000Z',
            parseStages: stages,
          },
        ],
      },
    ]);
    // 解析不是异常：引用取不到只降级，不把整轮判失败
    expect(turns[0].failed).toBe(false);
    expect(turns[0].items.map((item) => item.kind)).toEqual(['parse', 'text']);
    expect(turns[0].items[0].stages).toHaveLength(3);
    expect(turns[0].parse).toEqual({ attachments: 0, refs: 1, redactions: 1, degraded: 1 });
    expect(turns[0].tools).toBe(0);
  });

  it('附件数取用户消息上挂的文件数，无附件时缺省', () => {
    const turns = turnsFromMessages([
      { ...user('u1', '看下 run.log', '2026-11-23T10:00:00.000Z'), attachments: [uploaded] },
      {
        id: 'a1',
        role: 'assistant',
        text: 'ok',
        at: '2026-11-23T10:00:01.000Z',
        endedAt: '2026-11-23T10:00:01.000Z',
        steps: [],
      },
      user('u2', '再来一次', '2026-11-23T10:00:02.000Z'),
    ]);

    expect(turns[0].attachments).toBe(1);
    expect(turns[1].attachments).toBeUndefined();
  });

  it('附件读取阶段计入轮级计数，与脱敏互不覆盖', () => {
    const stage = (over: Partial<ParseStage> & { stage: string }): ParseStage => ({
      label: '',
      status: 'ok',
      summary: '',
      before: 0,
      after: 0,
      items: 0,
      ms: 0,
      detail: '',
      changed: true,
      failed: 0,
      ...over,
    });
    const turns = turnsFromMessages([
      user('u1', '看下 ./a.md', '2026-11-23T10:00:00.000Z'),
      {
        id: 'a1',
        role: 'assistant',
        text: '',
        at: '2026-11-23T10:00:01.000Z',
        steps: [
          {
            id: 'parse:input-parse',
            kind: 'parse',
            label: '输入解析',
            detail: '',
            state: 'success',
            at: '2026-11-23T10:00:01.000Z',
            parseStages: [
              stage({ stage: 'attach', items: 1 }),
              stage({ stage: 'detect', items: 1 }),
              stage({ stage: 'redact', items: 2 }),
            ],
          },
        ],
      },
    ]);

    expect(turns[0].parse).toEqual({ attachments: 1, refs: 1, redactions: 2, degraded: 0 });
  });
});

describe('记忆泳道', () => {
  it('记忆步骤进入步骤列表与第三条泳道', () => {
    const turns = turnsFromMessages([
      user('u1', '继续修', '2026-11-23T10:00:00.000Z'),
      {
        id: 'a1',
        role: 'assistant',
        text: '',
        at: '2026-11-23T10:00:01.000Z',
        endedAt: '2026-11-23T10:00:03.000Z',
        steps: [
          {
            id: 'memory:compress',
            kind: 'memory',
            label: '记忆压缩 · 短期→中期',
            detail: '103k → 41k',
            state: 'success',
            at: '2026-11-23T10:00:01.000Z',
            endedAt: '2026-11-23T10:00:01.008Z',
          },
        ],
      },
    ]);
    expect(turns[0].items[0]).toMatchObject({ kind: 'memory', layer: 'memory' });
    const lanes = buildLanes(turns);
    expect(lanes.memory).toHaveLength(1);
    expect(lanes.memory[0].lane).toBe('memory');
    expect(lanes.model).toHaveLength(0);
    expect(lanes.tool).toHaveLength(0);
  });

  it('两轮出现同名记忆步骤时，步骤 id 与泳道 id 都不重复', () => {
    /** 真实流里记忆步骤按「单轮内帧序号」编号，两轮撞出同一个 id 是常态。 */
    const memoryStep = (at: string, endedAt: string) => ({
      id: 'memory:MEMORY_RETRIEVE_END:7',
      kind: 'memory' as const,
      label: '记忆检索 · 长期',
      detail: '命中 2 条',
      state: 'success' as const,
      at,
      endedAt,
    });
    const turns = turnsFromMessages([
      user('u1', '第一问', '2026-11-23T10:00:00.000Z'),
      {
        id: 'a1',
        role: 'assistant',
        text: '',
        at: '2026-11-23T10:00:01.000Z',
        endedAt: '2026-11-23T10:00:03.000Z',
        steps: [memoryStep('2026-11-23T10:00:01.000Z', '2026-11-23T10:00:01.005Z')],
      },
      user('u2', '第二问', '2026-11-23T10:02:00.000Z'),
      {
        id: 'a2',
        role: 'assistant',
        text: '',
        at: '2026-11-23T10:02:01.000Z',
        endedAt: '2026-11-23T10:02:03.000Z',
        steps: [memoryStep('2026-11-23T10:02:01.000Z', '2026-11-23T10:02:01.005Z')],
      },
    ]);

    expect(turns[0].items[0].id).not.toBe(turns[1].items[0].id);
    const lanes = buildLanes(turns);
    expect(lanes.memory).toHaveLength(2);
    expect(new Set(lanes.memory.map((span) => span.id)).size).toBe(2);
    // 点泳道块要选中自己那一轮：stepId 必须指回本轮的步骤 id
    expect(lanes.memory.map((span) => span.stepId)).toEqual(
      turns.map((turn) => turn.items[0].id),
    );
  });

  it('轮次之间的空档被剪掉：两轮在横轴上首尾相接', () => {
    const call = (id: string, at: string, endedAt: string) => ({
      id,
      kind: 'tool' as const,
      label: '调用工具 · Bash',
      detail: '',
      state: 'success' as const,
      toolName: 'Bash',
      at,
      endedAt,
      args: '{"command":"ls"}',
      result: 'ok',
    });
    const turns = turnsFromMessages([
      user('u1', '第一问', '2026-11-23T10:00:00.000Z'),
      {
        id: 'a1',
        role: 'assistant',
        text: '',
        at: '2026-11-23T10:00:01.000Z',
        endedAt: '2026-11-23T10:00:03.000Z',
        steps: [call('t1', '2026-11-23T10:00:01.000Z', '2026-11-23T10:00:03.000Z')],
      },
      // 用户一小时后才回话：这一段空档不该出现在泳道上
      user('u2', '第二问', '2026-11-23T11:00:00.000Z'),
      {
        id: 'a2',
        role: 'assistant',
        text: '',
        at: '2026-11-23T11:00:01.000Z',
        endedAt: '2026-11-23T11:00:05.000Z',
        steps: [call('t2', '2026-11-23T11:00:01.000Z', '2026-11-23T11:00:05.000Z')],
      },
    ]);
    const lanes = buildLanes(turns);
    expect(lanes.tool).toHaveLength(2);
    // 第一段 2s + 第二段 4s 紧挨着：起点接在前一段的结束上
    expect(lanes.tool[1].start - lanes.tool[0].end).toBe(0);
    expect(lanes.tool[1].end).toBe(6_000);
  });
});

describe('traceStats 缓存口径', () => {
  /** cache 省略 = 模型没回传这个字段，与「回传了 0」必须区分开。 */
  const modelStep = (id: string, at: string, tokensIn: number, cache?: number) => ({
    id,
    kind: 'model' as const,
    label: '模型调用',
    detail: '',
    state: 'success' as const,
    at,
    tokensIn,
    tokensOut: 10,
    ...(cache === undefined ? {} : { cacheTokens: cache }),
  });

  it('命中率按 Σcache ÷ Σinput，首枪单独给出', () => {
    const turns = turnsFromMessages([
      user('u1', '第一问', '2026-11-23T10:00:00.000Z'),
      {
        id: 'a1',
        role: 'assistant',
        text: '',
        at: '2026-11-23T10:00:01.000Z',
        endedAt: '2026-11-23T10:00:09.000Z',
        inputTokens: 3060 + 9645,
        outputTokens: 20,
        cacheInputTokens: 9216,
        steps: [
          modelStep('m1', '2026-11-23T10:00:01.000Z', 3060, 0),
          modelStep('m2', '2026-11-23T10:00:05.000Z', 9645, 9216),
        ],
      },
    ]);
    const stats = traceStats(turns, buildLanes(turns));
    expect(stats.cacheRate).toBeCloseTo(9216 / 12705, 3);
    // 首枪必然是冷读：单独给数，才不会被当成「缓存失效」
    expect(stats.firstCallRate).toBe(0);
  });

  it('模型不回传缓存字段时两项均为 null，不编 0%', () => {
    const turns = turnsFromMessages([
      user('u1', '第一问', '2026-11-23T10:00:00.000Z'),
      {
        id: 'a1',
        role: 'assistant',
        text: '',
        at: '2026-11-23T10:00:01.000Z',
        inputTokens: 500,
        outputTokens: 10,
        steps: [modelStep('m1', '2026-11-23T10:00:01.000Z', 500)],
      },
    ]);
    const stats = traceStats(turns, buildLanes(turns));
    expect(stats.cacheRate).toBeNull();
    expect(stats.firstCallRate).toBeNull();
  });
});

describe('计时口径与三个视图的数据', () => {
  /** 一轮：10s 模型 + 2s 工具 + 最终正文。 */
  const timedTurn = () =>
    turnsFromMessages([
      user('u1', '为什么部署失败？', '2026-11-23T10:00:00.000Z'),
      {
        id: 'a1',
        role: 'assistant',
        text: '镜像拉取超时。',
        at: '2026-11-23T10:00:01.000Z',
        endedAt: '2026-11-23T10:00:13.000Z',
        steps: [
          {
            id: 'model:1',
            kind: 'model',
            label: '模型调用 · qwen3.8',
            detail: '',
            state: 'success',
            at: '2026-11-23T10:00:01.000Z',
            endedAt: '2026-11-23T10:00:11.000Z',
          },
          {
            id: 'tool:t1',
            kind: 'tool',
            label: '调用工具 · Bash',
            detail: '',
            state: 'success',
            toolName: 'Bash',
            at: '2026-11-23T10:00:11.000Z',
            endedAt: '2026-11-23T10:00:13.000Z',
          },
        ],
      },
    ]);

  it('模型调用进入步骤列表，计时来源分三档', () => {
    const [turn] = timedTurn();
    const model = turn.items.find((item) => item.kind === 'model');
    expect(model?.kind).toBe('model');
    expect(model?.title).toBe('qwen3.8');
    expect(model?.timing).toEqual({
      start: new Date('2026-11-23T10:00:01.000Z').getTime(),
      end: new Date('2026-11-23T10:00:11.000Z').getTime(),
      durationMs: 10_000,
      source: 'measured',
    });
    // 最终正文是合成行：读答案用，不参与耗时排名
    expect(turn.items.find((item) => item.title === '模型输出')?.synthetic).toBe(true);
  });

  it('占比分母只算真实步骤，估算值与正文不占份额', () => {
    const [turn] = timedTurn();
    const model = turn.items.find((item) => item.kind === 'model')!;
    const tool = turn.items.find((item) => item.kind === 'tool')!;
    expect(model.share).toBeCloseTo(10 / 12, 5);
    expect(tool.share).toBeCloseTo(2 / 12, 5);
    expect(Math.round(topShare(durationRows([turn], () => true), 1) * 100)).toBe(83);
  });

  it('实测是 span 并集，墙钟是起止差，两者不相等时差值就是用户空闲', () => {
    const turns = turnsFromMessages([
      user('u1', 'q1', '2026-11-23T10:00:00.000Z'),
      {
        id: 'a1',
        role: 'assistant',
        text: '',
        at: '2026-11-23T10:00:01.000Z',
        endedAt: '2026-11-23T10:00:03.000Z',
        steps: [
          {
            id: 'model:1',
            kind: 'model',
            label: '模型调用 · qwen',
            detail: '',
            state: 'success',
            at: '2026-11-23T10:00:01.000Z',
            endedAt: '2026-11-23T10:00:03.000Z',
          },
        ],
      },
      // 用户隔了 1 分钟才发下一条：墙钟要涨，实测不该涨
      user('u2', 'q2', '2026-11-23T10:01:03.000Z'),
    ]);
    const stats = traceStats(turns, buildLanes(turns));
    expect(stats.measuredMs).toBe(2_000);
    expect(stats.wallMs).toBe(63_000);
    expect(stats.modelCalls).toBe(1);
  });

  it('unionLength 把嵌套/重叠区间合并成一段', () => {
    expect(unionLength([])).toBe(0);
    expect(unionLength([[0, 100], [50, 150]])).toBe(150);
    expect(unionLength([[0, 100], [200, 260]])).toBe(160);
  });

  it('percentile 不足两个样本时返回 null，不假装有分位数', () => {
    expect(percentile([5], 0.95)).toBeNull();
    expect(percentile([1, 2, 3, 4], 0.5)).toBe(2);
    expect(percentile([1, 2, 3, 4], 0.95)).toBe(4);
  });
});
