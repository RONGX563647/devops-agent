/**
 * 质量页的视图状态：唯一来源是 URL。
 *
 * 视图链接必须基于**当前**查询串再改 `view`，不能直接写 `to="?view=x"`。
 * 质量页有两种挂载方式——独立路由 `/quality`，以及工作台的 `/?tab=quality`。
 * 后者里相对查询会把 `tab` 一起覆盖掉，`parseTab` 随即回落到「对话」，
 * 表现就是「点质量页里的分页签，跳回了会话」。
 */

export type QualityView = 'overview' | 'batches' | 'scoreflow' | 'studio' | 'memory';

export const VIEWS: { key: QualityView; label: string; hint: string }[] = [
  { key: 'overview', label: '概览', hint: '这批能不能用' },
  { key: 'batches', label: '批次', hint: '逐批看明细' },
  { key: 'scoreflow', label: '评分流程', hint: '分数从哪来' },
  { key: 'studio', label: '用例与模型', hint: '改题、切模型' },
  { key: 'memory', label: '记忆', hint: '记忆有没有用' },
];

export function parseView(raw: string | null, legacyFocus: string | null): QualityView {
  // 旧深链 `?focus=scoreflow` 仍要能读，所以它优先于 `view`；
  // 代价是切换视图必须删掉它（见 `viewHref`），否则点哪个都停在评分流程。
  if (legacyFocus === 'scoreflow') return 'scoreflow';
  const found = VIEWS.find((item) => item.key === raw);
  return found?.key ?? 'overview';
}

/**
 * 切换视图的链接：保留当前查询串里的其它参数（`tab` / `session` 等）。
 *
 * 返回 `?...`，react-router 会解析成「当前 pathname + 该查询」，因此
 * `/quality` 与 `/?tab=quality` 两种挂载方式下都只改 `view`。
 */
export function viewHref(params: URLSearchParams, view: QualityView): string {
  const next = new URLSearchParams(params);
  next.set('view', view);
  next.delete('focus');
  return `?${next.toString()}`;
}
