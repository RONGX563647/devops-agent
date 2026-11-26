/**
 * 结果区的纯逻辑：门禁点进哪个失败、按 case_id 怎么翻页找行。
 *
 * 抽出来的理由很具体：这两件事原来都写在组件里，一个默认取 `failures[0]`
 * （于是点「语义裁判」也跳到规则失败），一个把 `limit` 写死 500
 * （批次超过 500 条就永远「找不到用例」）。都是可以单测的逻辑，不该埋在 JSX 里。
 */

import type { OjCaseList, OjCaseRow, OjFailure } from '../../../types';
import type { DrawerTab } from './diagnosis';

/** 点某个门禁/阶段时携带的范围：决定跳到哪一条失败。 */
export interface InspectScope {
  tab: DrawerTab;
  /** 只看这个赛道的失败。 */
  track?: string;
  /** rule = 有规则断言失败；judge = 有裁判维度低分。 */
  kind?: 'rule' | 'judge';
}

/** 按范围挑出第一条可以下钻的失败；没有则返回 null，由调用方给出具体文案。 */
export function pickFailure(failures: OjFailure[], scope: InspectScope): OjFailure | null {
  const match = failures.find((failure) => {
    if (scope.track && failure.track !== scope.track) return false;
    if (scope.kind === 'rule') return (failure.failed_checks ?? []).length > 0;
    if (scope.kind === 'judge') return (failure.judge_dims ?? []).length > 0;
    return true;
  });
  return match ?? null;
}

/** 失败列表按赛道计数：表格里每行用它标「本赛道失败了几条」。 */
export function failuresByTrack(failures: OjFailure[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const failure of failures) {
    const key = failure.track || '?';
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return counts;
}

type CasesFetcher = (options: {
  track?: string;
  limit: number;
  offset: number;
}) => Promise<OjCaseList>;

/** 单页条数：服务端上限 500，取小一点让翻页在慢库上也能出结果。 */
const PAGE = 200;
/** 翻页上限：防止 total 异常时把浏览器拖死。 */
const MAX_PAGES = 25;

/**
 * 在批次里按 case_id 找一行，找不到返回 undefined。
 *
 * 已知赛道时先按赛道过滤（失败清单里带了 track），这样几百条的批次一页就能命中；
 * 未命中再顺着 offset 翻页，翻到 MAX_PAGES 或越过 total 为止 —— 原来的实现
 * 只查一次 500 条，超了就报「未找到」，把「没查完」说成了「不存在」。
 */
export async function findCaseRow(
  caseId: string,
  fetchCases: CasesFetcher,
  track?: string,
): Promise<OjCaseRow | undefined> {
  let offset = 0;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const chunk = await fetchCases({ track, limit: PAGE, offset });
    const hit = chunk.items.find((item) => item.case_id === caseId);
    if (hit) return hit;
    offset += chunk.items.length;
    if (chunk.items.length === 0 || offset >= chunk.total) return undefined;
  }
  return undefined;
}
