/** 左栏工作区分组的纯函数：后端一次给全量，这里只做形状转换、过滤与去重。
 *
 * 分开成模块的理由：分组取决于「后端 board ∪ 未发送草稿」两个来源，
 * 又有搜索、归档开关、未归属回落三条规则，塞进组件里只能靠肉眼测。
 */

import type { WorkspaceSessionBoard, WorkspaceSessionItem, WorkspaceStatus } from '../types';
import { toSessionTitle, type SessionItem } from './sidebar-utils';

export interface WorkspaceGroupView {
  id: string;
  name: string;
  rootPath: string;
  status: WorkspaceStatus;
  /** 已归档工作区：默认不渲染，只有打开「显示已归档」才出现。 */
  archived: boolean;
  /** 该工作区的会话总数（可能大于下面展示的最近几条）。 */
  count: number;
  sessions: SessionItem[];
}

/** 还没发出第一轮的草稿：只在界面上挂到某个工作区底下，不发请求也不落库。 */
export interface DraftSession {
  id: string;
  workspaceId: string;
  title: string;
}

export function toSessionItem(item: WorkspaceSessionItem): SessionItem {
  return {
    id: item.session_id,
    title: toSessionTitle(item.title),
    lastAt: item.last_at,
    runCount: item.run_count,
    failedCount: item.failed_count,
    archived: Boolean(item.archived_at),
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
      archived: group.status !== 'active',
      count:
        typeof group.session_count === 'number' ? group.session_count : group.sessions.length,
      sessions: group.sessions.map(toSessionItem),
    }));
}

/** 把未发送的草稿挂到它选中的工作区顶部；服务端已经有这条会话时不再重复插入。 */
export function injectDraft(
  groups: WorkspaceGroupView[],
  draft?: DraftSession | null,
): WorkspaceGroupView[] {
  if (!draft) return groups;
  return groups.map((group) => {
    if (group.id !== draft.workspaceId) return group;
    if (group.sessions.some((item) => item.id === draft.id)) return group;
    return {
      ...group,
      sessions: [
        {
          id: draft.id,
          title: draft.title,
          lastAt: '',
          runCount: 0,
          failedCount: 0,
          draft: true,
        },
        ...group.sessions,
      ],
    };
  });
}

export interface GroupFilter {
  query?: string;
  showArchived?: boolean;
}

/** 过滤后的分组。
 *
 * - 归档工作区 / 归档会话默认不出现，打开开关后原样（置灰）展示；
 * - 搜索命中工作区名时保留它全部会话——用户是在找这个工作区，不是在筛会话；
 * - 搜索时把「一条都不匹配」的工作区收起，列表才干净。
 */
export function filterGroups(
  groups: WorkspaceGroupView[],
  { query = '', showArchived = false }: GroupFilter = {},
): WorkspaceGroupView[] {
  const q = query.trim().toLowerCase();
  return groups
    .filter((group) => showArchived || !group.archived)
    .map((group) => {
      if (!q) {
        return {
          ...group,
          sessions: group.sessions.filter((item) => showArchived || !item.archived),
        };
      }
      // 命中工作区名：会话不再按标题筛，但归档仍然受开关约束
      if (group.name.toLowerCase().includes(q)) {
        return {
          ...group,
          sessions: group.sessions.filter((item) => showArchived || !item.archived),
        };
      }
      return {
        ...group,
        sessions: group.sessions.filter(
          (item) =>
            (showArchived || !item.archived) &&
            (item.title.toLowerCase().includes(q) || item.id.toLowerCase().includes(q)),
        ),
      };
    })
    .filter((group) => !q || group.sessions.length > 0 || group.name.toLowerCase().includes(q));
}

/** 未归属 = 后端的 unbound ∪ 本地缓存；已出现在工作区分组里的会话要去掉，避免重复。
 *
 * 只用于「工作区模型不可用」的降级视图与老数据兜底；新会话一律挂在工作区下。
 */
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
