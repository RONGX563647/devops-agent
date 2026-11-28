/** 侧栏的会话数据源：runs 聚合 + 本地缓存合并 + 两条 query 钩子。
 *
 * 单独成文件是因为侧栏组件只负责渲染，而「标题取最早一条输入、本地新会话
 * 也要出现」这两条口径要能被 Workspace 顶栏与测试直接复用。
 */
import { useQuery } from '@tanstack/react-query';
import { fetchRuns } from '../api/client';
import type { Run } from '../types';
import type { StoredSession } from './history';
import { listSessions } from './store/history';
import { useStoreVersion } from './store/state';
import { toSessionTitle, type SessionItem } from './sidebar-utils';

export function aggregateSessions(runs: Run[]): SessionItem[] {
  const map = new Map<string, SessionItem & { firstAt: string }>();
  for (const run of runs) {
    const cur = map.get(run.session_id);
    if (!cur) {
      map.set(run.session_id, {
        id: run.session_id,
        title: toSessionTitle(run.input),
        lastAt: run.started_at,
        firstAt: run.started_at,
        runCount: 1,
        failedCount: run.status === 'failed' ? 1 : 0,
      });
      continue;
    }
    cur.runCount += 1;
    cur.failedCount += run.status === 'failed' ? 1 : 0;
    if (run.started_at > cur.lastAt) cur.lastAt = run.started_at;
    // 标题取最早那条输入，避免最新一轮的短问题覆盖会话主题
    if (run.started_at < cur.firstAt) {
      cur.firstAt = run.started_at;
      cur.title = toSessionTitle(run.input);
    }
  }
  return [...map.values()]
    .sort((a, b) => b.lastAt.localeCompare(a.lastAt))
    .map((item) => ({
      id: item.id,
      title: item.title,
      lastAt: item.lastAt,
      runCount: item.runCount,
      failedCount: item.failedCount,
    }));
}

/** 合并远端会话与本地缓存：本地新会话（后端还没有运行记录）也要出现在侧栏。 */
export function mergeSessions(remote: SessionItem[], local: StoredSession[]): SessionItem[] {
  const merged = new Map(remote.map((item) => [item.id, { ...item }]));
  for (const item of local) {
    const existing = merged.get(item.id);
    if (existing) {
      existing.messageCount = item.messageCount;
      if (existing.runCount === 0) existing.lastAt = item.lastAt;
      continue;
    }
    merged.set(item.id, {
      id: item.id,
      title: item.title,
      lastAt: item.lastAt,
      runCount: 0,
      failedCount: 0,
      messageCount: item.messageCount,
    });
  }
  return [...merged.values()].sort((a, b) => b.lastAt.localeCompare(a.lastAt));
}

/** 会话列表数据源：侧栏与顶部统计共用同一个 queryKey，缓存自动复用。 */
export function useSessionRuns() {
  return useQuery({
    queryKey: ['runs', 'sessions'],
    queryFn: () => fetchRuns({ limit: 200 }),
    refetchInterval: 10000,
  });
}

/** 合并后的会话列表；订阅 store 版本号，本地新会话创建后立即出现。 */
export function useSessionList(): SessionItem[] {
  const runs = useSessionRuns();
  useStoreVersion();
  return mergeSessions(aggregateSessions(runs.data?.items ?? []), listSessions());
}
