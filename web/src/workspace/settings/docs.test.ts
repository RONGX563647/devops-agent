/**
 * 参数文案的覆盖棘轮（SPEC-0022 §8.1 / A-14）。
 *
 * 两个方向都要管：
 * - 模型参数页展示的 16 个键**必须**有四段式文案 —— 少一个就会出现「读列名」；
 * - 运行策略里还没写的键登记在 `fallback-allowlist.json`，`budget` 与条目数
 *   必须严格相等：补了文案就得删条目并减 budget，只减不增才叫棘轮。
 */
import { describe, expect, it } from 'vitest';
import allowlist from './fallback-allowlist.json';
import { PARAM_DOCS, docFor } from './docs';
import { GROUPS } from './model/groups';

const SECTIONS = Object.keys(PARAM_DOCS);

describe('模型参数文案覆盖', () => {
  it('分组的每个键都有文案，且四段都写了', () => {
    const missing: string[] = [];
    const thin: string[] = [];
    for (const group of GROUPS) {
      for (const spec of group.items) {
        const doc = PARAM_DOCS[spec.key];
        if (!doc) {
          missing.push(spec.key);
          continue;
        }
        if (!doc.title || !doc.what || !doc.impact) thin.push(spec.key);
      }
    }
    expect(missing).toEqual([]);
    expect(thin).toEqual([]);
  });

  it('标签是中文名，不是把键名直接搬上去', () => {
    for (const group of GROUPS) {
      for (const spec of group.items) {
        const doc = PARAM_DOCS[spec.key];
        expect(doc.title).not.toBe(spec.key);
      }
    }
  });

  it('高风险项必须写清方向，方便在改之前读懂后果', () => {
    const high = SECTIONS.filter((key) => PARAM_DOCS[key].risk === 'high');
    expect(high.length).toBeGreaterThan(0);
    for (const key of high) {
      expect(PARAM_DOCS[key].impact.length).toBeGreaterThan(10);
    }
  });
});

describe('兜底与棘轮', () => {
  it('认不出的键也能读：给中文兜底标题 + 原键名，不空白不报错', () => {
    const doc = docFor('memory.brand_new_knob');
    expect(doc.title).toBe('brand new knob');
    expect(doc.what).toContain('memory.brand_new_knob');
    expect(doc.impact.length).toBeGreaterThan(0);
    // 调用方可以指定更好的标题（模型页用分组里的 label）
    expect(docFor('memory.brand_new_knob', '新开关').title).toBe('新开关');
  });

  it('allowlist 的 budget 与条目数严格相等（增一条就得改 budget）', () => {
    expect(allowlist.budget).toBe(allowlist.keys.length);
  });

  it('已写文案的键不许留在 allowlist 里（只减不增）', () => {
    const stale = allowlist.keys.filter((key) => key in PARAM_DOCS);
    expect(stale).toEqual([]);
  });

  it('allowlist 的键不重复，都是「域.路径」形状', () => {
    expect(new Set(allowlist.keys).size).toBe(allowlist.keys.length);
    for (const key of allowlist.keys) {
      expect(key).toMatch(/^[a-z_]+\.[a-z0-9_.]+$/);
    }
  });
});
