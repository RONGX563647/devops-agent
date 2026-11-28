/** 侧栏会话条目的纯函数工具：标题压缩与时间分组。 */

export interface SessionItem {
  id: string;
  /** 会话标题：取该会话最早一条用户输入，超长截断。 */
  title: string;
  lastAt: string;
  runCount: number;
  failedCount: number;
  /** 本地缓存里的用户消息数（后端还没有运行记录时用来显示计数）。 */
  messageCount?: number;
  /** 已归档会话：置灰展示，从默认视图收起。 */
  archived?: boolean;
  /** 还没发出第一轮的草稿，只存在于界面。 */
  draft?: boolean;
}

export type SessionGroup = '今天' | '昨天' | '更早';

export const GROUP_ORDER: SessionGroup[] = ['今天', '昨天', '更早'];
const TITLE_MAX = 20;

/** 用户输入压成单行并截断，空输入给兜底标题。 */
export function toSessionTitle(input: string, max: number = TITLE_MAX): string {
  const clean = input.replace(/\s+/g, ' ').trim();
  if (!clean) return '未命名会话';
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

/** 按本地日界分组：今天 / 昨天 / 更早。 */
export function groupOf(iso: string, now: Date = new Date()): SessionGroup {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '更早';
  const dayStart = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const diffDays = Math.round((dayStart(now) - dayStart(date)) / 86_400_000);
  if (diffDays <= 0) return '今天';
  if (diffDays === 1) return '昨天';
  return '更早';
}
