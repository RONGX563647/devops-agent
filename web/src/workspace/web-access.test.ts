/** 网络获取决策的解析与文案（SPEC-0012 §8）。 */

import { describe, expect, it } from 'vitest';

import { parseWebAccess, webAccessLabel } from './web-access';

describe('parseWebAccess', () => {
  it('解析后端写入的 JSON', () => {
    expect(parseWebAccess('{"net":true,"fallback":"1 条引用内容不足","prefetch":"1 条内容不足"}')).toEqual({
      net: true,
      fallback: '1 条引用内容不足',
      prefetch: '1 条内容不足',
    });
  });

  it('空串与坏 JSON 都按「未记录」处理', () => {
    expect(parseWebAccess('')).toBeUndefined();
    expect(parseWebAccess(undefined)).toBeUndefined();
    expect(parseWebAccess('not json')).toBeUndefined();
  });

  it('记了一行但什么都没说明时也算未记录', () => {
    expect(parseWebAccess('{"net":null,"fallback":"","prefetch":""}')).toBeUndefined();
  });
});

describe('webAccessLabel', () => {
  it('有缺口先说缺口，再说检索是否可用', () => {
    expect(webAccessLabel({ net: true, fallback: '1 条引用内容不足', prefetch: '' })).toBe(
      '1 条引用内容不足 · 检索已授予',
    );
  });

  it('没缺口时退回预抓取摘要', () => {
    expect(webAccessLabel({ net: false, fallback: '', prefetch: '2 条成功' })).toBe(
      '2 条成功 · 未授予检索',
    );
  });

  it('能力未知时如实说未知，不假设一定在线或离线', () => {
    expect(webAccessLabel({ net: null, fallback: '', prefetch: '' })).toBe('检索能力未知');
  });
});
