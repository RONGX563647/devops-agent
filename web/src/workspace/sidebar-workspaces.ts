/** 左栏工作区分组的纯函数：后端一次给全量，这里只做形状转换与去重。
 *
 * 分开成模块的理由：分组取决于「后端board ∪ 本地草稿」两个来源，
 * 又有搜索过滤与「未归属」回落三条规则，塞进组件里只能靠肉眼测。
 */

import type { WorkspaceSessionBoard, WorkspaceSessionItem, WorkspaceStatus } from '../types';
import { toSessionTitle, type SessionItem } from './sidebar-utils';

export interface WorkspaceGroupView {
  id: string;
  name: string;
  rootPath: string;
  status: WorkspaceStatus;
  /** 该工作区的会话总数（可能大于下面展示的最近几条）。 */
  count: number;
  sessions: SessionItem[];
}

export function toSessionItem(item: WorkspaceSessionItem): SessionItem {
  return {
    id: item.session_id,
    title: toSessionTitle(item.title),
    lastAt: item.last_at,
    runCount: item.run_count,
    failedCount: item.failed_count,
  };
}

export function buildWorkspaceGroups(board?: WorkspaceSessionBoard): WorkspaceGroupView[] {
  if (!board || !Array.isArray(board.items)) return [];
  // 旧后端 / 代理降级时 items 可能是别的形状；缺字段的分组宁可跳过也不要渲染半截。
  return board.items
    .filter((group) => typeof group?.id === 'string' && Array.isArray(group.sessions))
    .map((group) => ({
      id: group.id,
      name: group.name || group.root_path || group.id,
      rootPath: group.root_path,
      status: group.status,
      count:
        typeof group.session_count === 'number' ? group.session_count : group.sessions.length,
      sessions: group.sessions.map(toSessionItem),
    }));
}

/** 未归属 = 后端的 unbound ∪ 本地草稿；已出现在工作区分组里的会话要去掉，避免重复。 */
export function unboundSessions(
  board: WorkspaceSessionBoard | undefined,
  legacy: SessionItem[],
): SessionItem[] {
  const bound = new Set(
    buildWorkspaceGroups(board).flatMap((group) => group.sessions.map((item) => item.id)),
  );
  const merged = new Map<string, SessionItem>();
  for (const item of board?.unbound ?? []) {
    merged.set(item.session_id, toSessionItem(item));
  }
  for (const item of legacy) {
    if (bound.has(item.id)) continue;
    const existing = merged.get(item.id);
    if (existing) {
      existing.messageCount = item.messageCount;
      if (existing.runCount === 0) existing.lastAt = item.lastAt;
      continue;
    }
    merged.set(item.id, { ...item });
  }
  return [...merged.values()]
    .filter((item) => !bound.has(item.id))
    .sort((a, b) => b.lastAt.localeCompare(a.lastAt));
}
