import { describe, expect, it } from 'vitest';
import type { Attachment, ChatMessage, ParseStage } from '../types';
import { turnsFromMessages } from './trace';

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
