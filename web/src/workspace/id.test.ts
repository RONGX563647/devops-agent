// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { newId } from './id';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('newId', () => {
  it('带前缀且不与已有 id 冲突', () => {
    const a = newId('sess');
    const b = newId('sess');
    expect(a.startsWith('sess-')).toBe(true);
    expect(b.startsWith('sess-')).toBe(true);
    expect(a).not.toBe(b);
  });

  it('非安全上下文缺少 randomUUID 时仍有兜底', () => {
    vi.stubGlobal('crypto', {});
    const id = newId('msg');
    expect(id.startsWith('msg-')).toBe(true);
    expect(id.length).toBeGreaterThan(4);
  });
});
