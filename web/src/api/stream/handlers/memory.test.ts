import { describe, expect, it } from 'vitest';
import { INITIAL_STATE } from '../state';
import { MEMORY_HANDLERS } from './memory';

describe('MEMORY_HANDLERS', () => {
  it('检索事件落成记忆步骤，带命中与注入字数', () => {
    const next = MEMORY_HANDLERS.MEMORY_RETRIEVE_END(INITIAL_STATE, {
      type: 'MEMORY_RETRIEVE_END',
      layer: 'long',
      state: 'ok',
      memory: { hits: 3, injected_chars: 480, ids: [1, 2], scores: [0.9, 0.8] },
    });
    expect(next.steps).toHaveLength(1);
    expect(next.steps[0]).toMatchObject({
      kind: 'memory',
      label: '记忆检索 · 长期',
      detail: '命中 3 条 · 注入 480 字',
      state: 'success',
    });
    expect(next.steps[0].result).toContain('"ids":[1,2]');
  });

  it('裁剪与压缩带前后 token，跳过态在标题标注', () => {
    const trimmed = MEMORY_HANDLERS.MEMORY_TRIM(INITIAL_STATE, {
      type: 'MEMORY_TRIM',
      state: 'ok',
      memory: { reclaimed_chars: 12000, dropped_tool_results: 3 },
    });
    expect(trimmed.steps[0].detail).toBe('回收 12000 字 · 旧工具结果 3 条');

    const compressed = MEMORY_HANDLERS.MEMORY_COMPRESS_END(INITIAL_STATE, {
      type: 'MEMORY_COMPRESS_END',
      state: 'skipped',
      memory: { tokens_before: 103000, tokens_after: 41000 },
    });
    expect(compressed.steps[0].label).toBe('记忆压缩 · 短期→中期（跳过）');
    expect(compressed.steps[0].detail).toBe('103k → 41k');
  });

  it('写入失败标为 error，且同一类事件不会互相覆盖', () => {
    const first = MEMORY_HANDLERS.MEMORY_WRITE_END(INITIAL_STATE, {
      type: 'MEMORY_WRITE_END',
      layer: 'long',
      state: 'ok',
      memory: { stored: true, deduped: false },
    });
    const second = MEMORY_HANDLERS.MEMORY_WRITE_END({ ...first, eventCount: 1 }, {
      type: 'MEMORY_WRITE_END',
      layer: 'episodic',
      state: 'error',
      memory: { stored: false, deduped: false },
    });
    expect(second.steps).toHaveLength(2);
    expect(second.steps[1].state).toBe('error');
    expect(second.steps[1].label).toBe('记忆写入 · 中期');
    expect(second.steps[0].detail).toBe('新增 1 / 去重 0');
    // id 只承诺单轮内唯一：两条同类事件不能靠 id 相同被 upsert 合并掉
    expect(second.steps[0].id).not.toBe(second.steps[1].id);
  });

  it('上下文装配事件读出历史条数与注入字数', () => {
    const next = MEMORY_HANDLERS.CONTEXT_ASSEMBLED(INITIAL_STATE, {
      type: 'CONTEXT_ASSEMBLED',
      state: 'ok',
      memory: {
        zones: { history: { messages: 7 }, tail: { injected: true } },
        injected_chars: 360,
      },
    });
    expect(next.steps).toHaveLength(1);
    expect(next.steps[0].label).toBe('上下文装配');
    expect(next.steps[0].detail).toBe('历史 7 条 · 注入 360 字');
  });
});
