/** 网络获取决策的解析与展示（SPEC-0012 §8）。
 *
 * 后端把决策写成 `runs.web_access` 的 JSON，空串表示「未记录」——那不是「没联网」。
 * 所以解析失败一律返回 undefined，界面宁可不显示，也不要编一个「离线」出来。
 */

import type { WebAccessInfo } from '../types';

/** 解析 `runs.web_access`；空串 / 坏 JSON / 非对象都返回 undefined。 */
export function parseWebAccess(raw: string | undefined): WebAccessInfo | undefined {
  if (!raw) return undefined;
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return undefined;
  }
  if (typeof data !== 'object' || data === null) return undefined;
  const record = data as Record<string, unknown>;
  const net = typeof record.net === 'boolean' ? record.net : null;
  const text = (value: unknown): string => (typeof value === 'string' ? value : '');
  const info: WebAccessInfo = {
    net,
    fallback: text(record.fallback),
    prefetch: text(record.prefetch),
  };
  // 三个字段都空 = 记了一行但什么都没说明，等同未记录
  if (info.net === null && !info.fallback && !info.prefetch) return undefined;
  return info;
}

/** 一行文案：先说缺口，再说有没有检索能力（状态用文字表达，不靠颜色）。 */
export function webAccessLabel(info: WebAccessInfo): string {
  const parts: string[] = [];
  if (info.fallback) parts.push(info.fallback);
  else if (info.prefetch) parts.push(info.prefetch);
  parts.push(info.net === true ? '检索已授予' : info.net === false ? '未授予检索' : '检索能力未知');
  return parts.join(' · ');
}
