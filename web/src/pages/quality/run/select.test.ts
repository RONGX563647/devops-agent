import { describe, expect, it, vi } from 'vitest';
import type { OjCaseList, OjFailure } from '../../../types';
import { failuresByTrack, findCaseRow, pickFailure } from './select';

function failure(overrides: Partial<OjFailure> = {}): OjFailure {
  return {
    case_id: 'a-1',
    track: 'A',
    status: 'success',
    violations: 0,
    failed_checks: [],
    judge_dims: [],
    failure_type: '',
    repro: '',
    ...overrides,
  };
}

describe('pickFailure', () => {
  const ruleFail = failure({ case_id: 'r', failed_checks: ['must_cite'], failure_type: 'rule' });
  const judgeFail = failure({ case_id: 'j', judge_dims: ['correctness'], failure_type: 'judge_score' });
  const trackB = failure({ case_id: 'b', track: 'B', failed_checks: ['x'], failure_type: 'rule' });

  it('按规则/裁判类型挑，不再永远给第一条', () => {
    expect(pickFailure([ruleFail, judgeFail], { tab: 'judges', kind: 'judge' })?.case_id).toBe('j');
    expect(pickFailure([judgeFail, ruleFail], { tab: 'checks', kind: 'rule' })?.case_id).toBe('r');
  });

  it('指定赛道时只看该赛道', () => {
    expect(pickFailure([ruleFail, trackB], { tab: 'checks', kind: 'rule', track: 'B' })?.case_id).toBe('b');
    expect(pickFailure([ruleFail], { tab: 'checks', track: 'B' })).toBeNull();
  });

  it('没有匹配项时返回 null，由调用方给出具体文案', () => {
    expect(pickFailure([ruleFail], { tab: 'judges', kind: 'judge' })).toBeNull();
    expect(pickFailure([], { tab: 'basis' })).toBeNull();
  });
});

describe('failuresByTrack', () => {
  it('按赛道计数，缺 track 的归到 ?', () => {
    expect(
      failuresByTrack([
        failure({ track: 'A' }),
        failure({ track: 'A' }),
        failure({ track: 'B' }),
        failure({ track: '' }),
      ]),
    ).toEqual({ A: 2, B: 1, '?': 1 });
  });
});

function chunk(items: OjCaseList['items'], total: number, offset: number): OjCaseList {
  return { items, total, limit: 200, offset };
}

function row(id: number, caseId: string) {
  return {
    id,
    case_id: caseId,
    track: 'A',
    subject: 'x',
    risk: 'low',
    status: 'success',
    passed: false,
    latency_ms: 1,
    tokens: {},
  };
}

describe('findCaseRow', () => {
  it('第一页命中就不再翻页', async () => {
    const fetchCases = vi.fn(async () => chunk([row(1, 'a-1')], 1, 0));
    expect((await findCaseRow('a-1', fetchCases))?.id).toBe(1);
    expect(fetchCases).toHaveBeenCalledTimes(1);
  });

  it('翻页直到找到：不再因写死 500 条而误报「不存在」', async () => {
    const pages = [
      chunk([row(1, 'a-1')], 3, 0),
      chunk([row(2, 'a-2')], 3, 1),
      chunk([row(3, 'a-3')], 3, 2),
    ];
    let index = 0;
    const fetchCases = vi.fn(async () => pages[index++]);
    expect((await findCaseRow('a-3', fetchCases))?.id).toBe(3);
    expect(fetchCases).toHaveBeenCalledTimes(3);
  });

  it('翻到底仍未命中返回 undefined', async () => {
    const fetchCases = vi.fn(async (options: { offset: number }) =>
      options.offset === 0 ? chunk([row(1, 'a-1')], 2, 0) : chunk([row(2, 'a-2')], 2, 1),
    );
    expect(await findCaseRow('missing', fetchCases)).toBeUndefined();
    expect(fetchCases).toHaveBeenCalledTimes(2);
  });

  it('把已知赛道传下去缩小范围', async () => {
    const fetchCases = vi.fn(async () => chunk([row(1, 'a-1')], 1, 0));
    await findCaseRow('a-1', fetchCases, 'B');
    expect(fetchCases).toHaveBeenCalledWith({ track: 'B', limit: 200, offset: 0 });
  });

  it('空页立即收手，避免死循环', async () => {
    const fetchCases = vi.fn(async () => chunk([], 0, 0));
    expect(await findCaseRow('a-1', fetchCases)).toBeUndefined();
    expect(fetchCases).toHaveBeenCalledTimes(1);
  });
});
